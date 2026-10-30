"""Area `storage`: disk use and the run actions slim, archive, delete, pin (W3-02: slim;
W5-01: the rest and the report; docs/02-workfolder-und-runs.md §9)."""

from arr.service.api_models import ApiException, RunAction, StorageReport
from arr.service.real_api.base import ServiceState, unavailable

ORDER = "W5-01"
STORAGE_ACTIONS = frozenset(
    {RunAction.SLIM, RunAction.ARCHIVE, RunAction.DELETE, RunAction.PIN, RunAction.UNPIN}
)
"""Run actions this area answers (all `unavailable` in W3-01)."""


class StorageArea:
    """`get_storage` and the storage run actions, `unavailable` until W3-02 / W5-01."""

    def __init__(self, state: ServiceState) -> None:
        self.state = state

    async def get_storage(self) -> StorageReport:
        raise unavailable("Die Speicher-Übersicht", ORDER)

    def action_error(self, action: RunAction) -> ApiException:
        """The error for a storage action on a run."""
        if action is RunAction.SLIM:
            return unavailable("Das Verschlanken von Runs", "W3-02")
        return unavailable("Archivieren, Löschen und Anheften von Runs", ORDER)
