"""`ApiClient`: the interface between the GUI and the service, plus live-stream plumbing.

The GUI talks only to an `ApiClient`. `arr.service.mock_api.MockApi` implements it on sample
data (W1-06), `RealApi` on top of the service (W3-01). Objects and errors are those of
`arr.service.api_models`. The counting rules of docs/08-ux-konzept.md §8.3 live here, so every
implementation counts the same way.
"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Iterable
from typing import Any, Protocol, runtime_checkable

from arr.models import Starter
from arr.permissions.types import ApprovalStatus, ApprovalType
from arr.service.api_models import (
    GLOBAL_EVENT_TYPES,
    ActionResult,
    AgentChanges,
    AgentState,
    AgentTranscriptEvent,
    ApprovalItem,
    AppSettings,
    ChannelInfo,
    CreateProfileRequest,
    CreateProjectRequest,
    DecisionRequest,
    DecisionResponse,
    FixedRule,
    GlobalCounts,
    LiveEvent,
    LogEntry,
    LoginStart,
    LoginStatus,
    MandateInfo,
    MandateUpdate,
    Message,
    MessagesPage,
    PostMessageRequest,
    ProfileActionResult,
    ProfileInfo,
    ProfileUsage,
    ProjectInfo,
    ProjectSetupResult,
    ResolveResult,
    RunAction,
    RunDetail,
    RunFilter,
    RunInputs,
    RunResults,
    RunsPage,
    RunSummary,
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

# --------------------------------------------------------------------------------------------
# Counting rules (docs/08 §8.3)
# --------------------------------------------------------------------------------------------


def count_waiting(approvals: Iterable[ApprovalItem]) -> int:
    """„Wartet auf dich“: open inbox items (run starts + permissions + escalations)."""
    return sum(1 for item in approvals if item.status is ApprovalStatus.OPEN)


def count_active(runs: Iterable[RunSummary]) -> int:
    """„Aktiv“: runs in `awaiting_approval`, `queued`, `preparing`, `running`, `paused`,
    `finalizing`."""
    return sum(1 for run in runs if run.active)


def count_runs_waiting(runs: Iterable[RunSummary]) -> int:
    """Runs filter „Wartet auf dich“: runs with at least one open inbox item.

    Differs from `count_waiting` when a run has several open items (docs/08 §8.3).
    """
    return sum(1 for run in runs if run.open_approvals > 0)


# --------------------------------------------------------------------------------------------
# Live stream plumbing
# --------------------------------------------------------------------------------------------


class Subscription:
    """A subscriber's bounded queue of live events (docs/spikes/S6: one hub, fan-out).

    Iterate with `async for`, or drain with `drain()` from a UI timer. `close()` ends the
    subscription; NiceGUI pages call it from `client.on_delete`.
    """

    def __init__(self, hub: "EventHub", run_id: str | None, agents: set[str] | None) -> None:
        self._hub = hub
        self.run_id = run_id
        self.agents = agents or set()
        self._queue: deque[LiveEvent] = deque(maxlen=2000)
        self._wakeup = asyncio.Event()
        self.closed = False

    def wants(self, event: LiveEvent) -> bool:
        """Return whether this subscriber receives the event (global vs. run stream)."""
        if self.run_id is None:
            return event.type in GLOBAL_EVENT_TYPES
        if event.run_id != self.run_id and event.type not in ("counts.changed",):
            return False
        if isinstance(event, AgentTranscriptEvent):
            return event.agent_id in self.agents
        return True

    def push(self, event: LiveEvent) -> None:
        """Queue an event; the oldest events are dropped when the queue is full."""
        self._queue.append(event)
        self._wakeup.set()

    def drain(self, limit: int = 500) -> list[LiveEvent]:
        """Take up to `limit` queued events without waiting."""
        out: list[LiveEvent] = []
        while self._queue and len(out) < limit:
            out.append(self._queue.popleft())
        if not self._queue:
            self._wakeup.clear()
        return out

    def close(self) -> None:
        """Stop receiving events."""
        self.closed = True
        self._hub.remove(self)
        self._wakeup.set()

    def __aiter__(self) -> AsyncIterator[LiveEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[LiveEvent]:
        while not self.closed:
            await self._wakeup.wait()
            for event in self.drain():
                yield event


class EventHub:
    """Fan-out of live events to all subscriptions (docs/spikes/S6 §5)."""

    def __init__(self) -> None:
        self._subs: set[Subscription] = set()
        self.seq = 0

    def add(self, run_id: str | None, agents: set[str] | None) -> Subscription:
        """Create a subscription for the global stream (`run_id=None`) or one run."""
        sub = Subscription(self, run_id, agents)
        self._subs.add(sub)
        return sub

    def remove(self, sub: Subscription) -> None:
        """Forget a subscription."""
        self._subs.discard(sub)

    def next_seq(self) -> int:
        """Next running number for an event."""
        self.seq += 1
        return self.seq

    def publish(self, event: LiveEvent) -> None:
        """Deliver an event to every subscriber that wants it."""
        for sub in list(self._subs):
            if sub.wants(event):
                sub.push(event)

    @property
    def subscriber_count(self) -> int:
        """Number of open subscriptions."""
        return len(self._subs)


# --------------------------------------------------------------------------------------------
# The client protocol
# --------------------------------------------------------------------------------------------


@runtime_checkable
class ApiClient(Protocol):
    """What the GUI needs from the service; `MockApi` now, `RealApi` in W3-01.

    One method per endpoint of `arr.service.api_models` (see the endpoint list there). All
    methods except `now` and `subscribe` are async. Expected failures raise `ApiException`
    with a German message (docs/08 §8.4).
    """

    def now(self) -> float:
        """Current time of the service as Unix seconds (the mock runs its own clock)."""
        ...

    def subscribe(self, run_id: str | None = None, agents: set[str] | None = None) -> Subscription:
        """Subscribe to the global live stream (`run_id=None`) or to one run."""
        ...

    async def get_workfolder(self) -> WorkfolderInfo:
        """Current workfolder (`GET /api/workfolder`)."""
        ...

    async def get_counts(self) -> GlobalCounts:
        """Global counters after docs/08 §8.3 (`GET /api/counts`)."""
        ...

    async def list_warnings(self) -> list[WarningInfo]:
        """Global warnings (`GET /api/warnings`)."""
        ...

    async def list_approvals(
        self,
        status: Iterable[ApprovalStatus] | None = None,
        run_id: str | None = None,
        type: ApprovalType | None = None,
        since: float | None = None,
    ) -> list[ApprovalItem]:
        """Inbox items; open ones sorted deadline first, then oldest; others newest first."""
        ...

    async def get_approval(self, approval_id: str) -> ApprovalItem:
        """One inbox item (`GET /api/approvals/{id}`)."""
        ...

    async def decide_approval(self, approval_id: str, request: DecisionRequest) -> DecisionResponse:
        """Decide an inbox item (`POST /api/approvals/{id}`, GUI only, E15)."""
        ...

    async def revoke_approval(self, approval_id: str) -> ApprovalItem:
        """Withdraw a decision (`POST /api/approvals/{id}/revoke`)."""
        ...

    async def list_runs(
        self,
        status: RunFilter = RunFilter.ALL,
        project: str | None = None,
        starter: Starter | None = None,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RunsPage:
        """One page of runs, active first (`GET /api/runs`)."""
        ...

    async def get_run(self, run_id: str) -> RunDetail:
        """Run detail head (`GET /api/runs/{id}`)."""
        ...

    async def run_action(
        self, run_id: str, action: RunAction, dry_run: bool = False
    ) -> ActionResult:
        """Pause, resume, stop, slim, archive, delete, pin or unpin a run."""
        ...

    async def list_channels(self, run_id: str) -> list[ChannelInfo]:
        """Channels of a run."""
        ...

    async def get_messages(
        self,
        run_id: str,
        channel: str,
        before_id: int | None = None,
        since_id: int | None = None,
        limit: int = 50,
    ) -> MessagesPage:
        """Messages of a channel, newest page by default, oldest first within the page."""
        ...

    async def post_message(self, run_id: str, channel: str, request: PostMessageRequest) -> Message:
        """Post a message as the user; the answer carries the delivery state."""
        ...

    async def list_agents(self, run_id: str) -> list[AgentState]:
        """Agent cards of a run."""
        ...

    async def get_transcript(
        self,
        run_id: str,
        agent_id: str,
        from_seq: int | None = None,
        limit: int = 200,
        kinds: Iterable[TranscriptKind] | None = None,
    ) -> TranscriptPage:
        """Transcript page; without `from_seq` the latest `limit` entries."""
        ...

    async def get_results(self, run_id: str) -> RunResults:
        """Results with ratings and summary."""
        ...

    async def get_log(self, run_id: str) -> list[LogEntry]:
        """Orchestrator log [S]."""
        ...

    async def get_changes(self, run_id: str) -> list[AgentChanges]:
        """File changes per agent [S]."""
        ...

    async def get_run_inputs(self, run_id: str) -> RunInputs:
        """Inputs of a run and the input that repeats it (`GET /api/runs/{id}/inputs`, W3-10)."""
        ...

    async def list_profiles(self) -> list[ProfileInfo]:
        """Auth profiles without secrets."""
        ...

    async def test_profile(self, name: str) -> ProfileInfo:
        """Test a profile and return it with `last_test`."""
        ...

    async def list_projects(self) -> list[ProjectInfo]:
        """Projects of the workfolder."""
        ...

    async def get_project(self, name: str) -> ProjectInfo:
        """One project."""
        ...

    async def list_skills(self, kind: SkillKind | None = None) -> list[SkillInfo]:
        """Skills of the workfolder."""
        ...

    async def get_skill(self, name: str) -> SkillInfo:
        """One skill."""
        ...

    async def get_usage(
        self,
        from_ts: float | None = None,
        to_ts: float | None = None,
        group_by: UsageGroupBy = UsageGroupBy.PROFILE,
        project: str | None = None,
    ) -> UsageReport:
        """Usage report."""
        ...

    async def get_storage(self) -> StorageReport:
        """Disk use of the runs with a suggestion."""
        ...

    async def resolve_run(self, run_input: dict[str, Any]) -> ResolveResult:
        """Resolve and check a run input without starting it."""
        ...

    async def start_run(self, run_input: dict[str, Any]) -> StartRunResponse:
        """Start a new run from the GUI."""
        ...

    async def get_setup_state(self) -> SetupState:
        """State for the setup assistant."""
        ...

    # --- settings, mandate, fixed rules (docs/08 §5.11) ------------------------------------

    async def get_app_settings(self) -> AppSettings:
        """App settings (`GET /api/settings/app`)."""
        ...

    async def update_app_settings(self, settings: AppSettings) -> AppSettings:
        """Save app settings (`PUT /api/settings/app`)."""
        ...

    async def get_workfolder_settings(self) -> WorkfolderSettingsInfo:
        """Settings of the workfolder, `arr.yaml` (`GET /api/settings/workfolder`)."""
        ...

    async def update_workfolder_settings(
        self, settings: WorkfolderSettingsInfo
    ) -> WorkfolderSettingsInfo:
        """Save workfolder settings (`PUT /api/settings/workfolder`)."""
        ...

    async def get_mandate(self, name: str = "standard") -> MandateInfo:
        """The mandate in plain text (`GET /api/mandates/{name}`)."""
        ...

    async def update_mandate(self, name: str, update: MandateUpdate) -> MandateInfo:
        """Change the mandate, effective at once in running runs (`PUT /api/mandates/{name}`)."""
        ...

    async def list_fixed_rules(self) -> list[FixedRule]:
        """Standard rules and own „immer“ rules (`GET /api/rules`)."""
        ...

    async def delete_fixed_rule(self, rule_id: str) -> list[FixedRule]:
        """Remove an own rule; standard rules raise `not_allowed` (`DELETE /api/rules/{id}`)."""
        ...

    async def test_toolchain(self, python: str | None = None) -> ToolchainTestResult:
        """Check the Python environment of the agents (`POST /api/toolchain/test`)."""
        ...

    # --- profiles (docs/08 §5.8) -----------------------------------------------------------

    async def create_profile(self, request: CreateProfileRequest) -> ProfileInfo:
        """Create a profile or, with `replace`, put a new key on it (`POST /api/profiles`).

        API keys go to the keychain. A replaced key leaves the profile as it is.
        """
        ...

    async def delete_profile(self, name: str) -> None:
        """Delete a profile and its secret (`DELETE /api/profiles/{name}`)."""
        ...

    async def disable_profile(self, name: str, dry_run: bool = False) -> ProfileActionResult:
        """Not-Aus: block the profile, stop its runs, end its logins (W3-06)."""
        ...

    async def enable_profile(self, name: str) -> ProfileActionResult:
        """Lift the block of a profile (W3-06)."""
        ...

    async def remove_login(self, name: str, dry_run: bool = False) -> ProfileActionResult:
        """„Login entfernen“: only what belongs to arr, on a blocked profile (W3-06)."""
        ...

    async def get_profile_usage(self, name: str) -> ProfileUsage:
        """„Nachweis je Profil“: runs, tokens and costs through arr (W3-06)."""
        ...

    async def start_login(self, name: str) -> LoginStart:
        """Start „Mit Claude/ChatGPT anmelden“ (`POST /api/profiles/{name}/login`, E18)."""
        ...

    async def get_login_status(self, name: str) -> LoginStatus:
        """Poll a running login (`GET` on `LoginStart.poll_url`)."""
        ...

    async def cancel_login(self, name: str) -> LoginStatus:
        """Cancel a running login (`DELETE /api/profiles/{name}/login`)."""
        ...

    # --- projects, skill files, workfolder (docs/08 §5.6, §5.7, §5.12) --------------------

    async def create_project(self, request: CreateProjectRequest) -> ProjectSetupResult:
        """Create a project or set up an existing folder (`POST /api/projects`)."""
        ...

    async def setup_project(self, name: str) -> ProjectSetupResult:
        """Repair the arr files of a project (`POST /api/projects/{name}/setup`)."""
        ...

    async def get_skill_file(self, name: str, path: str) -> SkillFile:
        """Read a file of a skill (`GET /api/skills/{name}/files/{path}`)."""
        ...

    async def select_workfolder(self, request: WorkfolderRequest) -> WorkfolderInfo:
        """Open or create a workfolder (`POST /api/workfolder`)."""
        ...
