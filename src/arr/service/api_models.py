"""API contract between the service and the GUI: response models, live events, error format.

The models follow the data needs of the UX concept (docs/08-ux-konzept.md §5.0, §5.1-§5.12,
§6.9). The GUI programs against them through `arr.service.mock_api.ApiClient`; the mock
(W1-06) and the real service (W3-01) both return exactly these models. They reuse the enums
of the core contract (W1-01: `arr.models`, `arr.permissions.types`, `arr.room.types`,
`arr.backends.base`) and add display enums only where the GUI shows more states than the core
stores (e.g. `AgentDisplayState`). Texts in the models (`title`, `message`, `label`, …) are
German and shown as is.

Units (all number fields, docs/08 §5.0 N12):

- Timestamps (`ts`, `*_at`, `deadline`, `since`): Unix seconds (float, UTC).
- Durations (`*_s`): seconds; `duration_ms`: milliseconds.
- Sizes (`*_bytes`, `size`, and the parts of `SizeInfo`/`StorageParts`): bytes (decimal
  units in the GUI, docs/08 §8.7).
- Money (`*_usd`): US dollars; `cost_kind` says billed or estimated (`≈`), `None` = unknown.
- Tokens: counts. Ratios (`utilization`, `cache_read_ratio`, `ratio`): 0.0-1.0.
- Scores: on the rubric scale of the run (usually 1-10).

Errors: every endpoint answers a failure with HTTP status + `ApiErrorResponse` (one format,
docs/08 §8.4 for the texts). In Python the client raises `ApiException` carrying an `ApiError`.

Endpoints (REST under `/api`, docs/04 §3 and docs/08 §5; `ApiClient` method in brackets):

    GET  /api/workfolder                         WorkfolderInfo          [get_workfolder]
    GET  /api/counts                             GlobalCounts            [get_counts]
    GET  /api/warnings                           list[WarningInfo]       [list_warnings]
    GET  /api/approvals?status=&run=&type=&since=  list[ApprovalItem]    [list_approvals]
    GET  /api/approvals/{id}                     ApprovalItem            [get_approval]
    POST /api/approvals/{id}                     DecisionRequest → DecisionResponse
                                                 (GUI only, E15)         [decide_approval]
    POST /api/approvals/{id}/revoke              ApprovalItem            [revoke_approval]
    GET  /api/runs?status=&project=&starter=&q=&sort=&cursor=&limit=
                                                 RunsPage                [list_runs]
    POST /api/runs                               StartRunRequest → StartRunResponse [start_run]
    POST /api/runs/resolve                       ResolveRequest → ResolveResult    [resolve_run]
    GET  /api/runs/{id}                          RunDetail               [get_run]
    POST /api/runs/{id}/{action}?dry_run=        ActionResult            [run_action]
         action: pause | resume | stop | slim | archive | delete | pin | unpin
         (resume with an open escalation → ApiError `not_allowed` with `gui_url`)
    GET  /api/runs/{id}/channels                 list[ChannelInfo]       [list_channels]
    GET  /api/runs/{id}/channels/{ch}?before_id=&since_id=&limit=
                                                 MessagesPage            [get_messages]
    POST /api/runs/{id}/channels/{ch}            PostMessageRequest → Message [post_message]
    GET  /api/runs/{id}/agents                   list[AgentState]        [list_agents]
    GET  /api/runs/{id}/agents/{aid}/transcript?from_seq=&limit=200&kinds=
                                                 TranscriptPage          [get_transcript]
    GET  /api/runs/{id}/results                  RunResults              [get_results]
    GET  /api/runs/{id}/log                      list[LogEntry]          [get_log]       [S]
    GET  /api/runs/{id}/changes                  list[AgentChanges]      [get_changes]   [S]
    GET  /api/projects[/{name}]                  list[ProjectInfo] | ProjectInfo
                                                 [list_projects, get_project]
    GET  /api/skills?kind=  /api/skills/{name}   list[SkillInfo] | SkillInfo
                                                 [list_skills, get_skill]
    GET  /api/profiles                           list[ProfileInfo]       [list_profiles]
    POST /api/profiles/{name}/test               ProfileInfo             [test_profile]
    GET  /api/usage?from=&to=&group_by=&project= UsageReport             [get_usage]
    GET  /api/storage                            StorageReport           [get_storage]
    GET  /api/setup/state                        SetupState              [get_setup_state]
    POST /api/workfolder                         WorkfolderRequest → WorkfolderInfo
                                                 [select_workfolder]
    GET/PUT /api/settings/app                    AppSettings   [get_/update_app_settings]
    GET/PUT /api/settings/workfolder             WorkfolderSettingsInfo
                                                 [get_/update_workfolder_settings]
    GET/PUT /api/mandates/{name}                 MandateInfo, PUT with MandateUpdate
                                                 [get_mandate, update_mandate]
    GET  /api/rules · DELETE /api/rules/{id}     list[FixedRule]
                                                 [list_fixed_rules, delete_fixed_rule]
    POST /api/toolchain/test                     ToolchainTestResult     [test_toolchain]
    POST /api/profiles                           CreateProfileRequest → ProfileInfo
                                                 [create_profile]
    DELETE /api/profiles/{name}                  –                       [delete_profile]
    POST /api/profiles/{name}/login              LoginStart              [start_login]
    GET  <LoginStart.poll_url> · DELETE /api/profiles/{name}/login
                                                 LoginStatus  [get_login_status, cancel_login]
    POST /api/projects                           CreateProjectRequest → ProjectSetupResult
                                                 [create_project]
    POST /api/projects/{name}/setup              ProjectSetupResult      [setup_project]
    GET  /api/skills/{name}/files/{path}         SkillFile               [get_skill_file]
    GET  (SSE) /api/events                       LiveEvent stream, global: counts.changed,
                                                 warnings.changed, run.status, approval.*,
                                                 usage.tick
                                                 [subscribe(None)]
    GET  (SSE) /api/runs/{id}/events?agents=     LiveEvent stream of one run (all kinds;
                                                 agent.event only for the listed agents)
                                                 [subscribe(run_id, agents=…)]

The GUI receives live events in-process (NiceGUI websocket, docs/spikes/S6); SSE is for the
CLI and external clients. `ui_seen` („gelesen bis“) lives in the browser (`UiSeen`).
"""

from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, model_validator

from arr.backends.base import CostKind
from arr.errors import ArrError
from arr.models import (
    AuthKind,
    ChannelMode,
    Decider,
    Effort,
    Provider,
    RunStatus,
    RunVenvMode,
    Starter,
    TopologyKind,
    WorkspaceMode,
)
from arr.permissions.types import (
    ApprovalStatus,
    ApprovalType,
    DecidedBy,
    DecisionAction,
    DecisionScope,
    EscalationOption,
    RequestCategory,
    RiskLevel,
)
from arr.room.types import DeliveryStatus, MessageKind, MessageVia, VoteChoice, VoteRule


class ApiModel(BaseModel):
    """Base of all API models: unknown fields are errors, so mock and service stay in sync.

    See docs/08-ux-konzept.md §5.0.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, hide_input_in_errors=True)


# --------------------------------------------------------------------------------------------
# Enums that only the API needs (closed lists, docs/08 §5.0 N12)
# --------------------------------------------------------------------------------------------


class Vendor(StrEnum):
    """Company that receives an agent's context (E19); see `Provider.vendor` (docs/08 §5.2)."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class AgentDisplayState(StrEnum):
    """State of an agent as the GUI shows it (docs/08 §5.0 `AgentState.state`, §8.3).

    `waiting_user`: `blocked` and the request lies with the user; `error`: the agent failed;
    `not_started`: the run failed before the agent's first turn.
    """

    WORKING = "working"
    IDLE = "idle"
    BLOCKED = "blocked"
    WAITING_USER = "waiting_user"
    DONE = "done"
    ERROR = "error"
    NOT_STARTED = "not_started"


class StageStatus(StrEnum):
    """State of a stage in the progress bar (docs/08 §5.0 `StageInfo`, K9)."""

    TODO = "todo"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    WAITING_USER = "waiting_user"


class ActivityKind(StrEnum):
    """Kind of an agent's current activity, the „jetzt“ line (docs/08 §5.0, G1, K3)."""

    TOOL = "tool"
    TEXT = "text"
    THINKING = "thinking"
    WAIT_MESSAGES = "wait_messages"
    WAIT_STAGE = "wait_stage"
    WAIT_APPROVAL = "wait_approval"
    BLOCKED = "blocked"
    DONE = "done"
    ERROR = "error"


class BlockedKind(StrEnum):
    """What an agent is blocked by (docs/08 §5.0 `AgentState.blocked_by.kind`)."""

    APPROVAL = "approval"
    GATEKEEPER = "gatekeeper"
    RATE_LIMIT = "rate_limit"
    CAPACITY = "capacity"


