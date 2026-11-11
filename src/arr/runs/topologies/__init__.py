"""Built-in topologies, implemented as Python policies parametrized via `group.yaml` (E8).

`topology_for(kind)` returns the topology function `async def run(ctx: RunContext) -> None`;
`check_topology(resolved)` checks before the start what only the topology needs (parameters,
stages, raters, channels) and raises `ConfigError` with a German message naming the field.
`planned_stages(resolved)` is the plan of the run: the `stages` of `run.yaml`, else the fixed
phases a topology reports in `PHASES` (W4-03 F2), which `open_from` must name.
Topologies use nothing but `RunContext`. The task comes from `ctx.config.task` and reaches the
agents through the prompt or the context package (`get_assignment()`), never through `task.md`.

See docs/03-skills-und-research-groups.md §4 and docs/01-architektur.md §6.
"""

import difflib
import fnmatch
import importlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from arr.backends.base import TurnOutcome, TurnStatus
from arr.errors import ArrError, ConfigError, ConfigIssue
from arr.models import ResolvedRun, TopologyKind
from arr.room.types import Result
from arr.runs.context import RunContext

__all__ = [
    "ASK_AGAIN",
    "AVAILABLE",
    "Topology",
    "agent_param",
    "agents_param",
    "ask_for_result",
    "bool_param",
    "check_topology",
    "choice_param",
    "describe",
    "ensure_valid",
    "int_param",
    "last_result",
    "phase_issues",
    "planned_stages",
    "require_task",
    "result_ref",
    "suggest",
    "topology_for",
]

Topology = Callable[[RunContext], Awaitable[None]]
"""A topology: `async def run(ctx: RunContext) -> None` (same as `arr.runs.manager.Topology`)."""

AVAILABLE = {
    TopologyKind.SOLO: "solo",
    TopologyKind.ROUNDTABLE: "roundtable",
    TopologyKind.EXPLORATION: "exploration",
    TopologyKind.WATERFALL: "waterfall",
    TopologyKind.REVIEW: "review",
}
"""Topologies implemented (module name in this package)."""

ASK_AGAIN = 1
"""How often a topology asks again for a missing result (or verdict) before it goes on."""

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


def planned_stages(resolved: ResolvedRun) -> list[str]:
    """The plan of a run (checkpoint `plan`): the names of `stages`, else the topology's
    fixed `PHASES` (W4-03 F2), else none."""
    if resolved.stages:
        return [s.name for s in resolved.stages]
    try:
        module = _module(resolved.topology.kind)
    except ArrError:
        return []
    return list(getattr(module, "PHASES", ()))


def suggest(value: str, known: Sequence[str]) -> str:
    """„ Meintest du „x“?“ for the closest known name, else empty."""
    close = difflib.get_close_matches(value, list(known), n=1, cutoff=0.5)
    return f" Meintest du „{close[0]}“?" if close else ""


def phase_issues(resolved: ResolvedRun, phases: Sequence[str]) -> list[ConfigIssue]:
    """F2: every `open_from` must name a phase of the topology, with a suggestion if not."""
    issues = []
    for index, chat in enumerate(resolved.groupchats):
        if chat.open_from is None or chat.open_from in phases:
            continue
        issues.append(
            ConfigIssue(
                f"groupchats[{index}].open_from",
                f"Der Kanal „{chat.name}“ öffnet ab „{chat.open_from}“, das ist keine Phase "
                f"von „{resolved.topology.kind.value}“.{suggest(chat.open_from, phases)} "
                f"Phasen: {', '.join(phases)}.",
            )
        )
    return issues


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


def choice_param(
    params: Mapping[str, Any],
    name: str,
    choices: Sequence[str],
    default: str,
    issues: list[ConfigIssue],
) -> str:
    """A parameter with fixed choices; adds an issue (with suggestion) and returns `default`."""
    value = params.get(name)
    if value is None:
        return default
    if not isinstance(value, str) or value not in choices:
        hint = suggest(value, choices) if isinstance(value, str) else ""
        issues.append(
            ConfigIssue(
                f"params.{name}",
                f"Parameter „{name}“ ist {describe(value)}.{hint} Erlaubt: {', '.join(choices)}.",
            )
        )
        return default
    return value


