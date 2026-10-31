"""Area `storage`: disk use and the run actions slim, archive, delete, pin (W3-02: slim;
W5-01: the rest and the report; docs/02-workfolder-und-runs.md §9).

`slim` deletes through `arr.runs.slim`, which has the rules for what may go. The sizes it names
are logical: clones of the project (APFS) free only what the agents changed, and the private
sizes come with W5-01.
"""

import asyncio

from arr.errors import ArrError
from arr.runs.slim import SlimPlan, check_slimmable, plan_slim, slim_run
from arr.service.api_models import ActionResult, ApiException, RunAction, StorageReport
from arr.service.gui import format as fmt
from arr.service.real_api.base import ServiceState, not_allowed, unavailable
from arr.service.real_api.rundir import RunRecord

ORDER = "W5-01"
STORAGE_ACTIONS = frozenset({RunAction.ARCHIVE, RunAction.DELETE, RunAction.PIN, RunAction.UNPIN})
"""Run actions that are `unavailable` until W5-01."""

SLIM_UNSAVED = (
    "Achtung: Dieser Run wurde nicht abgeschlossen, seine Änderungen sind nirgends gesichert. "
    "Die Arbeit der Agenten in den Arbeitskopien ist danach endgültig weg."
)
SLIM_FINAL = "Das lässt sich nicht rückgängig machen."
SLIM_CLONE_HINT = (
    "Das ist die logische Größe. Arbeitskopien sind APFS-Klone: Bei unverändertem Projekt wird "
    "nur der Platz der geänderten Dateien frei."
)


def _removes(plan: SlimPlan) -> str:
    return "Arbeitskopien, Snapshot und Run-venv" if plan.venv else "Arbeitskopien und Snapshot"


def _preview(plan: SlimPlan) -> str:
    """The lines of the dialog: what goes, what really stays, that it is final (§8.6)."""
    lines = [f"Frei werden bis zu {fmt.size(plan.logical_bytes)}: {_removes(plan)}."]
    if plan.unsaved:
        lines.append(SLIM_UNSAVED)
    if plan.kept:
        lines.append(f"Es bleiben {fmt.join_de(list(plan.kept))}.")
    return "\n".join([*lines, SLIM_FINAL, SLIM_CLONE_HINT])


class StorageArea:
    """`get_storage` and the storage run actions; the rest is `unavailable` until W5-01."""

    def __init__(self, state: ServiceState) -> None:
        self.state = state

    async def get_storage(self) -> StorageReport:
        raise unavailable("Die Speicher-Übersicht", ORDER)

    def action_error(self, action: RunAction) -> ApiException:
        """The error for a storage action on a run."""
        return unavailable("Archivieren, Löschen und Anheften von Runs", ORDER)

    async def slim(self, record: RunRecord, *, dry_run: bool) -> ActionResult:
        """Slim a finished run, or with `dry_run` say what would go and what stays (T12)."""
        try:
            if dry_run:
                check_slimmable(record.run_dir)
                plan = await asyncio.to_thread(plan_slim, record.run_dir)
                return ActionResult(message=_preview(plan), freed_bytes=plan.logical_bytes)
            plan = await asyncio.to_thread(slim_run, record.run_dir)
        except ArrError as exc:
            raise not_allowed(exc.message) from None
        return ActionResult(
            message=f"Verschlankt: bis zu {fmt.size(plan.logical_bytes)} frei ({_removes(plan)}). "
            f"Transkripte und Chat bleiben lesbar.",
            freed_bytes=plan.logical_bytes,
        )
