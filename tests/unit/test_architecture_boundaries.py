"""Dependency-direction and core-boundary checks (master prompt §45.1, §59.4, §59.8).

Enforced mechanically so the extension model cannot rot:
  * core must not import any product/domain module
  * domains depend only on published core interfaces
  * no circular core <-> domain dependency
  * the platform stays free of decorative abstractions
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path("app")

#: Packages that make up the protected platform core (§45.1).
CORE_PACKAGES = (
    "app.core",
    "app.workflows",
    "app.ui_directives",
    "app.rag",
    "app.ai",
    "app.integrations.contracts",
    "app.orchestration.router",
)

DOMAIN_PREFIX = "app.domains"


def _module_name(path: Path) -> str:
    return ".".join(path.with_suffix("").parts)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
    return found


def _python_files(*prefixes: str) -> list[Path]:
    return [p for p in APP.rglob("*.py") if any(_module_name(p).startswith(prefix) for prefix in prefixes)]


CORE_FILES = _python_files(*CORE_PACKAGES)
DOMAIN_FILES = _python_files(DOMAIN_PREFIX)


def test_the_check_actually_found_files():
    assert len(CORE_FILES) > 20
    assert len(DOMAIN_FILES) > 3


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: _module_name(p))
def test_core_never_imports_a_domain(path):
    """Core must not import product-specific modules (§45.1)."""
    offenders = [i for i in _imports(path) if i.startswith(DOMAIN_PREFIX)]
    assert not offenders, f"{_module_name(path)} imports domain code: {offenders}"


@pytest.mark.parametrize("path", DOMAIN_FILES, ids=lambda p: _module_name(p))
def test_domains_import_only_published_core_interfaces(path):
    """A domain may use core and other domains' *published* contracts, nothing private."""
    for imported in _imports(path):
        if not imported.startswith("app."):
            continue
        assert "._" not in imported, f"{_module_name(path)} imports a private module: {imported}"


#: Entry points are referenced by the runtime, not by another module.
ENTRY_POINT_MODULES = {"app.main", "app.bootstrap"}


def _referenced_modules(path: Path) -> set[str]:
    """Every module a file could be referring to: ``import a.b``, ``from a import b``
    (both ``a`` and ``a.b``), and string references such as ``"app.x.y"``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("app."):
            found.add(node.value)
    return found


def test_every_module_is_referenced_somewhere():
    """§1.2 / §59.10: a module nothing imports is architecture theatre and is removed.

    Every module under ``app`` must be referenced by another module in ``app``,
    ``evals``, ``scripts`` or ``tests``. This is the mechanical form of the deletion
    review: an unreferenced hook bridge was found and removed this way (M-4).
    """
    referenced: set[str] = set()
    for root in (Path("app"), Path("evals"), Path("scripts"), Path("tests")):
        for path in root.rglob("*.py"):
            referenced.update(_referenced_modules(path))

    dead: list[str] = []
    for path in APP.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        module = _module_name(path)
        if module in ENTRY_POINT_MODULES:
            continue
        if module in referenced or any(ref.startswith(module + ".") for ref in referenced):
            continue
        dead.append(module)
    assert not dead, f"modules referenced nowhere (dead code): {dead}"


def test_no_circular_dependency_between_core_and_domains():
    domain_modules = {_module_name(p) for p in DOMAIN_FILES}
    for path in CORE_FILES:
        for imported in _imports(path):
            assert imported not in domain_modules


def test_composition_root_is_the_only_place_that_knows_every_domain():
    """Only bootstrap wires domains; no core module enumerates them."""
    wiring = [
        _module_name(p)
        for p in APP.rglob("*.py")
        if "app.domains" in "".join(_imports(p)) and not _module_name(p).startswith(DOMAIN_PREFIX)
    ]
    allowed = {"app.bootstrap", "app.api.routers.policies", "app.api.routers.conversations"}
    unexpected = set(wiring) - allowed
    assert not unexpected, f"unexpected modules wire domains: {unexpected}"


def test_domains_do_not_reimplement_core_infrastructure():
    """§59.8: a domain must reuse auth/security/PII/observability/audit, not copy it."""
    forbidden = (
        "class MaskingService",
        "class GuardrailService",
        "class AuditService",
        "jwt.decode",
        "class RateLimiter",
        "configure_logging(",
        "class WorkflowEngine",
        "class DirectiveRegistry",
    )
    for path in DOMAIN_FILES:
        text = path.read_text(encoding="utf-8")
        for banned in forbidden:
            assert banned not in text, f"{_module_name(path)} reimplements core: {banned}"


def test_travel_domain_adds_no_new_core_files():
    """The extension proof must not have required new platform infrastructure."""
    travel_files = {p.name for p in (APP / "domains" / "travel").glob("*.py")}
    assert travel_files <= {"__init__.py", "module.py", "workflow.py"}, (
        "the second domain should need only its own workflow and module"
    )


def test_travel_domain_reuses_shared_platform_services():
    text = (APP / "domains" / "travel" / "module.py").read_text(encoding="utf-8")
    for reused in (
        "from app.core.auth.auth_context import",
        "from app.core.registry.capability import",
        "from app.ui_directives.registry.registry import",
        "from app.integrations.contracts.providers import",
        "from app.core.audit.service import",
    ):
        assert reused in text, f"travel domain should reuse core: {reused}"


def test_ai_layer_does_not_touch_providers_directly():
    """AI/agents must reach authoritative data only through typed tools (§8.1)."""
    agent_files = list((APP / "ai" / "agents").glob("*.py"))
    for path in agent_files:
        for imported in _imports(path):
            assert not imported.startswith("app.integrations"), (
                f"{path.name} must not import an integration directly"
            )


def test_agents_do_not_import_the_workflow_engine():
    """The model may not mutate authoritative state (§2.1)."""
    for path in (APP / "ai").rglob("*.py"):
        for imported in _imports(path):
            assert imported != "app.workflows.engine.engine", f"{path} must not import the workflow engine"
            assert imported != "app.workflows.engine.service"


def test_every_capability_declares_a_budget_and_an_audit_policy(container):
    """§1.2: no decorative capability - each carries real runtime declarations."""
    for capability in container.capability_registry.all():
        assert capability.budgets.timeout_ms > 0
        assert capability.audit_policy is not None
        assert capability.allowed_channels
        assert capability.service_binding
        if capability.model_required:
            assert capability.budgets.model_call_budget >= 1
        else:
            assert capability.budgets.model_call_budget == 0


def test_no_capability_exceeds_the_agent_complexity_budget(container):
    """§5.6: default request-path targets."""
    for capability in container.capability_registry.all():
        assert capability.budgets.model_call_budget <= 1, (
            f"{capability.id} would need more than one model call"
        )
        assert capability.budgets.agent_step_budget <= 2, (
            f"{capability.id} would need more than two agent steps"
        )


def test_only_three_platform_agents_exist():
    """§5.6: no agent without a documented justification."""
    agent_modules = {
        p.stem for p in (APP / "ai" / "agents").glob("*.py") if p.stem not in ("__init__", "schemas")
    }
    assert agent_modules == {"faq_agent", "intent_agent", "supervisor"}


def test_supervisor_documents_its_complexity_budget():
    """§5.6 requires a written justification for every agent."""
    text = (APP / "ai" / "agents" / "supervisor.py").read_text(encoding="utf-8")
    for required in (
        "distinct responsibility",
        "why deterministic routing is insufficient",
        "cost",
        "latency impact",
        "security boundary",
        "evaluation ownership",
    ):
        assert required in text.lower(), f"supervisor must document: {required}"