class TranscriptKind(StrEnum):
    """Kind of a transcript entry as served to the GUI (docs/08 §5.0 `TranscriptEvent`).

    Same as `arr.backends.base.EventKind`, but `tool_call` + `tool_result` are bundled into
    one entry `tool`.
    """

    TURN_START = "turn_start"
    TEXT = "text"
    THINKING = "thinking"
    TOOL = "tool"
    HINT = "hint"
    USAGE = "usage"
    RATE_LIMIT = "rate_limit"
    ERROR = "error"
    TURN_END = "turn_end"


class ToolStatus(StrEnum):
    """State of a bundled tool call (docs/08 §5.0 `TranscriptEvent.tool.status`, K6)."""

    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    WAITING_APPROVAL = "waiting_approval"


class ResultKind(StrEnum):
    """Kind of a submitted result (docs/08 §5.0 N12). Unknown core kinds map to `other`."""

    IDEA = "idea"
    EXPLORATION = "exploration"
    STRATEGY = "strategy"
    SKETCH = "sketch"
    PROOF = "proof"
    REVIEW = "review"
    VERDICT = "verdict"
    SUMMARY = "summary"
    OTHER = "other"


class ResultStatus(StrEnum):
    """Rating outcome of a result (docs/08 §5.0 `Result.status`, §8.3 words)."""

    ACCEPTED = "accepted"
    REWORK = "rework"
    FAILED = "failed"
    UNRATED = "unrated"


class WarningKind(StrEnum):
    """Kind of a global warning (docs/08 §4.2 „Warnungen“)."""

    STORAGE_LOW = "storage_low"
    USAGE_LIMIT = "usage_limit"
    PROFILE_ERROR = "profile_error"
    RUNS_INTERRUPTED = "runs_interrupted"


