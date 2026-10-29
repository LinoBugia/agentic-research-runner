"""Area `approvals`: the approval inbox of the service (W3-03 fills it in).

W3-01 provides only the decider the runs of the service get: it refuses every request at
once with the hint that approvals in the GUI come with W3-03. The inbox then marks the item
obsolete with that note and the agent sees a denial (docs/07 §6: nothing is ever granted
without a click in the GUI). W3-03 replaces this module and nothing else.
"""

from collections.abc import Iterable

from arr.permissions.inbox import DeciderUnavailable
from arr.permissions.types import ApprovalItem as CoreApprovalItem
from arr.permissions.types import ApprovalStatus, ApprovalType, Decision
from arr.service.api_models import ApprovalItem, DecisionRequest, DecisionResponse
from arr.service.real_api.base import ServiceState, unavailable

ORDER = "W3-03"
PENDING_NOTE = (
    "Freigaben in der Oberfläche kommen mit Auftrag W3-03. Bis dahin lehnt arr Anfragen von "
    "Runs, die über die Oberfläche laufen, sofort ab."
)


class GuiPendingDecider:
    """`ApprovalDecider` of W3-01: refuses at once, the item becomes obsolete with the note."""

    note = PENDING_NOTE

    async def decide(self, item: CoreApprovalItem) -> Decision | None:
        raise DeciderUnavailable(self.note)


class ApprovalsArea:
    """Inbox methods of `ApiClient` (all `unavailable` until W3-03) and the run decider."""

    def __init__(self, state: ServiceState) -> None:
        self.state = state
        self.decider = GuiPendingDecider()

    async def list_approvals(
        self,
        status: Iterable[ApprovalStatus] | None = None,
        run_id: str | None = None,
        type: ApprovalType | None = None,
        since: float | None = None,
    ) -> list[ApprovalItem]:
        raise unavailable("Die Freigabe-Inbox", ORDER)

    async def get_approval(self, approval_id: str) -> ApprovalItem:
        raise unavailable("Die Freigabe-Inbox", ORDER)

    async def decide_approval(self, approval_id: str, request: DecisionRequest) -> DecisionResponse:
        raise unavailable("Das Entscheiden von Freigaben", ORDER)

    async def revoke_approval(self, approval_id: str) -> ApprovalItem:
        raise unavailable("Das Zurückziehen von Freigaben", ORDER)
