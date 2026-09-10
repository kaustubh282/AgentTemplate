"""Server-issued confirmation tokens for high-risk workflow actions (master prompt §8).

"Explicit user confirmation" must mean more than a client-supplied boolean. Every flow
response that offers a confirmation-requiring action carries a token for it, minted
here and bound to the *exact* flow, action and state version the user was shown.
Submitting ``confirmed=true`` is only honoured together with that token, so:

* a client that always sends ``confirmed=true`` satisfies nothing
* a token cannot be replayed against a different action or a different flow
* a token minted for one state version is worthless once the state has moved on, so a
  confirmation always refers to the quote/review the user actually saw

Tokens are HMAC-SHA256 tags over ``flow_id | action | version`` under a server secret.
Nothing is stored server-side, so multiple instances verify identically provided they
share ``CONFIRMATION_TOKEN_SECRET`` (required in production). Without a configured
secret a per-process random key is used, which is only acceptable for a single local
process and is refused by the production guard.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_VERSION = "v1"


class ConfirmationService:
    """Mints and verifies action-bound confirmation tokens."""

    def __init__(self, secret: str | None) -> None:
        self._key = (secret or secrets.token_urlsafe(32)).encode("utf-8")
        self.shared_secret_configured = bool(secret)

    @staticmethod
    def _message(flow_id: str, action: str, version: int) -> bytes:
        return f"{TOKEN_VERSION}|{flow_id}|{action}|{version}".encode()

    def issue(self, flow_id: str, action: str, version: int) -> str:
        tag = hmac.new(self._key, self._message(flow_id, action, version), hashlib.sha256).hexdigest()
        return f"{TOKEN_VERSION}.{tag}"

    def verify(self, token: str | None, flow_id: str, action: str, version: int) -> bool:
        """Constant-time comparison; ``None`` or malformed tokens are simply invalid."""
        if not token or not isinstance(token, str):
            return False
        expected = self.issue(flow_id, action, version)
        return hmac.compare_digest(expected, token)
