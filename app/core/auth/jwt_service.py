"""JWT validation service (master prompt §13.1).

Validates signature, algorithm allow-list, issuer, audience, expiry, not-before and
subject before producing a trusted :class:`AuthContext`. Supports asymmetric keys
(PEM or JWKS with ``kid`` handling and rotation-friendly caching) as the production
pattern, plus a symmetric development secret that configuration refuses outside
local/dev/test.

No custom cryptography is implemented here; PyJWT performs verification.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

import httpx
import jwt
from jwt import PyJWK, PyJWKSet

from app.core.auth.auth_context import ActorType, AuthContext, Permission, Role
from app.core.config.settings import Settings
from app.core.errors.taxonomy import AuthenticationRequiredError, ConfigurationError


class KeyResolver(Protocol):
    """Resolves the verification key for a token header."""

    async def resolve(self, header: dict[str, Any]) -> Any: ...


class StaticPemKeyResolver:
    """Verification against a configured public key (single-key deployments)."""

    def __init__(self, public_key_pem: str) -> None:
        self._pem = public_key_pem

    async def resolve(self, header: dict[str, Any]) -> Any:
        return self._pem


class DevSecretKeyResolver:
    """Symmetric development secret. Never reachable in preprod/prod (see Settings)."""

    def __init__(self, secret: str) -> None:
        self._secret = secret

    async def resolve(self, header: dict[str, Any]) -> Any:
        return self._secret


class JwksKeyResolver:
    """JWKS-based resolution with ``kid`` selection and TTL caching for key rotation."""

    def __init__(self, jwks_uri: str, cache_seconds: int, client: httpx.AsyncClient | None = None) -> None:
        self._uri = jwks_uri
        self._cache_seconds = cache_seconds
        self._client = client
        self._keys: PyJWKSet | None = None
        self._fetched_at: float = 0.0

    async def _fetch(self) -> PyJWKSet:
        client = self._client or httpx.AsyncClient(timeout=5.0)
        try:
            response = await client.get(self._uri)
            response.raise_for_status()
            return PyJWKSet.from_dict(response.json())
        finally:
            if self._client is None:
                await client.aclose()

    async def resolve(self, header: dict[str, Any]) -> Any:
        kid = header.get("kid")
        now = time.monotonic()
        stale = self._keys is None or (now - self._fetched_at) > self._cache_seconds
        if stale:
            self._keys = await self._fetch()
            self._fetched_at = now
        assert self._keys is not None
        try:
            return self._select(self._keys, kid).key
        except AuthenticationRequiredError:
            # A newly rotated key may not be cached yet: refresh once, then fail closed.
            self._keys = await self._fetch()
            self._fetched_at = time.monotonic()
            return self._select(self._keys, kid).key

    @staticmethod
    def _select(key_set: PyJWKSet, kid: str | None) -> PyJWK:
        for key in key_set.keys:
            if kid is None or key.key_id == kid:
                return key
        raise AuthenticationRequiredError("unknown_kid")


def build_key_resolver(settings: Settings, client: httpx.AsyncClient | None = None) -> KeyResolver:
    """Select the configured verification strategy, preferring asymmetric keys."""
    if settings.jwt_jwks_uri:
        return JwksKeyResolver(settings.jwt_jwks_uri, settings.jwt_jwks_cache_seconds, client)
    if settings.jwt_public_key_pem:
        return StaticPemKeyResolver(settings.jwt_public_key_pem)
    if settings.jwt_dev_hs256_secret and settings.allows_mock_providers:
        return DevSecretKeyResolver(settings.jwt_dev_hs256_secret)
    raise ConfigurationError("no_jwt_verification_key_configured")


#: Claim -> role mapping. Unknown role strings are dropped rather than trusted.
_ROLE_BY_CLAIM: dict[str, Role] = {r.value: r for r in Role}
_PERMISSION_BY_CLAIM: dict[str, Permission] = {p.value: p for p in Permission}

_ACTOR_BY_CLAIM: dict[str, ActorType] = {a.value: a for a in ActorType}


class JwtValidationService:
    """Validates bearer tokens and mints trusted :class:`AuthContext` objects."""

    def __init__(self, settings: Settings, key_resolver: KeyResolver | None = None) -> None:
        self._settings = settings
        self._resolver = key_resolver

    def _get_resolver(self) -> KeyResolver:
        if self._resolver is None:
            self._resolver = build_key_resolver(self._settings)
        return self._resolver

    async def validate(self, token: str) -> AuthContext:
        """Validate a raw bearer token. Raises ``AuthenticationRequiredError`` on any failure."""
        if not token or token.count(".") != 2:
            raise AuthenticationRequiredError("malformed_token")

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthenticationRequiredError("malformed_token_header") from None

        algorithm = str(header.get("alg", "")).upper()
        allowed = [a.upper() for a in self._settings.allowed_algorithms]
        if algorithm in {"NONE", ""} or algorithm not in allowed:
            raise AuthenticationRequiredError("algorithm_not_allowed")

        key = await self._get_resolver().resolve(header)

        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=self._settings.allowed_algorithms,
                issuer=self._settings.jwt_issuer,
                audience=self._settings.jwt_audience,
                leeway=self._settings.jwt_clock_skew_seconds,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.ExpiredSignatureError:
            raise AuthenticationRequiredError("token_expired") from None
        except jwt.InvalidIssuerError:
            raise AuthenticationRequiredError("invalid_issuer") from None
        except jwt.InvalidAudienceError:
            raise AuthenticationRequiredError("invalid_audience") from None
        except jwt.InvalidSignatureError:
            raise AuthenticationRequiredError("invalid_signature") from None
        except jwt.MissingRequiredClaimError:
            raise AuthenticationRequiredError("missing_required_claim") from None
        except jwt.PyJWTError:
            raise AuthenticationRequiredError("invalid_token") from None

        return self._to_auth_context(claims)

    def _to_auth_context(self, claims: dict[str, Any]) -> AuthContext:
        """Project verified claims onto the minimal trusted context (§13.1)."""
        token_use = str(claims.get("token_use", "access")).lower()
        if token_use != "access":
            # Refresh/ID tokens must never authorize an API call.
            raise AuthenticationRequiredError("unexpected_token_use")

        subject = str(claims.get("sub", "")).strip()
        if not subject:
            raise AuthenticationRequiredError("missing_subject")

        actor_raw = str(claims.get("actor_type", "")).upper()
        actor_type = _ACTOR_BY_CLAIM.get(actor_raw)
        roles = tuple(
            _ROLE_BY_CLAIM[r] for r in (str(x) for x in claims.get("roles", []) or []) if r in _ROLE_BY_CLAIM
        )
        if actor_type is None or actor_type is ActorType.ANONYMOUS:
            actor_type = self._infer_actor_type(roles)

        permissions = tuple(
            _PERMISSION_BY_CLAIM[p]
            for p in (str(x) for x in claims.get("permissions", []) or [])
            if p in _PERMISSION_BY_CLAIM
        )

        assigned = tuple(str(c) for c in (claims.get("assigned_customer_ids") or []) if str(c).strip())
        if actor_type is not ActorType.AGENT:
            # Only agents may carry an assignment list; ignore it otherwise.
            assigned = ()

        return AuthContext(
            subject_id=subject,
            actor_type=actor_type,
            roles=roles,
            permissions=permissions,
            session_id=claims.get("sid"),
            tenant_id=claims.get("tenant_id"),
            token_id=claims.get("jti"),
            auth_time=str(claims["auth_time"]) if claims.get("auth_time") is not None else None,
            assigned_customer_ids=assigned,
            agency_id=claims.get("agency_id"),
            scopes=tuple(str(claims.get("scope", "")).split()) if claims.get("scope") else (),
        )

    @staticmethod
    def _infer_actor_type(roles: tuple[Role, ...]) -> ActorType:
        if Role.SERVICE in roles:
            return ActorType.SERVICE
        if Role.AGENT in roles or Role.AGENT_SUPERVISOR in roles:
            return ActorType.AGENT
        if Role.CUSTOMER in roles:
            return ActorType.CUSTOMER
        raise AuthenticationRequiredError("indeterminate_actor_type")
