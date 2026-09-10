"""Typed capability registry (master prompt §4.1).

The registry is the stable bridge between channels, workflows, agents and business
services. The deterministic router consults it instead of scattering if/else routing,
and the Policy Enforcement Point reads its declarations rather than trusting a caller.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel

from app.core.auth.auth_context import ActorType, Permission, Role
from app.core.context.request_context import Channel
from app.core.errors.taxonomy import CapabilityNotFoundError
from app.core.resilience.policies import SideEffectClass
from app.core.security.rate_limit import RateLimitClass


class ExecutionMode(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    AI_ASSISTED = "AI_ASSISTED"
    AGENTIC = "AGENTIC"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AuditPolicy(StrEnum):
    NONE = "NONE"
    SUMMARY = "SUMMARY"
    FULL = "FULL"


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    """Per-capability execution budget (§35, §58.3)."""

    timeout_ms: int = 5_000
    model_call_budget: int = 0
    token_budget: int = 0
    agent_step_budget: int = 0
    tool_call_budget: int = 0


@dataclass(frozen=True, slots=True)
class Capability:
    """One registered, executable unit of platform behaviour."""

    id: str
    domain: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    execution_mode: ExecutionMode
    model_required: bool
    risk_level: RiskLevel
    required_auth: bool
    allowed_actor_types: frozenset[ActorType]
    allowed_roles: frozenset[Role]
    required_permissions: frozenset[Permission]
    allowed_workflow_states: frozenset[str] | None
    side_effect_class: SideEffectClass
    service_binding: str
    budgets: BudgetSpec
    rate_limit_class: RateLimitClass
    audit_policy: AuditPolicy
    allowed_channels: frozenset[Channel]
    allowed_environments: frozenset[str] = field(
        default_factory=lambda: frozenset({"local", "dev", "test", "preprod", "prod"})
    )
    requires_confirmation: bool = False
    requires_idempotency_key: bool = False
    version: str = "1.0.0"
    #: Tools the capability may use. The model may request only from this set (§12).
    allowed_tools: frozenset[str] = frozenset()
    #: When true the Harness itself grounds every ALLOW against the evidence the
    #: handler supplied and abstains centrally when it is missing or insufficient (§6.3).
    requires_grounding: bool = False

    def __post_init__(self) -> None:
        if self.model_required and self.execution_mode is ExecutionMode.DETERMINISTIC:
            raise ValueError(f"capability {self.id}: deterministic mode cannot require a model")
        if not self.model_required and self.budgets.model_call_budget > 0:
            raise ValueError(f"capability {self.id}: model budget without model_required")
        if self.requires_grounding and self.execution_mode is ExecutionMode.DETERMINISTIC:
            raise ValueError(f"capability {self.id}: grounding applies to AI-assisted capabilities only")
        if (
            self.side_effect_class
            in (
                SideEffectClass.HIGH_RISK_WRITE,
                SideEffectClass.IRREVERSIBLE,
            )
            and not self.requires_idempotency_key
        ):
            raise ValueError(f"capability {self.id}: sensitive writes must require an idempotency key")


class CapabilityRegistry:
    """Immutable-after-startup registry of capabilities."""

    def __init__(self) -> None:
        self._by_id: dict[str, Capability] = {}

    def register(self, capability: Capability) -> Capability:
        if capability.id in self._by_id:
            raise ValueError(f"duplicate capability id: {capability.id}")
        self._by_id[capability.id] = capability
        return capability

    def register_all(self, capabilities: Iterable[Capability]) -> None:
        for capability in capabilities:
            self.register(capability)

    def get(self, capability_id: str) -> Capability:
        capability = self._by_id.get(capability_id)
        if capability is None:
            raise CapabilityNotFoundError(capability_id)
        return capability

    def try_get(self, capability_id: str) -> Capability | None:
        return self._by_id.get(capability_id)

    def all(self) -> tuple[Capability, ...]:
        return tuple(self._by_id.values())

    def by_domain(self, domain: str) -> tuple[Capability, ...]:
        return tuple(c for c in self._by_id.values() if c.domain == domain)

    def deterministic_ids(self) -> frozenset[str]:
        return frozenset(
            c.id for c in self._by_id.values() if c.execution_mode is ExecutionMode.DETERMINISTIC
        )

    def tool_ids(self) -> frozenset[str]:
        if not self._by_id:
            return frozenset()
        return frozenset().union(*(c.allowed_tools for c in self._by_id.values()))

    def __len__(self) -> int:
        return len(self._by_id)


capability_registry = CapabilityRegistry()
