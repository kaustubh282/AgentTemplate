"""The domain extension contract (master prompt §45, §45.1).

A new bot/product is added by implementing :class:`DomainModule` and registering it.
Core never imports a domain: domains depend on published core interfaces, and the
dependency-direction check in ``tests/unit/test_architecture_boundaries.py`` enforces
that direction mechanically.
"""

from __future__ import annotations

from typing import Protocol

from app.core.registry.capability import Capability
from app.workflows.engine.definition import WorkflowDefinition


class DomainModule(Protocol):
    """Everything a domain contributes to the platform."""

    #: Stable domain slug, e.g. "motor". Used for knowledge filters and metrics labels.
    domain: str

    def workflows(self) -> tuple[WorkflowDefinition, ...]:
        """Deterministic journeys owned by this domain."""
        ...

    def capabilities(self) -> tuple[Capability, ...]:
        """Capabilities the router and PEP will know about."""
        ...


class DomainRegistry:
    """Holds the enabled domains for this deployment."""

    def __init__(self) -> None:
        self._modules: dict[str, DomainModule] = {}

    def register(self, module: DomainModule) -> DomainModule:
        if module.domain in self._modules:
            raise ValueError(f"duplicate domain: {module.domain}")
        self._modules[module.domain] = module
        return module

    def get(self, domain: str) -> DomainModule | None:
        return self._modules.get(domain)

    def all(self) -> tuple[DomainModule, ...]:
        return tuple(self._modules.values())

    def domains(self) -> tuple[str, ...]:
        return tuple(self._modules)

    def __len__(self) -> int:
        return len(self._modules)