class Severity(StrEnum):
    """Severity of warnings, checks and skill issues (docs/08 §4.2, §5.6, §5.7)."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LimitKind(StrEnum):
    """Limit a `limit.warning` event refers to (docs/08 §5.0, K10)."""

    WALL_TIME = "wall_time"
    BUDGET = "budget"
    TURNS = "turns"


class RunAction(StrEnum):
    """Actions on a run (docs/04 §3, docs/08 §5.3, §6.9)."""

    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    SLIM = "slim"
    ARCHIVE = "archive"
    DELETE = "delete"
    PIN = "pin"
    UNPIN = "unpin"


class RunFilter(StrEnum):
    """Status segment of the runs list with its counters (docs/08 §5.3, §8.3)."""

    ALL = "all"
    ACTIVE = "active"
    WAITING = "waiting"
    DONE = "done"
    ERROR = "error"
    ARCHIVED = "archived"


class SkillKind(StrEnum):
    """Kind of a skill (docs/03 §1, docs/08 §5.7): group (startable), tool, user agent."""

    GROUP = "group"
    TOOL = "tool"
    USER_AGENT = "user_agent"


class UsageGroupBy(StrEnum):
    """Grouping of the usage report (docs/08 §5.9)."""

    PROFILE = "profile"
    MODEL = "model"
    PROJECT = "project"
    RUN = "run"
    AGENT = "agent"


class ErrorCode(StrEnum):
    """Machine-readable code of an `ApiError` (docs/08 §8.4); HTTP status in brackets.

    `invalid` (400) wrong input · `not_found` (404) · `conflict` (409) state changed, e.g.
    already decided in another tab · `not_allowed` (409) action not possible now, e.g.
    resume with open escalation · `config_invalid` (422) configuration errors with `issues` ·
    `unavailable` (503) service or index not readable · `start_failed` (410) the preparation
    failed and the run is gone, the message says why · `internal` (500).
    """

    INVALID = "invalid"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    NOT_ALLOWED = "not_allowed"
    CONFIG_INVALID = "config_invalid"
    UNAVAILABLE = "unavailable"
    START_FAILED = "start_failed"
    INTERNAL = "internal"


# --------------------------------------------------------------------------------------------
# Shared small models
# --------------------------------------------------------------------------------------------


class Action(ApiModel):
    """A follow-up action with a German label and a GUI URL (docs/08 §8.4 „[Aktion]“)."""

    label: str
    url: str


class FieldIssue(ApiModel):
    """A problem at one field of a request, e.g. `limits.budget_usd` (docs/08 §5.4)."""

    path: str
    field: str | None = None
    message: str
    action: Action | None = None


class ApiError(ApiModel):
    """Uniform error of all endpoints (docs/08 §8.4).

    `message` follows the pattern „what happened → why → what you can do“, German, without
    commands to type (E18). `detail` holds technical details shown behind „Details“.
    """

    code: ErrorCode
    message: str
    detail: str | None = None
    field: str | None = None
    action: Action | None = None
    gui_url: str | None = None
    issues: list[FieldIssue] = Field(default_factory=list)


class ApiErrorResponse(ApiModel):
    """HTTP body of every error response: `{"error": ApiError}` (docs/08 §8.4)."""

    error: ApiError


class ApiException(ArrError):
    """Raised by an `ApiClient` method for an expected failure; carries the `ApiError`.

    The GUI shows `error.message` as banner or toast (docs/08 §8.4).
    """

    def __init__(self, error: ApiError) -> None:
        super().__init__(error.message)
        self.error = error

    @classmethod
    def of(cls, code: ErrorCode, message: str, **fields: Any) -> "ApiException":
        """Build the exception from a code, a German message and optional `ApiError` fields."""
        return cls(ApiError(code=code, message=message, **fields))


class ChangeInfo(ApiModel):
    """One change made with „Anpassen“, e.g. `judges: 3 → 2` (docs/08 §5.2, docs/04 §2)."""

    path: str
    from_: Any = Field(default=None, alias="from")
    to: Any = None
    label: str | None = Field(default=None, description="German, e.g. „Web judge-1: an → aus“")


class Range(ApiModel):
    """A range `min`-`max` for estimates (docs/08 §5.0 `estimate`)."""

    min: float
    max: float

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min > self.max:
            raise ValueError("Range: min > max")
        return self


class ToolAccess(ApiModel):
    """Optional tools of an agent (docs/08 §5.0 `tools {shell, web}`)."""

    shell: bool = True
    web: bool = False


# --------------------------------------------------------------------------------------------
# Workfolder, counts, warnings
# --------------------------------------------------------------------------------------------


class WorkfolderRef(ApiModel):
    """A known workfolder for „Workfolder wechseln“ (docs/08 §5.1)."""

    name: str
    path: str
    projects_count: int = Field(ge=0)
    active_runs: int | None = Field(default=None, ge=0)
    """Active runs from the run data; only the open workfolder knows them, else `None`."""


class WorkfolderInfo(ApiModel):
    """Current workfolder for the start page head and the navigation (docs/08 §5.1, §4.1)."""

    name: str
    path: str
    projects_count: int = Field(ge=0)
    runs_count: int = Field(ge=0)
    disk_free_bytes: int = Field(ge=0)
    apfs: bool | None = None
    known: list[WorkfolderRef] = Field(default_factory=list)
    service_url: str = Field(description="e.g. http://127.0.0.1:8765")


class GlobalCounts(ApiModel):
    """The counters of docs/08 §8.3, the same everywhere (badge, chips, start, tab title).

    `waiting`: open inbox items (run starts + permissions + escalations); `active`: runs in an
    active status; `warnings`: number of global warnings.
    """

    waiting: int = Field(ge=0)
    active: int = Field(ge=0)
    warnings: int = Field(ge=0)


class WarningInfo(ApiModel):
    """A global warning with an action (docs/08 §4.2, §5.1 `Warning`)."""

    id: str
    kind: WarningKind
    severity: Severity = Severity.WARNING
    title: str
    text: str
    action: Action


# --------------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------------


class StageRef(ApiModel):
    """Current stage of a run in lists: `{index, count, label, detail}` (docs/08 §5.0)."""

    index: int = Field(ge=0)
    count: int = Field(ge=0)
    label: str
    detail: str | None = None


class AgentsByState(ApiModel):
    """Number of agents per display state (docs/08 §5.0 `agents_by_state`)."""

    working: int = 0
    idle: int = 0
    blocked: int = 0
    waiting_user: int = 0
    done: int = 0
    error: int = 0
    not_started: int = 0

    @property
    def total(self) -> int:
        """Number of agents."""
        return self.working + self.idle + self.blocked + self.waiting_user + self.done + self.error


class RunLimits(ApiModel):
    """Limits of a run with usage so far (docs/08 §5.0 `limits`, G1 „x von y“)."""

    wall_time_s: int | None = None
    elapsed_s: int = 0
    budget_usd: float | None = None
    spent_usd: float | None = None
    turns_per_agent: int | None = None
    max_turns_used: int = 0


class SizeInfo(ApiModel):
    """Disk use of a run in bytes, actual (with clones) and logical (docs/08 §5.0 `size`)."""

    actual_bytes: int = Field(ge=0)
    logical_bytes: int = Field(ge=0)
    work: int = Field(default=0, ge=0)
    logs: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)
    archive: int = Field(default=0, ge=0)


class BestResult(ApiModel):
    """Best rated result of a finished run (docs/08 §5.0 `best_result`)."""

    title: str
    score: float | None = None
    pass_score: float | None = None


class LastMessage(ApiModel):
    """Latest channel message of a run for list cards (docs/08 §5.0 `last_message`)."""

    channel: str
    sender: str
    excerpt: str
    ts: float
    unread_for_user: int = Field(default=0, ge=0)


class RunError(ApiModel):
    """Why a run failed or was interrupted, with the next step (docs/08 §5.0, §8.4)."""

    message: str
    action: Action | None = None


class RunSummary(ApiModel):
    """A run in lists: start page, runs list, projects, storage (docs/08 §5.0 `RunSummary`).

    `active` follows docs/08 §8.3. `open_approvals` counts the run's open inbox items.
    `open_escalation` marks a run paused by an escalation (shown as „Wartet auf dich“, §7.1).
    `info` is the German short line of the list („Exploration (1 von 3) · 5 Agenten“).
    """

    id: str
    name: str
    project: str
    skill: str | None = None
    topology: TopologyKind
    status: RunStatus
    status_since: float | None = None
    status_detail: str | None = Field(default=None, description="e.g. queue reason")
    active: bool
    open_approvals: int = Field(default=0, ge=0)
    open_escalation: bool = False
    starter: Starter
    created_at: float
    started_at: float | None = None
    ended_at: float | None = None
    duration_s: int | None = None
    stage: StageRef | None = None
    agents_by_state: AgentsByState = Field(default_factory=AgentsByState)
    limits: RunLimits = Field(default_factory=RunLimits)
    tokens_total: int | None = None
    cost_usd: float | None = None
    cost_kind: CostKind | None = None
    size: SizeInfo
    pinned: bool = False
    slimmed: bool = False
    archived: bool = False
    best_result: BestResult | None = None
    last_message: LastMessage | None = None
    error: RunError | None = None
    info: str | None = None
    adjusted_by_user: bool = False
    changes: list[ChangeInfo] = Field(default_factory=list)
    gui_url: str

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        # a reservation nobody took stays `queued` but is not active (W3-01 review G5)
        if self.active != self.status.is_active and self.status is not RunStatus.QUEUED:
            raise ValueError(f"active={self.active} passt nicht zu status={self.status}")
        if self.open_escalation and self.status is not RunStatus.PAUSED:
            raise ValueError("open_escalation nur bei pausierten Runs")
        return self


class RunCounts(ApiModel):
    """Counters of the runs status segment (docs/08 §5.3, §8.3)."""

    all: int = Field(ge=0)
    active: int = Field(ge=0)
    waiting: int = Field(ge=0, description="Runs with at least one open inbox item")
    done: int = Field(ge=0)
    error: int = Field(ge=0, description="failed + interrupted")
    archived: int = Field(ge=0)


class RunsPage(ApiModel):
    """One page of the runs list with counters and cursor (docs/08 §5.3).

    `total` is the number of runs matching the filter; `next_cursor` is `None` on the last
    page. Active runs always come first, finished ones by `ended_at` descending.
    """

    items: list[RunSummary]
    counts: RunCounts
    total: int = Field(ge=0)
    next_cursor: str | None = None
    projects: list[str] = Field(default_factory=list, description="For the project filter")


class Attempt(ApiModel):
    """One attempt of a stage with its average score (docs/08 §5.0 `attempts[]`).

    `passed` is `None` while the attempt runs or is unrated.
    """

    n: int = Field(ge=1)
    avg_score: float | None = None
    pass_score: float | None = None
    passed: bool | None = None


class StageInfo(ApiModel):
    """A stage or round in the progress bar, including planned ones (docs/08 §5.0, K9)."""

    key: str
    index: int = Field(ge=0)
    label: str
    status: StageStatus
    agents: list[str] = Field(default_factory=list)
    detail: str | None = None
    attempts: list[Attempt] = Field(default_factory=list)
    round: int | None = None


class ApprovalTop(ApiModel):
    """Most urgent open inbox item of a run for the banner in the run head (docs/08 §6.5)."""

    id: str
    summary: str
    risk: "RiskInfo | None" = None
    deadline: float | None = None
    more: int = Field(default=0, ge=0, description="„und n weitere“")
    gui_url: str


class RunDetail(ApiModel):
    """Head of the run detail: summary, banner, stages, allowed actions (docs/08 §6.9)."""

    run: RunSummary
    open_approvals_top: ApprovalTop | None = None
    questions_to_user: int = Field(default=0, ge=0, description="Does not count as waiting")
    decider: Decider
    actions_allowed: list[RunAction] = Field(default_factory=list)
    stages: list[StageInfo] = Field(default_factory=list)
    handoff_text: str = Field(description="„Für Claude Code kopieren“")
    run_dir: str
    task_md: str | None = None


class LogEntry(ApiModel):
    """An entry of `orchestrator.jsonl` for the view „Ablauf“ [S] (docs/08 §6.5)."""

    ts: float
    kind: str
    text: str
    stage: str | None = None


class FileChange(ApiModel):
    """A changed file of an agent compared with the project [S] (docs/08 §6.9)."""

    path: str
    added: int = Field(ge=0)
    removed: int = Field(ge=0)


class AgentChanges(ApiModel):
    """Changes of one agent for the view „Änderungen“ [S] (docs/08 §6.9)."""

    agent: str
    files: list[FileChange] = Field(default_factory=list)


class ActionResult(ApiModel):
    """Answer to a run action; `message` is the German toast text (docs/08 §4.2, §6.9).

    With `dry_run=True` nothing changes; `message` then describes what would happen and
    `freed_bytes` what would be freed.
    """

    ok: bool = True
    message: str
    run: RunSummary | None = None
    freed_bytes: int | None = None


# --------------------------------------------------------------------------------------------
# Agents, channels, messages, transcript, results
# --------------------------------------------------------------------------------------------


class BlockedBy(ApiModel):
    """What a blocked agent waits for (docs/08 §5.0 `blocked_by`)."""

    kind: BlockedKind
    approval_id: str | None = None
    summary: str
    deadline: float | None = None


class Activity(ApiModel):
    """Last activity of an agent, the „jetzt“ line (docs/08 §5.0 `activity`, G1).

    `label` e.g. „Bash“, `arg` e.g. „python stoppzeiten.py --bis 10**8“; `running` while the
    tool runs (the GUI then shows the duration since `since`).
    """

    kind: ActivityKind
    label: str
    arg: str | None = None
    since: float | None = None
    running: bool = False


class AgentState(ApiModel):
    """An agent card in the run detail (docs/08 §5.0 `AgentState`, K3)."""

    id: str
    role_label: str
    role_file: str | None = None
    provider: Provider
    vendor: Vendor
    model: str
    model_label: str = Field(description="e.g. „Claude Opus 5.5“")
    auth_profile: str
    effort: Effort | None = None
    skills: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    tools: ToolAccess = Field(default_factory=ToolAccess)
    workspace_mode: WorkspaceMode = WorkspaceMode.COPY
    read_dirs: list[str] = Field(default_factory=list)
    assignment_label: str | None = None
    state: AgentDisplayState
    state_since: float | None = None
    blocked_by: BlockedBy | None = None
    activity: Activity | None = None
    turn: int = Field(default=0, ge=0)
    turns_limit: int | None = None
    wakeups: int = Field(default=0, ge=0)
    max_wakeups: int | None = None
    cost_usd: float | None = None
    cost_kind: CostKind | None = None
    tokens: int | None = None
    cache_read_ratio: float | None = Field(default=None, ge=0, le=1)
    open_questions_to: int = Field(default=0, ge=0)
    questions_to_user: int = Field(default=0, ge=0)
    results_count: int = Field(default=0, ge=0)
    ratings_count: int = Field(default=0, ge=0)
    work_dir: str

    @model_validator(mode="after")
    def _blocked_needs_reason(self) -> Self:
        blocking = (AgentDisplayState.BLOCKED, AgentDisplayState.WAITING_USER)
        if (self.state in blocking) != (self.blocked_by is not None):
            raise ValueError("blocked_by gehört genau zu den Zuständen blocked/waiting_user")
        return self


class ChannelMemberInfo(ApiModel):
    """Member of a channel; in anonymous channels the user sees alias and ID (docs/08 §6.8)."""

    agent: str
    alias: str | None = None
    role_label: str | None = None


class VoteSummary(ApiModel):
    """State of a vote on a proposal in a moderated channel (docs/06 §7, docs/08 N12)."""

    proposal_id: int
    rule: VoteRule
    open: bool
    yes: int = Field(default=0, ge=0)
    no: int = Field(default=0, ge=0)
    abstain: int = Field(default=0, ge=0)
    result: str | None = Field(default=None, description="German, e.g. „angenommen (2 : 1)“")


class ChannelInfo(ApiModel):
    """A channel in the run detail (docs/08 §5.0 `ChannelInfo`).

    `unread_for_user` is computed by the service only as a hint; the GUI counts „n neu“ itself
    from `ui_seen` in the browser. `opens_at_stage` is the stage label for `open_from`.
    """

    name: str
    purpose: str | None = None
    members: list[ChannelMemberInfo] = Field(default_factory=list)
    mode: ChannelMode
    moderator: str | None = None
    open: bool = True
    opens_at_stage: str | None = None
    anonymous: bool = False
    message_count: int = Field(default=0, ge=0)
    last_message_id: int | None = None
    unread_for_user: int = Field(default=0, ge=0)
    open_questions: int = Field(default=0, ge=0)
    pinned: list[int] = Field(default_factory=list, description="IDs of pinned messages")
    summary_next_at: int | None = Field(default=None, description="Message count of next „Stand“")
    votes: list[VoteSummary] = Field(default_factory=list)


class AttachmentInfo(ApiModel):
    """File attached to a message (docs/08 §5.0 `refs[]`); `path` relative to the run folder."""

    name: str
    size: int = Field(ge=0)
    path: str


class DeliveryInfo(ApiModel):
    """Delivery state of a message from the user (docs/08 §5.0 `delivery`, §6.5)."""

    status: DeliveryStatus
    ts: float


class Message(ApiModel):
    """A channel message (docs/08 §5.0 `Message`, K4).

    `sender` is an agent ID, `orchestrator` or `user`; `via` and `delivery` only for `user`.
    `vote` is the choice of a `vote` message.
    """

    id: int = Field(ge=1)
    ts: float
    channel: str
    sender: str
    sender_alias: str | None = None
    via: MessageVia | None = None
    kind: MessageKind = MessageKind.NOTE
    body: str
    to: list[str] = Field(default_factory=list)
    reply_to: int | None = None
    reply_count: int = Field(default=0, ge=0)
    refs: list[AttachmentInfo] = Field(default_factory=list)
    pinned: bool = False
    open: bool = False
    delivery: DeliveryInfo | None = None
    vote: VoteChoice | None = None

    @model_validator(mode="after")
    def _user_fields(self) -> Self:
        if self.sender != "user" and (self.via is not None or self.delivery is not None):
            raise ValueError("via und delivery gibt es nur bei Nachrichten von user")
        return self


class MessagesPage(ApiModel):
    """Messages of a channel, oldest first (docs/08 §6.9; paging with `before_id`/`since_id`).

    `older_count` is the number of older messages that are not loaded („▲ 9 870 ältere“).
    """

    channel: str
    items: list[Message]
    total: int = Field(ge=0)
    older_count: int = Field(ge=0)


class PostMessageRequest(ApiModel):
    """Message from the user into a channel (docs/08 §6.9 „Senden“)."""

    body: str = Field(min_length=1)
    kind: MessageKind = MessageKind.NOTE
    to: list[str] = Field(default_factory=list)


class ToolBundle(ApiModel):
    """`tool_call` + `tool_result` as one transcript entry (docs/08 §5.0, K6).

    `output_excerpt` has at most 10 lines; `output_lines` counts the full output; outputs over
    1 MB live in `blob_path` (relative to the run folder).
    """

    id: str
    name: str
    input_summary: str
    input: dict[str, Any] = Field(default_factory=dict)
    status: ToolStatus
    duration_ms: int | None = None
    output_excerpt: str | None = None
    output_lines: int | None = None
    blob_path: str | None = None
    approval_id: str | None = None


class TranscriptEvent(ApiModel):
    """One entry of an agent transcript (docs/08 §5.0 `TranscriptEvent`, docs/02 §6).

    `data` has the shape of the core event data (`arr.backends.base.*Data`) for all kinds
    except `tool`, which uses `tool`.
    """

    seq: int = Field(ge=0)
    ts: float
    turn: int = Field(ge=0)
    kind: TranscriptKind
    data: dict[str, Any] = Field(default_factory=dict)
    tool: ToolBundle | None = None

    @model_validator(mode="after")
    def _tool_only_for_tool(self) -> Self:
        if (self.kind is TranscriptKind.TOOL) != (self.tool is not None):
            raise ValueError("tool gehört genau zu kind=tool")
        return self


class TurnRange(ApiModel):
    """Summary of collapsed older turns („Turn 1-5 · 186 Ereignisse · 11 Tool-Calls“, G5)."""

    turn_from: int = Field(ge=1)
    turn_to: int = Field(ge=1)
    start_ts: float
    end_ts: float
    events: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    messages_posted: int = Field(ge=0)


class TranscriptPage(ApiModel):
    """A page of a transcript, oldest first (docs/08 §6.9, at most 200 entries).

    `total` counts all entries of the agent; `next_from_seq` continues forwards, `older` sums
    up what lies before `items`.
    """

    agent_id: str
    items: list[TranscriptEvent]
    total: int = Field(ge=0)
    next_from_seq: int | None = None
    older: TurnRange | None = None


class RatingInfo(ApiModel):
    """A rating of a result (docs/08 §5.0 `ratings[]`)."""

    rater: str
    score: float
    criteria: dict[str, float] = Field(default_factory=dict)
    comment: str = ""


class Result(ApiModel):
    """A submitted result with ratings (docs/08 §5.0 `Result`)."""

    id: int = Field(ge=1)
    ts: float
    agent: str
    stage: str | None = None
    stage_label: str | None = None
    kind: ResultKind
    title: str
    body_excerpt: str | None = None
    path: str | None = None
    attempt: int = Field(default=1, ge=1)
    ratings: list[RatingInfo] = Field(default_factory=list)
    avg_score: float | None = None
    pass_score: float | None = None
    status: ResultStatus
    transcript_seq: int | None = None


class RunResults(ApiModel):
    """Results and summary of a run for the view „Ergebnisse“ (docs/08 §6.9)."""

    items: list[Result]
    summary_md: str | None = None
    summary_path: str | None = None
    empty_text: str | None = Field(default=None, description="e.g. „Jeder Explorer gibt …“")


# --------------------------------------------------------------------------------------------
# Approvals (inbox)
# --------------------------------------------------------------------------------------------


class RiskInfo(ApiModel):
    """Risk class with a German sentence (docs/08 §5.0 `risk`)."""

    level: RiskLevel
    text: str


class ApprovalRunRef(ApiModel):
    """Run of an inbox item (docs/08 §5.0 `run {id, name, project}`)."""

    id: str
    name: str
    project: str
    gui_url: str


class ApprovalAgentRef(ApiModel):
    """Requesting agent of a permission (docs/08 §5.0 `agent {id, role_label, model}`)."""

    id: str
    role_label: str
    model: str
    model_label: str
    provider: Provider
    vendor: Vendor


class EscalatedByInfo(ApiModel):
    """Who passed the request on to the user, and why (docs/08 §5.0 `escalated_by`)."""

    decider: DecidedBy
    note: str


class Consequences(ApiModel):
    """What happens on allow, deny and when the deadline passes (docs/08 §5.0, §2.3 Q4)."""

    allow: str
    deny: str
    expire: str


class RunStartAgent(ApiModel):
    """An agent of a requested run (docs/08 §5.0 `agents[]` of `run_start`).

    `skills` are the research skills the agent gets (column „Skills“ of „Neuer Run“, W1-08 §4).
    """

    id: str
    role_label: str
    provider: Provider
    vendor: Vendor
    model: str
    model_label: str
    profile: str
    tools: ToolAccess = Field(default_factory=ToolAccess)
    read_dirs: list[str] = Field(default_factory=list)
    workspace_mode: WorkspaceMode = WorkspaceMode.COPY
    skills: list[str] = Field(default_factory=list)


class RunStartChannel(ApiModel):
    """A channel of a requested run (docs/08 §5.0 `channels[]` of `run_start`)."""

    name: str
    members: list[str]
    mode: ChannelMode
    anonymous: bool = False
    open_from: str | None = Field(default=None, description="Stage label, e.g. „Diskussion“")


class LimitsInfo(ApiModel):
    """Limits of a requested run (docs/08 §5.0 `limits`)."""

    wall_time_s: int | None = None
    turns_per_agent: int | None = None
    budget_usd: float | None = None


class Estimate(ApiModel):
    """Estimate of a run as ranges (docs/08 §5.0 `estimate {turns, cost_usd, duration_s}`)."""

    turns: Range
    cost_usd: Range
    cost_kind: CostKind | None = None
    duration_s: Range


class CapacityInfo(ApiModel):
    """Free places per auth profile versus what the run needs (docs/08 §5.0 `capacity[]`)."""

    profile: str
    free: int = Field(ge=0)
    needed: int = Field(ge=0)
    limit: int = Field(ge=0)


class ProviderShare(ApiModel):
    """Which vendor receives content, and for which agents (E19, docs/08 §5.0 `providers[]`)."""

    vendor: Vendor
    agents: list[str]


class Deviation(ApiModel):
    """A deviation of a requested run from the user's standards (docs/08 §5.0 `deviations[]`).

    `sensitive` = extends rights (web, read folders, `shared`, weaker decider, other profile).
    Limits above standard are listed with `sensitive: false`. `label`, `value_text` and
    `default_text` are the German line of the card („Web-Zugriff an“ · „für judge-1 …“ ·
    „aus“). `providers` is set for read folders and working copies (E19).
    """

    path: str
    label: str
    value_text: str
    default_text: str
    default: Any = None
    value: Any = None
    sensitive: bool
    providers: list[ProviderShare] = Field(default_factory=list)


class LastRating(ApiModel):
    """A recent rater comment on an escalation card (docs/08 §5.0 `last_ratings[]`)."""

    rater: str
    score: float
    comment: str


class EscalationOptionInfo(ApiModel):
    """An option of an escalation with German label and help (docs/08 §5.0 `options[]`)."""

    key: EscalationOption
    label: str
    help: str


class DecisionInfo(ApiModel):
    """A decision in the history (docs/08 §5.0 `decision`).

    `rationale` is the reason of the gatekeeper or the rule; `comment` the user's note.
    """

    action: DecisionAction
    scope: DecisionScope = DecisionScope.ONCE
    option_key: EscalationOption | None = None
    changes: list[ChangeInfo] = Field(default_factory=list)
    comment: str | None = None
    decided_by: DecidedBy
    decided_at: float
    rationale: str | None = None


_TYPE_ACTIONS = {
    ApprovalType.RUN_START: {DecisionAction.ALLOW, DecisionAction.DENY, DecisionAction.ADJUST},
    ApprovalType.PERMISSION: {DecisionAction.ALLOW, DecisionAction.DENY},
    ApprovalType.ESCALATION: {DecisionAction.OPTION},
}


class ApprovalItem(ApiModel):
    """An entry of the approval inbox, complete per type (docs/08 §5.0 `ApprovalItem`, K5).

    Fields are grouped by type: permission (`agent` … `consequences`), run start (`skill` …
    `standard_notes`), escalation (`stage` … `options`). `title` is the German sentence of
    the card. The same rules as the core `arr.permissions.types.ApprovalItem` apply: run
    starts have no deadline, run starts and escalations are decided only by the user once.
    """

    id: str
    type: ApprovalType
    status: ApprovalStatus = ApprovalStatus.OPEN
    created_at: float
    gui_url: str
    title: str
    run: ApprovalRunRef
    deadline: float | None = None
    # permission
    agent: ApprovalAgentRef | None = None
    tool: str | None = None
    command: str | None = None
    path: str | None = None
    url: str | None = None
    work_dir: str | None = None
    reason: str | None = None
    risk: RiskInfo | None = None
    categories: list[RequestCategory] = Field(default_factory=list)
    escalated_by: EscalatedByInfo | None = None
    consequences: Consequences | None = None
    content_to: list[Vendor] = Field(default_factory=list, description="read_outside (E19)")
    rule_label: str | None = Field(
        default=None, description="Rule that „immer“ would create; None: „immer“ is not offered"
    )
    rule_effects: list[str] = Field(
        default_factory=list, description="What that rule allows, for the „immer“ dialog"
    )
    also_hosts: list[str] = Field(
        default_factory=list, description="Package hosts an allowed installation may reach too"
    )
    transcript_seq: int | None = None
    why: str | None = Field(default=None, description="„Warum bei dir“ without `escalated_by`")
    no_sandbox: bool = Field(
        default=False, description="Codex escalation: leaves the sandbox, scope only `once`"
    )
    # run start
    requested_by: Starter | None = None
    skill: str | None = None
    topology: TopologyKind | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    task_md: str | None = None
    agents: list[RunStartAgent] = Field(default_factory=list)
    channels: list[RunStartChannel] = Field(default_factory=list)
    limits: LimitsInfo | None = None
    decider: Decider | None = None
    estimate: Estimate | None = None
    capacity: list[CapacityInfo] = Field(default_factory=list)
    deviations: list[Deviation] = Field(default_factory=list)
    standard_notes: list[str] = Field(default_factory=list)
    # escalation
    stage: str | None = None
    attempts: list[Attempt] = Field(default_factory=list)
    last_ratings: list[LastRating] = Field(default_factory=list)
    options: list[EscalationOptionInfo] = Field(default_factory=list)
    result_id: int | None = Field(default=None, description="Latest attempt („Skizze V3 lesen“)")
    comment_to: ApprovalAgentRef | None = Field(
        default=None, description="Agent who gets the user's comment (escalation: the worker)"
    )
    # all open items
    count: int = Field(default=1, ge=1, description="Equal requests this one card answers")
    terminal: bool = Field(default=False, description="Decided in the terminal of `arr run`")
    # history
    decision: DecisionInfo | None = None
    revocable: bool = False
    status_text: str | None = Field(default=None, description="e.g. „Nicht mehr nötig: …“")

    @property
    def deviations_count(self) -> int:
        """Number of deviations (start page: „⚠ n Abweichungen“)."""
        return len(self.deviations)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.type is ApprovalType.PERMISSION and (self.agent is None or self.risk is None):
            raise ValueError("Eine Berechtigung braucht agent und risk")
        if self.type is ApprovalType.RUN_START and self.deadline is not None:
            raise ValueError("Run-Start-Freigaben haben keine Frist (07 §5)")
        if self.type is ApprovalType.ESCALATION and (not self.options or self.topology is None):
            raise ValueError("Eine Eskalation braucht Optionen und die Topologie des Runs")
        self._check_vendors()
        if self.status is ApprovalStatus.OPEN and self.decision is not None:
            raise ValueError("Ein offener Eintrag hat keine decision")
        if self.status in (ApprovalStatus.DECIDED, ApprovalStatus.REVOKED) and not self.decision:
            raise ValueError("Ein entschiedener Eintrag braucht decision")
        if self.decision is not None:
            if self.decision.action not in _TYPE_ACTIONS[self.type]:
                raise ValueError(f"{self.decision.action} passt nicht zu {self.type}")
            if self.type is not ApprovalType.PERMISSION and (
                self.decision.decided_by is not DecidedBy.USER
                or self.decision.scope is not DecisionScope.ONCE
            ):
                raise ValueError("Run-Starts und Eskalationen entscheidet nur der User, einmalig")
        if self.revocable and self.status is not ApprovalStatus.DECIDED:
            raise ValueError("revocable nur bei entschiedenen Einträgen")
        return self

    def _check_vendors(self) -> None:
        """E19: the vendors named on the card are those of the agents that read the content."""
        if self.type is ApprovalType.PERMISSION and self.agent is not None:
            if self.content_to and self.content_to != [self.agent.vendor]:
                raise ValueError(
                    f"content_to {self.content_to} passt nicht zu {self.agent.id} "
                    f"({self.agent.vendor})"
                )
            if RequestCategory.READ_OUTSIDE in self.categories and not self.content_to:
                raise ValueError("Lesen außerhalb braucht content_to (E19)")
        elif self.content_to:
            raise ValueError("content_to gibt es nur bei Berechtigungen")
        agents = {a.id: a.vendor for a in self.agents}
        for dev in self.deviations:
            for share in dev.providers:
                for agent_id in share.agents:
                    if agents.get(agent_id) is not share.vendor:
                        raise ValueError(
                            f"Abweichung {dev.path}: {agent_id} gehört nicht zu {share.vendor}"
                        )


class DecisionRequest(ApiModel):
    """Decision sent by the GUI (docs/08 §5.2 „Entscheiden“, „Anpassen“).

    `allow`/`deny` for permissions and run starts, `option` + `option_key` for escalations,
    `adjust` + `config` (changed values by deviation path) for run starts.
    """

    action: DecisionAction
    scope: DecisionScope = DecisionScope.ONCE
    comment: str | None = None
    option_key: EscalationOption | None = None
    config: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.action is DecisionAction.ASK:
            raise ValueError("„ask“ ist keine Entscheidung")
        if (self.action is DecisionAction.OPTION) != (self.option_key is not None):
            raise ValueError("option_key gehört genau zu action=option")
        if (self.action is DecisionAction.ADJUST) != (self.config is not None):
            raise ValueError("config gehört genau zu action=adjust")
        return self


class DecisionResponse(ApiModel):
    """Answer to a decision: the updated item, the run and the toast text (docs/08 §5.2)."""

    item: ApprovalItem
    run_id: str
    changes: list[ChangeInfo] = Field(default_factory=list)
    message: str


# --------------------------------------------------------------------------------------------
# Profiles, projects, skills, usage, storage, new run, setup
# --------------------------------------------------------------------------------------------


class ProfileConcurrency(ApiModel):
    """Parallel places of a profile: „3 von 4 belegt“ (docs/08 §5.0 `concurrency`)."""

    limit: int = Field(ge=0)
    in_use: int = Field(ge=0)


class ProfileTest(ApiModel):
    """Result of the last profile test (docs/08 §5.0 `last_test`, §5.8)."""

    ts: float
    ok: bool
    duration_ms: int | None = None
    models: list[str] = Field(default_factory=list)
    message: str | None = None
    action: Action | None = None


class UsageWindow(ApiModel):
    """Usage in the limit window of a subscription (docs/08 §5.0 `usage_window`)."""

    utilization: float = Field(ge=0, le=1)
    resets_at: float | None = None
    label: str | None = Field(default=None, description="e.g. „5-Stunden-Fenster“")


class ProfileInfo(ApiModel):
    """An auth profile without its secret (docs/08 §5.0 `ProfileInfo`, §5.8)."""

    name: str
    kind: AuthKind
    provider: Provider
    default_for: list[Provider] = Field(default_factory=list)
    concurrency: ProfileConcurrency
    secret_present: bool
    keychain_label: str | None = Field(default=None, description="e.g. „arr/max-privat“")
    login_dir: str | None = None
    last_test: ProfileTest | None = None
    usage_window: UsageWindow | None = None
    models: list[str] = Field(default_factory=list)
    disabled: bool = Field(default=False, description="Not-Aus: nothing starts with the profile")


class ProfileActionResult(ApiModel):
    """Answer to „Sperren“, „Entsperren“ and „Login entfernen“ (W3-06).

    With `dry_run=True` nothing changes, `runs` then names what would stop. `failed_runs` are
    runs the Not-Aus could not reach: the page says so instead of a calm message.
    """

    ok: bool = True
    message: str
    runs: list[str] = Field(default_factory=list)
    failed_runs: list[str] = Field(default_factory=list)
    removable: bool = Field(default=False, description="`dry_run`: arr has a login to remove")
    removed: bool = False
    profile: ProfileInfo | None = None


class ProfileUsageSum(ApiModel):
    """Tokens and costs of a period; the three cost fields keep billed and estimated apart."""

    label: str
    runs: int = Field(default=0, ge=0)
    turns: int = Field(default=0, ge=0)
    input: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)
    cache_read: int = Field(default=0, ge=0)
    cache_write: int = Field(default=0, ge=0)
    reasoning: int = Field(default=0, ge=0)
    cost_billed_usd: float = Field(default=0, ge=0)
    cost_estimated_usd: float = Field(default=0, ge=0)
    cost_unknown_turns: int = Field(default=0, ge=0)


class ProfileRunUsage(ProfileUsageSum):
    """One run in the record of a profile; only the agents on this profile count."""

    run_id: str
    status: RunStatus
    agents: list[str] = Field(default_factory=list)
    started: float | None = None
    ended: float | None = None
    aborted: bool = Field(default=False, description="stopped, failed or interrupted")
    active: bool = False
    url: str


class ProfileUsage(ApiModel):
    """„Nachweis je Profil“: what ran through arr on this profile (W3-06)."""

    profile: str
    today: ProfileUsageSum
    week: ProfileUsageSum
    total: ProfileUsageSum
    runs: list[ProfileRunUsage] = Field(default_factory=list)
    note: str


class ProjectCheck(ApiModel):
    """A setup check of a project (docs/08 §5.6 `checks[]`)."""

    key: str
    ok: bool
    severity: Severity = Severity.INFO
    message: str
    detail: str | None = None
    action: Action | None = None


class ProjectRuns(ApiModel):
    """Run counters of a project (docs/08 §5.6 `runs {total, active, waiting}`)."""

    total: int = Field(ge=0)
    active: int = Field(ge=0)
    waiting: int = Field(ge=0)


class ProjectFolder(ApiModel):
    """A top-level folder of a project with its file count (docs/08 §5.6 `folders[]`)."""

    name: str
    count: int = Field(ge=0)


class ProjectInfo(ApiModel):
    """A project (docs/08 §5.6 `ProjectInfo`)."""

    name: str
    title: str
    path: str
    setup_ok: bool
    size_bytes: int = Field(ge=0)
    modified_at: float
    runs: ProjectRuns
    checks: list[ProjectCheck] = Field(default_factory=list)
    folders: list[ProjectFolder] = Field(default_factory=list)
    copy_exclude: list[str] = Field(default_factory=list)
    read_dirs: list[str] = Field(default_factory=list)


class SkillIssue(ApiModel):
    """A problem in a skill file (docs/08 §5.7 `issues[]`)."""

    file: str
    line: int | None = None
    message: str
    severity: Severity = Severity.ERROR


class SkillUsage(ApiModel):
    """Where a skill is used (docs/08 §5.7 `used_by`)."""

    projects: list[str] = Field(default_factory=list)
    runs_count: int = Field(default=0, ge=0)
    last_run_at: float | None = None


class ParamInfo(ApiModel):
    """A declared parameter of a group skill for forms (docs/08 §5.4, §5.7)."""

    name: str
    type: Literal["int", "float", "bool", "str", "choice", "path"]
    default: Any = None
    min: float | None = None
    max: float | None = None
    choices: list[Any] | None = None
    doc: str | None = None


class SkillInfo(ApiModel):
    """A skill of the workfolder (docs/08 §5.7 `SkillInfo`); group fields only for groups."""

    name: str
    description: str
    kind: SkillKind
    audience: str | None = None
    files: list[str] = Field(default_factory=list)
    valid: bool = True
    issues: list[SkillIssue] = Field(default_factory=list)
    used_by: SkillUsage = Field(default_factory=SkillUsage)
    topology: TopologyKind | None = None
    params: list[ParamInfo] = Field(default_factory=list)
    agents_spec: list[dict[str, Any]] = Field(default_factory=list)
    channels_spec: list[dict[str, Any]] = Field(default_factory=list)
    rubric: dict[str, Any] | None = None
    agents_count: int | None = None


class UsageRow(ApiModel):
    """One row of the usage report (docs/08 §5.9 `UsageRow`); `None` = not reported."""

    key: str
    label: str
    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    reasoning: int | None = None
    cost_usd: float | None = None
    cost_kind: CostKind | None = None
    runs: int = Field(default=0, ge=0)


class RateLimitEvent(ApiModel):
    """A rate limit event of a profile (docs/08 §5.9 `ratelimits[]`)."""

    ts: float
    profile: str
    status: str
    utilization: float | None = Field(default=None, ge=0, le=1)
    run: str | None = None
    agent: str | None = None
    consequence: str


class UsageReport(ApiModel):
    """Usage in a period, grouped (docs/08 §5.9)."""

    from_ts: float
    to_ts: float
    group_by: UsageGroupBy
    rows: list[UsageRow]
    totals: UsageRow
    billed_usd: float | None = None
    estimated_usd: float | None = None
    cache_read_ratio: float | None = Field(default=None, ge=0, le=1)
    ratelimits: list[RateLimitEvent] = Field(default_factory=list)


class StorageParts(ApiModel):
    """Disk use by part in bytes (docs/08 §5.10 `by_part`)."""

    work: int = Field(default=0, ge=0)
    logs: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)
    archive: int = Field(default=0, ge=0)


class RunRef(ApiModel):
    """Short reference to a run (docs/08 §5.10 `StorageItem.run`)."""

    id: str
    name: str
    project: str
    gui_url: str


class StorageItem(ApiModel):
    """A run in „Runs nach Größe“ (docs/08 §5.10 `StorageItem`)."""

    run: RunRef
    actual: int = Field(ge=0)
    logical: int = Field(ge=0)
    by_part: StorageParts
    slimmable: bool
    archivable: bool
    protected_reason: str | None = Field(default=None, description="e.g. „aktiv“, „angeheftet“")


class ProjectStorage(ApiModel):
    """Disk use of one project (docs/08 §5.10 `by_project[]`)."""

    project: str
    actual: int = Field(ge=0)
    logical: int = Field(ge=0)
    runs: int = Field(ge=0)


class StorageSuggestion(ApiModel):
    """Suggested clean-up with what is freed and what is excluded (docs/08 §5.10)."""

    action: RunAction
    run_ids: list[str]
    freed_bytes: int = Field(ge=0)
    excluded_pinned: int = Field(default=0, ge=0)
    excluded_active: int = Field(default=0, ge=0)
    keeps: list[str] = Field(default_factory=list, description="German „Bleibt“ list")
    removes: list[str] = Field(default_factory=list, description="German „Weg“ list")


class StorageReport(ApiModel):
    """Disk use of the runs, limit and suggestion (docs/08 §5.10)."""

    runs_actual: int = Field(ge=0)
    runs_logical: int = Field(ge=0)
    by_part: StorageParts
    limit_bytes: int | None = None
    disk_free: int = Field(ge=0)
    disk_total: int = Field(ge=0)
    by_project: list[ProjectStorage] = Field(default_factory=list)
    suggestion: StorageSuggestion | None = None
    runs: list[StorageItem] = Field(default_factory=list)


class StartRunRequest(ApiModel):
    """A new run from the GUI: run input as in `run.yaml` (docs/08 §5.4)."""

    run_input: dict[str, Any]


class StartRunResponse(ApiModel):
    """Answer of `POST /api/runs` (docs/08 §5.4)."""

    run_id: str
    status: RunStatus
    gui_url: str


class ResolveRequest(ApiModel):
    """Resolution and check of a run input without starting it (docs/08 §5.4)."""

    run_input: dict[str, Any]


class ResolveResult(ApiModel):
    """Resolved configuration, problems, estimate and deviations (docs/08 §5.4)."""

    resolved_run_yaml: str
    agents: list[RunStartAgent] = Field(default_factory=list)
    channels: list[RunStartChannel] = Field(default_factory=list)
    errors: list[FieldIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    estimate: Estimate | None = None
    capacity: list[CapacityInfo] = Field(default_factory=list)
    deviations: list[Deviation] = Field(default_factory=list)


class ToolchainCandidate(ApiModel):
    """A Python environment found for the setup assistant (docs/08 §5.12)."""

    path: str
    python_version: str
    packages: dict[str, bool] = Field(default_factory=dict)


class ToolchainNotice(ApiModel):
    """Why Codex agents cannot use a Python (docs/08 §8.4): what happened, then why and how."""

    title: str
    text: str


class SetupState(ApiModel):
    """State of the setup assistant (docs/08 §5.12 `GET /api/setup/state`)."""

    workfolder: WorkfolderInfo | None = None
    profiles: list[ProfileInfo] = Field(default_factory=list)
    toolchain_candidates: list[ToolchainCandidate] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Settings, mandate, fixed rules, profiles, projects, skill files, workfolder (W1-08 screens)
# --------------------------------------------------------------------------------------------

SLUG = r"^[a-z0-9][a-z0-9_-]{0,63}$"


class OpenIn(StrEnum):
    """Where the GUI opens (docs/08 §5.11 „Allgemein“, E18): browser tab or own window."""

    BROWSER = "browser"
    WINDOW = "window"


class Appearance(StrEnum):
    """Light/dark setting (docs/08 §5.11 „Darstellung“, §7.5)."""

    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


class NotificationEvent(StrEnum):
    """Events that can raise a macOS notification (docs/08 §4.2, §5.11)."""

    APPROVAL = "approval"
    RUN_FINISHED = "run_finished"
    LIMIT_REACHED = "limit_reached"
    STORAGE_LOW = "storage_low"
    MESSAGE_TO_USER = "message_to_user"


class AppSettings(ApiModel):
    """App settings from `~/.arr` (docs/08 §5.11 „Allgemein“, „Mitteilungen“, „Darstellung“).

    New approvals always notify (§4.2); the validator keeps that switch on.
    """

    open_in: OpenIn = OpenIn.BROWSER
    port: int = Field(default=8765, ge=1024, le=65535)
    appearance: Appearance = Appearance.SYSTEM
    notifications: dict[NotificationEvent, bool] = Field(default_factory=dict)
    workfolders: list[WorkfolderRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _approvals_always_notify(self) -> Self:
        if self.notifications.get(NotificationEvent.APPROVAL) is False:
            raise ValueError("Mitteilungen zu neuen Freigaben lassen sich nicht abschalten")
        return self


SYSTEM_PYTHON = "System-Python"
"""Choice „no base venv“ in the settings: the Python arr itself is based on."""


class ProviderDefaultsInfo(ApiModel):
    """Standard profile, model and effort of one provider (docs/08 §5.11 „Standards“)."""

    auth: str | None = None
    model: str | None = None
    effort: Effort | None = None


class ToolchainInfo(ApiModel):
    """Python environment of the agents (docs/08 §5.11 „Python-Umgebung“, docs/07 §2)."""

    python: str | None = None
    extra_path: list[str] = Field(default_factory=list)
    run_venv: RunVenvMode = RunVenvMode.OVERLAY


class ApprovalSettingsInfo(ApiModel):
    """Who decides permission requests and how long a request waits (docs/08 §5.11)."""

    decider: Decider
    timeout_s: int = Field(ge=60)
    gatekeeper_model: str | None = None
    mandate: str | None = Field(default=None, description="e.g. mandates/standard.md")


class RetentionInfo(ApiModel):
    """Clean-up rules; arr never deletes automatically (docs/08 §5.11 „Aufräumregeln“)."""

    slim_after_days: int | None = Field(default=None, ge=1)
    archive_after_days: int | None = Field(default=None, ge=1)
    max_runs_size_gb: float | None = Field(default=None, gt=0)


class WorkfolderSettingsInfo(ApiModel):
    """Settings of the current workfolder (`arr.yaml`, docs/08 §5.11).

    `user_agent_runs_require_approval` is shown only and always true (E15). `running_runs`
    feeds the hint „gilt sofort, auch in n laufenden Runs“. `toolchain_candidates` are the
    base venvs to choose from, `toolchain_notice` says why Codex agents cannot use the current
    Python, before a run fails on it (docs/08 §8.4).
    """

    defaults: dict[Provider, ProviderDefaultsInfo] = Field(default_factory=dict)
    limits: "LimitsInfo"
    concurrency: dict[str, int] = Field(default_factory=dict)
    toolchain: ToolchainInfo = Field(default_factory=ToolchainInfo)
    approvals: ApprovalSettingsInfo
    retention: RetentionInfo = Field(default_factory=RetentionInfo)
    user_agent_runs_require_approval: Literal[True] = True
    running_runs: int = Field(default=0, ge=0)
    toolchain_candidates: list[ToolchainCandidate] = Field(default_factory=list)
    toolchain_notice: ToolchainNotice | None = None


class MandateChecks(ApiModel):
    """Parts of the mandate that arr checks itself (docs/07 §4 „Vollmacht (von arr geprüft)“)."""

    domains: list[str] = Field(default_factory=list)
    read_dirs: list[str] = Field(default_factory=list)
    packages: list[str] = Field(default_factory=list)


class MandateInfo(ApiModel):
    """The mandate in plain text (docs/08 §5.11 „Vollmacht“, `GET/PUT /api/mandates/{name}`)."""

    name: str
    path: str
    text: str
    modified_at: float | None = None
    checked: MandateChecks = Field(default_factory=MandateChecks)
    running_runs: int = Field(default=0, ge=0)
    example: str | None = Field(default=None, description="For „Beispiel einfügen“")


class MandateUpdate(ApiModel):
    """New text of a mandate (docs/08 §5.11, confirmed by a dialog in the GUI)."""

    text: str = Field(min_length=1)


class RuleEffect(StrEnum):
    """Effect of a fixed rule (docs/07 §3 ②): allowed, forbidden or an approval request."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


