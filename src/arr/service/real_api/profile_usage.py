"""„Nachweis je Profil“: what ran through arr on one auth profile (W3-06, docs/08 §5.8).

The source is `usage.json` of a finished run. A run without one (`interrupted`, or one that is
still active) is summed from its transcripts by `compute_run_usage`, so aborted runs and runs
of `arr run` count too; they lie in the same run folders. Only the agents on this profile count
in a mixed run; which those are says `run.yaml`, so a run without a finished turn shows too,
with zero. A period takes a run by the moment it started.

The record covers this workfolder only, and no use of Claude Code or Codex outside arr: `NOTE`
says so on the page.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from arr.models import RunStatus
from arr.service.api_models import ProfileRunUsage, ProfileUsage, ProfileUsageSum
from arr.service.real_api.base import ServiceState
from arr.service.real_api.rundir import RunRecord
from arr.usage import AgentUsage, RunUsage, UsageTotal, compute_run_usage

NOTE = (
    "Erfasst sind nur Läufe über arr in diesem Workfolder, auch aus „arr run“. Die direkte "
    "Nutzung von Claude Code oder Codex fehlt hier."
)
ABORTED = (RunStatus.STOPPED, RunStatus.FAILED, RunStatus.INTERRUPTED)
WEEK_S = 7 * 24 * 3600

_TOKENS = ("input", "output", "cache_read", "cache_write", "reasoning")


def _sum_fields(total: UsageTotal) -> dict[str, Any]:
    tokens = {name: getattr(total.usage, name) or 0 for name in _TOKENS}
    return tokens | {
        "turns": total.turns,
        "cost_billed_usd": total.cost_billed_usd,
        "cost_estimated_usd": total.cost_estimated_usd,
        "cost_unknown_turns": total.cost_unknown_turns,
    }


def _added(total: UsageTotal, part: UsageTotal) -> UsageTotal:
    return UsageTotal(
        usage=total.usage + part.usage,
        turns=total.turns + part.turns,
        cost_billed_usd=total.cost_billed_usd + part.cost_billed_usd,
        cost_estimated_usd=total.cost_estimated_usd + part.cost_estimated_usd,
        cost_unknown_turns=total.cost_unknown_turns + part.cost_unknown_turns,
    )


def _agents_on(usage: RunUsage | None, name: str, planned: dict[str, str]) -> dict[str, AgentUsage]:
    """Agents of the run on profile `name`. An agent without a finished turn has no profile in
    the usage, so `planned` (`run.yaml`) names it: a run that is still in its first turn,
    or ended before one finished, shows with zero (review W3-06, B4)."""
    known = usage.by_agent if usage is not None else {}
    agents = {agent: item for agent, item in known.items() if item.profile == name}
    for agent, profile in planned.items():
        if profile == name and known.get(agent, AgentUsage()).profile is None:
            agents[agent] = known.get(agent) or AgentUsage(profile=name)
    return agents


def _planned(record: RunRecord) -> dict[str, str]:
    """Agent → auth profile from `run.yaml`."""
    resolved = record.load_resolved()
    return {a.id: a.auth for a in resolved.agents if a.auth} if resolved is not None else {}


def _period(label: str, rows: list[tuple[ProfileRunUsage, UsageTotal]]) -> ProfileUsageSum:
    total = UsageTotal()
    for _, part in rows:
        total = _added(total, part)
    return ProfileUsageSum(label=label, runs=len(rows), **_sum_fields(total))


class ProfileUsageArea:
    """`get_profile_usage` of `ApiClient`; `find` raises for an unknown profile."""

    def __init__(self, state: ServiceState, find: Callable[[str], Any]) -> None:
        self.state = state
        self.find = find
        self._settled: dict[str, RunUsage] = {}
        """Transcripts of an ended run do not change: summed once."""

    def _run_usage(self, record: RunRecord) -> RunUsage | None:
        """`usage.json` of the finalizer, else the sum of the transcripts."""
        if record.final and record.usage is not None:
            return record.usage
        known = self._settled.get(record.run_id)
        if known is not None and record.final:
            return known
        resolved = record.load_resolved()
        if resolved is None:
            return None
        usage = compute_run_usage(record.run_dir, resolved.agents, run_id=record.run_id)
        if record.final:
            self._settled[record.run_id] = usage
        return usage

    def _row(self, record: RunRecord, name: str) -> tuple[ProfileRunUsage, UsageTotal] | None:
        agents = _agents_on(self._run_usage(record), name, _planned(record))
        if not agents or record.meta is None:
            return None
        total = UsageTotal()
        for item in agents.values():
            total = _added(total, item)
        meta = record.meta
        row = ProfileRunUsage(
            label=record.run_id,
            runs=1,
            run_id=record.run_id,
            status=meta.status,
            agents=sorted(agents),
            started=meta.started or meta.created,
            ended=meta.ended,
            aborted=meta.status in ABORTED,
            active=meta.status.is_active,
            url=f"/runs/{record.run_id}",
            **_sum_fields(total),
        )
        return row, total

    def get(self, name: str, *, now: datetime | None = None) -> ProfileUsage:
        self.find(name)
        moment = now or datetime.now()
        midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        week = moment.timestamp() - WEEK_S
        rows = [row for r in self.state.store.scan() if (row := self._row(r, name)) is not None]
        rows.sort(key=lambda item: -(item[0].started or 0))
        return ProfileUsage(
            profile=name,
            today=_period("Heute", [r for r in rows if (r[0].started or 0) >= midnight]),
            week=_period("7 Tage", [r for r in rows if (r[0].started or 0) >= week]),
            total=_period("Gesamt", rows),
            runs=[row for row, _ in rows],
            note=NOTE,
        )
