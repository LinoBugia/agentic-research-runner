"""Area `setup`: workfolder, projects, settings, mandate, rules, toolchain (W3-04 fills it in).

Every method raises `unavailable` with a German message; the pages show it instead of their
content (docs/08 §8.4). W3-04 replaces this module (and its pages) and nothing else.
"""

from arr.service.api_models import (
    AppSettings,
    CreateProjectRequest,
    FixedRule,
    MandateInfo,
    MandateUpdate,
    ProjectSetupResult,
    SetupState,
    ToolchainTestResult,
    WorkfolderInfo,
    WorkfolderRequest,
    WorkfolderSettingsInfo,
)
from arr.service.real_api.base import ServiceState, unavailable

ORDER = "W3-04"


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
        raise unavailable("Die Einstellungen des Workfolders", ORDER)

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
