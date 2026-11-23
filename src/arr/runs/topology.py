"""The topology of a run: only `topology: phases` runs (W4b-06b; docs/10 §4.10, §5).

The five old topologies (`solo`, `roundtable`, `waterfall`, `review`, `exploration`) are gone.
`TopologyKind` keeps their values so that the `run.yaml` of old runs stays readable; a run or
group that names one gets `old_topology_message` (`arr.plan.legacy`) as a `ConfigError`.
Refused in four places, so no way starts an old topology (W4b-06b §6.1):

1. `plan_run_input` before `resolve_run` (`refuse_old_topology` on the kind of `run.yaml`
   and of the group),
2. the check of a group skill in the service (`catalog._check_group`),
3. `check_topology` and `topology_for` (here),
4. `RunManager.__init__`, even with a topology function of its own.

`topology_for(kind)` returns the phase runner `arr.runs.phases.runner.run`;
`check_topology(resolved)` checks before the start what the runner needs (`runner.check`).
"""

from collections.abc import Awaitable, Callable

from arr.errors import ArrError, ConfigError
from arr.models import ResolvedRun, TopologyKind
from arr.plan.legacy import old_topology_message
from arr.runs.context import RunContext

__all__ = [
    "GROUP_SOURCE",
    "SOURCE",
    "Topology",
    "check_topology",
    "refuse_old_topology",
    "topology_for",
]

Topology = Callable[[RunContext], Awaitable[None]]
"""A topology: `async def run(ctx: RunContext) -> None` (same as `arr.runs.manager.Topology`)."""

SOURCE = "run.yaml"
GROUP_SOURCE = "group.yaml"


def _kind(kind: TopologyKind | str) -> TopologyKind:
    try:
        return TopologyKind(kind)
    except ValueError:
        raise ArrError(
            f"Die Topologie „{kind}“ gibt es nicht. Ein Run beschreibt seinen Ablauf mit "
            "„topology: phases“."
        ) from None


def refuse_old_topology(
    kind: TopologyKind | str | None, *, path: str = "topology.kind", source: str = SOURCE
) -> None:
    """`ConfigError` at `path` with `old_topology_message` if `kind` is an old topology."""
    if kind is None:
        return
    message = old_topology_message(kind)
    if message is not None:
        raise ConfigError.single(path, message, source=source)


def topology_for(kind: TopologyKind | str) -> Topology:
    """The topology function of `kind`: the phase runner for `phases`; `ArrError` (with the
    message for old topologies) otherwise."""
    kind = _kind(kind)
    refuse_old_topology(kind)
    from arr.runs.phases import runner  # late: the runner imports the orchestrator

    return runner.run


def check_topology(resolved: ResolvedRun) -> None:
    """Check what the phase runner needs, before `prepare_run` (W2-05 calls it).

    Raises `ConfigError` (an `ArrError`) listing every problem with the field it concerns;
    an old topology gets its message.
    """
    refuse_old_topology(resolved.topology.kind)
    from arr.runs.phases import runner

    issues = runner.check(resolved)
    if issues:
        raise ConfigError(issues, source=SOURCE)
