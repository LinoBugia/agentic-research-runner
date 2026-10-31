"""Area `setup`: workfolder, projects, settings, mandate, rules, toolchain (W3-04 fills it in).

Every method raises `unavailable` with a German message; the pages show it instead of their
content (docs/08 §8.4). W3-04 replaces this module (and its pages) and nothing else. Only
`get_workfolder_settings` works already (W3-02): „Neuer Run“ reads its standards from it.
"""

from arr.errors import ArrError
from arr.models import BUILTIN_LIMITS, WorkfolderSettings
from arr.service.api_models import (
    ApprovalSettingsInfo,
    AppSettings,
    CreateProjectRequest,
    FixedRule,
    LimitsInfo,
    MandateInfo,
    MandateUpdate,
    ProjectSetupResult,
    ProviderDefaultsInfo,
    RetentionInfo,
    SetupState,
    ToolchainInfo,
    ToolchainTestResult,
    WorkfolderInfo,
    WorkfolderRequest,
    WorkfolderSettingsInfo,
)
from arr.service.real_api.base import ServiceState, from_arr_error, unavailable
from arr.workfolder import load_workfolder_settings

ORDER = "W3-04"
MIN_TIMEOUT_S = 60
"""Lower bound of `ApprovalSettingsInfo.timeout_s`; shorter values in `arr.yaml` show as this."""


def settings_info(settings: WorkfolderSettings, *, running_runs: int) -> WorkfolderSettingsInfo:
    """`arr.yaml` for the GUI; limits fall back to the built-in ones (docs/02 §3.3)."""
    limits = settings.limits
    wall_time = limits.wall_time if limits.wall_time is not None else BUILTIN_LIMITS.wall_time
    approvals = settings.approvals
    gatekeeper = approvals.gatekeeper
    return WorkfolderSettingsInfo(
        defaults={
            provider: ProviderDefaultsInfo(auth=d.auth, model=d.model, effort=d.effort)
            for provider, d in settings.defaults.items()
        },
        limits=LimitsInfo(
            wall_time_s=int(wall_time.total_seconds()) if wall_time is not None else None,
            turns_per_agent=limits.turns_per_agent or BUILTIN_LIMITS.turns_per_agent,
            budget_usd=limits.budget_usd
            if limits.budget_usd is not None
            else BUILTIN_LIMITS.budget_usd,
        ),
        concurrency=dict(settings.concurrency),
        toolchain=ToolchainInfo(
            python=settings.toolchain.python,
            extra_path=list(settings.toolchain.extra_path),
            run_venv=settings.toolchain.run_venv,
        ),
        approvals=ApprovalSettingsInfo(
            decider=approvals.decider,
            timeout_s=max(MIN_TIMEOUT_S, int(approvals.timeout.total_seconds())),
            gatekeeper_model=gatekeeper.model if gatekeeper else None,
            mandate=gatekeeper.mandate if gatekeeper else None,
        ),
        retention=RetentionInfo(
            slim_after_days=settings.retention.slim_after_days,
            archive_after_days=settings.retention.archive_after_days,
            max_runs_size_gb=settings.retention.max_runs_size_gb,
        ),
        running_runs=running_runs,
    )


class SetupArea:
    """Setup methods of `ApiClient`, all `unavailable` until W3-04."""

    def __init__(self, state: ServiceState) -> None:
        self.state = state

    async def get_setup_state(self) -> SetupState:
        raise unavailable("Der Einrichtungs-Assistent", ORDER)

    async def select_workfolder(self, request: WorkfolderRequest) -> WorkfolderInfo:
        raise unavailable("Das Wechseln und Anlegen von Workfoldern", ORDER)

    async def create_project(self, request: CreateProjectRequest) -> ProjectSetupResult:
        raise unavailable("Das Anlegen und Einrichten von Projekten", ORDER)

    async def setup_project(self, name: str) -> ProjectSetupResult:
        raise unavailable("Das Einrichten von Projekten", ORDER)

    async def get_app_settings(self) -> AppSettings:
        raise unavailable("Die Einstellungen", ORDER)

    async def update_app_settings(self, settings: AppSettings) -> AppSettings:
        raise unavailable("Die Einstellungen", ORDER)

    async def get_workfolder_settings(self) -> WorkfolderSettingsInfo:
        """`arr.yaml` as it is now, like a run would read it."""
        try:
            settings = load_workfolder_settings(self.state.workfolder)
        except ArrError as exc:
            raise from_arr_error(exc) from None
        return settings_info(settings, running_runs=len(self.state.registry.active()))

    async def update_workfolder_settings(
        self, settings: WorkfolderSettingsInfo
    ) -> WorkfolderSettingsInfo:
        raise unavailable("Die Einstellungen des Workfolders", ORDER)

    async def get_mandate(self, name: str = "standard") -> MandateInfo:
        raise unavailable("Die Vollmacht des Aufsichts-Agenten", ORDER)

    async def update_mandate(self, name: str, update: MandateUpdate) -> MandateInfo:
        raise unavailable("Die Vollmacht des Aufsichts-Agenten", ORDER)

    async def list_fixed_rules(self) -> list[FixedRule]:
        raise unavailable("Die festen Regeln", ORDER)

    async def delete_fixed_rule(self, rule_id: str) -> list[FixedRule]:
        raise unavailable("Die festen Regeln", ORDER)

    async def test_toolchain(self, python: str | None = None) -> ToolchainTestResult:
        raise unavailable("Der Test der Python-Umgebung", ORDER)
