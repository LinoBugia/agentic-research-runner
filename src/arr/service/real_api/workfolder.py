"""Area `workfolder`: the current workfolder, the global counters and the warnings (W3-01).

The counters follow docs/08 §8.3 and are computed from the run store: „Aktiv“ counts the
runs in an active status, „Wartet auf dich“ the open entries of `approvals.jsonl` of active
runs (the inbox itself comes with W3-03). Warnings come from run data only: runs that were
set to `interrupted` when the service started, and active runs close to a limit.
"""

import shutil
from pathlib import Path

from arr.service.api_models import (
    Action,
    GlobalCounts,
    Severity,
    WarningInfo,
    WarningKind,
    WorkfolderInfo,
    WorkfolderRef,
)
from arr.service.real_api.base import ServiceState
from arr.service.real_api.rundir import (
    RunRecord,
    counts_as_active,
    open_items,
    spent_usd,
    turn_counts,
)
from arr.service.state import recent_workfolders
from arr.workfolder import is_workfolder

LIMIT_WARN_RATIO = 0.8


def _projects_count(workfolder: Path) -> int:
    try:
        return sum(1 for p in (workfolder / "projects").iterdir() if p.is_dir())
    except OSError:
        return 0


def counts(state: ServiceState) -> GlobalCounts:
    """The global counters (docs/08 §8.3) from the run store."""
    records = state.store.scan(refresh=False)
    now = state.now()
    active = []
    for record in records:
        if record.status.is_active:
            record.refresh_meta()
            if counts_as_active(record, now):
                record.approvals.refresh()
                active.append(record)
    waiting = sum(len(open_items(r)) for r in active)
    return GlobalCounts(waiting=waiting, active=len(active), warnings=len(warnings(state)))


def _limit_warning(record: RunRecord, base_url: str) -> WarningInfo | None:
    resolved = record.load_resolved()
    if resolved is None or record.meta is None:
        return None
    limits = resolved.limits
    entries = record.log.entries
    parts = []
    spent = spent_usd(entries)
    if limits.budget_usd and spent is not None and spent / limits.budget_usd >= LIMIT_WARN_RATIO:
        parts.append(f"Budget {spent:.2f} von {limits.budget_usd:.2f} $")
    used = max(turn_counts(entries).values(), default=0)
    if limits.turns_per_agent and used / limits.turns_per_agent >= LIMIT_WARN_RATIO:
        parts.append(f"Turns {used} von {limits.turns_per_agent}")
    if not parts:
        return None
    name = record.meta.name or record.run_id
    return WarningInfo(
        id=f"limit-{record.run_id}",
        kind=WarningKind.USAGE_LIMIT,
        severity=Severity.WARNING,
        title=f"„{name}“ ist nahe an einem Limit",
        text=", ".join(parts) + ". Erreicht der Run das Limit, endet er sauber mit „Fertig“.",
        action=Action(label="Run ansehen", url=f"{base_url}/runs/{record.run_id}"),
    )


def warnings(state: ServiceState) -> list[WarningInfo]:
    """Warnings from run data (docs/08 §4.2); empty when nothing needs attention."""
    out: list[WarningInfo] = []
    if state.interrupted_at_start:
        n = len(state.interrupted_at_start)
        out.append(
            WarningInfo(
                id="runs-interrupted",
                kind=WarningKind.RUNS_INTERRUPTED,
                severity=Severity.WARNING,
                title=f"{n} Run{'s' if n != 1 else ''} wurde{'n' if n != 1 else ''} abgebrochen",
                text=(
                    "arr wurde beendet, während diese Runs liefen. Alles bis dahin ist "
                    "gespeichert und lesbar; fortsetzen geht in v1 nicht."
                ),
                action=Action(label="Runs ansehen", url=f"{state.base_url}/runs?status=fehler"),
            )
        )
    for record in state.store.scan(refresh=False):
        if record.status.is_active:
            warning = _limit_warning(record, state.base_url)
            if warning is not None:
                out.append(warning)
    return out


class WorkfolderArea:
    """`get_workfolder`, `get_counts` and `list_warnings` of `ApiClient`."""

    def __init__(self, state: ServiceState) -> None:
        self.state = state

    async def get_workfolder(self) -> WorkfolderInfo:
        root = self.state.workfolder
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            free = 0
        known = []
        for path in [root, *recent_workfolders()]:
            if any(k.path == str(path) for k in known) or not is_workfolder(path):
                continue
            known.append(
                WorkfolderRef(
                    name=path.name,
                    path=str(path),
                    projects_count=_projects_count(path),
                    active_runs=0,
                )
            )
        return WorkfolderInfo(
            name=root.name,
            path=str(root),
            projects_count=_projects_count(root),
            runs_count=len(self.state.store.scan(refresh=False)),
            disk_free_bytes=free,
            known=known,
            service_url=self.state.base_url,
        )

    async def get_counts(self) -> GlobalCounts:
        return counts(self.state)

    async def list_warnings(self) -> list[WarningInfo]:
        return warnings(self.state)
