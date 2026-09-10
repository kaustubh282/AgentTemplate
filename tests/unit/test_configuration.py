"""Configuration and environment tests (master prompt §14, §15, §53).

Asserts the guarantees the master prompt makes about configuration:
  * every environment variable the application reads is documented
  * no secret is committed to source control
  * configuration is validated at startup and fails fast when unsafe
  * environments are separated and production refuses unsafe settings
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.config.settings import AppEnv, Settings, get_settings, reset_settings_cache
from tests.conftest import make_settings

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"


def declared_aliases() -> set[str]:
    return {f.alias for f in Settings.model_fields.values() if f.alias}


def documented_keys() -> set[str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    # Both live entries and commented eval-only entries count as documented.
    return set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=", text, re.MULTILINE))


# ------------------------------------------------------------- documentation ---
def test_env_example_exists():
    assert ENV_EXAMPLE.exists(), ".env.example is required (§14)"


def test_env_example_documents_every_setting():
    """§53: there must be no undocumented environment variables."""
    missing = sorted(declared_aliases() - documented_keys())
    assert not missing, f".env.example is missing: {missing}"


def test_env_example_documents_nothing_the_app_ignores():
    """A stale entry is misleading; eval-only keys are commented out deliberately."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    active = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, re.MULTILINE))
    unknown = sorted(active - declared_aliases())
    assert not unknown, f".env.example documents settings the app does not read: {unknown}"


def test_every_alias_is_screaming_snake_case():
    for alias in declared_aliases():
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", alias), alias


# ------------------------------------------------------------------ secrets ---
#: Patterns that would indicate a real credential committed to the repository.
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # A PEM header only counts as a credential when key material follows it: the
    # bare marker appears legitimately in detector patterns and documentation.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s*\n?[A-Za-z0-9+/=]{40,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
)

SCANNED_SUFFIXES = (".py", ".md", ".yaml", ".yml", ".toml", ".json", ".env", ".example")
SKIPPED_DIRS = {".git", ".venv", "__pycache__", "node_modules", "var", "reports"}

#: Directories whose whole purpose includes exercising the secret detectors. They may
#: contain secret-*shaped* strings, but a separate test proves those are synthetic.
FIXTURE_ROOTS = ("tests", "evals", "scripts")

#: Markers that make a secret-shaped string unmistakably synthetic.
SYNTHETIC_MARKERS = (
    "abcdefghijklmnop",
    "EXAMPLE",
    "not-a-real",
    "placeholder",
    "unit-test",
    "-----BEGIN RSA PRIVATE KEY-----abc",
)


def repository_files(*, fixtures: bool) -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if any(part in SKIPPED_DIRS for part in path.parts):
            continue
        is_fixture = path.relative_to(ROOT).parts[0] in FIXTURE_ROOTS
        if is_fixture == fixtures:
            files.append(path)
    return files


def find_secret_shapes(paths: list[Path]) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            found.extend((path, match.group(0)) for match in pattern.finditer(text))
    return found


def test_no_credentials_are_committed_in_application_code():
    """§14: no secret may exist in source control (application, docs and config)."""
    offenders = [
        f"{path.relative_to(ROOT)}: {value[:24]}..."
        for path, value in find_secret_shapes(repository_files(fixtures=False))
    ]
    assert not offenders, f"possible committed credentials: {offenders}"


def test_fixture_secrets_are_unmistakably_synthetic():
    """Fixtures may hold secret-shaped strings, but never anything usable."""
    offenders: list[str] = []
    for path, value in find_secret_shapes(repository_files(fixtures=True)):
        if not any(marker in value for marker in SYNTHETIC_MARKERS):
            offenders.append(f"{path.relative_to(ROOT)}: {value[:24]}...")
    assert not offenders, (
        f"a fixture secret does not look synthetic and may be a real credential: {offenders}"
    )


def test_env_example_contains_no_populated_secret_values():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in ("MODEL_API_KEY", "INSUREMO_API_KEY", "JWT_DEV_HS256_SECRET", "JWT_PUBLIC_KEY_PEM"):
        match = re.search(rf"^{key}=([^#\n]*)", text, re.MULTILINE)
        assert match, f"{key} must be listed in .env.example"
        # A trailing explanatory comment is fine; a populated value is not.
        assert not match.group(1).strip(), f"{key} must be empty in .env.example"