def agents_param(
    resolved: ResolvedRun,
    name: str,
    issues: list[ConfigIssue],
    *,
    default: Sequence[str] = (),
) -> list[str]:
    """A parameter naming agents: an ID, a pattern (`explorer-*`) or a list of them (W4-03).

    Returns the agent IDs in configuration order, without duplicates; `default` (IDs or
    patterns) when the parameter is not set. An unknown ID or a pattern that matches no agent
    is an issue with a suggestion.
    """
    value = resolved.params.get(name)
    entries: Sequence[Any] = default if value is None else value
    if isinstance(entries, str):
        entries = [entries]
    ids = [a.id for a in resolved.agents]
    if not isinstance(entries, list | tuple) or not all(isinstance(e, str) for e in entries):
        issues.append(
            ConfigIssue(
                f"params.{name}",
                f"Parameter „{name}“ muss einen Agenten, ein Muster wie „explorer-*“ oder eine "
                f"Liste davon nennen, gefunden {describe(value)}.",
            )
        )
        return []
    found: list[str] = []
    for entry in entries:
        matched = [i for i in ids if fnmatch.fnmatchcase(i, entry)]
        if not matched and value is not None:
            issues.append(
                ConfigIssue(
                    f"params.{name}",
                    f"Parameter „{name}“ nennt „{entry}“, darauf passt kein Agent dieses Runs."
                    f"{suggest(entry, ids)} Vorhanden: {', '.join(ids)}.",
                )
            )
        found += matched
    return [i for i in ids if i in found]


# --------------------------------------------------------------------------------------------
# Turns and results shared by the topologies
# --------------------------------------------------------------------------------------------


def result_ref(result: Result, name: str | None = None) -> dict[str, Any]:
    """A result for a context package: ID, stage, title, path; the body only without a file.

    `name` adds who submitted it (`von`), as the receiving agent sees that agent.
    """
    ref: dict[str, Any] = {
        "id": result.id,
        "stage": result.stage,
        "title": result.title,
        "path": result.path,
    }
    if name is not None:
        ref["von"] = name
    if result.path is None:
        ref["body"] = result.body
    return ref


def last_result(
    outcome: TurnOutcome, accept: Callable[[Result], bool] | None = None
) -> Result | None:
    """The last result of a turn that `accept` takes; a turn with status `error` has none."""
    if outcome.status is TurnStatus.ERROR:
        return None
    results = [r for r in outcome.results if accept is None or accept(r)]
    return results[-1] if results else None


async def ask_for_result(
    ctx: RunContext,
    agent_id: str,
    prompt: str,
    again: str | Callable[[Result | None], str],
    *,
    stage: str | None = None,
    context: Mapping[str, Any] | None = None,
    accept: Callable[[Result], bool] | None = None,
) -> Result | None:
    """A turn, then up to `ASK_AGAIN` more while no result came that `accept` takes.

    `again` is the prompt of the follow-up turn, or a function of the last result submitted
    (None if there is none). Stops asking when the run stops or a turn ends with `limit` (the
    agent used up `turns_per_agent`); the caller checks `ctx.stop_requested`. Every wait thus
    ends after at most `1 + ASK_AGAIN` turns.
    """
    outcome = await ctx.turn(agent_id, prompt, stage=stage, context=context)
    found = last_result(outcome, accept)
    asked = 0
    while (
        found is None
        and asked < ASK_AGAIN
        and outcome.status is not TurnStatus.LIMIT  # the agent used up its turns
        and not ctx.stop_requested
    ):
        text = again if isinstance(again, str) else again(last_result(outcome))
        outcome = await ctx.turn(agent_id, text, stage=stage, context=context)
        found = last_result(outcome, accept)
        asked += 1
    return found


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
