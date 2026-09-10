"""Resource scope / ownership enforcement (master prompt §13.2).

Ownership is *never* taken from a request payload or from model-generated arguments
when the authenticated context can determine or constrain it. Every customer-,
agent-, policy-, claim- and payment-scoped access passes through
:class:`ResourceScopeInterceptor`, which fails closed with ``FORBIDDEN`` and emits an
audit event without leaking whether the protected resource exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.core.auth.auth_context import ActorType, AuthContext
from app.core.errors.taxonomy import ForbiddenError


class ResourceType(StrEnum):
    CUSTOMER = "CUSTOMER"
    POLICY = "POLICY"
    CLAIM = "CLAIM"
    QUOTE = "QUOTE"
    PAYMENT = "PAYMENT"
    DOCUMENT = "DOCUMENT"
    CONVERSATION = "CONVERSATION"
    WORKFLOW = "WORKFLOW"


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """A resource being accessed, plus the owner asserted by the authoritative source."""

    resource_type: ResourceType
    resource_id: str
    #: Owning customer as recorded by the System of Record - not by the caller.
    owner_customer_id: str | None = None
    tenant_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceOwnership:
    """Who owns a resource according to the System of Record - and in which tenant."""

    customer_id: str
    tenant_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    allowed: bool
    reason: str
    resource_ref: ResourceRef | None = None


class OwnershipResolver(Protocol):
    """Resolves the authoritative owner *and tenant* of a resource id.

    The tenant must come from the System of Record, never from the caller: comparing
    the caller's tenant with itself is a check that can never fail (H-1).
    """

    async def resolve_owner(
        self, resource_type: ResourceType, resource_id: str
    ) -> ResourceOwnership | None: ...


class AgentAssignmentPolicy(Protocol):
    """Decides whether an agent may service a given customer.

    REQUIRES_VERIFICATION: the real assignment / consent / delegation rules must come
    from an approved source before production use.
    """

    async def is_authorized(self, auth: AuthContext, customer_id: str) -> bool: ...


class ClaimBasedAgentAssignmentPolicy:
    """Default policy: agents may only service explicitly assigned customers.

    Presence of the ``AGENT`` role alone grants nothing (§13.2).
    """

    async def is_authorized(self, auth: AuthContext, customer_id: str) -> bool:
        if auth.actor_type is not ActorType.AGENT:
            return False
        return customer_id in auth.assigned_customer_ids


class ResourceScopeInterceptor:
    """Single enforcement point for ownership and tenancy."""

    def __init__(
        self,
        ownership_resolver: OwnershipResolver,
        agent_policy: AgentAssignmentPolicy | None = None,
    ) -> None:
        self._ownership = ownership_resolver
        self._agent_policy = agent_policy or ClaimBasedAgentAssignmentPolicy()

    def effective_customer_id(self, auth: AuthContext, requested_customer_id: str | None) -> str:
        """Derive the customer scope, ignoring any caller-supplied identity for customers.

        A customer is always pinned to their own subject. An agent must name a customer
        explicitly, and that customer is then checked against the assignment policy.
        """
        if auth.actor_type is ActorType.CUSTOMER:
            return auth.subject_id
        if auth.actor_type is ActorType.AGENT:
            if not requested_customer_id:
                raise ForbiddenError("agent_customer_scope_required")
            return requested_customer_id
        raise ForbiddenError("actor_cannot_access_customer_scope")

    async def authorize_customer_scope(self, auth: AuthContext, customer_id: str) -> ScopeDecision:
        if auth.actor_type is ActorType.CUSTOMER:
            if customer_id != auth.subject_id:
                raise ForbiddenError("cross_customer_access_denied")
            return ScopeDecision(True, "own_customer_scope")
        if auth.actor_type is ActorType.AGENT:
            if await self._agent_policy.is_authorized(auth, customer_id):
                return ScopeDecision(True, "agent_assigned_customer")
            raise ForbiddenError("agent_customer_not_assigned")
        raise ForbiddenError("actor_cannot_access_customer_scope")

    async def authorize_resource(
        self,
        auth: AuthContext,
        resource_type: ResourceType,
        resource_id: str,
        *,
        requested_customer_id: str | None = None,
    ) -> ResourceRef:
        """Authorize access to one resource, returning its authoritative reference.

        Fails closed with an indistinguishable ``FORBIDDEN`` whether the resource is
        missing or simply not owned, so existence is not disclosed.
        """
        if not resource_id or not resource_id.strip():
            raise ForbiddenError("invalid_resource_reference")

        # The caller's own authority is settled *before* the System of Record is
        # touched. An agent who is not authorized for this customer is refused without
        # any lookup, so the denial cannot disclose whether the resource exists or who
        # owns it, and an unauthorized caller never causes an upstream call.
        scope_customer = self.effective_customer_id(auth, requested_customer_id)
        await self.authorize_customer_scope(auth, scope_customer)

        ownership = await self._ownership.resolve_owner(resource_type, resource_id)
        if ownership is None or ownership.customer_id != scope_customer:
            # A missing resource and a resource owned by someone else are
            # indistinguishable to the caller (§13.2 fail-closed).
            raise ForbiddenError("resource_scope_denied")

        # The reference carries the *resource's* tenant as recorded by the System of
        # Record. The tenant check therefore compares the caller with the resource, not
        # the caller with itself (H-1).
        ref = ResourceRef(
            resource_type=resource_type,
            resource_id=resource_id,
            owner_customer_id=ownership.customer_id,
            tenant_id=ownership.tenant_id,
        )
        self._check_tenant(auth, ref)
        return ref

    @staticmethod
    def _check_tenant(auth: AuthContext, ref: ResourceRef) -> None:
        """Fail closed: a tenant-scoped caller may only read resources whose tenant is
        known and identical. An unresolved resource tenant is a denial, not a pass."""
        if auth.tenant_id and ref.tenant_id != auth.tenant_id:
            raise ForbiddenError("cross_tenant_access_denied")
