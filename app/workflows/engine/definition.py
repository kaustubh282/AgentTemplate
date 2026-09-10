"""Workflow definitions: allow-listed states, transitions and validators (§2.2).

A workflow is data, not code branches. Every transition is explicitly declared with
the action that triggers it, the permissions it needs, the fields it writes and the
validator that must pass. Anything not declared is rejected.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.auth.auth_context import Permission
from app.core.errors.taxonomy import ValidationError
from app.core.resilience.policies import SideEffectClass

#: A validator receives the submitted payload plus current state data and either
#: returns the fields to persist or raises ValidationError.
Validator = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]


def no_op_validator(payload: Mapping[str, Any], _state: Mapping[str, Any]) -> dict[str, Any]:
    return dict(payload)


@dataclass(frozen=True, slots=True)
class Transition:
    """One allow-listed edge in the state machine."""

    action: str
    from_state: str
    to_state: str
    description: str
    required_permissions: frozenset[Permission] = frozenset()
    fields_written: tuple[str, ...] = ()
    #: Fields that must already be present in state before this transition may run.
    requires_fields: tuple[str, ...] = ()
    validator: Validator = no_op_validator
    side_effect_class: SideEffectClass = SideEffectClass.READ_ONLY
    requires_confirmation: bool = False
    requires_idempotency_key: bool = False
    #: Optional service binding executed by the workflow service before committing.
    service_action: str | None = None
    is_terminal: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """A complete, versioned deterministic journey."""

    workflow_id: str
    version: str
    domain: str
    initial_state: str
    states: tuple[str, ...]
    terminal_states: frozenset[str]
    transitions: tuple[Transition, ...]
    #: Fields projected into AI context for this workflow (§58.2 minimal context).
    ai_context_fields: tuple[str, ...] = ()
    description: str = ""
    _index: dict[tuple[str, str], Transition] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        index: dict[tuple[str, str], Transition] = {}
        for transition in self.transitions:
            if transition.from_state not in self.states:
                raise ValueError(f"{self.workflow_id}: unknown from_state {transition.from_state}")
            if transition.to_state not in self.states:
                raise ValueError(f"{self.workflow_id}: unknown to_state {transition.to_state}")
            key = (transition.from_state, transition.action)
            if key in index:
                raise ValueError(f"{self.workflow_id}: duplicate transition {key}")
            index[key] = transition
        if self.initial_state not in self.states:
            raise ValueError(f"{self.workflow_id}: initial_state not in states")
        if not self.terminal_states <= set(self.states):
            raise ValueError(f"{self.workflow_id}: terminal_states not a subset of states")
        object.__setattr__(self, "_index", index)

    def find(self, from_state: str, action: str) -> Transition | None:
        return self._index.get((from_state, action))

    def actions_for(self, state: str) -> tuple[str, ...]:
        return tuple(sorted(a for (s, a) in self._index if s == state))

    def is_terminal(self, state: str) -> bool:
        return state in self.terminal_states


def require_fields(payload: Mapping[str, Any], names: tuple[str, ...]) -> None:
    missing = [n for n in names if payload.get(n) in (None, "", [])]
    if missing:
        raise ValidationError("missing_required_fields", details={"fields": missing})


class WorkflowRegistry:
    """Domain workflows register here at startup; core never imports a domain."""

    def __init__(self) -> None:
        self._by_id: dict[str, WorkflowDefinition] = {}

    def register(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        if definition.workflow_id in self._by_id:
            raise ValueError(f"duplicate workflow: {definition.workflow_id}")
        self._by_id[definition.workflow_id] = definition
        return definition

    def get(self, workflow_id: str) -> WorkflowDefinition:
        definition = self._by_id.get(workflow_id)
        if definition is None:
            raise ValidationError("unknown_workflow", details={"workflowId": workflow_id})
        return definition

    def all(self) -> tuple[WorkflowDefinition, ...]:
        return tuple(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)


workflow_registry = WorkflowRegistry()