def test_gitignore_excludes_local_env_files():
    gitignore = ROOT / ".gitignore"
    assert gitignore.exists(), ".gitignore is required so a local .env is never committed"
    text = gitignore.read_text(encoding="utf-8")
    assert ".env" in text
    assert not re.search(r"^\.env\.example$", text, re.MULTILINE), ".env.example must stay tracked"


# ---------------------------------------------------------- startup validation ---
def test_settings_load_with_defaults():
    settings = make_settings()
    assert settings.app_env is AppEnv.TEST
    assert settings.max_context_tokens > 0
    assert settings.allowed_algorithms


def test_settings_singleton_can_be_reset():
    reset_settings_cache()
    first = get_settings()
    assert get_settings() is first
    reset_settings_cache()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MAX_CONTEXT_TOKENS", "10"),
        ("RAG_TOP_K", "0"),
        ("RAG_MIN_SCORE", "2.0"),
        ("TRACE_SAMPLE_RATE", "5.0"),
        ("MODEL_TEMPERATURE", "9.0"),
        ("MAX_MODEL_CALLS_PER_REQUEST", "99"),
        ("JWT_CLOCK_SKEW_SECONDS", "-1"),
        ("PROVIDER_CIRCUIT_RESET_SECONDS", "0"),
    ],
)
def test_out_of_range_configuration_is_rejected(field, value):
    """Configuration is validated at startup, not discovered at runtime (§14)."""
    with pytest.raises(PydanticValidationError):
        make_settings(**{field: value})


def test_unknown_provider_literal_is_rejected():
    with pytest.raises(PydanticValidationError):
        make_settings(MODEL_PROVIDER="magic-8-ball")


def test_alg_none_is_rejected_in_every_environment():
    for env in ("local", "dev", "test"):
        with pytest.raises(PydanticValidationError, match="alg=none"):
            make_settings(APP_ENV=env, JWT_ALLOWED_ALGORITHMS="none")


# --------------------------------------------------------- environment policy ---
@pytest.mark.parametrize("env", ["local", "dev", "test"])
def test_mock_providers_are_permitted_in_lower_environments(env):
    settings = make_settings(APP_ENV=env)
    assert settings.allows_mock_providers
    assert not settings.is_production


@pytest.mark.parametrize("env", ["preprod", "prod"])
def test_mock_providers_are_not_permitted_from_preprod_upwards(env):
    """Preprod mirrors production; mock data must not be mistaken for real data."""
    settings_kwargs = {
        "APP_ENV": env,
        "JWT_DEV_HS256_SECRET": "",
        "JWT_ALLOWED_ALGORITHMS": "RS256",
        "JWT_JWKS_URI": "https://idp.example/jwks.json",
    }
    # Preprod mirrors production (§15.2): mocks are refused in both, not documented as an
    # exception. Readiness evidence produced against mocks is not preprod evidence.
    with pytest.raises(PydanticValidationError, match="mock providers"):
        make_settings(**settings_kwargs, MODEL_PROVIDER="openai", JWT_ISSUER="https://idp.example/")