BUILTIN_FIXED_RULES = [
    (RuleEffect.ALLOW, "Lesen und Schreiben im Arbeitsordner, Python in der Sandbox"),
    (RuleEffect.DENY, "Schreiben außerhalb des Runs (auch Original-Projekt)"),
    (RuleEffect.DENY, "Sensible Pfade (~/.ssh, ~/.aws, ~/.arr, Schlüsselbund, Browser-Profile)"),
    (RuleEffect.DENY, "sudo, security, launchctl, ssh, git push"),
    (RuleEffect.ASK, "Lesen außerhalb des Runs, sonstiges Netzwerk, Paketinstallation"),
]
"""The standard rules of docs/07 §3 ② as the settings page shows them."""


class FixedRule(ApiModel):
    """A fixed rule (docs/08 §5.11 „Feste Regeln“): standard rules are fixed, own „immer“ rules
    can be removed."""

    id: str
    effect: RuleEffect
    rule: str
    origin: Literal["builtin", "user"]
    origin_text: str = Field(description="e.g. „Standard“, „von dir: Reichweite „immer“ …“")
    removable: bool
    created_at: float | None = None

    @model_validator(mode="after")
    def _only_user_rules_removable(self) -> Self:
        if self.removable and self.origin != "user":
            raise ValueError("Nur eigene Regeln lassen sich entfernen")
        return self


