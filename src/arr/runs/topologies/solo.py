"""Topology `solo`: a single agent works on the task.

The agent gets the task in its first prompt (and as `context.task`). With
`require_result: true` (default) the topology asks again until a `submit_result` comes, for at
most `max_turns` turns. A turn with status `error` counts as a turn without a result. At the
end a checkpoint `arbeit` records the result (or that there is none). The baseline for
comparisons. See docs/03-skills-und-research-groups.md §4.1.

Parameters: `agent` (default: the only agent), `require_result` (true), `max_turns` (3).
"""

from arr.backends.base import TurnStatus
from arr.errors import ConfigIssue
from arr.models import ResolvedRun
from arr.runs.context import RunContext
from arr.runs.topologies import agent_param, bool_param, ensure_valid, int_param, require_task

STAGE = "arbeit"
"""Name of the checkpoint at the end of the work."""
MAX_TURNS = 3


def check(resolved: ResolvedRun) -> list[ConfigIssue]:
    """Problems of the `solo` parameters (see `check_topology`)."""
    issues: list[ConfigIssue] = []
    agent_param(resolved, "agent", issues)
    if "agent" not in resolved.params and len(resolved.agents) != 1:
        issues.append(
            ConfigIssue(
                "params.agent",
                f"„solo“ braucht genau einen Agenten, der Run hat {len(resolved.agents)}. Nenne "
                "den arbeitenden Agenten im Parameter „agent“.",
            )
        )
    bool_param(resolved.params, "require_result", True, issues)
    int_param(resolved.params, "max_turns", MAX_TURNS, issues, minimum=1)
    return issues


async def run(ctx: RunContext) -> None:
    """Run `solo` (docs/03 §4.1)."""
    ensure_valid(ctx, check)
    params = ctx.params
    agent_id = params.get("agent") or next(iter(ctx.agents))
    require_result = params.get("require_result") is not False
    max_turns = int(params.get("max_turns") or MAX_TURNS)
    task = require_task(ctx)
    context = {"task": task}
    prompt = f"## Aufgabe\n\n{task}\n\nGib dein Ergebnis mit `submit_result` ab, sobald es steht."
    results = []
    used = 0
    while used < max_turns:
        outcome = await ctx.turn(agent_id, prompt, context=context)
        used += 1
        if ctx.stop_requested:
            return
        if outcome.status is not TurnStatus.ERROR:
            results += outcome.results
        if results or not require_result:
            break
        prompt = (
            "Du hast noch kein Ergebnis abgegeben. Gib es jetzt mit `submit_result` ab "
            f"(Nachfrage {used} von {max_turns - 1}). Die Aufgabe steht in `get_assignment()`."
        )
    info = {"agent": agent_id, "turns": used, "results": [r.id for r in results]}
    if results or not require_result:
        await ctx.checkpoint(STAGE, **info, decision="passed")
    else:
        await ctx.checkpoint(STAGE, **info, decision="failed", reason="no_result")
