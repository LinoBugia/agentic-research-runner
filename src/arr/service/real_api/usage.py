"""Area `usage`: the usage report (W5-01 fills it in, together with the index of
docs/02-workfolder-und-runs.md §8)."""

from arr.service.api_models import UsageGroupBy, UsageReport
from arr.service.real_api.base import ServiceState, unavailable

ORDER = "W5-01"


class UsageArea:
    """`get_usage` of `ApiClient`, `unavailable` until W5-01."""

    def __init__(self, state: ServiceState) -> None:
        self.state = state

    async def get_usage(
        self,
        from_ts: float | None = None,
        to_ts: float | None = None,
        group_by: UsageGroupBy = UsageGroupBy.PROFILE,
        project: str | None = None,
    ) -> UsageReport:
        raise unavailable("Die Usage-Auswertung", ORDER)