class ToolchainTestResult(ApiModel):
    """Result of „Testen“ for the Python environment (docs/08 §5.11, `POST /api/toolchain/test`)."""

    ok: bool
    python: str | None = None
    python_version: str | None = None
    packages: dict[str, bool] = Field(default_factory=dict)
    message: str
    detail: str | None = None
    """Why and what to do, after `message` (docs/08 §8.4)."""
    duration_ms: int | None = None


class CreateProfileRequest(ApiModel):
    """„Neues Profil“ (docs/08 §5.8, `POST /api/profiles`). The key travels only in the request.

    API-key kinds need `api_key`; subscription kinds log in afterwards with `start_login`
    (E18), they never take a pasted token. `replace` puts a new key on an existing profile
    (§8.4 „Key ersetzen“): the profile stays, `concurrency` is ignored.
    """

    name: str = Field(pattern=SLUG)
    kind: AuthKind
    concurrency: int = Field(default=2, ge=1, le=16)
    api_key: SecretStr | None = None
    replace: bool = False

    @model_validator(mode="after")
    def _key_only_for_key_kinds(self) -> Self:
        # Not a ValueError: pydantic would add the whole input, with the key in clear, to
        # `errors()` and `.json()`; `hide_input_in_errors` only covers `str()`.
        key_kinds = (AuthKind.CLAUDE_API_KEY, AuthKind.OPENAI_API_KEY)
        if (self.kind in key_kinds) != (self.api_key is not None):
            raise ApiException.of(
                ErrorCode.INVALID, "Ein API-Key gehört genau zu Profilen mit API-Key."
            )
        if self.replace and self.api_key is None:
            raise ApiException.of(ErrorCode.INVALID, "Ersetzen geht nur mit einem neuen API-Key.")
        return self


