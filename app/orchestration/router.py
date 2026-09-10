"""Deterministic capability router (master prompt §2.3, §5.7.1, §58.5).

Every inbound request passes through here **before** any LLM or agent is considered.
The router classifies the request into one of the paths in §2.3 and returns a routing
decision. It contains no model call and no agent handoff.

The classification is intentionally conservative: anything it cannot resolve safely by
structure alone is handed to the AI path, and anything it *can* resolve is resolved
with zero model calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.registry.capability import CapabilityRegistry, ExecutionMode
from app.workflows.state.models import WorkflowState


class RoutePath(StrEnum):
    """The eight resolution paths in §2.3."""

    WORKFLOW_TRANSITION = "WORKFLOW_TRANSITION"
    UI_ACTION = "UI_ACTION"
    DETERMINISTIC_VALIDATION = "DETERMINISTIC_VALIDATION"
    PROVIDER_INVOCATION = "PROVIDER_INVOCATION"
    CACHED_READ = "CACHED_READ"
    FAQ_RAG = "FAQ_RAG"
    INTENT_EXTRACTION = "INTENT_EXTRACTION"
    AGENTIC = "AGENTIC"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    path: RoutePath
    capability_id: str | None
    requires_model: bool
    reason: str
    #: Resolved workflow action when the path is a deterministic transition.
    action: str | None = None

    @property
    def is_deterministic(self) -> bool:
        return not self.requires_model


#: Free-text phrases that map to a *navigation* action with no language understanding.
_NAVIGATION_PHRASES: dict[str, str] = {
    "go back": "GO_BACK",
    "back": "GO_BACK",
    "continue": "CONTINUE",
    "next": "CONTINUE",
    "resume": "CONTINUE",
}

#: Question shapes that are unambiguously FAQ. Used only to skip a routing model call.
_FAQ_MARKERS = (
    "what is",
    "what are",
    "what does",
    "how do i",
    "how does",
    "is third party",
    "does travel",
    "explain",
    "meaning of",
    "define",
)

#: Purchase-intent markers strong enough to start a flow without a model call.
_PURCHASE_MARKERS = (
    "buy a policy",
    "buy insurance",
    "buy motor",
    "buy car insurance",
    "buy travel insurance",
    "want to buy",
    "purchase a policy",
    "new policy",
    "get a quote",
)


class CapabilityRouter:
    """Structural, zero-model routing."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        faq_capability_id: str,
        intent_capability_id: str,
        supervisor_capability_id: str,
        purchase_capability_by_domain: dict[str, str],
        agentic_escalation_enabled: bool = True,
    ) -> None:
        self._registry = registry
        self._faq = faq_capability_id
        self._intent = intent_capability_id
        self._supervisor = supervisor_capability_id
        self._purchase = purchase_capability_by_domain
        #: FEATURE_AGENTIC_ESCALATION_ENABLED: when off, long or unclear text takes the
        #: single extraction call instead of the supervisor (§5.1, §5.6).
        self._agentic = agentic_escalation_enabled

    # ------------------------------------------------------------ structured ---
    def route_action(self, capability_id: str, action: str) -> RouteDecision:
        """A structured action from the client. Always deterministic (§58.5)."""
        capability = self._registry.get(capability_id)
        if capability.execution_mode is not ExecutionMode.DETERMINISTIC:
            return RouteDecision(RoutePath.AGENTIC, capability_id, True, "capability_requires_ai", action)
        return RouteDecision(RoutePath.WORKFLOW_TRANSITION, capability_id, False, "registered_action", action)

    # ------------------------------------------------------------- free text ---
    def route_message(self, message: str, *, active_flow: WorkflowState | None = None) -> RouteDecision:
        """Classify a free-text message without calling a model."""
        normalized = " ".join(message.lower().split())

        # 1/2. Navigation phrases against an active flow are pure UI actions.
        if active_flow is not None:
            mapped = _NAVIGATION_PHRASES.get(normalized)
            if mapped is not None:
                return RouteDecision(RoutePath.UI_ACTION, None, False, "navigation_phrase", mapped)

        # 6. Unambiguous FAQ shapes go straight to the grounded FAQ path: no routing
        #    model call is spent deciding something the structure already tells us.
        if any(normalized.startswith(marker) or f" {marker}" in normalized for marker in _FAQ_MARKERS):
            return RouteDecision(RoutePath.FAQ_RAG, self._faq, True, "faq_question_shape")

        # 4. Explicit purchase intent starts the deterministic journey directly.
        for marker in _PURCHASE_MARKERS:
            if marker in normalized:
                domain = self._infer_domain(normalized)
                capability = self._purchase.get(domain)
                if capability is not None:
                    return RouteDecision(
                        RoutePath.WORKFLOW_TRANSITION,
                        capability,
                        False,
                        f"purchase_intent:{domain}",
                        "BEGIN",
                    )

        # 7. Ambiguous language needs one extraction call.
        if len(normalized.split()) <= 40:
            return RouteDecision(RoutePath.INTENT_EXTRACTION, self._intent, True, "ambiguous_short_message")

        # 8. Anything longer or genuinely unclear escalates to the supervisor.
        if not self._agentic:
            return RouteDecision(RoutePath.INTENT_EXTRACTION, self._intent, True, "escalation_disabled")
        return RouteDecision(RoutePath.AGENTIC, self._supervisor, True, "escalation_required")

    @staticmethod
    def _infer_domain(normalized: str) -> str:
        """Structural keyword match. Never a model call, never a business decision."""
        if any(word in normalized for word in ("travel", "trip", "abroad", "overseas")):
            return "travel"
        return "motor"

    def is_deterministic_capability(self, capability_id: str) -> bool:
        capability = self._registry.try_get(capability_id)
        return capability is not None and capability.execution_mode is ExecutionMode.DETERMINISTIC