def test_production_configuration_guard_is_comprehensive():
    base = {
        "APP_ENV": "prod",
        "MODEL_PROVIDER": "openai",
        "MODEL_API_KEY": "placeholder-not-a-real-key",
        "JWT_JWKS_URI": "https://idp.example/jwks.json",
        "JWT_ALLOWED_ALGORITHMS": "RS256",
        "JWT_DEV_HS256_SECRET": "",
        "JWT_ISSUER": "https://idp.example/",
        "CUSTOMER_PROVIDER": "insuremo",
        "POLICY_PROVIDER": "insuremo",
        "QUOTE_PROVIDER": "insuremo",
        "PRODUCT_PROVIDER": "insuremo",
        "CLAIMS_PROVIDER": "insuremo",
        "PAYMENT_PROVIDER": "insuremo",
        "DOCUMENT_PROVIDER": "insuremo",
        "REFERENCE_DATA_PROVIDER": "insuremo",
        "INSUREMO_BASE_URL": "https://insuremo.example/api",
        "CORS_ALLOWED_ORIGINS": "https://app.protec.example",
        "SESSION_STORE_PROVIDER": "redis",
        "WORKFLOW_STORE_PROVIDER": "redis",
        "RATE_LIMIT_STORE_PROVIDER": "redis",
        "REDIS_URL": "rediss://redis.internal.example:6380/0",
        "CONFIRMATION_TOKEN_SECRET": "placeholder-not-a-real-secret-0123456789",
        "AUDIT_STORE_PROVIDER": "chained_file",
        "AUDIT_CHAIN_SECRET": "placeholder-not-a-real-secret-0123456789",
        "PSEUDONYM_SECRET": "placeholder-not-a-real-secret-0123456789",
    }
    # The valid configuration must start.
    assert Settings(**base).is_production  # type: ignore[arg-type]

    unsafe = [
        # H-5: process-local critical state can never be a production choice.
        ({"SESSION_STORE_PROVIDER": "inmemory"}, "in-memory critical state"),
        ({"WORKFLOW_STORE_PROVIDER": "inmemory"}, "in-memory critical state"),
        ({"RATE_LIMIT_STORE_PROVIDER": "inmemory"}, "in-memory critical state"),
        # M-2: every instance must verify the same confirmation tokens.
        ({"CONFIRMATION_TOKEN_SECRET": ""}, "CONFIRMATION_TOKEN_SECRET"),
        # B2: the audit trail must be tamper-evident in production.
        ({"AUDIT_STORE_PROVIDER": "file"}, "tamper-evident"),
        ({"AUDIT_STORE_PROVIDER": "inmemory"}, "tamper-evident"),
        ({"AUDIT_CHAIN_SECRET": ""}, "AUDIT_CHAIN_SECRET"),
        # The in-process Redis emulator is a test double.
        ({"REDIS_URL": "fakeredis://prod"}, "fakeredis"),
        ({"REDIS_URL": ""}, "REDIS_URL"),
        ({"MODEL_PROVIDER": "deterministic"}, "deterministic"),
        ({"POLICY_PROVIDER": "mock"}, "mock providers"),
        ({"JWT_ALLOWED_ALGORITHMS": "HS256"}, "symmetric JWT"),
        ({"JWT_JWKS_URI": ""}, "JWT_JWKS_URI"),
        ({"DEBUG_ENDPOINTS_ENABLED": "true"}, "DEBUG_ENDPOINTS_ENABLED"),
        ({"LOG_LEVEL": "DEBUG"}, "LOG_LEVEL"),
        ({"RATE_LIMIT_ENABLED": "false"}, "rate limiting"),
        ({"CORS_ALLOWED_ORIGINS": "*"}, "wildcard or null CORS"),
        ({"JWT_DEV_HS256_SECRET": "shhh"}, "JWT_DEV_HS256_SECRET"),
    ]
    for override, expected in unsafe:
        with pytest.raises(PydanticValidationError, match=expected):
            Settings(**{**base, **override})  # type: ignore[arg-type]


def test_startup_validation_rejects_a_hand_built_unsafe_settings_object():
    """Defence in depth: the guard also holds for a Settings object built elsewhere."""
    from app.bootstrap import validate_startup
    from app.core.errors.taxonomy import ConfigurationError

    class FakeProdSettings:
        is_production = True
        is_hardened = True
        model_provider = "deterministic"

        @staticmethod
        def configured_providers() -> dict[str, str]:
            return {"policy_provider": "insuremo"}

    with pytest.raises(ConfigurationError, match="deterministic"):
        validate_startup(FakeProdSettings())  # type: ignore[arg-type]


def test_all_critical_providers_are_covered_by_the_production_guard():
    from app.core.config.settings import CRITICAL_PROVIDER_FIELDS

    for field in (
        "customer_provider",
        "policy_provider",
        "quote_provider",
        "product_provider",
        "claims_provider",
        "payment_provider",
        "document_provider",
        "reference_data_provider",
    ):
        assert field in CRITICAL_PROVIDER_FIELDS


def test_container_builds_for_every_lower_environment():
    from app.bootstrap import build_container

    for env in ("local", "dev", "test"):
        container = build_container(make_settings(APP_ENV=env))
        assert len(container.capability_registry) > 0
        assert container.settings.app_env.value == env