class LoginStart(ApiModel):
    """Start of a login in the browser or by device code (docs/08 §5.8, E18).

    `note` tells what arr runs for it (G4, e.g. „arr führt dafür `claude setup-token` aus“).
    With `browser` the CLI opens the browser itself, `browser_url` stays empty; with
    `device_code` it is the page where the user enters the code.
    """

    profile: str
    method: Literal["browser", "device_code"]
    browser_url: str | None = None
    device_code: str | None = None
    poll_url: str
    expires_at: float
    note: str | None = None

    @model_validator(mode="after")
    def _matches_method(self) -> Self:
        if (self.method == "device_code") != (self.device_code is not None):
            raise ValueError("device_code gehört genau zu method=device_code")
        return self


class LoginState(StrEnum):
    """State of a running login (docs/08 §5.8 „lädt“, „Fehler“)."""

    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LoginStatus(ApiModel):
    """Poll answer of a login; on success with the tested profile (docs/08 §5.8)."""

    profile: str
    state: LoginState
    message: str | None = None
    action: Action | None = None
    profile_info: ProfileInfo | None = None


class CreateProjectRequest(ApiModel):
    """„Neues Projekt“ or „Vorhandenen Ordner einrichten“ (docs/08 §5.6, §5.12).

    `template` e.g. `collatz-beispiel`; `existing_path` sets up an existing folder (arr adds
    only missing arr files, G8). At most one of both.
    """

    name: str = Field(pattern=SLUG)
    title: str | None = None
    template: str | None = None
    existing_path: str | None = None

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if self.template and self.existing_path:
            raise ValueError("Entweder Vorlage oder vorhandener Ordner, nicht beides")
        return self


