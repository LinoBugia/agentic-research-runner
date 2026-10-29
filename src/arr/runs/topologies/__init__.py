"""Built-in topologies, implemented as Python policies parametrized via `group.yaml` (E8).

`topology_for(kind)` returns the topology function `async def run(ctx: RunContext) -> None`;
`check_topology(resolved)` checks before the start what only the topology needs (parameters,
stages, raters, channels) and raises `ConfigError` with a German message naming the field.
Topologies use nothing but `RunContext`. The task comes from `ctx.config.task` and reaches the
agents through the prompt or the context package (`get_assignment()`), never through `task.md`.

See docs/03-skills-und-research-groups.md §4 and docs/01-architektur.md §6.
"""

import importlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arr.errors import ArrError, ConfigError, ConfigIssue
from arr.models import ResolvedRun, TopologyKind
from arr.runs.context import RunContext

__all__ = [
    "AVAILABLE",
    "Topology",
    "agent_param",
    "bool_param",
    "check_topology",
    "describe",
    "ensure_valid",
    "int_param",
    "require_task",
    "topology_for",
]

Topology = Callable[[RunContext], Awaitable[None]]
"""A topology: `async def run(ctx: RunContext) -> None` (same as `arr.runs.manager.Topology`)."""

AVAILABLE = {
    TopologyKind.SOLO: "solo",
    TopologyKind.ROUNDTABLE: "roundtable",
    TopologyKind.WATERFALL: "waterfall",
}
"""Topologies implemented so far (module name in this package); the others follow in W4-03."""

SOURCE = "run.yaml"


def _module(kind: TopologyKind | str) -> Any:
    try:
        kind = TopologyKind(kind)
    except ValueError:
        known = ", ".join(k.value for k in TopologyKind)
        raise ArrError(f"Die Topologie „{kind}“ gibt es nicht. Vorhanden: {known}.") from None
    if kind not in AVAILABLE:
        ready = ", ".join(k.value for k in AVAILABLE)
        raise ArrError(
            f"Die Topologie „{kind.value}“ ist noch nicht verfügbar. Verfügbar sind: {ready}."
        )
    # imported lazily: the modules import the helpers below
    return importlib.import_module(f"{__name__}.{AVAILABLE[kind]}")


def topology_for(kind: TopologyKind | str) -> Topology:
    """The topology function of `kind`; `ArrError` for unknown or not yet available kinds."""
    return _module(kind).run


def check_topology(resolved: ResolvedRun) -> None:
    """Check what only the topology needs, before `prepare_run` (W2-05 calls it).

    Raises `ConfigError` (an `ArrError`) listing every problem with the field it concerns,
    or `ArrError` if the topology is not available yet.
    """
    issues: list[ConfigIssue] = _module(resolved.topology.kind).check(resolved)
    if issues:
        raise ConfigError(issues, source=SOURCE)


def ensure_valid(ctx: RunContext, check: Callable[[ResolvedRun], list[ConfigIssue]]) -> None:
    """Run the topology's own check at its start, in case `check_topology` was skipped."""
    issues = check(ctx.config)
    if issues:
        raise ConfigError(issues, source=SOURCE)


def require_task(ctx: RunContext) -> str:
    """The task text of the run; `ArrError` if it is empty, before the first turn starts."""
    task = (ctx.config.task or "").strip()
    if not task:
        raise ArrError(
            "Der Run hat keinen Auftrag: task.md ist leer. Gib die Aufgabe mit „task:“ oder "
            "„task_file:“ in der run.yaml an."
        )
    return task


# --------------------------------------------------------------------------------------------
# Parameter checks shared by the topologies
# --------------------------------------------------------------------------------------------


def int_param(
    params: Mapping[str, Any],
    name: str,
    default: int,
    issues: list[ConfigIssue],
    *,
    minimum: int = 0,
) -> int:
    """An integer parameter (`2.0` counts as 2); adds an issue and returns `default` if bad.

    Parameters are numbers of one kind (docs/03 §3), so `2.5` passes the resolution and is
    caught here (W1-01.1 review N3).
    """
    value = params.get(name, default)
    if value is None:
        value = default
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(
            ConfigIssue(
                f"params.{name}",
                f"Parameter „{name}“ muss eine ganze Zahl sein, gefunden {describe(value)}.",
            )
        )
        return default
    if value < minimum:
        issues.append(
            ConfigIssue(
                f"params.{name}",
                f"Parameter „{name}“ muss mindestens {minimum} sein, gefunden {value}.",
            )
        )
        return default
    return value


def bool_param(
    params: Mapping[str, Any], name: str, default: bool, issues: list[ConfigIssue]
) -> bool:
    """A boolean parameter; adds an issue and returns `default` if it is not true/false."""
    value = params.get(name, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        issues.append(
            ConfigIssue(
                f"params.{name}",
                f"Parameter „{name}“ muss true oder false sein, gefunden {describe(value)}.",
            )
        )
        return default
    return value


def agent_param(
    resolved: ResolvedRun, name: str, issues: list[ConfigIssue], *, required: bool = False
) -> str | None:
    """A parameter naming an agent of the run (or None if it is not set)."""
    value = resolved.params.get(name)
    if value is None:
        if required:
            issues.append(ConfigIssue(f"params.{name}", f"Parameter „{name}“ fehlt."))
        return None
    ids = [a.id for a in resolved.agents]
    if not isinstance(value, str) or value not in ids:
        issues.append(
            ConfigIssue(
                f"params.{name}",
                f"Parameter „{name}“ nennt {describe(value)}, das ist kein Agent dieses Runs. "
                f"Vorhanden: {', '.join(ids)}.",
            )
        )
        return None
    return value


def describe(value: Any) -> str:
    """A parameter value for a German message: „text“, true/false, leer, eine Liste, …."""
    if isinstance(value, str):
        return f"„{value}“"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "leer"
    if isinstance(value, list):
        return "eine Liste"
    if isinstance(value, dict):
        return "ein Abschnitt"
    return str(value)
