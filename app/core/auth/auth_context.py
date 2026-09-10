"""Trusted authentication context (master prompt §13.1).

An ``AuthContext`` is produced only by the server-side JWT validation service. It is
the sole identity source for authorization, resource scoping and audit. It never
carries the raw token, and it is never constructed from a request body or from
model output.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ActorType(StrEnum):
    CUSTOMER = "CUSTOMER"
    AGENT = "AGENT"
    SERVICE = "SERVICE"
    ANONYMOUS = "ANONYMOUS"


class Role(StrEnum):
    """Platform roles. Customer and Agent authority stay separate (§13)."""

    CUSTOMER = "CUSTOMER"
    AGENT = "AGENT"
    AGENT_SUPERVISOR = "AGENT_SUPERVISOR"
    SERVICE = "SERVICE"
    KNOWLEDGE_ADMIN = "KNOWLEDGE_ADMIN"
    ANONYMOUS = "ANONYMOUS"


class Permission(StrEnum):
    """Fine-grained permissions checked by the Policy Enforcement Point."""

    FAQ_ASK = "faq:ask"
    POLICY_READ = "policy:read"
    POLICY_READ_ASSIGNED = "policy:read:assigned"
    CLAIM_READ = "claim:read"
    PAYMENT_READ = "payment:read"
    QUOTE_CREATE = "quote:create"
    QUOTE_READ = "quote:read"
    PURCHASE_SUBMIT = "purchase:submit"
    PAYMENT_INITIATE = "payment:initiate"
    #: Gateway callback identity only: records the authoritative payment outcome.
    PAYMENT_CONFIRM = "payment:confirm"
    WORKFLOW_ADVANCE = "workflow:advance"
    KNOWLEDGE_ADMIN = "knowledge:admin"
    FINOPS_READ = "finops:read"


#: Default permission grants per role. Resource-level checks still apply on top.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ANONYMOUS: frozenset({Permission.FAQ_ASK}),
    Role.CUSTOMER: frozenset(
        {
            Permission.FAQ_ASK,
            Permission.POLICY_READ,
            Permission.CLAIM_READ,
            Permission.PAYMENT_READ,
            Permission.QUOTE_CREATE,
            Permission.QUOTE_READ,
            Permission.PURCHASE_SUBMIT,
            Permission.PAYMENT_INITIATE,
            Permission.WORKFLOW_ADVANCE,
        }
    ),
    Role.AGENT: frozenset(
        {
            Permission.FAQ_ASK,
            Permission.POLICY_READ_ASSIGNED,
            Permission.CLAIM_READ,
            Permission.QUOTE_CREATE,
            Permission.QUOTE_READ,
            Permission.WORKFLOW_ADVANCE,
        }
    ),
    Role.AGENT_SUPERVISOR: frozenset(
        {
            Permission.FAQ_ASK,
            Permission.POLICY_READ_ASSIGNED,
            Permission.CLAIM_READ,
            Permission.QUOTE_CREATE,
            Permission.QUOTE_READ,
            Permission.WORKFLOW_ADVANCE,
        }
    ),
    Role.SERVICE: frozenset({Permission.FAQ_ASK, Permission.FINOPS_READ}),
    Role.KNOWLEDGE_ADMIN: frozenset({Permission.FAQ_ASK, Permission.KNOWLEDGE_ADMIN}),
}


class AuthContext(BaseModel):
    """Server-generated identity. Immutable and free of credential material."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_id: str
    actor_type: ActorType
    roles: tuple[Role, ...] = ()
    permissions: tuple[Permission, ...] = ()
    session_id: str | None = None
    tenant_id: str | None = None
    token_id: str | None = None
    auth_time: str | None = None
    #: Agent-only: identifiers the agent is authorized to service. REQUIRES_VERIFICATION
    #: against the approved agent-assignment source before production use.
    assigned_customer_ids: tuple[str, ...] = ()
    agency_id: str | None = None
    scopes: tuple[str, ...] = Field(default=())

    @property
    def is_authenticated(self) -> bool:
        return self.actor_type is not ActorType.ANONYMOUS

    @property
    def subject_ref(self) -> str:
        """Pseudonymous subject reference for logs, audit and rate-limit keys.

        Keyed and process-independent (§22): the same subject yields the same
        reference on every instance and after every restart, so audit events can be
        joined and a shared rate limiter keeps one budget per subject.
        """
        if not self.is_authenticated:
            return "anonymous"
        from app.core.privacy.pseudonym import pseudonymize

        return pseudonymize("sub", self.subject_id)

    def has_role(self, *roles: Role) -> bool:
        return any(role in self.roles for role in roles)

    def has_permission(self, permission: Permission) -> bool:
        if permission in self.permissions:
            return True
        return any(permission in ROLE_PERMISSIONS.get(role, frozenset()) for role in self.roles)

    def effective_permissions(self) -> frozenset[Permission]:
        granted: set[Permission] = set(self.permissions)
        for role in self.roles:
            granted |= ROLE_PERMISSIONS.get(role, frozenset())
        return frozenset(granted)


def anonymous_context(session_id: str | None = None) -> AuthContext:
    """The only ``AuthContext`` that may be created without a validated token."""
    return AuthContext(
        subject_id="anonymous",
        actor_type=ActorType.ANONYMOUS,
        roles=(Role.ANONYMOUS,),
        permissions=(Permission.FAQ_ASK,),
        session_id=session_id,
    )


ActorTypeLiteral = Literal["CUSTOMER", "AGENT", "SERVICE", "ANONYMOUS"]