class ProjectSetupResult(ApiModel):
    """Answer of creating, setting up or repairing a project (docs/08 §5.6)."""

    project: ProjectInfo
    created: list[str] = Field(default_factory=list, description="Files arr wrote")
    message: str


class SkillFile(ApiModel):
    """A file of a skill, read only in v1 (docs/08 §5.7 `GET /api/skills/{name}/files/{path}`)."""

    skill: str
    path: str
    content: str
    language: Literal["markdown", "yaml", "python", "text"]
    size: int = Field(ge=0)


class WorkfolderRequest(ApiModel):
    """Open or create a workfolder (docs/08 §5.12, `POST /api/workfolder`).

    `python`: a base venv from `toolchain_candidates` for `toolchain.python` of a new workfolder.
    """

    path: str = Field(min_length=1)
    create: bool = False
    name: str | None = None
    python: str | None = None


class UiSeen(ApiModel):
    """Display state „gelesen bis“ per run, kept in the browser in W1-W3 (docs/08 §5.0)."""

    run_id: str
    channels: dict[str, int] = Field(default_factory=dict, description="Channel → last seen ID")
    view: str | None = None


# --------------------------------------------------------------------------------------------
# Live events (docs/08 §5.0 „Live-Ereignisse“)
# --------------------------------------------------------------------------------------------


