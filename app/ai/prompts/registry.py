"""Versioned prompt registry (master prompt §33, §58.6).

Prompts live in versioned files, never as inline strings scattered through services.
Every prompt carries metadata (id, version, owner, purpose, change note, evaluation
baseline) so a prompt change is traceable and triggers regression evals.

Prompts are deliberately *short*: security and workflow rules that can be enforced
deterministically are enforced in code, not restated to the model every request
(§58.6).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from app.ai.models.provider import approx_tokens as estimate_tokens
from app.core.errors.taxonomy import ConfigurationError

PROMPTS_DIR = Path(__file__).parent / "templates"


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """One versioned prompt with its governance metadata."""

    prompt_id: str
    version: str
    purpose: str
    owner: str
    change_note: str
    evaluation_baseline: str
    text: str

    def render(self, **variables: object) -> str:
        try:
            return self.text.format(**variables)
        except KeyError as exc:
            raise ConfigurationError(f"prompt_missing_variable:{self.prompt_id}:{exc.args[0]}") from None

    @property
    def approx_tokens(self) -> int:
        """Same estimator as the context builder, so budgets and reports agree (L-4)."""
        return max(1, estimate_tokens(self.text))


class PromptRegistry:
    """Loads and serves prompt templates by id."""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or PROMPTS_DIR
        self._by_id: dict[str, PromptTemplate] = {}
        self._loaded = False

    def load(self) -> PromptRegistry:
        if self._loaded:
            return self
        if not self._dir.exists():
            raise ConfigurationError(f"prompt_directory_missing:{self._dir}")
        for path in sorted(self._dir.rglob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            missing = [
                k
                for k in (
                    "prompt_id",
                    "version",
                    "purpose",
                    "owner",
                    "change_note",
                    "evaluation_baseline",
                    "text",
                )
                if k not in data
            ]
            if missing:
                raise ConfigurationError(f"prompt_metadata_incomplete:{path.name}:{','.join(missing)}")
            template = PromptTemplate(
                prompt_id=str(data["prompt_id"]),
                version=str(data["version"]),
                purpose=str(data["purpose"]),
                owner=str(data["owner"]),
                change_note=str(data["change_note"]),
                evaluation_baseline=str(data["evaluation_baseline"]),
                text=str(data["text"]).strip(),
            )
            self._by_id[template.prompt_id] = template
        self._loaded = True
        return self

    def get(self, prompt_id: str) -> PromptTemplate:
        self.load()
        template = self._by_id.get(prompt_id)
        if template is None:
            raise ConfigurationError(f"unknown_prompt:{prompt_id}")
        return template

    def all(self) -> tuple[PromptTemplate, ...]:
        self.load()
        return tuple(self._by_id.values())

    def combined_version(self) -> str:
        """Version fingerprint across all prompts, stamped into audit and cache keys."""
        self.load()
        ordered = sorted(self._by_id.values(), key=lambda x: x.prompt_id)
        return "|".join(f"{p.prompt_id}@{p.version}" for p in ordered)


prompt_registry = PromptRegistry()
