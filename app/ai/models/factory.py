"""Model selection from configuration (master prompt §32, §14).

Switching provider is a configuration change; no business service is aware of the
vendor. The deterministic test double is refused in production by ``Settings``, and
this factory refuses it again as a defence in depth.
"""

from __future__ import annotations

from typing import Any

from strands.models.model import Model

from app.ai.models.provider import (
    DeterministicModel,
    EvidenceGroundedResponder,
    GenerationSettings,
    ModelInvoker,
    ModelPricing,
    ScriptedResponder,
)
from app.core.config.settings import Settings
from app.core.errors.taxonomy import ConfigurationError
from app.core.logging.structured import get_logger

logger = get_logger(__name__)


def build_model(settings: Settings, *, responder: ScriptedResponder | None = None) -> Model:
    """Instantiate the configured Strands model adapter."""
    provider = settings.model_provider

    if provider == "deterministic":
        if settings.is_hardened:
            raise ConfigurationError("production_refuses_deterministic_model")
        # With no injected responder the offline default behaves like a *well-behaved
        # grounded model*: it answers extractively from the supplied evidence and
        # declines when the evidence does not cover the question. That is what makes the
        # shipped demos and `run_evals` meaningful without credentials; a bare responder
        # would decline every question and prove nothing (§32, §50).
        return DeterministicModel(settings.model_id, responder=responder or EvidenceGroundedResponder())

    if provider == "openai":
        if not settings.model_api_key:
            raise ConfigurationError("MODEL_API_KEY_required_for_openai_provider")
        try:
            from strands.models.openai import OpenAIModel
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ConfigurationError("openai_model_extra_not_installed") from exc
        client_args: dict[str, Any] = {"api_key": settings.model_api_key}
        if settings.model_base_url:
            client_args["base_url"] = settings.model_base_url
        return OpenAIModel(
            client_args=client_args,
            model_id=settings.model_id,
            params={
                "temperature": settings.model_temperature,
                "max_tokens": settings.model_max_tokens,
            },
        )

    if provider == "bedrock":
        try:
            from strands.models import BedrockModel
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ConfigurationError("bedrock_model_extra_not_installed") from exc
        return BedrockModel(
            model_id=settings.model_id,
            temperature=settings.model_temperature,
            max_tokens=settings.model_max_tokens,
        )

    raise ConfigurationError(f"unsupported_model_provider:{provider}")


def build_model_invoker(settings: Settings, *, responder: ScriptedResponder | None = None) -> ModelInvoker:
    """Build the single ``ModelInvoker`` the Harness uses."""
    primary = build_model(settings, responder=responder)

    fallback: Model | None = None
    if settings.model_fallback_provider == "deterministic" and not settings.is_hardened:
        # A test-double fallback is only ever acceptable outside production, and the
        # Harness still refuses fallback for high-risk capabilities (§20).
        fallback = DeterministicModel(
            "deterministic-fallback", responder=responder or EvidenceGroundedResponder()
        )

    return ModelInvoker(
        primary,
        settings=GenerationSettings(
            temperature=settings.model_temperature,
            max_output_tokens=settings.max_output_tokens,
            timeout_ms=settings.model_timeout_ms,
            streaming=settings.model_streaming_enabled and settings.feature_streaming_enabled,
        ),
        pricing=ModelPricing(
            input_per_1k=settings.model_input_cost_per_1k,
            output_per_1k=settings.model_output_cost_per_1k,
            currency=settings.model_currency,
        ),
        provider_name=settings.model_provider,
        model_id=settings.model_id,
        fallback_model=fallback,
    )
