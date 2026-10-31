"""`RealApi`: the `ApiClient` of the service on the files of the workfolder (W3-01 §3.3,
docs/04-service-gui-und-schnittstellen.md §3, docs/02-workfolder-und-runs.md §4).

It is assembled from areas, one class per module, each the border of a later order:

- `runs` (`runs.py`, W3-01; W3-02 changes it): list_runs, get_run, run_action (pause, resume,
  stop), list_channels, get_messages, post_message, list_agents, get_transcript, get_results,
  get_log, get_changes, resolve_run, start_run.
- `live` (`live.py`, W3-01): subscribe, now; starts the watcher (`arr.service.watcher`).
- `workfolder` (`workfolder.py`, W3-01): get_workfolder, get_counts, list_warnings.
- `catalog` (`catalog.py`, W3-01, read only): list_projects, get_project, list_skills,
  get_skill, get_skill_file, list_profiles.
- `approvals` (`approvals.py`, W3-03): list_approvals, get_approval, decide_approval,
  revoke_approval; plus the `decider` of the runs of the service.
- `setup` (`setup.py`, W3-04): get_setup_state, select_workfolder, create_project,
  setup_project, get/update_app_settings, get/update_workfolder_settings, get/update_mandate,
  list/delete_fixed_rule, test_toolchain.
- `profiles` (`profiles.py`, W3-05): test_profile, create_profile, delete_profile, start_login,
  get_login_status, cancel_login.
- `usage` (`usage.py`, W5-01): get_usage.
- `storage` (`storage.py`, W5-01): get_storage; run_action slim (W3-02), archive, delete, pin,
  unpin.

All areas share one `ServiceState` (`base.py`): workfolder and settings, the `RunStore` that
reads the run folders (`rundir.py`, `transcript.py`), the `EventHub`, the `RunRegistry` of
the runs this service executes (with their `RunManager`s) and the `ProfileGate`s shared by
all runs. A later order changes its module (and its pages) and takes what it needs from the
state; `RealApi` itself only delegates.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from arr.models import ProfilesFile, Starter, WorkfolderSettings
from arr.permissions.types import ApprovalStatus, ApprovalType
from arr.runs.launch import discard_orphaned_reservations
from arr.runs.manager import mark_interrupted_runs
from arr.service.api_client import EventHub, Subscription
from arr.service.api_models import (
    ActionResult,
    AgentChanges,
    AgentState,
    ApprovalItem,
    AppSettings,
    ChannelInfo,
    CreateProfileRequest,
    CreateProjectRequest,
    DecisionRequest,
    DecisionResponse,
    FixedRule,
    GlobalCounts,
    LogEntry,
    LoginStart,
    LoginStatus,
    MandateInfo,
    MandateUpdate,
    Message,
    MessagesPage,
    PostMessageRequest,
    ProfileInfo,
    ProjectInfo,
    ProjectSetupResult,
    ResolveResult,
    RunAction,
    RunDetail,
    RunFilter,
    RunResults,
    RunsPage,
    SetupState,
    SkillFile,
    SkillInfo,
    SkillKind,
    StartRunResponse,
    StorageReport,
    ToolchainTestResult,
    TranscriptKind,
    TranscriptPage,
    UsageGroupBy,
    UsageReport,
    WarningInfo,
    WorkfolderInfo,
    WorkfolderRequest,
    WorkfolderSettingsInfo,
)
from arr.service.notify import Notifier, Sender, send_notification
from arr.service.real_api.approvals import ApprovalsArea
from arr.service.real_api.base import ServiceState
from arr.service.real_api.catalog import CatalogArea
from arr.service.real_api.live import LiveArea
from arr.service.real_api.profiles import ProfilesArea
from arr.service.real_api.runs import RunsArea
from arr.service.real_api.setup import SetupArea
from arr.service.real_api.storage import StorageArea
from arr.service.real_api.usage import UsageArea
from arr.service.real_api.workfolder import WorkfolderArea

__all__ = ["RealApi", "ServiceState"]


class RealApi:
    """`ApiClient` on the run folders of a workfolder (see the module docstring)."""

    def __init__(
        self,
        workfolder: str | Path,
        *,
        base_url: str,
        settings: WorkfolderSettings | None = None,
        profiles: ProfilesFile | None = None,
        hub: EventHub | None = None,
        builders: Any = None,
        scan: bool = True,
        secret_reader: Any = None,
        manager_kwargs: dict[str, Any] | None = None,
        notify: Sender | None = None,
    ) -> None:
        self.state = ServiceState(
            workfolder,
            base_url=base_url,
            settings=settings,
            profiles=profiles,
            hub=hub,
            builders=builders,
            scan=scan,
            secret_reader=secret_reader,
            manager_kwargs=manager_kwargs,
        )
        self.workfolder_area = WorkfolderArea(self.state)
        self.catalog = CatalogArea(self.state)
        self.approvals = ApprovalsArea(self.state)
        self.setup = SetupArea(self.state)
        self.profiles = ProfilesArea(self.state)
        self.usage = UsageArea(self.state)
        self.storage = StorageArea(self.state)
        self.runs = RunsArea(self.state, self.approvals, self.storage)
        self.live = LiveArea(self.state)
        self.notifier = Notifier(self.state.hub, notify or send_notification)

    # -- lifecycle --------------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return self.state.base_url

    @property
    def hub(self) -> EventHub:
        return self.state.hub

    def recover(self) -> list[str]:
        """Crash recovery at start: runs left active become `interrupted`, orphaned
        reservations are removed (like `arr run`, W3-01 §3.1); returns the interrupted IDs."""
        interrupted = mark_interrupted_runs(self.state.workfolder)
        discard_orphaned_reservations(self.state.workfolder)
        self.state.interrupted_at_start = interrupted
        return interrupted

    async def start(self) -> None:
        """Recover and start the watcher (in the running event loop)."""
        self.recover()
        await self.live.start()
        self.notifier.start()

    async def stop_runs(self) -> None:
        """Stop all runs of this service and wait for finalizers and scans."""
        await self.runs.stop_all()

    async def close(self) -> None:
        """Stop watcher and notifier (runs are stopped with `stop_runs` first)."""
        await self.live.stop()
        await self.notifier.stop()

    # -- ApiClient: live, workfolder ---------------------------------------------------------

    def now(self) -> float:
        return self.live.now()

    def subscribe(self, run_id: str | None = None, agents: set[str] | None = None) -> Subscription:
        return self.live.subscribe(run_id, agents)

    async def get_workfolder(self) -> WorkfolderInfo:
        return await self.workfolder_area.get_workfolder()

    async def get_counts(self) -> GlobalCounts:
        return await self.workfolder_area.get_counts()

    async def list_warnings(self) -> list[WarningInfo]:
        return await self.workfolder_area.list_warnings()

    # -- ApiClient: approvals ----------------------------------------------------------------

    async def list_approvals(
        self,
        status: Iterable[ApprovalStatus] | None = None,
        run_id: str | None = None,
        type: ApprovalType | None = None,
        since: float | None = None,
    ) -> list[ApprovalItem]:
        return await self.approvals.list_approvals(status, run_id, type, since)

    async def get_approval(self, approval_id: str) -> ApprovalItem:
        return await self.approvals.get_approval(approval_id)

    async def decide_approval(self, approval_id: str, request: DecisionRequest) -> DecisionResponse:
        return await self.approvals.decide_approval(approval_id, request)

    async def revoke_approval(self, approval_id: str) -> ApprovalItem:
        return await self.approvals.revoke_approval(approval_id)

    # -- ApiClient: runs ---------------------------------------------------------------------

    async def list_runs(
        self,
        status: RunFilter = RunFilter.ALL,
        project: str | None = None,
        starter: Starter | None = None,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RunsPage:
        return await self.runs.list_runs(status, project, starter, q, cursor, limit)

    async def get_run(self, run_id: str) -> RunDetail:
        return await self.runs.get_run(run_id)

    async def run_action(
        self, run_id: str, action: RunAction, dry_run: bool = False
    ) -> ActionResult:
        return await self.runs.run_action(run_id, action, dry_run)

    async def list_channels(self, run_id: str) -> list[ChannelInfo]:
        return await self.runs.list_channels(run_id)

    async def get_messages(
        self,
        run_id: str,
        channel: str,
        before_id: int | None = None,
        since_id: int | None = None,
        limit: int = 50,
    ) -> MessagesPage:
        return await self.runs.get_messages(run_id, channel, before_id, since_id, limit)

    async def post_message(self, run_id: str, channel: str, request: PostMessageRequest) -> Message:
        return await self.runs.post_message(run_id, channel, request)

    async def list_agents(self, run_id: str) -> list[AgentState]:
        return await self.runs.list_agents(run_id)

    async def get_transcript(
        self,
        run_id: str,
        agent_id: str,
        from_seq: int | None = None,
        limit: int = 200,
        kinds: Iterable[TranscriptKind] | None = None,
    ) -> TranscriptPage:
        return await self.runs.get_transcript(run_id, agent_id, from_seq, limit, kinds)

    async def get_results(self, run_id: str) -> RunResults:
        return await self.runs.get_results(run_id)

    async def get_log(self, run_id: str) -> list[LogEntry]:
        return await self.runs.get_log(run_id)

    async def get_changes(self, run_id: str) -> list[AgentChanges]:
        return await self.runs.get_changes(run_id)

    async def resolve_run(self, run_input: dict[str, Any]) -> ResolveResult:
        return await self.runs.resolve_run(run_input)

    async def start_run(self, run_input: dict[str, Any]) -> StartRunResponse:
        return await self.runs.start_run(run_input)

    # -- ApiClient: catalog ------------------------------------------------------------------

    async def list_profiles(self) -> list[ProfileInfo]:
        return await self.catalog.list_profiles()

    async def list_projects(self) -> list[ProjectInfo]:
        return await self.catalog.list_projects()

    async def get_project(self, name: str) -> ProjectInfo:
        return await self.catalog.get_project(name)

    async def list_skills(self, kind: SkillKind | None = None) -> list[SkillInfo]:
        return await self.catalog.list_skills(kind)

    async def get_skill(self, name: str) -> SkillInfo:
        return await self.catalog.get_skill(name)

    async def get_skill_file(self, name: str, path: str) -> SkillFile:
        return await self.catalog.get_skill_file(name, path)

    # -- ApiClient: profiles (W3-05) ---------------------------------------------------------

    async def test_profile(self, name: str) -> ProfileInfo:
        return await self.profiles.test_profile(name)

    async def create_profile(self, request: CreateProfileRequest) -> ProfileInfo:
        return await self.profiles.create_profile(request)

    async def delete_profile(self, name: str) -> None:
        await self.profiles.delete_profile(name)

    async def start_login(self, name: str) -> LoginStart:
        return await self.profiles.start_login(name)

    async def get_login_status(self, name: str) -> LoginStatus:
        return await self.profiles.get_login_status(name)

    async def cancel_login(self, name: str) -> LoginStatus:
        return await self.profiles.cancel_login(name)

    # -- ApiClient: usage, storage (W5-01) ---------------------------------------------------

    async def get_usage(
        self,
        from_ts: float | None = None,
        to_ts: float | None = None,
        group_by: UsageGroupBy = UsageGroupBy.PROFILE,
        project: str | None = None,
    ) -> UsageReport:
        return await self.usage.get_usage(from_ts, to_ts, group_by, project)

    async def get_storage(self) -> StorageReport:
        return await self.storage.get_storage()

    # -- ApiClient: setup (W3-04) ------------------------------------------------------------

    async def get_setup_state(self) -> SetupState:
        return await self.setup.get_setup_state()

    async def select_workfolder(self, request: WorkfolderRequest) -> WorkfolderInfo:
        return await self.setup.select_workfolder(request)

    async def create_project(self, request: CreateProjectRequest) -> ProjectSetupResult:
        return await self.setup.create_project(request)

    async def setup_project(self, name: str) -> ProjectSetupResult:
        return await self.setup.setup_project(name)

    async def get_app_settings(self) -> AppSettings:
        return await self.setup.get_app_settings()

    async def update_app_settings(self, settings: AppSettings) -> AppSettings:
        return await self.setup.update_app_settings(settings)

    async def get_workfolder_settings(self) -> WorkfolderSettingsInfo:
        return await self.setup.get_workfolder_settings()

    async def update_workfolder_settings(
        self, settings: WorkfolderSettingsInfo
    ) -> WorkfolderSettingsInfo:
        return await self.setup.update_workfolder_settings(settings)

    async def get_mandate(self, name: str = "standard") -> MandateInfo:
        return await self.setup.get_mandate(name)

    async def update_mandate(self, name: str, update: MandateUpdate) -> MandateInfo:
        return await self.setup.update_mandate(name, update)

    async def list_fixed_rules(self) -> list[FixedRule]:
        return await self.setup.list_fixed_rules()

    async def delete_fixed_rule(self, rule_id: str) -> list[FixedRule]:
        return await self.setup.delete_fixed_rule(rule_id)

    async def test_toolchain(self, python: str | None = None) -> ToolchainTestResult:
        return await self.setup.test_toolchain(python)
