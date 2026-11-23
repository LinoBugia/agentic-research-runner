"""What a phases run must pass before its start (W4b-02b §6.1; docs/10 §4.10).

`start_issues(resolved, context_type)` gives the errors of `arr.plan.check.check` (hints do not
stop a start; the same error of every agent of a pattern is reported once) and what this
version of the runner cannot run yet, each with its path and a German sentence:

- topologies listed in `NOT_YET` (empty since W4b-04: `moderated` runs; `chat` and `standby`
  run since W4b-03),
- `topology: moderated` with a context class that does not implement the methods of the
  assignments (`DELEGATION_METHODS`, W4b-04): the `Orchestrator` does, so a real run is never
  refused for it,
- `folder: fresh` and `context: compact` only with a context class that does not implement
  `fresh_folder` or `compact_session` (a test context). The `Orchestrator` implements both
  (W4b-05a, W4b-05b), so a real run is never refused for them (W4b-02b.1 Teil 2). The check
  stays as a wall against a context that would raise `NotImplementedError` in the middle of
  a run.

`check_topology` calls it with the `Orchestrator` before anything is created; the runner calls
it again at its start with the class of its context (`ensure_valid`), in case the check was
skipped. `SOURCE` and `require_task` came here from `arr.runs.topologies` (W4b-06b).
"""

import re
from collections.abc import Sequence

from arr.errors import ArrError, ConfigIssue
from arr.models import ContextMode, FolderMode, PhaseTopology, ResolvedRun
from arr.plan.check import check
from arr.runs.context import RunContext

__all__ = [
    "DELEGATION_METHODS",
    "NOT_YET",
    "READY",
    "SOURCE",
    "STANDBY_READY",
    "implements",
    "merge_issues",
    "require_task",
    "start_issues",
]

SOURCE = "run.yaml"
"""The source named in the `ConfigError` of the start checks."""

NOT_YET: dict[PhaseTopology, str] = {}
"""Topologies of a phase this version refuses at the start (empty since W4b-04)."""

STANDBY_READY = True
"""Agents on standby (`standby`) can run (since W4b-03)."""

READY = "solo, parallel, chat, moderated"

DELEGATION_METHODS = (
    "delegations",
    "delegation",
    "start_delegation",
    "end_delegation",
    "cancel_delegations",
    "end_turn",
)
"""What a phase `moderated` needs of its context (W4b-04): the table of the assignments and
the end of a turn. A context class without its own methods refuses `moderated` at the start."""


def implements(context_type: type, method: str) -> bool:
    """True if `context_type` has its own `method`, not the default of `RunContext` that
    raises `NotImplementedError`."""
    return getattr(context_type, method, None) is not getattr(RunContext, method)


def require_task(ctx: RunContext) -> str:
    """The task text of the run; `ArrError` if it is empty, before the first turn starts."""
    task = (ctx.config.task or "").strip()
    if not task:
        raise ArrError(
            "Der Run hat keinen Auftrag: task.md ist leer. Gib die Aufgabe mit „task:“ oder "
            "„task_file:“ in der run.yaml an."
        )
    return task


_QUOTED = re.compile(r"„([^“]*)“")


def merge_issues(issues: Sequence[ConfigIssue], agents: Sequence[str]) -> list[ConfigIssue]:
    """The same error at the same path for several agents (of a pattern) becomes one, naming
    them all; identical errors are reported once."""
    known = set(agents)
    groups: dict[tuple[str, str], list[str]] = {}
    order: list[tuple[str, str]] = []
    for issue in issues:
        names = [n for n in _QUOTED.findall(issue.message) if n in known]
        template = issue.message
        if len(names) == 1:
            template = issue.message.replace(f"„{names[0]}“", "„{agent}“", 1)
        key = (issue.path, template)
        if key not in groups:
            groups[key] = []
            order.append(key)
        if len(names) == 1 and names[0] not in groups[key]:
            groups[key].append(names[0])
    out = []
    for path, template in order:
        names = groups[(path, template)]
        if "{agent}" not in template:
            out.append(ConfigIssue(path, template))
            continue
        joined = "“, „".join(names)
        out.append(ConfigIssue(path, template.replace("{agent}", joined, 1)))
    return out


def start_issues(resolved: ResolvedRun, context_type: type) -> list[ConfigIssue]:
    """Errors that keep a phases run from starting with a context of `context_type`."""
    found = check(resolved)
    issues = merge_issues(found.errors, [a.id for a in resolved.agents])
    fresh = implements(context_type, "fresh_folder")
    compact = implements(context_type, "compact_session")
    delegates = all(implements(context_type, m) for m in DELEGATION_METHODS)
    for phase in resolved.phases:
        at = f"phases[{phase.index}]"
        if phase.topology in NOT_YET:
            issues.append(
                ConfigIssue(
                    f"{at}.topology",
                    f"Die Topologie „{NOT_YET[phase.topology]}“ (Phase „{phase.name}“) ist in "
                    f"dieser Version noch nicht verfügbar. Verfügbar sind: {READY}.",
                )
            )
        elif phase.topology is PhaseTopology.MODERATED and not delegates:
            issues.append(
                ConfigIssue(
                    f"{at}.topology",
                    f"Die Topologie „moderated“ (Phase „{phase.name}“) braucht Aufträge des "
                    "Leads, und dieser Ablauf kann sie nicht vergeben. Nimm „solo“, "
                    "„parallel“ oder „chat“.",
                )
            )
        if phase.standby and not STANDBY_READY:
            issues.append(
                ConfigIssue(
                    f"{at}.standby",
                    f"Agenten auf Abruf („standby“, Phase „{phase.name}“) sind in dieser "
                    "Version noch nicht verfügbar. Gib ihnen eine Action oder lass sie weg.",
                )
            )
        fresh_agents = [a for a, mode in phase.folder.items() if mode is FolderMode.FRESH]
        if fresh_agents and not fresh:
            issues.append(
                ConfigIssue(
                    f"{at}.folder",
                    f"Ein frischer Arbeitsordner („folder: fresh“, Phase „{phase.name}“) ist in "
                    "dieser Version noch nicht verfügbar. „context: reset“ gibt ihn im "
                    "Standard; setze dort „folder: keep“.",
                )
            )
        compacted = [a for a, mode in phase.context.items() if mode is ContextMode.COMPACT]
        if compacted and not compact:
            issues.append(
                ConfigIssue(
                    f"{at}.context",
                    f"„context: compact“ (Phase „{phase.name}“) ist in dieser Version noch "
                    "nicht verfügbar. Nimm „keep“ oder „reset“.",
                )
            )
    return issues
