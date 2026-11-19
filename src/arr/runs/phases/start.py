"""What a phases run must pass before its start (W4b-02b §6.1; docs/10 §4.10).

`start_issues(resolved, context_type)` gives the errors of `arr.plan.check.check` (hints do not
stop a start; the same error of every agent of a pattern is reported once) and what this
version of the runner cannot run yet, each with its path and a German sentence:

- phases with `topology: chat` or `moderated` (`NOT_YET`; W4b-03, W4b-04),
- phases with `standby` (W4b-03),
- `folder: fresh` and `context: compact` only with a context class that does not implement
  `fresh_folder` or `compact_session` (a test context). The `Orchestrator` implements both
  (W4b-05a, W4b-05b), so a real run is never refused for them (W4b-02b.1 Teil 2). The check
  stays as a wall against a context that would raise `NotImplementedError` in the middle of
  a run.

`check_topology` calls it with the `Orchestrator` before anything is created; the runner calls
it again at its start with the class of its context (`ensure_valid`), in case the check was
skipped.
"""

import re
from collections.abc import Sequence

from arr.errors import ConfigIssue
from arr.models import ContextMode, FolderMode, PhaseTopology, ResolvedRun
from arr.plan.check import check
from arr.runs.context import RunContext

__all__ = [
    "NOT_YET",
    "STANDBY_READY",
    "implements",
    "merge_issues",
    "start_issues",
]

NOT_YET: dict[PhaseTopology, str] = {
    PhaseTopology.CHAT: "chat",
    PhaseTopology.MODERATED: "moderated",
}
"""Topologies of a phase this version refuses at the start (W4b-03 and W4b-04 remove theirs)."""

STANDBY_READY = False
"""Agents on standby (`standby`) can run (W4b-03 sets it)."""

READY = "solo, parallel"


def implements(context_type: type, method: str) -> bool:
    """True if `context_type` has its own `method`, not the default of `RunContext` that
    raises `NotImplementedError`."""
    return getattr(context_type, method, None) is not getattr(RunContext, method)


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
