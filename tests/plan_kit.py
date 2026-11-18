"""Helpers for the tests of the phase toolkit (W4b-02a): load the test data of
`tests/fixtures/phases/`, resolve a group with fixed workfolder settings and collect what
`resolve_run` and `arr.plan.check.check` report. No model, no run. See docs/10 §4.10."""

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arr.errors import ConfigError, ConfigIssue
from arr.models import (
    GroupSpec,
    Provider,
    ProviderDefaults,
    ResolvedRun,
    RunInput,
    WorkfolderSettings,
)
from arr.plan.check import check
from arr.resolve import check_group_spec, parse_yaml_text, resolve_run, validate_model

FIXTURES = Path(__file__).parent / "fixtures" / "phases"
TEMPLATES = ("solo", "roundtable", "waterfall", "review", "exploration")
"""The five old topologies as `phases` groups (A15)."""

SETTINGS = WorkfolderSettings(
    defaults={
        Provider.CLAUDE: ProviderDefaults(model="claude-sonnet-5-5", auth="max"),
        Provider.CODEX: ProviderDefaults(model="gpt-5.5", auth="chatgpt"),
    }
)


def text(name: str) -> str:
    """The YAML text of a test group."""
    return (FIXTURES / f"{name}.yaml").read_text(encoding="utf-8")


def load(name: str = "beispiel") -> dict[str, Any]:
    """A test group as plain data (a fresh copy every time)."""
    data, _lines = parse_yaml_text(text(name), f"{name}.yaml")
    return copy.deepcopy(data)


def resolve(data: Mapping[str, Any], params: Mapping[str, Any] | None = None) -> ResolvedRun:
    """Load, check placeholders and resolve a group like the skills page does."""
    spec = validate_model(GroupSpec, dict(data), source="group.yaml")
    check_group_spec(spec)
    return resolve_run(RunInput(project="p", task="Aufgabe"), spec, SETTINGS, params)


@dataclass
class Found:
    """What resolving and checking a group report."""

    errors: list[ConfigIssue] = field(default_factory=list)
    warnings: list[ConfigIssue] = field(default_factory=list)
    resolved: ResolvedRun | None = None

    @property
    def paths(self) -> list[str]:
        return [i.path for i in self.errors]

    def error(self, path: str) -> ConfigIssue:
        """The error at `path`; fails the test if there is none."""
        for issue in self.errors:
            if issue.path == path:
                return issue
        raise AssertionError(f"kein Fehler bei {path}: {[str(i) for i in self.errors]}")


def findings(data: Mapping[str, Any], params: Mapping[str, Any] | None = None) -> Found:
    """Errors and hints of a group: those of loading/resolving, else those of `check`."""
    try:
        resolved = resolve(data, params)
    except ConfigError as exc:
        return Found(errors=list(exc.issues))
    result = check(resolved)
    return Found(result.errors, result.warnings, resolved)


def phase(data: dict[str, Any], name: str) -> dict[str, Any]:
    """The phase `name` of plain group data, to change it in place."""
    for entry in data["phases"]:
        if entry["name"] == name:
            return entry
    raise KeyError(name)