class LiveEventBase(ApiModel):
    """Common fields of all live events; `seq` grows per stream (docs/08 §5.0, §6.6)."""

    seq: int = Field(ge=0)
    ts: float
    run_id: str | None = None


class RunStatusEvent(LiveEventBase):
    """A run changed (status, counters, usage), with the new summary (docs/08 §5.0)."""

    type: Literal["run.status"] = "run.status"
    run: RunSummary
    previous: RunStatus | None = None


class StageCheckpointEvent(LiveEventBase):
    """A stage changed (checkpoint in `orchestrator.jsonl`, docs/08 §5.0)."""

    type: Literal["stage.checkpoint"] = "stage.checkpoint"
    stage: StageInfo


class AgentStateEvent(LiveEventBase):
    """An agent card changed (docs/08 §5.0 `agent.state`)."""

    type: Literal["agent.state"] = "agent.state"
    agent: AgentState


class AgentActivityEvent(LiveEventBase):
    """The „jetzt“ line of an agent changed (docs/08 §5.0 `agent.activity`)."""

    type: Literal["agent.activity"] = "agent.activity"
    agent_id: str
    activity: Activity


class AgentTranscriptEvent(LiveEventBase):
    """A new transcript entry, only for subscribed agents (docs/08 §5.0 `agent.event`)."""

    type: Literal["agent.event"] = "agent.event"
    agent_id: str
    event: TranscriptEvent


class ChannelMessageEvent(LiveEventBase):
    """A new message in a channel (docs/08 §5.0 `channel.message`)."""

    type: Literal["channel.message"] = "channel.message"
    message: Message


class ResultSubmittedEvent(LiveEventBase):
    """An agent submitted a result (docs/08 §5.0 `result.submitted`)."""

    type: Literal["result.submitted"] = "result.submitted"
    result: Result


class RatingSubmittedEvent(LiveEventBase):
    """A rater rated a result (docs/08 §5.0 `rating.submitted`)."""

    type: Literal["rating.submitted"] = "rating.submitted"
    result_id: int
    rating: RatingInfo
    avg_score: float | None = None
    status: ResultStatus


class ApprovalEvent(LiveEventBase):
    """An inbox item was created, decided (also in another tab), revoked or expired.

    See docs/08 §5.0 `approval.created/decided/expired`, §5.2 „live“. `approval.revoked`
    marks a withdrawn decision in the history; `approval.updated` a card that now answers more
    equal requests. An item that became obsolete comes as `approval.expired`.
    """

    type: Literal[
        "approval.created",
        "approval.updated",
        "approval.decided",
        "approval.expired",
        "approval.revoked",
    ]
    item: ApprovalItem


class UsageTickEvent(LiveEventBase):
    """Time and cost of a running run advanced (docs/08 §5.0 `usage.tick`)."""

    type: Literal["usage.tick"] = "usage.tick"
    elapsed_s: int
    spent_usd: float | None = None
    cost_kind: CostKind | None = None
    tokens_total: int | None = None


class LimitWarningEvent(LiveEventBase):
    """A limit of a run reached 80 % or more (docs/08 §5.0 `limit.warning`, K10)."""

    type: Literal["limit.warning"] = "limit.warning"
    limit: LimitKind
    used: float
    limit_value: float
    ratio: float = Field(ge=0)


class CountsChangedEvent(LiveEventBase):
    """The global counters changed (docs/08 §5.0 global stream `/api/events`, §8.3)."""

    type: Literal["counts.changed"] = "counts.changed"
    counts: GlobalCounts


class WarningsChangedEvent(LiveEventBase):
    """The list of global warnings changed (docs/08 §4.2)."""

    type: Literal["warnings.changed"] = "warnings.changed"
    warnings: list[WarningInfo]


LiveEvent = Annotated[
    RunStatusEvent
    | StageCheckpointEvent
    | AgentStateEvent
    | AgentActivityEvent
    | AgentTranscriptEvent
    | ChannelMessageEvent
    | ResultSubmittedEvent
    | RatingSubmittedEvent
    | ApprovalEvent
    | UsageTickEvent
    | LimitWarningEvent
    | CountsChangedEvent
    | WarningsChangedEvent,
    Field(discriminator="type"),
]
"""Any live event, told apart by `type` (docs/08 §5.0)."""

LIVE_EVENT = TypeAdapter(LiveEvent)
"""Validates and serializes `LiveEvent` payloads, e.g. for the SSE endpoints."""

GLOBAL_EVENT_TYPES = frozenset(
    {
        "counts.changed",
        "warnings.changed",
        "run.status",
        "approval.created",
        "approval.updated",
        "approval.decided",
        "approval.expired",
        "approval.revoked",
        "usage.tick",
    }
)
"""Event types of the global stream `/api/events` (counters, warnings, run summaries, inbox,
usage of active runs for lists); run streams carry all types."""

ApprovalTop.model_rebuild()
WorkfolderSettingsInfo.model_rebuild()
