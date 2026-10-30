"""`ApiClient` interface of the GUI and `MockApi`, its implementation on sample data.

The GUI talks only to an `ApiClient`. `MockApi` (W1-06) serves the two data sets of
docs/08-ux-konzept.md §9.2 from memory: the **scenario** of the wireframes (Friday,
24.10.2026, 15:24) and the **load** data set (200 runs, a run with 12 agents, 4 channels,
6 stages, about 10 000 messages, 50 000 transcript events). Decisions, run actions and
messages change the mock state; a simulated live stream (`start_simulation`) moves time,
costs, agent activity and messages forward and lets deadlines expire. `RealApi` (W3-01)
implements the same protocol on top of the service.

The counting rules of docs/08 §8.3 live in `count_waiting`, `count_active` and
`count_runs_waiting`; every counter the GUI shows comes from them.
"""

import asyncio
import contextlib
import dataclasses
import random
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any

from pydantic import BaseModel

from arr.backends.base import CostKind
from arr.models import (
    AuthKind,
    ChannelMode,
    Decider,
    Effort,
    Provider,
    RunStatus,
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
from arr.room.types import DeliveryStatus, MessageKind, MessageVia
from arr.service.api_client import (  # noqa: E402 - re-exported for the GUI and tests
    ApiClient,
    EventHub,
    Subscription,
    count_active,
    count_runs_waiting,
    count_waiting,
)
from arr.service.api_models import (
    Action,
    ActionResult,
    Activity,
    ActivityKind,
    AgentActivityEvent,
    AgentChanges,
    AgentDisplayState,
    AgentsByState,
    AgentState,
    AgentStateEvent,
    AgentTranscriptEvent,
    ApiException,
    ApprovalAgentRef,
    ApprovalEvent,
    ApprovalItem,
    ApprovalRunRef,
    ApprovalSettingsInfo,
    ApprovalTop,
    AppSettings,
    AttachmentInfo,
    Attempt,
    BestResult,
    BlockedBy,
    BlockedKind,
    CapacityInfo,
    ChangeInfo,
    ChannelInfo,
    ChannelMemberInfo,
    ChannelMessageEvent,
    Consequences,
    CountsChangedEvent,
    CreateProfileRequest,
    CreateProjectRequest,
    DecisionInfo,
    DecisionRequest,
    DecisionResponse,
    DeliveryInfo,
    Deviation,
    ErrorCode,
    EscalatedByInfo,
    EscalationOptionInfo,
    Estimate,
    FieldIssue,
    FileChange,
    FixedRule,
    GlobalCounts,
    LastMessage,
    LastRating,
    LimitsInfo,
    LiveEvent,
    LogEntry,
    LoginStart,
    LoginState,
    LoginStatus,
    MandateChecks,
    MandateInfo,
    MandateUpdate,
    Message,
    MessagesPage,
    NotificationEvent,
    ParamInfo,
    PostMessageRequest,
    ProfileConcurrency,
    ProfileInfo,
    ProfileTest,
    ProjectCheck,
    ProjectFolder,
    ProjectInfo,
    ProjectRuns,
    ProjectSetupResult,
    ProjectStorage,
    ProviderDefaultsInfo,
    ProviderShare,
    Range,
    RateLimitEvent,
    RatingInfo,
    ResolveResult,
    Result,
    ResultKind,
    ResultStatus,
    RetentionInfo,
    RiskInfo,
    RuleEffect,
    RunAction,
    RunCounts,
    RunDetail,
    RunError,
    RunFilter,
    RunLimits,
    RunRef,
    RunResults,
    RunsPage,
    RunStartAgent,
    RunStartChannel,
    RunStatusEvent,
    RunSummary,
    SetupState,
    Severity,
    SizeInfo,
    SkillFile,
    SkillInfo,
    SkillIssue,
    SkillKind,
    SkillUsage,
    StageInfo,
    StageRef,
    StageStatus,
    StartRunResponse,
    StorageItem,
    StorageParts,
    StorageReport,
    StorageSuggestion,
    ToolAccess,
    ToolBundle,
    ToolchainCandidate,
    ToolchainInfo,
    ToolchainTestResult,
    ToolStatus,
    TranscriptEvent,
    TranscriptKind,
    TranscriptPage,
    TurnRange,
    UsageGroupBy,
    UsageReport,
    UsageRow,
    UsageTickEvent,
    UsageWindow,
    Vendor,
    WarningInfo,
    WarningKind,
    WarningsChangedEvent,
    WorkfolderInfo,
    WorkfolderRef,
    WorkfolderRequest,
    WorkfolderSettingsInfo,
)

__all__ = [
    "ApiClient",
    "Dataset",
    "EventHub",
    "ForcedStateApi",
    "MockApi",
    "PageState",
    "Subscription",
    "count_active",
    "count_runs_waiting",
    "count_waiting",
]

DEFAULT_BASE_URL = "http://127.0.0.1:8765"

PAUSE_TURNS_END_S = 8.0
"""After „Pausieren“ the running turns of the agents end after this many seconds."""

RUN_DETAIL_STREAM_RATE = 1.0
"""Speed of the simulated live stream of running runs (transcripts, messages, turns).

1.0 gives about one new transcript entry every 3 s and a channel message every 12 s per run;
browser tests and the load measurement of W1-07 raise it (`MockApi.stream_rate`)."""
"""Service URL of the mock when `arr gui` does not pass its own (docs/08 §4.3)."""

GB = 1_000_000_000
MB = 1_000_000

# --------------------------------------------------------------------------------------------
# Mock clock and internal state
# --------------------------------------------------------------------------------------------


class Dataset(StrEnum):
    """Data sets of the mock (docs/08 §9.2); the value is used in `?daten=`."""

    SCENARIO = "szenario"
    LOAD = "last"
    ACCEPTANCE = "abnahme"


DATASET_LABELS = {
    Dataset.SCENARIO: "Szenario (Wireframes)",
    Dataset.LOAD: "Last-Datensatz",
    Dataset.ACCEPTANCE: "Abnahme (UX-Abnahme 2)",
}


class MockClock:
    """Clock that starts at a fixed moment of the data set and then runs in real time.

    `advance()` moves it forward without waiting (tests, deadlines).
    """

    def __init__(self, start: float) -> None:
        self._start = start
        self._t0 = time.monotonic()
        self._offset = 0.0

    def now(self) -> float:
        """Current mock time as Unix seconds."""
        return self._start + (time.monotonic() - self._t0) + self._offset

    def advance(self, seconds: float) -> None:
        """Jump forward by `seconds`."""
        self._offset += seconds


def _at(day: int, hour: int, minute: int, second: int = 0, month: int = 10) -> float:
    """Local timestamp in 2026 (the data sets play in October 2026)."""
    return datetime(2026, month, day, hour, minute, second).timestamp()


@dataclass
class _Channel:
    info: ChannelInfo
    fixed: dict[int, Message] = field(default_factory=dict)
    generated: int = 0
    gen: Callable[[int], Message] | None = None
    extra: list[Message] = field(default_factory=list)

    @property
    def count(self) -> int:
        return (
            max([self.generated, *self.fixed]) + len(self.extra)
            if (self.fixed or self.generated)
            else len(self.extra)
        )

    def message(self, msg_id: int) -> Message | None:
        if msg_id in self.fixed:
            return self.fixed[msg_id]
        base = max([self.generated, *self.fixed]) if (self.fixed or self.generated) else 0
        if msg_id > base:
            idx = msg_id - base - 1
            return self.extra[idx] if 0 <= idx < len(self.extra) else None
        if self.gen is not None and 1 <= msg_id <= self.generated:
            return self.gen(msg_id)
        return None

    def replace(self, msg: Message) -> None:
        if msg.id in self.fixed:
            self.fixed[msg.id] = msg
            return
        for i, old in enumerate(self.extra):
            if old.id == msg.id:
                self.extra[i] = msg


@dataclass
class _Transcript:
    total_generated: int = 0
    gen: Callable[[int], TranscriptEvent] | None = None
    fixed: dict[int, TranscriptEvent] = field(default_factory=dict)
    extra: list[TranscriptEvent] = field(default_factory=list)
    older: TurnRange | None = None

    @property
    def total(self) -> int:
        base = max([self.total_generated - 1, *self.fixed], default=-1) + 1
        return base + len(self.extra)

    def event(self, seq: int) -> TranscriptEvent | None:
        if seq in self.fixed:
            return self.fixed[seq]
        base = max([self.total_generated - 1, *self.fixed], default=-1) + 1
        if seq >= base:
            idx = seq - base
            return self.extra[idx] if 0 <= idx < len(self.extra) else None
        if self.gen is not None and 0 <= seq < self.total_generated:
            return self.gen(seq)
        return None

    def replace(self, ev: TranscriptEvent) -> None:
        """Store a changed entry under its `seq` (e.g. a tool call that finished)."""
        base = max([self.total_generated - 1, *self.fixed], default=-1) + 1
        if ev.seq >= base and 0 <= ev.seq - base < len(self.extra):
            self.extra[ev.seq - base] = ev
        else:
            self.fixed[ev.seq] = ev


@dataclass
class _Run:
    """Mutable mock state of one run; `MockApi._summary` turns it into a `RunSummary`."""

    id: str
    name: str
    project: str
    skill: str | None
    topology: TopologyKind
    status: RunStatus
    starter: Starter
    created_at: float
    started_at: float | None = None
    ended_at: float | None = None
    status_since: float | None = None
    status_detail: str | None = None
    elapsed_frozen: float | None = None
    stage: StageRef | None = None
    wall_time_s: int | None = 7200
    budget_usd: float | None = 20.0
    spent_usd: float | None = None
    turns_per_agent: int | None = 40
    max_turns_used: int = 0
    tokens_total: int | None = None
    cost_kind: CostKind | None = CostKind.ESTIMATED
    size: SizeInfo = field(default_factory=lambda: SizeInfo(actual_bytes=0, logical_bytes=0))
    pinned: bool = False
    slimmed: bool = False
    archived: bool = False
    best_result: BestResult | None = None
    error: RunError | None = None
    info: str | None = None
    adjusted_by_user: bool = False
    changes: list[ChangeInfo] = field(default_factory=list)
    decider: Decider = Decider.GATEKEEPER_THEN_USER
    task_md: str | None = None
    cost_rate: float = 0.0  # $/s while running
    token_rate: float = 0.0  # tokens/s while running
    stages: list[StageInfo] = field(default_factory=list)
    agents: dict[str, AgentState] = field(default_factory=dict)
    channels: dict[str, _Channel] = field(default_factory=dict)
    transcripts: dict[str, _Transcript] = field(default_factory=dict)
    results: list[Result] = field(default_factory=list)
    summary_md: str | None = None
    results_empty_text: str | None = None
    log: list[LogEntry] = field(default_factory=list)
    file_changes: list[AgentChanges] = field(default_factory=list)
    handoff_extra: str = ""
    detail_built: bool = False
    running_tools: dict[str, int] = field(default_factory=dict)  # agent → seq (live stream)
    turns_end_at: float | None = None  # paused: running turns end at this time
    paused_workers: list[str] = field(default_factory=list)  # back to work on resume


@dataclass
class _State:
    workfolder: WorkfolderInfo
    runs: dict[str, _Run]
    approvals: dict[str, ApprovalItem]
    warnings: list[WarningInfo]
    profiles: list[ProfileInfo]
    projects: list[ProjectInfo]
    skills: list[SkillInfo]
    user_rules: list[FixedRule] = field(default_factory=list)
    app_settings: AppSettings | None = None
    workfolder_settings: WorkfolderSettingsInfo | None = None
    mandate_text: str = ""
    mandate_modified: float | None = None
    logins: dict[str, float] = field(default_factory=dict)
    next_approval: int = 100
    sim_rng: random.Random = field(default_factory=lambda: random.Random(7))
    last_sim: float = 0.0
    pending_transitions: list[tuple[float, str, RunStatus]] = field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Small builders
# --------------------------------------------------------------------------------------------

_MODEL_LABELS = {
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-haiku-5-5": "Claude Haiku 5.5",
    "gpt-5.5": "gpt-5.5 (Codex)",
    "gpt-5.5-mini": "gpt-5.5-mini (Codex)",
}


def _model_label(model: str) -> str:
    return _MODEL_LABELS.get(model, model)


def _vendor(provider: Provider) -> Vendor:
    return Vendor(provider.vendor)


def _provider_for(model: str) -> Provider:
    return Provider.CODEX if model.startswith("gpt") else Provider.CLAUDE


def _profile_for(model: str) -> str:
    return "chatgpt-privat" if model.startswith("gpt") else "max-privat"


def _agent(
    run_id: str,
    agent_id: str,
    role: str,
    model: str,
    state: AgentDisplayState,
    activity: Activity | None = None,
    *,
    channels: Iterable[str] = (),
    since: float | None = None,
    blocked_by: BlockedBy | None = None,
    turn: int = 0,
    turns_limit: int | None = 40,
    wakeups: int = 0,
    max_wakeups: int | None = 12,
    cost: float | None = None,
    tokens: int | None = None,
    cache: float | None = None,
    assignment: str | None = None,
    skills: Iterable[str] = (),
    questions_to_user: int = 0,
    open_questions_to: int = 0,
    results: int = 0,
    ratings: int = 0,
    web: bool = False,
    read_dirs: Iterable[str] = (),
) -> AgentState:
    provider = _provider_for(model)
    return AgentState(
        id=agent_id,
        role_label=role,
        role_file=f"roles/{agent_id.split('-')[0]}.md",
        provider=provider,
        vendor=_vendor(provider),
        model=model,
        model_label=_model_label(model),
        auth_profile=_profile_for(model),
        skills=list(skills),
        channels=list(channels),
        tools=ToolAccess(shell=True, web=web),
        read_dirs=list(read_dirs),
        assignment_label=assignment,
        state=state,
        state_since=since,
        blocked_by=blocked_by,
        activity=activity,
        turn=turn,
        turns_limit=turns_limit,
        wakeups=wakeups,
        max_wakeups=max_wakeups,
        cost_usd=None if provider is Provider.CODEX else cost,
        cost_kind=None if provider is Provider.CODEX else CostKind.ESTIMATED,
        tokens=tokens,
        cache_read_ratio=cache,
        questions_to_user=questions_to_user,
        open_questions_to=open_questions_to,
        results_count=results,
        ratings_count=ratings,
        work_dir=f"runs/{run_id}/agents/{agent_id}/work",
    )


def _msg(
    channel: str,
    msg_id: int,
    ts: float,
    sender: str,
    kind: MessageKind,
    body: str,
    **kw: Any,
) -> Message:
    return Message(id=msg_id, ts=ts, channel=channel, sender=sender, kind=kind, body=body, **kw)


def _tool_event(
    seq: int,
    ts: float,
    turn: int,
    name: str,
    summary: str,
    status: ToolStatus,
    *,
    duration_ms: int | None = None,
    output: str | None = None,
    lines: int | None = None,
    blob: str | None = None,
    approval_id: str | None = None,
) -> TranscriptEvent:
    return TranscriptEvent(
        seq=seq,
        ts=ts,
        turn=turn,
        kind=TranscriptKind.TOOL,
        tool=ToolBundle(
            id=f"tc_{seq}",
            name=name,
            input_summary=summary,
            input={"command": summary} if name in ("Bash", "Shell") else {"path": summary},
            status=status,
            duration_ms=duration_ms,
            output_excerpt=output,
            output_lines=lines,
            blob_path=blob,
            approval_id=approval_id,
        ),
    )


def _ev(seq: int, ts: float, turn: int, kind: TranscriptKind, **data: Any) -> TranscriptEvent:
    return TranscriptEvent(seq=seq, ts=ts, turn=turn, kind=kind, data=data)


def _escalation_options(
    worker: str, stage: str, next_stage: str, attempts: int
) -> list[EscalationOptionInfo]:
    """Options of an escalation, worded from the run's own data (docs/03 §4.4)."""
    return [
        EscalationOptionInfo(
            key=EscalationOption.RETRY,
            label="Noch ein Versuch",
            help=f"erhöht die Nacharbeit um 1. {worker} bekommt die Kommentare und überarbeitet "
            f"(Versuch {attempts + 1}).",
        ),
        EscalationOptionInfo(
            key=EscalationOption.CONTINUE,
            label="Trotzdem weiter",
            help=f"Stufe „{next_stage}“ startet mit {stage} V{attempts} als Grundlage.",
        ),
        EscalationOptionInfo(
            key=EscalationOption.END,
            label="Run beenden",
            help="Zusammenfassung wird erstellt, das Protokoll bleibt vollständig.",
        ),
    ]


def _shares(agents: list[RunStartAgent]) -> list[ProviderShare]:
    """Vendors that receive the content of a read folder or working copy, with their agents."""
    shares: dict[Vendor, list[str]] = {}
    for agent in agents:
        shares.setdefault(agent.vendor, []).append(agent.id)
    return [ProviderShare(vendor=v, agents=ids) for v, ids in shares.items()]


def _copy_note(project: str, agents: list[RunStartAgent]) -> str:
    """„Arbeitsordner (Kopie von collatz, Inhalte gehen an Anthropic und OpenAI)“ (E19)."""
    names = {Vendor.ANTHROPIC: "Anthropic", Vendor.OPENAI: "OpenAI"}
    vendors = [names[s.vendor] for s in _shares(agents)]
    joined = " und ".join(vendors)
    return f"Arbeitsordner (Kopie von {project}, Inhalte gehen an {joined})"


_MATH_SNIPPETS = [
    "Für $n ≡ 3 \\pmod{16}$ fällt die Folge nach vier Schritten unter $n$.",
    "Die Restklasse $27 \\bmod 32$ bleibt der schwierige Fall.",
    "Abschätzung $T^k(n) < n$ für alle $k ≥ 4$ nachgerechnet bis $2^{24}$.",
    "Dichte der Ausnahmemenge höchstens $O(x^{0,85})$.",
    "Gegenbeispiel-Suche bis $10^9$ ohne Treffer.",
    "Lemma 4.2 deckt die Fälle A–C ab, offen bleibt D.",
    "Die Induktion über $k$ braucht den Fall $k = 1$ separat.",
    "Sage-Rechnung bestätigt die Tabelle der Iterationen.",
]

_KIND_CYCLE = [
    MessageKind.NOTE,
    MessageKind.CLAIM,
    MessageKind.ANSWER,
    MessageKind.NOTE,
    MessageKind.IDEA,
    MessageKind.OBJECTION,
    MessageKind.ANSWER,
    MessageKind.PROOF,
    MessageKind.QUESTION,
    MessageKind.NOTE,
]


def _generated_message_factory(
    run_id: str, channel: str, members: list[str], start: float, step: float
) -> Callable[[int], Message]:
    def make(msg_id: int) -> Message:
        rng = random.Random(f"{run_id}:{channel}:{msg_id}")
        kind = _KIND_CYCLE[msg_id % len(_KIND_CYCLE)]
        sender = members[rng.randrange(len(members))]
        body = _MATH_SNIPPETS[rng.randrange(len(_MATH_SNIPPETS))]
        reply_to = msg_id - rng.randint(1, 5) if kind is MessageKind.ANSWER and msg_id > 5 else None
        to = (
            [members[(members.index(sender) + 1) % len(members)]]
            if kind is MessageKind.QUESTION
            else []
        )
        return Message(
            id=msg_id,
            ts=start + msg_id * step,
            channel=channel,
            sender=sender,
            kind=kind,
            body=body,
            to=to,
            reply_to=reply_to,
        )

    return make


def _generated_transcript_factory(
    run_id: str, agent_id: str, total: int, start: float, end: float, turns: int
) -> Callable[[int], TranscriptEvent]:
    per_turn = max(4, total // max(1, turns))
    step = (end - start) / max(1, total)

    def make(seq: int) -> TranscriptEvent:
        turn = min(turns, seq // per_turn + 1)
        pos = seq % per_turn
        ts = start + seq * step
        if pos == 0:
            return _ev(seq, ts, turn, TranscriptKind.TURN_START, prompt=f"Turn {turn}", stage=None)
        if pos == per_turn - 1:
            return _ev(
                seq,
                ts,
                turn,
                TranscriptKind.TURN_END,
                status="ok",
                usage={"input": 12000, "output": 900},
                cost_usd=0.12,
                session=None,
                num_turns=6,
            )
        rng = random.Random(f"{run_id}:{agent_id}:{seq}")
        roll = 0.5 if seq % 997 == 0 else rng.random()  # every 997th event: tool with a blob
        if roll < 0.15:
            return _ev(seq, ts, turn, TranscriptKind.THINKING, text="Ich prüfe den nächsten Fall.")
        if roll < 0.35:
            return _ev(seq, ts, turn, TranscriptKind.TEXT, text=_MATH_SNIPPETS[seq % 8])
        if roll < 0.37:
            return _ev(
                seq,
                ts,
                turn,
                TranscriptKind.HINT,
                text="Neue Nachricht in #werkstatt",
                channel="werkstatt",
                method="post_tool_use",
            )
        big = seq % 997 == 0
        status = ToolStatus.ERROR if seq % 211 == 0 else ToolStatus.OK
        return _tool_event(
            seq,
            ts,
            turn,
            "Bash" if roll < 0.8 else "Read",
            f"sage beweis_teil{seq % 5}.sage" if roll < 0.8 else f"notes/fall_{seq % 7}.md",
            status,
            duration_ms=rng.randint(80, 40000),
            output="Fall geprüft: 4096 von 4096\nLaufzeit 3,1 s" if not big else "(Ausgabe 1,4 MB)",
            lines=2 if not big else 41800,
            blob=f"agents/{agent_id}/blobs/{seq:06d}.txt" if big else None,
        )

    return make


# --------------------------------------------------------------------------------------------
# Data set: scenario (wireframes, Friday 24.10.2026 15:24)
# --------------------------------------------------------------------------------------------

R_EXPLORE = "20261024-1412_collatz_exploration_a3f9"
R_RAMSEY = "20261024-1305_ramsey-schranken_proof_b2e4"
R_REVIEW = "20261024-1521_collatz_proof-review_e81b"
R_LEMMA = "20261024-0930_collatz_proof_7c21"

SCENARIO_NOW = _at(24, 15, 24)
LOAD_NOW = _at(24, 14, 41)

_LEMMA3_TASK = """**Aussage:** Für $n > 1$ gilt $σ(n) = 4$ genau dann, wenn $n ≡ 3 \\pmod{16}$.

**Beweis:** `tex/lemma3.tex` (Abschnitt 2).

**Schwerpunkte:** Vollständigkeit der Fallunterscheidung modulo 16, Notation σ (Stoppzeit) \
gegenüber Gesamtstoppzeit.

**Ergebnis:** Je Judge ein Gutachten mit Urteil (accept, minor, major, reject) und \
nummerierten Einwänden.

**Hinweise:** Die Definition von σ folgt Terras (1976). Abschnitt 1 von `tex/lemma3.tex` ist \
nicht Teil der Prüfung.
"""


def _scenario_runs(now: float) -> dict[str, _Run]:
    runs: dict[str, _Run] = {}

    def add(run: _Run) -> None:
        runs[run.id] = run

    add(
        _Run(
            id=R_EXPLORE,
            name="Collatz – Ideen-Exploration",
            project="collatz",
            skill="exploration",
            topology=TopologyKind.EXPLORATION,
            status=RunStatus.RUNNING,
            starter=Starter.GUI,
            created_at=_at(24, 14, 12),
            started_at=_at(24, 14, 12),
            stage=StageRef(
                index=1, count=3, label="Exploration", detail="3 Ideen · Turn 5–7 von 8"
            ),
            wall_time_s=3 * 3600,
            budget_usd=25.0,
            spent_usd=6.42,
            turns_per_agent=40,
            max_turns_used=9,
            tokens_total=1_100_000,
            size=SizeInfo(
                actual_bytes=int(2.3 * GB),
                logical_bytes=int(9.6 * GB),
                work=int(2.0 * GB),
                logs=int(0.25 * GB),
                output=int(0.05 * GB),
            ),
            info="Exploration (1 von 3) · 5 Agenten",
            decider=Decider.GATEKEEPER_THEN_USER,
            cost_rate=6.42 / (72 * 60),
            token_rate=1_100_000 / (72 * 60),
            task_md="Finde neue Zugänge zu Stoppzeiten der Collatz-Abbildung.",
        )
    )
    add(
        _Run(
            id=R_RAMSEY,
            name="R(4,5) ≤ 25 – Beweisweg ausarbeiten",
            project="ramsey-schranken",
            skill="proof",
            topology=TopologyKind.WATERFALL,
            status=RunStatus.PAUSED,
            status_since=_at(24, 15, 2),
            status_detail="pausiert wegen Eskalation",
            starter=Starter.USER_AGENT,
            created_at=_at(24, 13, 5),
            started_at=_at(24, 13, 5),
            elapsed_frozen=117 * 60,
            stage=StageRef(
                index=2, count=3, label="Skizze", detail="Versuch 3 von 3 nicht bestanden"
            ),
            wall_time_s=3 * 3600,
            budget_usd=20.0,
            spent_usd=9.10,
            turns_per_agent=40,
            max_turns_used=14,
            tokens_total=1_600_000,
            size=SizeInfo(
                actual_bytes=int(1.4 * GB),
                logical_bytes=int(4.1 * GB),
                work=int(1.2 * GB),
                logs=int(0.18 * GB),
                output=int(0.02 * GB),
            ),
            info="Eskalation: Skizze nicht bestanden",
        )
    )
    add(
        _Run(
            id=R_REVIEW,
            name="Lemma 3 – unabhängige Prüfung",
            project="collatz",
            skill="proof-review",
            topology=TopologyKind.REVIEW,
            status=RunStatus.AWAITING_APPROVAL,
            status_since=_at(24, 15, 21),
            starter=Starter.USER_AGENT,
            created_at=_at(24, 15, 21),
            wall_time_s=2 * 3600,
            budget_usd=20.0,
            turns_per_agent=60,
            size=SizeInfo(actual_bytes=0, logical_bytes=0),
            info="von Claude Code · startet nach deiner Freigabe",
            task_md=_LEMMA3_TASK,
        )
    )
    add(
        _Run(
            id=R_LEMMA,
            name="Lemma 3 (Stoppzeit 4) – Beweis ausarbeiten",
            project="collatz",
            skill="proof",
            topology=TopologyKind.WATERFALL,
            status=RunStatus.DONE,
            starter=Starter.USER_AGENT,
            created_at=_at(24, 9, 30),
            started_at=_at(24, 9, 30),
            ended_at=_at(24, 11, 22),
            wall_time_s=3 * 3600,
            budget_usd=25.0,
            spent_usd=11.30,
            tokens_total=1_900_000,
            size=SizeInfo(
                actual_bytes=int(1.2 * GB),
                logical_bytes=int(5.2 * GB),
                work=int(1.0 * GB),
                logs=int(0.15 * GB),
                output=int(0.05 * GB),
            ),
            pinned=True,
            best_result=BestResult(title="003 · Skizze V2", score=8.5, pass_score=7),
            info="3/3 Stufen bestanden · beste Bewertung 8,5",
            stage=StageRef(index=3, count=3, label="Beweis", detail="bestanden"),
        )
    )
    finished = [
        (
            "20261022-1105_primzahl-luecken_roundtable_19ab",
            "Primzahllücken – Roundtable",
            "primzahl-luecken",
            "roundtable",
            TopologyKind.ROUNDTABLE,
            RunStatus.FAILED,
            Starter.GUI,
            _at(22, 11, 5),
            4 * 60,
            38_000,
            0.21,
            0.3,
            "Anmeldung von chatgpt-privat abgelaufen",
        ),
        (
            "20261021-2210_collatz_exploration_5d0e",
            "Collatz – Zyklen mod 2ᵏ",
            "collatz",
            "exploration",
            TopologyKind.EXPLORATION,
            RunStatus.INTERRUPTED,
            Starter.USER_AGENT,
            _at(21, 22, 10),
            91 * 60,
            2_400_000,
            13.80,
            4.8,
            "arr wurde beendet · Protokoll bis 23:41",
        ),
        (
            "20261019-1640_collatz_proof-review_0f3c",
            "Terras-Lemma – Review",
            "collatz",
            "proof-review",
            TopologyKind.REVIEW,
            RunStatus.DONE,
            Starter.USER_AGENT,
            _at(19, 16, 40),
            58 * 60,
            900_000,
            7.05,
            3.9,
            "Urteil: minor (2 : 1)",
        ),
        (
            "20261012-0915_collatz_custom_77aa",
            "Stoppzeiten bis 10⁹ – solo",
            "collatz",
            None,
            TopologyKind.SOLO,
            RunStatus.DONE,
            Starter.GUI,
            _at(12, 9, 15),
            22 * 60,
            200_000,
            1.10,
            0.038,
            None,
        ),
        (
            "20261010-2030_ramsey-schranken_exploration_4b19",
            "Ramsey – Färbungsideen",
            "ramsey-schranken",
            "exploration",
            TopologyKind.EXPLORATION,
            RunStatus.STOPPED,
            Starter.GUI,
            _at(10, 20, 30),
            15 * 60,
            300_000,
            1.90,
            0.9,
            "von dir gestoppt nach Stufe 1",
        ),
        (
            "20260928-1400_collatz_exploration_e2d1",
            "Collatz – erste Exploration",
            "collatz",
            "exploration",
            TopologyKind.EXPLORATION,
            RunStatus.DONE,
            Starter.GUI,
            _at(28, 14, 0, month=9),
            160 * 60,
            3_100_000,
            18.20,
            0.4,
            None,
        ),
    ]
    for (
        rid,
        name,
        project,
        skill,
        topo,
        status,
        starter,
        start,
        dur,
        tok,
        cost,
        gb,
        info,
    ) in finished:
        run = _Run(
            id=rid,
            name=name,
            project=project,
            skill=skill,
            topology=topo,
            status=status,
            starter=starter,
            created_at=start,
            started_at=start,
            ended_at=start + dur,
            spent_usd=cost,
            tokens_total=tok,
            size=_size(gb),
            info=info,
        )
        runs[rid] = run
    runs["20261012-0915_collatz_custom_77aa"].slimmed = True
    runs["20260928-1400_collatz_exploration_e2d1"].archived = True
    runs["20260928-1400_collatz_exploration_e2d1"].size = SizeInfo(
        actual_bytes=int(0.4 * GB), logical_bytes=int(0.4 * GB), archive=int(0.4 * GB)
    )
    runs["20261022-1105_primzahl-luecken_roundtable_19ab"].error = RunError(
        message="Run fehlgeschlagen: Die Anmeldung von „chatgpt-privat“ ist abgelaufen. Alle 3 "
        "Agenten wurden nach 4 Minuten beendet, das Protokoll bis dahin ist vollständig. Melde "
        "dich neu an und starte den Run erneut.",
        action=Action(label="Profil prüfen", url="/profile/chatgpt-privat"),
    )
    runs["20261021-2210_collatz_exploration_5d0e"].error = RunError(
        message="arr wurde am 21.10. um 23:41 beendet, während dieser Run lief. Alles bis dahin "
        "ist gespeichert und lesbar. Fortsetzen geht in v1 nicht, du kannst den Run mit gleicher "
        "Konfiguration neu starten.",
        action=Action(
            label="Neu starten", url="/neuer-run?wie=20261021-2210_collatz_exploration_5d0e"
        ),
    )
    runs["20261019-1640_collatz_proof-review_0f3c"].best_result = BestResult(
        title="Urteil minor", score=None
    )
    # 21 older runs: collatz 15, ramsey-schranken 4, primzahl-luecken 2 (31 in total).
    # Their sizes fill up the project totals of the scenario (39,8 / 5,1 / 1,3 GB = 46,2 GB).
    older = [("collatz", 15, 27.162), ("ramsey-schranken", 4, 2.8), ("primzahl-luecken", 2, 1.0)]
    n = 0
    for project, count, total_gb in older:
        weights = [1 + (i * 7) % 5 for i in range(count)]
        for i in range(count):
            n += 1
            rng = random.Random(f"older:{project}:{i}")
            day = 9 - (n % 9)
            month = 10 if n <= 9 else 9
            day = day if month == 10 else 30 - (n % 28)
            start = _at(max(1, day), 9 + (n % 10), (n * 7) % 60, month=month)
            skill, topo = [
                ("exploration", TopologyKind.EXPLORATION),
                ("proof", TopologyKind.WATERFALL),
                ("roundtable", TopologyKind.ROUNDTABLE),
                ("proof-review", TopologyKind.REVIEW),
            ][n % 4]
            rid = (
                datetime.fromtimestamp(start).strftime("%Y%m%d-%H%M")
                + f"_{project}_{skill}_{rng.randrange(16**4):04x}"
            )
            gb = total_gb * weights[i] / sum(weights)
            dur = rng.randint(12, 150) * 60
            runs[rid] = _Run(
                id=rid,
                name=f"{_OLDER_TITLES[project][i % len(_OLDER_TITLES[project])]}",
                project=project,
                skill=skill,
                topology=topo,
                status=RunStatus.STOPPED if n % 7 == 0 else RunStatus.DONE,
                starter=Starter.USER_AGENT if n % 2 else Starter.GUI,
                created_at=start,
                started_at=start,
                ended_at=start + dur,
                spent_usd=round(rng.uniform(0.5, 14), 2),
                tokens_total=rng.randint(80, 2600) * 1000,
                size=_size(gb),
            )
    return runs


_OLDER_TITLES = {
    "collatz": [
        "Collatz – Stoppzeiten-Statistik",
        "Terras-Satz – Beweisskizze",
        "Paritätsvektoren mod 2¹⁶",
        "Collatz – 2-adische Ideen",
        "Lemma 2 – Review",
        "Zyklen der Länge k – Exploration",
        "Collatz – Heuristik ln(4/3)",
    ],
    "ramsey-schranken": ["R(4,5) – Strategien", "R(3,3,3) – Färbungen", "Ramsey – Literatur"],
    "primzahl-luecken": ["Primzahllücken – Statistik", "Cramér-Vermutung – Review"],
}


def _size(gb: float) -> SizeInfo:
    actual = int(gb * GB)
    return SizeInfo(
        actual_bytes=actual,
        logical_bytes=int(actual * 3.2),
        work=int(actual * 0.8),
        logs=int(actual * 0.15),
        output=actual - int(actual * 0.8) - int(actual * 0.15),
    )


def _scenario_approvals(base: str) -> dict[str, ApprovalItem]:
    explore_ref = ApprovalRunRef(
        id=R_EXPLORE,
        name="Collatz – Ideen-Exploration",
        project="collatz",
        gui_url=f"{base}/runs/{R_EXPLORE}",
    )
    ramsey_ref = ApprovalRunRef(
        id=R_RAMSEY,
        name="R(4,5) ≤ 25 – Beweisweg ausarbeiten",
        project="ramsey-schranken",
        gui_url=f"{base}/runs/{R_RAMSEY}",
    )
    review_ref = ApprovalRunRef(
        id=R_REVIEW,
        name="Lemma 3 – unabhängige Prüfung",
        project="collatz",
        gui_url=f"{base}/runs/{R_REVIEW}",
    )
    lemma_ref = ApprovalRunRef(
        id=R_LEMMA,
        name="Lemma 3 (Stoppzeit 4) – Beweis ausarbeiten",
        project="collatz",
        gui_url=f"{base}/runs/{R_LEMMA}",
    )

    def agent_ref(agent_id: str, role: str, model: str) -> ApprovalAgentRef:
        provider = _provider_for(model)
        return ApprovalAgentRef(
            id=agent_id,
            role_label=role,
            model=model,
            model_label=_model_label(model),
            provider=provider,
            vendor=_vendor(provider),
        )

    review_agents = [
        _run_start_agent("author", "Autor", "claude-opus-5-5", web=False),
        _run_start_agent("judge-1", "Judge", "claude-opus-5-5", web=True),
        _run_start_agent("judge-2", "Judge", "gpt-5.5", web=True),
        _run_start_agent("judge-3", "Judge", "claude-opus-5-5", web=True),
    ]
    items = [
        ApprovalItem(
            id="apr_0042",
            type=ApprovalType.PERMISSION,
            created_at=_at(24, 15, 16),
            gui_url=f"{base}/freigaben/apr_0042",
            title="explorer-1 möchte eine Datei von oeis.org laden",
            run=explore_ref,
            deadline=_at(24, 15, 36),
            agent=agent_ref("explorer-1", "Explorer", "claude-opus-5-5"),
            tool="Bash",
            command="curl -sO https://oeis.org/A006577/b006577.txt",
            url="https://oeis.org/A006577/b006577.txt",
            work_dir=f"…/runs/{R_EXPLORE}/agents/explorer-1/work",
            reason="Ich brauche die Tabelle A006577 (Schritte bis 1) für n ≤ 10 000, um meine "
            "Stoppzeit-Verteilung aus der 2-adischen Darstellung mit veröffentlichten Werten zu "
            "vergleichen. Die Datei ist reiner Text, etwa 90 kB.",
            risk=RiskInfo(
                level=RiskLevel.MEDIUM,
                text="Netzwerkzugriff auf eine Domain, die deine Vollmacht nicht nennt.",
            ),
            categories=[RequestCategory.NETWORK],
            escalated_by=EscalatedByInfo(
                decider=DecidedBy.MANDATE_RULE,
                note="Netzwerk ist nur zu pypi.org, files.pythonhosted.org und arxiv.org "
                "erlaubt. oeis.org gehört nicht dazu, deshalb entscheidest du.",
            ),
            consequences=Consequences(
                allow="Der Befehl läuft einmal, die Datei landet im Arbeitsordner von explorer-1.",
                deny="explorer-1 bekommt deinen Kommentar und arbeitet ohne die Datei weiter.",
                expire="Keine Entscheidung bis 15:36: gilt als abgelehnt.",
            ),
            rule_label="Netzwerk zu oeis.org",
            transcript_seq=214,
        ),
        ApprovalItem(
            id="apr_0040",
            type=ApprovalType.ESCALATION,
            created_at=_at(24, 15, 2),
            gui_url=f"{base}/freigaben/apr_0040",
            title="Stufe „Skizze“ nach 3 Versuchen nicht bestanden",
            run=ramsey_ref,
            stage="Skizze",
            attempts=[
                Attempt(n=1, avg_score=4.5, pass_score=7, passed=False),
                Attempt(n=2, avg_score=5.0, pass_score=7, passed=False),
                Attempt(n=3, avg_score=5.5, pass_score=7, passed=False),
            ],
            last_ratings=[
                LastRating(
                    rater="rater-1",
                    score=5.0,
                    comment="Die Reduktion auf (4,5)-Graphen mit 24 Knoten ist richtig. Es fehlt "
                    "aber, wie die verbleibenden Fälle ausgeschlossen werden.",
                ),
                LastRating(
                    rater="rater-2",
                    score=6.0,
                    comment="Schritt 3 setzt R(3,5) = 14 voraus, ohne Quelle oder Beleg.",
                ),
            ],
            options=_escalation_options("prover", "Skizze", "Beweis", 3),
            result_id=4,
            topology=TopologyKind.WATERFALL,
            comment_to=agent_ref("prover", "Beweiser", "claude-opus-5-5"),
        ),
        ApprovalItem(
            id="apr_0041",
            type=ApprovalType.RUN_START,
            created_at=_at(24, 15, 21),
            gui_url=f"{base}/freigaben/apr_0041",
            title="proof-review: Lemma 3 – unabhängige Prüfung",
            run=review_ref,
            requested_by=Starter.USER_AGENT,
            skill="proof-review",
            topology=TopologyKind.REVIEW,
            params={
                "judges": 3,
                "max_revisions": 2,
                "verdict": "majority",
                "judge_memory": "fresh",
                "artifact": "tex/lemma3.tex",
            },
            task_md=_LEMMA3_TASK,
            agents=review_agents,
            channels=[
                RunStartChannel(name="author-desk", members=["author"], mode=ChannelMode.EVENT),
                RunStartChannel(
                    name="judges",
                    members=["judge-1", "judge-2", "judge-3"],
                    mode=ChannelMode.ROUNDS,
                    anonymous=True,
                    open_from="Diskussion",
                ),
            ],
            limits=LimitsInfo(wall_time_s=2 * 3600, turns_per_agent=60, budget_usd=20),
            decider=Decider.GATEKEEPER_THEN_USER,
            estimate=Estimate(
                turns=Range(min=60, max=90),
                cost_usd=Range(min=8, max=14),
                cost_kind=CostKind.ESTIMATED,
                duration_s=Range(min=40 * 60, max=70 * 60),
            ),
            capacity=[CapacityInfo(profile="max-privat", free=1, needed=3, limit=4)],
            deviations=[
                Deviation(
                    path="agents.judge-*.tools.web",
                    label="Web-Zugriff an",
                    value_text="für judge-1, judge-2, judge-3",
                    default_text="aus",
                    default=False,
                    value=True,
                    sensitive=True,
                ),
                Deviation(
                    path="defaults.read_dirs",
                    label="Zusätzlicher Lese-Ordner",
                    value_text="~/Documents/Mathe-Literatur für alle Agenten",
                    default_text="keiner",
                    default=[],
                    value=["~/Documents/Mathe-Literatur"],
                    sensitive=True,
                    providers=_shares(review_agents),
                ),
                Deviation(
                    path="limits.turns_per_agent",
                    label="Turns pro Agent 60",
                    value_text="",
                    default_text="40",
                    default=40,
                    value=60,
                    sensitive=False,
                ),
            ],
            standard_notes=[
                _copy_note("collatz", review_agents),
                "Auth-Profile",
                "Entscheider für Berechtigungsanfragen",
                "Laufzeit",
                "Budget",
            ],
        ),
        # history
        ApprovalItem(
            id="apr_0039",
            type=ApprovalType.PERMISSION,
            status=ApprovalStatus.DECIDED,
            created_at=_at(24, 15, 11, 40),
            gui_url=f"{base}/freigaben/apr_0039",
            title="explorer-2 möchte python-flint installieren",
            run=explore_ref,
            agent=agent_ref("explorer-2", "Explorer", "claude-opus-5-5"),
            tool="Bash",
            command="pip install python-flint",
            work_dir=f"…/runs/{R_EXPLORE}/agents/explorer-2/work",
            reason="FLINT rechnet die Restklassen-Tabellen um ein Vielfaches schneller.",
            risk=RiskInfo(level=RiskLevel.MEDIUM, text="Installation in die Run-venv."),
            categories=[RequestCategory.INSTALL],
            escalated_by=EscalatedByInfo(decider=DecidedBy.GATEKEEPER, note=""),
            consequences=Consequences(
                allow="Das Paket wird in die Run-venv installiert.",
                deny="explorer-2 arbeitet ohne python-flint weiter.",
                expire="gilt als abgelehnt",
            ),
            rule_label="pip install python-flint",
            decision=DecisionInfo(
                action=DecisionAction.ALLOW,
                scope=DecisionScope.ONCE,
                decided_by=DecidedBy.GATEKEEPER,
                decided_at=_at(24, 15, 12),
                rationale="python-flint ist ein Mathe-Paket von PyPI. Das deckt deine Vollmacht.",
            ),
            revocable=True,
        ),
        ApprovalItem(
            id="apr_0038",
            type=ApprovalType.PERMISSION,
            status=ApprovalStatus.DECIDED,
            created_at=_at(24, 14, 58),
            gui_url=f"{base}/freigaben/apr_0038",
            title="explorer-gpt möchte eine Datei außerhalb lesen",
            run=explore_ref,
            agent=agent_ref("explorer-gpt", "Explorer", "gpt-5.5"),
            tool="Read",
            path="~/Documents/Mathe-Literatur/lagarias-3x+1.pdf",
            risk=RiskInfo(level=RiskLevel.LOW, text="Lesen in einem erlaubten Lese-Ordner."),
            categories=[RequestCategory.READ_OUTSIDE],
            content_to=[Vendor.OPENAI],
            decision=DecisionInfo(
                action=DecisionAction.ALLOW,
                decided_by=DecidedBy.MANDATE_RULE,
                decided_at=_at(24, 14, 58),
                rationale="Lese-Ordner ~/Documents/Mathe-Literatur ist erlaubt.",
            ),
        ),
        ApprovalItem(
            id="apr_0037",
            type=ApprovalType.PERMISSION,
            status=ApprovalStatus.DECIDED,
            created_at=_at(24, 14, 49),
            gui_url=f"{base}/freigaben/apr_0037",
            title="explorer-gpt möchte eine Datei außerhalb lesen",
            run=explore_ref,
            agent=agent_ref("explorer-gpt", "Explorer", "gpt-5.5"),
            tool="Read",
            path="~/Documents/Collatz-Daten/orbits.csv",
            risk=RiskInfo(level=RiskLevel.MEDIUM, text="Lesen außerhalb der Lese-Ordner."),
            categories=[RequestCategory.READ_OUTSIDE],
            content_to=[Vendor.OPENAI],
            decision=DecisionInfo(
                action=DecisionAction.ALLOW,
                scope=DecisionScope.RUN,
                decided_by=DecidedBy.USER,
                decided_at=_at(24, 14, 50),
            ),
            revocable=True,
        ),
        ApprovalItem(
            id="apr_0036",
            type=ApprovalType.PERMISSION,
            status=ApprovalStatus.DECIDED,
            created_at=_at(24, 14, 40),
            gui_url=f"{base}/freigaben/apr_0036",
            title="critic möchte einen SSH-Schlüssel lesen",
            run=explore_ref,
            agent=agent_ref("critic", "Kritiker", "claude-sonnet-5-5"),
            tool="Bash",
            command="cat ~/.ssh/id_ed25519",
            risk=RiskInfo(level=RiskLevel.HIGH, text="Zugriff auf einen sensiblen Pfad."),
            categories=[RequestCategory.READ_OUTSIDE],
            content_to=[Vendor.ANTHROPIC],
            decision=DecisionInfo(
                action=DecisionAction.DENY,
                decided_by=DecidedBy.FIXED_RULE,
                decided_at=_at(24, 14, 40),
                rationale="Sensible Pfade sind gesperrt.",
            ),
        ),
        ApprovalItem(
            id="apr_0035",
            type=ApprovalType.RUN_START,
            status=ApprovalStatus.DECIDED,
            created_at=_at(24, 9, 29),
            gui_url=f"{base}/freigaben/apr_0035",
            title="proof: Lemma 3 (Stoppzeit 4) – Beweis ausarbeiten",
            run=lemma_ref,
            requested_by=Starter.USER_AGENT,
            skill="proof",
            topology=TopologyKind.WATERFALL,
            decision=DecisionInfo(
                action=DecisionAction.ALLOW,
                decided_by=DecidedBy.USER,
                decided_at=_at(24, 9, 31),
            ),
        ),
    ]
    return {item.id: item for item in items}


def _run_start_agent(agent_id: str, role: str, model: str, *, web: bool) -> RunStartAgent:
    provider = _provider_for(model)
    return RunStartAgent(
        id=agent_id,
        role_label=role,
        provider=provider,
        vendor=_vendor(provider),
        model=model,
        model_label=_model_label(model),
        profile=_profile_for(model),
        tools=ToolAccess(shell=True, web=web),
        read_dirs=["~/Documents/Mathe-Literatur"],
    )


def _profiles(now: float) -> list[ProfileInfo]:
    return [
        ProfileInfo(
            name="max-privat",
            kind=AuthKind.CLAUDE_OAUTH,
            provider=Provider.CLAUDE,
            default_for=[Provider.CLAUDE],
            concurrency=ProfileConcurrency(limit=4, in_use=3),
            secret_present=True,
            keychain_label="arr/max-privat",
            last_test=ProfileTest(
                ts=_at(24, 13, 2),
                ok=True,
                duration_ms=2300,
                models=["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
            ),
            usage_window=UsageWindow(
                utilization=0.41, resets_at=_at(24, 17, 0), label="5-Stunden-Fenster"
            ),
            models=["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
        ),
        ProfileInfo(
            name="chatgpt-privat",
            kind=AuthKind.CODEX_CHATGPT,
            provider=Provider.CODEX,
            default_for=[Provider.CODEX],
            concurrency=ProfileConcurrency(limit=3, in_use=1),
            secret_present=True,
            login_dir="~/.arr/codex-homes/chatgpt-privat",
            last_test=ProfileTest(
                ts=_at(24, 13, 2), ok=True, duration_ms=3100, models=["gpt-5.5", "gpt-5.5-mini"]
            ),
            usage_window=UsageWindow(utilization=0.82, resets_at=_at(24, 18, 40)),
            models=["gpt-5.5", "gpt-5.5-mini"],
        ),
        ProfileInfo(
            name="claude-api-uni",
            kind=AuthKind.CLAUDE_API_KEY,
            provider=Provider.CLAUDE,
            concurrency=ProfileConcurrency(limit=2, in_use=0),
            secret_present=True,
            keychain_label="arr/claude-api-uni",
            last_test=ProfileTest(
                ts=_at(24, 13, 2),
                ok=False,
                duration_ms=800,
                message="Der API-Key von „claude-api-uni“ wurde abgelehnt (401). Vermutlich wurde "
                "er in der Anthropic Console widerrufen. Erzeuge dort einen neuen Key und füge ihn "
                "hier ein.",
                action=Action(label="Key ersetzen", url="/profile/claude-api-uni"),
            ),
        ),
    ]


_ROLES = {
    "exploration": ["explorer", "critic", "ranker"],
    "proof": ["strategist", "prover", "rater"],
    "proof-review": ["author", "judge", "meta-judge"],
    "roundtable": ["member"],
}


_RUBRIC = {
    "scale": "1–10",
    "pass_score": 7,
    "criteria": ["Korrektheit", "Vollständigkeit", "Strenge", "Klarheit"],
}

# Group skills of the start set (docs/03 §2): parameters, agents and channels as the GUI shows
# them (docs/08 §5.4, §5.7); `count`, `mix` and `when` in plain words. The first line of a
# parameter's `doc` is its German label, the rest the help text (gui/components/params.py).
_GROUP_SKILLS: dict[str, dict[str, Any]] = {
    "exploration": {
        "description": "Ideen breit erkunden",
        "long": "Mehrere Explorer verfolgen je eine Idee, ein Kritiker prüft im gemeinsamen "
        "Kanal mit. Danach bewertet ein Ranker die Ergebnisse, eine Zusammenfassung schließt "
        "den Run ab.",
        "topology": TopologyKind.EXPLORATION,
        "runs": 14,
        "agents": 5,
        "params": [
            ParamInfo(
                name="ideas",
                type="str",
                default=[],
                doc="Vorgegebene Ideen\nLeer: Die Agenten erzeugen selbst 3 Ideen.",
            ),
            ParamInfo(
                name="turns_per_idea",
                type="int",
                default=6,
                min=1,
                max=20,
                doc="Turns pro Idee\nWie lange jeder Explorer an seiner Idee arbeitet.",
            ),
            ParamInfo(
                name="exchange",
                type="choice",
                default="channel",
                choices=["none", "channel", "summaries"],
                doc="Austausch zwischen den Explorern\n„keiner“ ergibt unabhängige Ergebnisse, gut "
                "für Vergleiche. „gemeinsamer Kanal“ lässt die Explorer in #main diskutieren.",
            ),
        ],
        "agents_spec": [
            {
                "id": "explorer",
                "role": "roles/explorer.md",
                "count": "einer je Idee",
                "model": "abwechselnd Claude Opus 5.5 und gpt-5.5",
            },
            {"id": "critic", "role": "roles/critic.md", "model": "Claude Sonnet 5.5"},
            {
                "id": "ranker",
                "role": "roles/ranker.md",
                "model": "Claude Sonnet 5.5",
                "when": "ab Stufe „Bewertung“",
            },
        ],
        "channels_spec": [
            {"name": "main", "members": "explorer-*, critic", "mode": "ereignisgesteuert"},
            {
                "name": "ranking",
                "members": "ranker, explorer-*",
                "mode": "Runden",
                "open_from": "Bewertung",
            },
        ],
    },
    "proof": {
        "description": "Wasserfall mit Ratings",
        "long": "Ein Stratege plant den Beweis, ein Beweiser arbeitet ihn in Stufen aus. Zwei "
        "Rater bewerten jede Stufe; unter der Grenze gibt es Nacharbeit, nach der letzten "
        "Nacharbeit entscheidest du.",
        "topology": TopologyKind.WATERFALL,
        "runs": 5,
        "agents": 4,
        "params": [
            ParamInfo(
                name="max_rework",
                type="int",
                default=2,
                min=0,
                max=5,
                doc="Nacharbeiten je Stufe\nDanach entscheidest du, wie es weitergeht.",
            ),
            ParamInfo(
                name="pass_score",
                type="int",
                default=7,
                min=1,
                max=10,
                doc="Bestanden ab\nDurchschnitt der Ratings, ab dem eine Stufe bestanden ist.",
            ),
        ],
        "agents_spec": [
            {"id": "strategist", "role": "roles/strategist.md", "model": "Claude Opus 5.5"},
            {"id": "prover", "role": "roles/prover.md", "model": "Claude Opus 5.5"},
            {
                "id": "rater",
                "role": "roles/rater.md",
                "count": "2",
                "model": "Claude Sonnet 5.5 und gpt-5.5",
            },
        ],
        "channels_spec": [
            {"name": "werkstatt", "members": "strategist, prover", "mode": "ereignisgesteuert"},
            {"name": "bewertung", "members": "prover, rater-*", "mode": "ereignisgesteuert"},
        ],
    },
    "proof-review": {
        "description": "Judges prüfen einen Beweis",
        "long": "Lässt einen Beweis(entwurf) aus dem aktuellen Projekt von mehreren "
        "unabhängigen Judges prüfen. Der Autor arbeitet Einwände nach, bis die Judges "
        "zustimmen oder die Rundenzahl erreicht ist.",
        "topology": TopologyKind.REVIEW,
        "runs": 4,
        "agents": 4,
        "params": [
            ParamInfo(
                name="judges",
                type="int",
                default=3,
                min=1,
                max=6,
                doc="Judges\nAnzahl unabhängiger Judges.",
            ),
            ParamInfo(
                name="max_revisions",
                type="int",
                default=2,
                min=0,
                max=5,
                doc="Überarbeitungen\nÜberarbeitungsrunden des Autors.",
            ),
            ParamInfo(
                name="discussion_rounds",
                type="int",
                default=1,
                min=0,
                max=3,
                doc="Diskussionsrunden\nRunden im Kanal #judges.",
            ),
            ParamInfo(
                name="verdict",
                type="choice",
                default="majority",
                choices=["majority", "unanimous", "meta_judge"],
                doc="Urteil\nWie das Urteil entsteht.",
            ),
            ParamInfo(
                name="judge_memory",
                type="choice",
                default="fresh",
                choices=["fresh", "continue"],
                doc="Gedächtnis der Judges\nJudges starten jede Runde neu oder setzen ihre "
                "Sitzung fort.",
            ),
            ParamInfo(
                name="artifact",
                type="path",
                default=None,
                doc="Beweis-Datei\nPfad im Projekt. Leer: Der Autor schreibt zuerst.",
            ),
        ],
        "agents_spec": [
            {"id": "author", "role": "roles/author.md", "model": "Claude Opus 5.5"},
            {
                "id": "judge",
                "role": "roles/judge.md",
                "count": "judge-1 … judge-N (Parameter „Judges“)",
                "model": "abwechselnd Claude Opus 5.5 und gpt-5.5",
            },
            {
                "id": "meta-judge",
                "role": "roles/meta-judge.md",
                "when": "nur bei Urteil „Meta-Judge“",
            },
        ],
        "channels_spec": [
            {"name": "author-desk", "members": "author", "mode": "ereignisgesteuert"},
            {
                "name": "judges",
                "members": "judge-*, meta-judge",
                "mode": "Runden",
                "anonymous": True,
                "open_from": "Diskussion",
            },
        ],
    },
    "roundtable": {
        "description": "Offene Diskussion",
        "long": "Mehrere Agenten diskutieren eine Frage in Runden. Wer nichts beizutragen hat, "
        "passt; nach der letzten Runde fasst ein Agent den Stand zusammen.",
        "topology": TopologyKind.ROUNDTABLE,
        "runs": 2,
        "agents": 3,
        "params": [
            ParamInfo(
                name="members",
                type="int",
                default=3,
                min=2,
                max=6,
                doc="Teilnehmer\nAnzahl der Agenten am Tisch.",
            ),
            ParamInfo(
                name="rounds",
                type="int",
                default=3,
                min=1,
                max=10,
                doc="Runden\nAnzahl der Runden.",
            ),
        ],
        "agents_spec": [
            {
                "id": "member",
                "role": "roles/member.md",
                "count": "Parameter „Teilnehmer“",
                "model": "abwechselnd Claude Opus 5.5 und gpt-5.5",
            }
        ],
        "channels_spec": [{"name": "main", "members": "member-*", "mode": "Runden"}],
    },
}


# The parameter that drives the cost of each group skill: the budget error names its label.
_COST_PARAM = {
    "exploration": "turns_per_idea",
    "proof": "max_rework",
    "proof-review": "judges",
    "roundtable": "rounds",
}


def _param_label(skill: str | None, name: str) -> str:
    """German label of a parameter: the first line of its `doc` (as in the GUI form)."""
    for p in _GROUP_SKILLS.get(skill or "", {}).get("params", []):
        lines = (p.doc or "").splitlines()
        if p.name == name and len(lines) >= 2:
            return lines[0]
    return name


def _skills() -> list[SkillInfo]:
    skills = [
        SkillInfo(
            name=name,
            description=spec["description"],
            kind=SkillKind.GROUP,
            audience="Projekt-Agenten und Claude Code",
            files=["SKILL.md", "group.yaml", *[f"roles/{r}.md" for r in _ROLES[name]]],
            used_by=SkillUsage(projects=["collatz"], runs_count=spec["runs"]),
            topology=spec["topology"],
            agents_count=spec["agents"],
            params=spec["params"],
            agents_spec=spec["agents_spec"],
            channels_spec=spec["channels_spec"],
            rubric=_RUBRIC if name != "roundtable" else None,
        )
        for name, spec in _GROUP_SKILLS.items()
    ]
    skills += [
        SkillInfo(
            name="sympy-check",
            description="Rechnungen mit SymPy prüfen",
            kind=SkillKind.TOOL,
            files=["SKILL.md"],
        ),
        SkillInfo(
            name="latex-writeup",
            description="Ergebnisse in LaTeX festhalten",
            kind=SkillKind.TOOL,
            files=["SKILL.md"],
            valid=False,
            issues=[SkillIssue(file="SKILL.md", line=3, message="Beschreibung fehlt im Kopf.")],
        ),
        SkillInfo(
            name="arr",
            description="Bedienung von arr für Claude Code",
            kind=SkillKind.USER_AGENT,
            files=["SKILL.md"],
        ),
    ]
    return skills


def _projects(runs: dict[str, _Run], now: float) -> list[ProjectInfo]:
    titles = {
        "collatz": "Collatz-Varianten",
        "ramsey-schranken": "Ramsey-Zahlen, obere Schranken",
        "primzahl-luecken": "Primzahllücken",
        "goldbach-ternaer": "Ternäre Goldbach-Darstellungen",
    }
    out = []
    for name in sorted({r.project for r in runs.values()}):
        prs = [r for r in runs.values() if r.project == name]
        ok = name != "primzahl-luecken"
        out.append(
            ProjectInfo(
                name=name,
                title=titles.get(name, name),
                path=f"~/Mathe/projects/{name}",
                setup_ok=ok,
                size_bytes=int(2.6 * GB) if name == "collatz" else int(0.4 * GB),
                modified_at=now - 3 * 3600,
                runs=ProjectRuns(
                    total=len(prs),
                    active=sum(1 for r in prs if r.status.is_active),
                    waiting=0,
                ),
                checks=[
                    ProjectCheck(key="claude_md", ok=True, message="CLAUDE.md mit arr-Block"),
                    ProjectCheck(
                        key="mcp_json",
                        ok=ok,
                        severity=Severity.INFO if ok else Severity.ERROR,
                        message=".mcp.json verbindet Claude Code mit arr"
                        if ok
                        else ".mcp.json fehlt",
                        action=None if ok else Action(label="Reparieren", url=f"/projekte/{name}"),
                    ),
                    ProjectCheck(key="skill_links", ok=True, message="Skill-Links: 7 von 7"),
                ],
                folders=[ProjectFolder(name="notes", count=14), ProjectFolder(name="tex", count=6)],
            )
        )
    return out


def _scenario_warnings() -> list[WarningInfo]:
    return [
        WarningInfo(
            id="storage",
            kind=WarningKind.STORAGE_LOW,
            title="Speicher knapp",
            text="Runs belegen 46,2 GB von 50 GB. 8 fertige Runs lassen sich verschlanken "
            "(14,9 GB).",
            action=Action(label="Speicher ansehen", url="/speicher"),
        ),
        WarningInfo(
            id="usage-chatgpt-privat",
            kind=WarningKind.USAGE_LIMIT,
            title="chatgpt-privat bei 82 % des Nutzungslimits",
            text="Zurückgesetzt um 18:40. explorer-gpt arbeitet weiter.",
            action=Action(label="Usage ansehen", url="/usage#ratelimits"),
        ),
    ]


_MANDATE_TEXT = """Erlaubt:
- pip install bekannter Mathe- und Wissenschaftspakete von PyPI in die Run-venv
  (z. B. sympy, numpy, scipy, networkx, python-flint, mpmath).
- Netzwerk nur zu pypi.org, files.pythonhosted.org, arxiv.org.
- Lesen in ~/Documents/Mathe-Literatur.
Nie erlauben:
- Schreiben außerhalb des Runs, Zugriff auf Zugangsdaten, Programme mit Systemrechten.
"""

_BUILTIN_RULES = [
    (RuleEffect.ALLOW, "Lesen und Schreiben im Arbeitsordner, Python in der Sandbox"),
    (RuleEffect.DENY, "Schreiben außerhalb des Runs (auch Original-Projekt)"),
    (RuleEffect.DENY, "Sensible Pfade (~/.ssh, ~/.aws, ~/.arr, Schlüsselbund, Browser-Profile)"),
    (RuleEffect.DENY, "sudo, security, launchctl, ssh, git push"),
    (RuleEffect.ASK, "Lesen außerhalb des Runs, sonstiges Netzwerk, Paketinstallation"),
]


def _skill_file_text(skill: SkillInfo, path: str) -> str:
    """Sample content of a skill file (read only in v1, docs/08 §5.7)."""
    spec = _GROUP_SKILLS.get(skill.name, {})
    if path == "SKILL.md":
        long = spec.get("long", skill.description)
        return (
            f"---\nname: {skill.name}\ndescription: {skill.description}\n---\n\n"
            f"# {skill.name}\n\n{long}\n\n## Wann du diesen Skill nutzt\n\n"
            "Wenn eine Frage mehrere Sichtweisen oder eine unabhängige Prüfung braucht. "
            "Starte den Run über arr; jeder Start aus Claude Code braucht die Freigabe in "
            "der GUI.\n"
        )
    if path == "group.yaml":
        lines = [
            "kind: research-group",
            "version: 1",
            f"topology: {skill.topology.value if skill.topology else 'solo'}",
            "params:",
        ]
        for p in skill.params:
            lines.append(f"  {p.name}:")
            lines.append(
                f"    default: {p.default!r}" if p.default is not None else "    default: ~"
            )
            if p.min is not None:
                lines.append(f"    min: {_num(p.min)}")
            if p.max is not None:
                lines.append(f"    max: {_num(p.max)}")
            if p.choices:
                lines.append(f"    choices: [{', '.join(map(str, p.choices))}]")
            if p.doc:
                lines.append("    doc: |")
                lines += [f"      {line}" for line in p.doc.splitlines()]
        lines.append("agents:")
        for agent in skill.agents_spec:
            lines.append(f"  - id: {agent['id']}")
            lines.append(f"    role: {agent.get('role', '')}")
        lines.append("channels:")
        for channel in skill.channels_spec:
            lines.append(f"  - name: {channel['name']}")
        return "\n".join(lines) + "\n"
    role = path.removeprefix("roles/").removesuffix(".md")
    return (
        f"# Rolle: {role}\n\nDu arbeitest in der Research-Group von {skill.name}. "
        "Lies den Posteingang, arbeite an deinem Auftrag und gib dein Ergebnis mit "
        "submit_result ab.\n"
    )


# --- run details of the scenario --------------------------------------------------------------


def _build_explore_detail(run: _Run) -> None:
    rid = run.id
    main = ["explorer-1", "explorer-2", "explorer-gpt", "critic"]
    run.agents = {
        a.id: a
        for a in [
            _agent(
                rid,
                "explorer-1",
                "Explorer",
                "claude-opus-5-5",
                AgentDisplayState.WAITING_USER,
                Activity(
                    kind=ActivityKind.WAIT_APPROVAL,
                    label="Bash",
                    arg="curl -sO https://oeis.org/A006577/b006577.txt",
                    since=_at(24, 15, 16, 2),
                    running=False,
                ),
                channels=["main", "ranking"],
                since=_at(24, 15, 16, 2),
                blocked_by=BlockedBy(
                    kind=BlockedKind.APPROVAL,
                    approval_id="apr_0042",
                    summary="Netzwerk: oeis.org",
                    deadline=_at(24, 15, 36),
                ),
                turn=6,
                wakeups=2,
                cost=1.92,
                tokens=318_000,
                cache=0.81,
                assignment="Idee „2-adische Darstellung“",
                skills=["sympy-check"],
                open_questions_to=1,
            ),
            _agent(
                rid,
                "explorer-2",
                "Explorer",
                "claude-opus-5-5",
                AgentDisplayState.WORKING,
                Activity(
                    kind=ActivityKind.TOOL,
                    label="Bash",
                    arg="python stoppzeiten.py --bis 10**8",
                    since=_at(24, 15, 22),
                    running=True,
                ),
                channels=["main", "ranking"],
                since=_at(24, 15, 10),
                turn=7,
                wakeups=3,
                cost=2.05,
                tokens=342_000,
                cache=0.78,
                assignment="Idee „Stoppzeiten-Statistik“",
            ),
            _agent(
                rid,
                "explorer-gpt",
                "Explorer",
                "gpt-5.5",
                AgentDisplayState.WORKING,
                Activity(
                    kind=ActivityKind.TOOL,
                    label="Datei schreiben",
                    arg="paritaet/terras.md",
                    since=_at(24, 15, 23, 40),
                    running=True,
                ),
                channels=["main", "ranking"],
                since=_at(24, 15, 11),
                turn=9,
                wakeups=4,
                tokens=286_000,
                assignment="Idee „Terras-Paritätsvektoren“",
            ),
            _agent(
                rid,
                "critic",
                "Kritiker",
                "claude-sonnet-5-5",
                AgentDisplayState.IDLE,
                Activity(kind=ActivityKind.WAIT_MESSAGES, label="auf neue Nachrichten"),
                channels=["main"],
                since=_at(24, 15, 14),
                turn=5,
                wakeups=4,
                cost=0.61,
                tokens=120_000,
            ),
            _agent(
                rid,
                "ranker",
                "Ranker",
                "claude-sonnet-5-5",
                AgentDisplayState.IDLE,
                Activity(kind=ActivityKind.WAIT_STAGE, label="startet in Stufe 2 · Bewertung"),
                channels=["ranking"],
                turn=0,
                wakeups=0,
            ),
        ]
    }
    ch_main = _Channel(
        info=ChannelInfo(
            name="main",
            purpose="Zwischenstände teilen, Ansätze gegenseitig prüfen",
            members=[ChannelMemberInfo(agent=a, role_label=run.agents[a].role_label) for a in main],
            mode=ChannelMode.EVENT,
            open_questions=1,
            unread_for_user=3,
            pinned=[1],
            summary_next_at=15,
        )
    )
    msgs = [
        _msg(
            "main",
            1,
            _at(24, 14, 12),
            "orchestrator",
            MessageKind.NOTE,
            "**Steckbrief** · Zweck: Zwischenstände teilen, Ansätze gegenseitig prüfen. "
            "4 Mitglieder. Regeln: Fragen mit `to`, Einwände mit Bezug. Ende nach 5 min Ruhe.",
            pinned=True,
        ),
        _msg(
            "main",
            2,
            _at(24, 14, 31),
            "explorer-2",
            MessageKind.CLAIM,
            "Die mittlere Gesamtstoppzeit von $T$ für $n < 10^7$ liegt bei $6{,}95 \\cdot \\ln n$ "
            "(Abweichung unter 2 %). Das passt zur Heuristik $2 / \\ln(4/3) ≈ 6{,}95$.",
            refs=[
                AttachmentInfo(
                    name="stoppzeiten_1e7.csv",
                    size=212_000,
                    path="groupchats/attachments/2/stoppzeiten_1e7.csv",
                )
            ],
            reply_count=2,
        ),
        _msg(
            "main",
            3,
            _at(24, 14, 52),
            "explorer-1",
            MessageKind.IDEA,
            "Auf $\\mathbb{Z}_2$ ist die Collatz-Abbildung $T$ stetig und maßerhaltend und zur "
            "Shift-Abbildung konjugiert (Lagarias 1985). Idee: Dichteaussagen über Stoppzeiten "
            "über diese Konjugation übertragen.",
            refs=[
                AttachmentInfo(
                    name="2adisch.md", size=6_100, path="groupchats/attachments/3/2adisch.md"
                )
            ],
        ),
        _msg(
            "main",
            4,
            _at(24, 15, 2),
            "explorer-gpt",
            MessageKind.CLAIM,
            "Terras (1976): Die Paritätsvektoren der Länge $k$ von $0, …, 2^k − 1$ sind "
            "paarweise verschieden. Für $k ≤ 20$ nachgerechnet.",
            refs=[
                AttachmentInfo(
                    name="check_terras.py",
                    size=2_400,
                    path="groupchats/attachments/4/check_terras.py",
                )
            ],
        ),
        _msg(
            "main",
            5,
            _at(24, 15, 9),
            "critic",
            MessageKind.OBJECTION,
            "Eine Abweichung von 2 % bei $10^7$ sagt wenig, die Konvergenz ist logarithmisch "
            "langsam. Wie verhält sich der Fehler über die Dekaden $10^4 … 10^7$?",
            to=["explorer-2"],
            reply_to=2,
        ),
        Message(
            id=6,
            ts=_at(24, 15, 12),
            channel="main",
            sender="user",
            via=MessageVia.GUI,
            kind=MessageKind.NOTE,
            body="Bitte prüfe auch $k$ bis 24, wenn es unter 2 Minuten bleibt.",
            to=["explorer-gpt"],
            delivery=DeliveryInfo(status=DeliveryStatus.HINT_MID_TURN, ts=_at(24, 15, 12)),
        ),
        _msg(
            "main",
            7,
            _at(24, 15, 14),
            "critic",
            MessageKind.QUESTION,
            "Überträgt die Konjugation zum Shift wirklich Aussagen über die natürliche Dichte in "
            "$\\mathbb{N}$? Die Maß-Aussage gilt für das Haar-Maß auf $\\mathbb{Z}_2$, nicht für "
            "das Zählen in $\\mathbb{N}$.",
            to=["explorer-1"],
            open=True,
        ),
        _msg(
            "main",
            8,
            _at(24, 15, 16),
            "explorer-2",
            MessageKind.ANSWER,
            "Fehler über die Dekaden: $10^4$: 4,1 %, $10^5$: 3,0 %, $10^6$: 2,4 %, $10^7$: "
            "1,9 %. Fällt ungefähr wie $1 / \\log n$, Tabelle im Anhang.",
            reply_to=5,
            refs=[
                AttachmentInfo(
                    name="fehlerterm.md", size=3_300, path="groupchats/attachments/8/fehlerterm.md"
                )
            ],
        ),
        _msg(
            "main",
            9,
            _at(24, 15, 18),
            "explorer-gpt",
            MessageKind.ANSWER,
            "$k ≤ 24$ geprüft (1 min 40 s), weiterhin bijektiv.",
            reply_to=6,
        ),
    ]
    ch_main.fixed = {m.id: m for m in msgs}
    ch_ranking = _Channel(
        info=ChannelInfo(
            name="ranking",
            purpose="Ideen vergleichen und bewerten",
            members=[
                ChannelMemberInfo(agent=a)
                for a in ["ranker", "explorer-1", "explorer-2", "explorer-gpt"]
            ],
            mode=ChannelMode.ROUNDS,
            open=False,
            opens_at_stage="Bewertung",
        )
    )
    run.channels = {"main": ch_main, "ranking": ch_ranking}
    run.stages = [
        StageInfo(
            key="exploration",
            index=1,
            label="Exploration",
            status=StageStatus.RUNNING,
            agents=["explorer-1", "explorer-2", "explorer-gpt", "critic"],
            detail="3 Ideen · Turn 5–7 von 8",
        ),
        StageInfo(
            key="rating",
            index=2,
            label="Bewertung",
            status=StageStatus.TODO,
            agents=["ranker"],
            detail="ranker",
        ),
        StageInfo(
            key="summary",
            index=3,
            label="Zusammenfassung",
            status=StageStatus.TODO,
            detail="summary.md",
        ),
    ]
    run.results_empty_text = (
        "Jeder Explorer gibt nach 8 Turns ein Ergebnis ab. In Stufe 2 bewertet ranker die "
        "Ergebnisse."
    )
    # explorer-1: turns 1-5 generated (seq 0-207), turn 6 as in the wireframe (seq 208-214)
    t = _Transcript(
        total_generated=208,
        gen=_generated_transcript_factory(
            rid, "explorer-1", 208, _at(24, 14, 12), _at(24, 15, 9), 5
        ),
        older=TurnRange(
            turn_from=1,
            turn_to=5,
            start_ts=_at(24, 14, 12),
            end_ts=_at(24, 15, 9),
            events=186,
            tool_calls=11,
            messages_posted=4,
        ),
    )
    t.fixed = {
        208: _ev(
            208,
            _at(24, 15, 10, 2),
            6,
            TranscriptKind.TURN_START,
            prompt="Stufe Exploration, Turn 6 von 8. Idee: 2-adische Darstellung.\n"
            "Posteingang: 2 neue Nachrichten in #main (explorer-gpt, critic). Keine offenen "
            "Fragen an dich.\nArbeite weiter und gib am Ende von Turn 8 dein Ergebnis mit "
            'submit_result(kind="exploration") ab.',
            stage="exploration",
        ),
        209: _ev(
            209,
            _at(24, 15, 10, 9),
            6,
            TranscriptKind.THINKING,
            text="Ich will die Stoppzeit-Verteilung aus der Shift-Konjugation mit den Werten "
            "aus OEIS A006577 vergleichen. Lokal habe ich nur meine eigenen Werte bis 10⁶. "
            "Zuerst prüfe ich, ob die Stoppzeiten aus den Paritätsvektoren mod 2¹⁶ mit der "
            "direkten Rechnung übereinstimmen. Danach brauche ich die b-Datei als unabhängige "
            "Referenz.",
            summarized=True,
        ),
        210: _tool_event(
            210,
            _at(24, 15, 10, 15),
            6,
            "Read",
            "notizen/2adisch.md",
            ToolStatus.OK,
            duration_ms=100,
            output="# 2-adische Sicht\nT: ℤ₂ → ℤ₂ …",
            lines=84,
        ),
        211: _tool_event(
            211,
            _at(24, 15, 11, 40),
            6,
            "Bash",
            "python stoppzeit_2adisch.py --k 16",
            ToolStatus.OK,
            duration_ms=38_000,
            output="k=16: 65536 Restklassen geprüft\nStoppzeit aus Paritätsvektor = "
            "direkte Rechnung: 65536 von 65536\nLaufzeit 37,6 s",
            lines=3,
        ),
        212: _ev(
            212,
            _at(24, 15, 13, 5),
            6,
            TranscriptKind.TEXT,
            text="Die Stoppzeiten aus den Paritätsvektoren stimmen mod 2¹⁶ mit der direkten "
            "Rechnung überein. Für den Vergleich mit großen n brauche ich die b-Datei aus "
            "OEIS.",
        ),
        213: _ev(
            213,
            _at(24, 15, 14, 31),
            6,
            TranscriptKind.HINT,
            text="1 Frage an dich von critic in #main",
            channel="main",
            method="post_tool_use",
        ),
        214: _tool_event(
            214,
            _at(24, 15, 16, 2),
            6,
            "Bash",
            "curl -sO https://oeis.org/A006577/b006577.txt",
            ToolStatus.WAITING_APPROVAL,
            approval_id="apr_0042",
        ),
    }
    run.transcripts = {"explorer-1": t}
    for aid, state in run.agents.items():
        if aid == "explorer-1":
            continue
        n = 40 + 30 * state.turn
        run.transcripts[aid] = _Transcript(
            total_generated=n,
            gen=_generated_transcript_factory(
                rid, aid, n, _at(24, 14, 12), _at(24, 15, 23), max(1, state.turn)
            ),
        )
    run.log = [
        LogEntry(
            ts=_at(24, 14, 12),
            kind="plan",
            text="Geplant: Exploration, Bewertung, Zusammenfassung",
            stage=None,
        ),
        LogEntry(
            ts=_at(24, 14, 12, 30),
            kind="stage",
            text="Stufe Exploration gestartet",
            stage="exploration",
        ),
    ]


def _build_lemma_detail(run: _Run) -> None:
    rid = run.id
    run.agents = {
        a.id: a
        for a in [
            _agent(
                rid,
                "strategist",
                "Stratege",
                "claude-opus-5-5",
                AgentDisplayState.DONE,
                Activity(kind=ActivityKind.DONE, label="Stufe 1 fertig"),
                turn=2,
                cost=1.40,
                results=1,
                channels=["werkstatt"],
            ),
            _agent(
                rid,
                "prover",
                "Beweiser",
                "claude-opus-5-5",
                AgentDisplayState.DONE,
                Activity(kind=ActivityKind.DONE, label="fertig"),
                turn=6,
                cost=8.00,
                results=3,
                channels=["werkstatt", "bewertung"],
            ),
            _agent(
                rid,
                "rater-1",
                "Rater",
                "claude-sonnet-5-5",
                AgentDisplayState.DONE,
                Activity(kind=ActivityKind.DONE, label="fertig"),
                turn=4,
                cost=1.90,
                ratings=4,
                channels=["bewertung"],
            ),
            _agent(
                rid,
                "rater-2",
                "Rater",
                "gpt-5.5",
                AgentDisplayState.DONE,
                Activity(kind=ActivityKind.DONE, label="fertig"),
                turn=4,
                tokens=286_000,
                ratings=4,
                channels=["bewertung"],
            ),
        ]
    }
    for name, members, count in [
        ("werkstatt", ["strategist", "prover"], 38),
        ("bewertung", ["prover", "rater-1", "rater-2"], 12),
    ]:
        run.channels[name] = _Channel(
            info=ChannelInfo(
                name=name,
                members=[ChannelMemberInfo(agent=m) for m in members],
                mode=ChannelMode.EVENT,
            ),
            generated=count,
            gen=_generated_message_factory(rid, name, members, _at(24, 9, 31), 150),
        )
    run.stages = [
        StageInfo(
            key="strategy",
            index=1,
            label="Strategie",
            status=StageStatus.DONE,
            agents=["strategist"],
            attempts=[Attempt(n=1, avg_score=8.0, pass_score=7, passed=True)],
        ),
        StageInfo(
            key="sketch",
            index=2,
            label="Skizze",
            status=StageStatus.DONE,
            agents=["prover"],
            attempts=[
                Attempt(n=1, avg_score=6.0, pass_score=7, passed=False),
                Attempt(n=2, avg_score=8.5, pass_score=7, passed=True),
            ],
        ),
        StageInfo(
            key="proof",
            index=3,
            label="Beweis",
            status=StageStatus.DONE,
            agents=["prover"],
            attempts=[Attempt(n=1, avg_score=8.0, pass_score=7, passed=True)],
        ),
        StageInfo(
            key="summary",
            index=4,
            label="Zusammenfassung",
            status=StageStatus.DONE,
            detail="output/summary.md",
        ),
    ]
    crit = ["Korrektheit", "Vollständigkeit", "Strenge", "Klarheit"]

    def ratings(a: list[float], b: list[float], ca: str, cb: str) -> list[RatingInfo]:
        return [
            RatingInfo(
                rater="rater-1",
                score=round(sum(a) / 4, 1),
                criteria=dict(zip(crit, a, strict=True)),
                comment=ca,
            ),
            RatingInfo(
                rater="rater-2",
                score=round(sum(b) / 4, 1),
                criteria=dict(zip(crit, b, strict=True)),
                comment=cb,
            ),
        ]

    run.results = [
        Result(
            id=1,
            ts=_at(24, 9, 52),
            agent="strategist",
            stage="strategy",
            stage_label="Strategie",
            kind=ResultKind.STRATEGY,
            title="Strategie",
            body_excerpt="Fallunterscheidung nach $n \\bmod 16$.",
            path="output/results/001-strategist-strategie.md",
            attempt=1,
            ratings=ratings([8, 8, 8, 8], [8, 8, 8, 8], "Tragfähig.", "Klarer Plan."),
            avg_score=8.0,
            pass_score=7,
            status=ResultStatus.ACCEPTED,
            transcript_seq=40,
        ),
        Result(
            id=2,
            ts=_at(24, 10, 15),
            agent="prover",
            stage="sketch",
            stage_label="Skizze",
            kind=ResultKind.SKETCH,
            title="Skizze",
            attempt=1,
            path="output/results/002-prover-skizze.md",
            ratings=ratings(
                [6, 5, 6, 7], [6, 6, 6, 6], "Fall n ≡ 7 fehlt.", "Rückrichtung unvollständig."
            ),
            avg_score=6.0,
            pass_score=7,
            status=ResultStatus.REWORK,
            transcript_seq=120,
        ),
        Result(
            id=3,
            ts=_at(24, 10, 41),
            agent="prover",
            stage="sketch",
            stage_label="Skizze",
            kind=ResultKind.SKETCH,
            title="Skizze",
            attempt=2,
            path="output/results/003-prover-skizze.md",
            ratings=ratings([9, 8, 8, 9], [8, 9, 8, 9], "Jetzt vollständig.", "Gut."),
            avg_score=8.5,
            pass_score=7,
            status=ResultStatus.ACCEPTED,
            transcript_seq=210,
        ),
        Result(
            id=4,
            ts=_at(24, 11, 5),
            agent="prover",
            stage="proof",
            stage_label="Beweis",
            kind=ResultKind.PROOF,
            title="Beweis",
            attempt=1,
            body_excerpt="**Beweis.** Sei $n = 16m + 3$ mit $m ≥ 0$. Dann gilt "
            "$$T(n) = 24m + 5,\\ T^2(n) = 36m + 8,\\ T^3(n) = 18m + 4,\\ T^4(n) = 9m + 2 < n$$",
            path="output/results/004-prover-beweis.md",
            ratings=ratings(
                [9, 8, 8, 8],
                [9, 8, 7, 7],
                "Vollständig und korrekt. Die Rückrichtung ließe sich in einer "
                "Tabelle der Restklassen zusammenfassen.",
                "Korrekt. Bitte die vier Iterationen als Tabelle und die Definition "
                "von σ nach Terras ausdrücklich zitieren.",
            ),
            avg_score=8.0,
            pass_score=7,
            status=ResultStatus.ACCEPTED,
            transcript_seq=301,
        ),
    ]
    run.summary_md = (
        "**Lemma 3 ist bewiesen:** Für $n > 1$ gilt $σ(n) = 4$ genau dann, wenn "
        "$n ≡ 3 \\pmod{16}$. Der Beweis rechnet die vier Schritte von $T$ für $n = 16m + 3$ aus "
        "und schließt die übrigen Restklassen modulo 16 aus.\n\n"
        "- **Strategie (8,0):** Fallunterscheidung nach $n \\bmod 16$.\n"
        "- **Skizze:** Versuch 1 (6,0) übersah den Fall $n ≡ 7 \\pmod{16}$. Versuch 2 (8,5) "
        "ergänzt ihn.\n"
        "- **Beweis (8,0):** rater-2 wünscht eine Tabelle der vier Iterationen.\n"
        "- **Offen:** Verallgemeinerung auf $σ(n) = k$."
    )
    run.file_changes = [
        AgentChanges(
            agent="prover",
            files=[
                FileChange(path="tex/lemma3.tex", added=84, removed=0),
                FileChange(path="notes/stoppzeit4.md", added=31, removed=0),
            ],
        ),
    ]
    run.handoff_extra = " und schlag vor, was wir nach tex/lemma3.tex übernehmen"
    for aid, state in run.agents.items():
        n = 60 + 50 * state.turn
        run.transcripts[aid] = _Transcript(
            total_generated=n,
            gen=_generated_transcript_factory(
                rid, aid, n, _at(24, 9, 30), _at(24, 11, 20), max(1, state.turn)
            ),
        )


def _build_ramsey_detail(run: _Run) -> None:
    rid = run.id
    run.agents = {
        a.id: a
        for a in [
            _agent(
                rid,
                "strategist",
                "Stratege",
                "claude-opus-5-5",
                AgentDisplayState.DONE,
                Activity(kind=ActivityKind.DONE, label="Stufe 1 fertig"),
                turn=3,
                results=1,
            ),
            _agent(
                rid,
                "prover",
                "Beweiser",
                "claude-opus-5-5",
                AgentDisplayState.IDLE,
                Activity(kind=ActivityKind.WAIT_MESSAGES, label="wartet auf deine Entscheidung"),
                turn=9,
                results=3,
            ),
            _agent(
                rid,
                "rater-1",
                "Rater",
                "claude-sonnet-5-5",
                AgentDisplayState.IDLE,
                Activity(kind=ActivityKind.WAIT_MESSAGES, label="nach Abgabe"),
                turn=4,
                ratings=4,
            ),
            _agent(
                rid,
                "rater-2",
                "Rater",
                "gpt-5.5",
                AgentDisplayState.IDLE,
                Activity(kind=ActivityKind.WAIT_MESSAGES, label="nach Abgabe"),
                turn=4,
                ratings=4,
            ),
        ]
    }
    run.stages = [
        StageInfo(
            key="strategy",
            index=1,
            label="Strategie",
            status=StageStatus.DONE,
            attempts=[Attempt(n=1, avg_score=8.0, pass_score=7, passed=True)],
        ),
        StageInfo(
            key="sketch",
            index=2,
            label="Skizze",
            status=StageStatus.WAITING_USER,
            attempts=[
                Attempt(n=i + 1, avg_score=s, pass_score=7, passed=False)
                for i, s in enumerate([4.5, 5.0, 5.5])
            ],
        ),
        StageInfo(key="proof", index=3, label="Beweis", status=StageStatus.TODO),
    ]
    run.channels["werkstatt"] = _Channel(
        info=ChannelInfo(
            name="werkstatt",
            members=[ChannelMemberInfo(agent=a) for a in ["strategist", "prover"]],
            mode=ChannelMode.EVENT,
        ),
        generated=24,
        gen=_generated_message_factory(
            rid, "werkstatt", ["strategist", "prover"], _at(24, 13, 6), 240
        ),
    )
    run.results = [
        Result(
            id=1,
            ts=_at(24, 13, 30),
            agent="strategist",
            stage="strategy",
            stage_label="Strategie",
            kind=ResultKind.STRATEGY,
            title="Strategie",
            avg_score=8.0,
            pass_score=7,
            status=ResultStatus.ACCEPTED,
        ),
    ] + [
        Result(
            id=2 + i,
            ts=_at(24, 14, 0) + i * 1200,
            agent="prover",
            stage="sketch",
            stage_label="Skizze",
            kind=ResultKind.SKETCH,
            title="Skizze",
            attempt=i + 1,
            avg_score=s,
            pass_score=7,
            status=ResultStatus.FAILED if i == 2 else ResultStatus.REWORK,
        )
        for i, s in enumerate([4.5, 5.0, 5.5])
    ]


def _build_review_detail(run: _Run) -> None:
    rid = run.id
    run.agents = {
        a.id: a
        for a in [
            _agent(
                rid,
                aid,
                role,
                model,
                AgentDisplayState.IDLE,
                Activity(kind=ActivityKind.WAIT_STAGE, label="startet nach deiner Freigabe"),
                web=aid != "author",
                read_dirs=["~/Documents/Mathe-Literatur"],
                turns_limit=60,
            )
            for aid, role, model in [
                ("author", "Autor", "claude-opus-5-5"),
                ("judge-1", "Judge", "claude-opus-5-5"),
                ("judge-2", "Judge", "gpt-5.5"),
                ("judge-3", "Judge", "claude-opus-5-5"),
            ]
        ]
    }
    run.channels = {
        "author-desk": _Channel(
            info=ChannelInfo(
                name="author-desk",
                mode=ChannelMode.EVENT,
                members=[ChannelMemberInfo(agent="author")],
            )
        ),
        "judges": _Channel(
            info=ChannelInfo(
                name="judges",
                mode=ChannelMode.ROUNDS,
                anonymous=True,
                open=False,
                opens_at_stage="Diskussion",
                members=[
                    ChannelMemberInfo(agent=f"judge-{i}", alias=f"Mitglied {'ABC'[i - 1]}")
                    for i in (1, 2, 3)
                ],
            )
        ),
    }
    run.stages = [
        StageInfo(
            key="artifact",
            index=0,
            label="Artefakt",
            status=StageStatus.TODO,
            detail="tex/lemma3.tex",
        ),
        StageInfo(key="review-1", index=1, label="Prüfung", status=StageStatus.TODO, round=1),
        StageInfo(
            key="discussion-1", index=2, label="Diskussion", status=StageStatus.TODO, round=1
        ),
        StageInfo(key="verdict-1", index=3, label="Urteil", status=StageStatus.TODO, round=1),
    ]
    run.results_empty_text = "Die Judges geben ihre Gutachten in Stufe „Prüfung“ ab."


def _build_generic_detail(run: _Run) -> None:
    """Small but valid detail data for runs without hand-made data."""
    rid = run.id
    roles = {
        TopologyKind.EXPLORATION: [
            ("explorer-1", "Explorer"),
            ("explorer-2", "Explorer"),
            ("ranker", "Ranker"),
        ],
        TopologyKind.WATERFALL: [
            ("strategist", "Stratege"),
            ("prover", "Beweiser"),
            ("rater-1", "Rater"),
        ],
        TopologyKind.REVIEW: [("author", "Autor"), ("judge-1", "Judge"), ("judge-2", "Judge")],
        TopologyKind.ROUNDTABLE: [
            ("member-1", "Mitglied"),
            ("member-2", "Mitglied"),
            ("member-3", "Mitglied"),
        ],
        TopologyKind.SOLO: [("solver", "Löser")],
    }[run.topology]
    final = run.status.is_final
    state = AgentDisplayState.DONE if final else AgentDisplayState.WORKING
    if run.status is RunStatus.FAILED:
        state = AgentDisplayState.ERROR
    run.agents = {
        aid: _agent(
            rid,
            aid,
            role,
            "claude-opus-5-5",
            state,
            Activity(
                kind=ActivityKind.DONE if final else ActivityKind.TOOL,
                label="fertig" if final else "Bash",
                arg=None if final else "python check.py",
                running=not final,
            ),
            turn=6,
            channels=["main"],
        )
        for aid, role in roles
    }
    members = [aid for aid, _ in roles]
    start = run.started_at or run.created_at
    run.channels = {
        "main": _Channel(
            info=ChannelInfo(
                name="main",
                members=[ChannelMemberInfo(agent=m) for m in members],
                mode=ChannelMode.EVENT,
            ),
            generated=20,
            gen=_generated_message_factory(rid, "main", members, start, 60),
        )
    }
    end = run.ended_at or start + 3600
    for aid in members:
        run.transcripts[aid] = _Transcript(
            total_generated=120,
            gen=_generated_transcript_factory(rid, aid, 120, start, end, 6),
        )
    run.stages = [
        StageInfo(
            key="work",
            index=1,
            label="Arbeit",
            status=StageStatus.DONE if final else StageStatus.RUNNING,
        ),
        StageInfo(
            key="summary",
            index=2,
            label="Zusammenfassung",
            status=StageStatus.DONE if run.status is RunStatus.DONE else StageStatus.TODO,
        ),
    ]
    if run.status is RunStatus.DONE:
        run.summary_md = f"**{run.name}** ist abgeschlossen. Die Ergebnisse stehen unten."
        run.results = [
            Result(
                id=1,
                ts=end - 600,
                agent=members[0],
                stage="work",
                stage_label="Arbeit",
                kind=ResultKind.OTHER,
                title="Ergebnis",
                avg_score=7.5,
                pass_score=7,
                ratings=[RatingInfo(rater=members[-1], score=7.5, comment="Solide.")],
                status=ResultStatus.ACCEPTED,
                path="output/results/001.md",
            ),
        ]


# --------------------------------------------------------------------------------------------
# Data set: load (docs/08 §9.2)
# --------------------------------------------------------------------------------------------

R_SATZ2 = "20261024-0800_collatz_proof_d41a"


def _load_runs(now: float) -> dict[str, _Run]:
    runs: dict[str, _Run] = {}
    runs[R_SATZ2] = _Run(
        id=R_SATZ2,
        name="Satz 2 – Wasserfall mit 12 Agenten",
        project="collatz",
        skill="proof",
        topology=TopologyKind.WATERFALL,
        status=RunStatus.RUNNING,
        starter=Starter.USER_AGENT,
        created_at=_at(24, 8, 0),
        started_at=_at(24, 8, 0),
        stage=StageRef(index=4, count=6, label="Hauptbeweis", detail="V1 läuft"),
        wall_time_s=8 * 3600,
        budget_usd=120,
        spent_usd=61.20,
        turns_per_agent=60,
        max_turns_used=31,
        tokens_total=9_400_000,
        size=_size(6.2),
        info="Hauptbeweis (4 von 6) · 12 Agenten",
        cost_rate=61.20 / (401 * 60),
        token_rate=9_400_000 / (401 * 60),
    )
    actives = [
        (
            "20261024-1402_partitionen_proof-review_77c1",
            "Partitionen – Review",
            "partitionen",
            "proof-review",
            TopologyKind.REVIEW,
            RunStatus.AWAITING_APPROVAL,
            _at(24, 14, 2),
        ),
        (
            "20261024-1110_ramsey-schranken_proof_31d0",
            "R(5,5) – Schranken verbessern",
            "ramsey-schranken",
            "proof",
            TopologyKind.WATERFALL,
            RunStatus.PAUSED,
            _at(24, 11, 10),
        ),
        (
            "20261024-1230_collatz_exploration_90aa",
            "Goldbach-Stichproben – Exploration",
            "collatz",
            "exploration",
            TopologyKind.EXPLORATION,
            RunStatus.RUNNING,
            _at(24, 12, 30),
        ),
        (
            "20261024-1315_partitionen_roundtable_5e2f",
            "Partitionen – Roundtable",
            "partitionen",
            "roundtable",
            TopologyKind.ROUNDTABLE,
            RunStatus.RUNNING,
            _at(24, 13, 15),
        ),
    ]
    for rid, name, project, skill, topo, status, start in actives:
        runs[rid] = _Run(
            id=rid,
            name=name,
            project=project,
            skill=skill,
            topology=topo,
            status=status,
            starter=Starter.USER_AGENT if status is not RunStatus.RUNNING else Starter.GUI,
            created_at=start,
            started_at=None if status is RunStatus.AWAITING_APPROVAL else start,
            status_since=start + 3600 if status is RunStatus.PAUSED else None,
            elapsed_frozen=3600 if status is RunStatus.PAUSED else None,
            spent_usd=None if status is RunStatus.AWAITING_APPROVAL else 4.2,
            tokens_total=None if status is RunStatus.AWAITING_APPROVAL else 700_000,
            size=_size(0 if status is RunStatus.AWAITING_APPROVAL else 1.1),
            cost_rate=0.001 if status is RunStatus.RUNNING else 0.0,
            token_rate=150 if status is RunStatus.RUNNING else 0.0,
            stage=StageRef(index=1, count=3, label="Arbeit")
            if status is RunStatus.RUNNING
            else None,
        )
    statuses = [RunStatus.DONE] * 7 + [RunStatus.STOPPED, RunStatus.FAILED, RunStatus.INTERRUPTED]
    projects = ["collatz", "ramsey-schranken", "partitionen"]
    topos = [
        ("exploration", TopologyKind.EXPLORATION),
        ("proof", TopologyKind.WATERFALL),
        ("proof-review", TopologyKind.REVIEW),
        ("roundtable", TopologyKind.ROUNDTABLE),
        (None, TopologyKind.SOLO),
    ]
    for i in range(195):
        rng = random.Random(f"load:{i}")
        start = now - (i + 1) * 0.9 * 86400 - rng.randint(0, 40000)  # spread over ~6 months
        project = projects[i % 3]
        skill, topo = topos[i % 5]
        status = statuses[i % len(statuses)]
        rid = (
            datetime.fromtimestamp(start).strftime("%Y%m%d-%H%M")
            + f"_{project}_{skill or 'custom'}_{rng.randrange(16**4):04x}"
        )
        dur = rng.randint(5, 240) * 60
        gb = rng.uniform(0.02, 4.5)
        runs[rid] = _Run(
            id=rid,
            name=f"{['Collatz', 'Ramsey', 'Partitionen'][i % 3]} – Lauf {195 - i}",
            project=project,
            skill=skill,
            topology=topo,
            status=status,
            starter=[Starter.GUI, Starter.USER_AGENT, Starter.CLI][i % 3],
            created_at=start,
            started_at=start,
            ended_at=start + dur,
            spent_usd=round(rng.uniform(0.2, 30), 2),
            tokens_total=rng.randint(30, 5000) * 1000,
            size=_size(gb),
            pinned=i % 37 == 0,
            slimmed=i % 9 == 4,
            archived=i > 150 and i % 4 == 0,
            info="Protokoll lesbar" if status is RunStatus.INTERRUPTED else None,
            error=RunError(
                message="Run fehlgeschlagen: Ein Agent hat sein Limit überschritten.", action=None
            )
            if status is RunStatus.FAILED
            else None,
        )
    return runs


def _load_approvals(base: str, runs: dict[str, _Run]) -> dict[str, ApprovalItem]:
    def ref(rid: str) -> ApprovalRunRef:
        r = runs[rid]
        return ApprovalRunRef(id=rid, name=r.name, project=r.project, gui_url=f"{base}/runs/{rid}")

    def agent(aid: str, model: str = "claude-opus-5-5") -> ApprovalAgentRef:
        provider = _provider_for(model)
        return ApprovalAgentRef(
            id=aid,
            role_label=aid.split("-")[0].capitalize(),
            model=model,
            model_label=_model_label(model),
            provider=provider,
            vendor=_vendor(provider),
        )

    def permission(
        aid_: str,
        rid: str,
        agent_id: str,
        title: str,
        command: str,
        cat: RequestCategory,
        created: float,
        deadline: float,
        model: str = "claude-opus-5-5",
    ) -> ApprovalItem:
        who = agent(agent_id, model)
        return ApprovalItem(
            id=aid_,
            type=ApprovalType.PERMISSION,
            created_at=created,
            gui_url=f"{base}/freigaben/{aid_}",
            title=title,
            run=ref(rid),
            deadline=deadline,
            agent=who,
            tool="Bash",
            command=command,
            work_dir=f"…/runs/{rid}/agents/{agent_id}/work",
            reason="Ich brauche das für den nächsten Rechenschritt.",
            risk=RiskInfo(level=RiskLevel.MEDIUM, text="Die Anfrage verlässt die Sandbox."),
            categories=[cat],
            escalated_by=EscalatedByInfo(
                decider=DecidedBy.GATEKEEPER,
                note="Der Aufsichts-Agent ist unsicher und fragt dich.",
            ),
            consequences=Consequences(
                allow="Der Befehl läuft einmal.",
                deny=f"{agent_id} arbeitet ohne weiter.",
                expire="Ohne Entscheidung gilt die Anfrage als abgelehnt.",
            ),
            content_to=[who.vendor] if cat is RequestCategory.READ_OUTSIDE else [],
            rule_label=command,
        )

    partition_agents = [
        _run_start_agent("author", "Autor", "claude-opus-5-5", web=False),
        _run_start_agent("judge-1", "Judge", "claude-opus-5-5", web=False),
        _run_start_agent("judge-2", "Judge", "gpt-5.5", web=False),
    ]
    items = [
        permission(
            "apr_1201",
            R_SATZ2,
            "prover-3",
            "prover-3 möchte sagemath-standard installieren",
            "pip install sagemath-standard",
            RequestCategory.INSTALL,
            _at(24, 14, 30),
            _at(24, 14, 50),
        ),
        permission(
            "apr_1202",
            "20261024-1230_collatz_exploration_90aa",
            "explorer-1",
            "explorer-1 möchte eine Datei von primes.utm.edu laden",
            "wget https://primes.utm.edu/lists/small/millions/primes1.zip",
            RequestCategory.NETWORK,
            _at(24, 14, 33),
            _at(24, 14, 53),
        ),
        permission(
            "apr_1203",
            "20261024-1230_collatz_exploration_90aa",
            "explorer-gpt",
            "explorer-gpt möchte eine Datei außerhalb lesen",
            "cat ~/Documents/Goldbach-Daten/stichprobe.csv",
            RequestCategory.READ_OUTSIDE,
            _at(24, 14, 35),
            _at(24, 14, 55),
            model="gpt-5.5",
        ),
        permission(
            "apr_1204",
            "20261024-1315_partitionen_roundtable_5e2f",
            "member-2",
            "member-2 möchte außerhalb des Arbeitsordners schreiben",
            "cp tabelle.csv ~/Documents/Partitionen-Daten/",
            RequestCategory.WRITE_OUTSIDE,
            _at(24, 14, 36),
            _at(24, 14, 56),
        ),
        ApprovalItem(
            id="apr_1205",
            type=ApprovalType.RUN_START,
            created_at=_at(24, 14, 2),
            gui_url=f"{base}/freigaben/apr_1205",
            title="proof-review: Partitionen – Review",
            run=ref("20261024-1402_partitionen_proof-review_77c1"),
            requested_by=Starter.USER_AGENT,
            skill="proof-review",
            topology=TopologyKind.REVIEW,
            params={"judges": 2},
            task_md="Prüfe den Beweis von $p(5n+4) ≡ 0 \\pmod 5$ in `tex/ramanujan.tex`.",
            agents=partition_agents,
            limits=LimitsInfo(wall_time_s=6 * 3600, turns_per_agent=40, budget_usd=20),
            decider=Decider.GATEKEEPER_THEN_USER,
            estimate=Estimate(
                turns=Range(min=40, max=70),
                cost_usd=Range(min=6, max=11),
                cost_kind=CostKind.ESTIMATED,
                duration_s=Range(min=3600, max=2 * 3600),
            ),
            deviations=[
                Deviation(
                    path="defaults.read_dirs",
                    label="Zusätzlicher Lese-Ordner",
                    value_text="~/Documents/Partitionen-Daten für alle Agenten",
                    default_text="keiner",
                    default=[],
                    value=["~/Documents/Partitionen-Daten"],
                    sensitive=True,
                    providers=_shares(partition_agents),
                ),
                Deviation(
                    path="limits.wall_time",
                    label="Laufzeit 6 h",
                    value_text="",
                    default_text="2 h",
                    default=7200,
                    value=21600,
                    sensitive=False,
                ),
            ],
            standard_notes=[_copy_note("partitionen", partition_agents), "Web-Zugriff", "Budget"],
        ),
        ApprovalItem(
            id="apr_1206",
            type=ApprovalType.ESCALATION,
            created_at=_at(24, 12, 10),
            gui_url=f"{base}/freigaben/apr_1206",
            title="Stufe „Konstruktion“ nach 3 Versuchen nicht bestanden",
            run=ref("20261024-1110_ramsey-schranken_proof_31d0"),
            stage="Konstruktion",
            attempts=[
                Attempt(n=i + 1, avg_score=s, pass_score=7, passed=False)
                for i, s in enumerate([5.0, 6.0, 6.5])
            ],
            last_ratings=[
                LastRating(
                    rater="rater-1",
                    score=6.5,
                    comment="Die Färbung ist nicht vollständig angegeben.",
                )
            ],
            options=_escalation_options("prover", "Konstruktion", "Beweis", 3),
            topology=TopologyKind.WATERFALL,
            comment_to=agent("prover"),
        ),
    ]
    history = []
    for i in range(40):
        rid = list(runs)[10 + i]
        decided_by = [
            DecidedBy.GATEKEEPER,
            DecidedBy.USER,
            DecidedBy.MANDATE_RULE,
            DecidedBy.FIXED_RULE,
        ][i % 4]
        action = DecisionAction.DENY if decided_by is DecidedBy.FIXED_RULE else DecisionAction.ALLOW
        scope = (
            DecisionScope.RUN
            if decided_by is DecidedBy.USER and i % 8 == 1
            else (DecisionScope.ONCE)
        )
        fixed = decided_by is DecidedBy.FIXED_RULE
        rationale = {
            DecidedBy.GATEKEEPER: "Ein Mathe-Paket von PyPI. Das deckt deine Vollmacht.",
            DecidedBy.MANDATE_RULE: "pypi.org steht in deiner Vollmacht.",
            DecidedBy.FIXED_RULE: "Sensible Pfade sind gesperrt.",
        }.get(decided_by)
        decision = DecisionInfo(
            action=action,
            scope=scope,
            decided_by=decided_by,
            decided_at=runs[rid].created_at + 600,
            rationale=rationale,
        )
        history.append(
            ApprovalItem(
                id=f"apr_{1100 - i}",
                type=ApprovalType.PERMISSION,
                status=ApprovalStatus.DECIDED,
                created_at=runs[rid].created_at + 500,
                gui_url=f"{base}/freigaben/apr_{1100 - i}",
                title="prover-1 möchte einen SSH-Schlüssel lesen"
                if fixed
                else f"prover-1 möchte paket-{i} installieren",
                run=ref(rid),
                agent=agent("prover-1"),
                tool="Bash",
                command="cat ~/.ssh/config" if fixed else f"pip install paket-{i}",
                risk=RiskInfo(level=RiskLevel.HIGH, text="Zugriff auf einen sensiblen Pfad.")
                if fixed
                else RiskInfo(level=RiskLevel.LOW, text="Installation in die Run-venv."),
                categories=[RequestCategory.READ_OUTSIDE if fixed else RequestCategory.INSTALL],
                content_to=[Vendor.ANTHROPIC] if fixed else [],
                decision=decision,
                revocable=decided_by is DecidedBy.GATEKEEPER
                or (decided_by is DecidedBy.USER and scope is DecisionScope.RUN),
            )
        )
    return {item.id: item for item in [*items, *history]}


def _build_satz2_detail(run: _Run) -> None:
    rid = run.id
    A = AgentDisplayState  # noqa: N806
    W = ActivityKind  # noqa: N806
    spec = [
        (
            "prover-3",
            "Beweiser",
            "claude-opus-5-5",
            A.WAITING_USER,
            Activity(
                kind=W.WAIT_APPROVAL,
                label="Bash",
                arg="pip install sagemath-standard",
                since=_at(24, 14, 30),
            ),
            BlockedBy(
                kind=BlockedKind.APPROVAL,
                approval_id="apr_1201",
                summary="Installation: sagemath-standard",
                deadline=_at(24, 14, 50),
            ),
        ),
        (
            "critic-2",
            "Kritiker",
            "claude-sonnet-5-5",
            A.IDLE,
            Activity(kind=W.WAIT_MESSAGES, label="wartet auf deine Antwort"),
            None,
        ),
        (
            "prover-1",
            "Beweiser",
            "claude-opus-5-5",
            A.WORKING,
            Activity(
                kind=W.TOOL,
                label="Bash",
                arg="sage beweis_teil2.sage",
                since=_at(24, 14, 38),
                running=True,
            ),
            None,
        ),
        (
            "prover-2",
            "Beweiser",
            "claude-opus-5-5",
            A.WORKING,
            Activity(
                kind=W.TOOL, label="Edit", arg="tex/satz2.tex", since=_at(24, 14, 40), running=True
            ),
            None,
        ),
        (
            "prover-gpt",
            "Beweiser",
            "gpt-5.5",
            A.WORKING,
            Activity(
                kind=W.TOOL,
                label="Shell",
                arg="python fall_c.py",
                since=_at(24, 14, 39),
                running=True,
            ),
            None,
        ),
        (
            "critic-1",
            "Kritiker",
            "claude-sonnet-5-5",
            A.WORKING,
            Activity(
                kind=W.TOOL, label="liest", arg="Anhang #9812", since=_at(24, 14, 40), running=True
            ),
            None,
        ),
        (
            "rater-gpt",
            "Rater",
            "gpt-5.5",
            A.BLOCKED,
            Activity(
                kind=W.BLOCKED,
                label="Aufsichts-Agent prüft",
                arg="Lesen ~/Documents/Mathe-Literatur/tao-2019.pdf",
            ),
            BlockedBy(
                kind=BlockedKind.GATEKEEPER,
                summary="Aufsichts-Agent prüft: Lesen ~/Documents/Mathe-Literatur/tao-2019.pdf",
            ),
        ),
        (
            "formalizer",
            "Formalisierer",
            "claude-opus-5-5",
            A.IDLE,
            Activity(kind=W.WAIT_STAGE, label="ab Stufe 5"),
            None,
        ),
        (
            "rater-1",
            "Rater",
            "claude-sonnet-5-5",
            A.IDLE,
            Activity(kind=W.WAIT_MESSAGES, label="nach Abgabe"),
            None,
        ),
        (
            "rater-2",
            "Rater",
            "claude-sonnet-5-5",
            A.IDLE,
            Activity(kind=W.WAIT_MESSAGES, label="nach Abgabe"),
            None,
        ),
        (
            "summarizer",
            "Zusammenfasser",
            "claude-haiku-5-5",
            A.IDLE,
            Activity(kind=W.WAIT_STAGE, label="ab Stufe 6"),
            None,
        ),
        (
            "strategist",
            "Stratege",
            "claude-opus-5-5",
            A.DONE,
            Activity(kind=W.DONE, label="Stufe 1 fertig"),
            None,
        ),
    ]
    run.agents = {}
    for aid, role, model, state, act, blocked in spec:
        run.agents[aid] = _agent(
            rid,
            aid,
            role,
            model,
            state,
            act,
            blocked_by=blocked,
            since=act.since,
            turn=31 if aid == "prover-1" else 12,
            turns_limit=60,
            channels=["werkstatt", "kritik"],
            questions_to_user=1 if aid == "critic-2" else 0,
            cost=None if model.startswith("gpt") else 4.1,
            tokens=640_000,
        )
    werk_members = ["prover-1", "prover-2", "prover-3", "prover-gpt", "critic-1", "strategist"]
    werk = _Channel(
        info=ChannelInfo(
            name="werkstatt",
            purpose="Beweisarbeit an Satz 2",
            members=[ChannelMemberInfo(agent=a) for a in werk_members],
            mode=ChannelMode.EVENT,
            open_questions=2,
            unread_for_user=12,
        ),
        generated=9870,
        gen=_generated_message_factory(rid, "werkstatt", werk_members, _at(24, 8, 1), 2.4),
    )
    werk.fixed = {
        9871: _msg(
            "werkstatt",
            9871,
            _at(24, 14, 41),
            "prover-1",
            MessageKind.CLAIM,
            "Fall C reduziert sich auf $n ≡ 27 \\pmod{32}$. Sage-Rechnung bis $2^{24}$ "
            "ohne Gegenbeispiel.",
            refs=[
                AttachmentInfo(
                    name="fall_c.sage", size=4200, path="groupchats/attachments/9871/fall_c.sage"
                )
            ],
        ),
        9872: _msg(
            "werkstatt",
            9872,
            _at(24, 14, 44),
            "critic-2",
            MessageKind.QUESTION,
            "Warum genügt $2^{24}$? Wo steht die Schranke, ab der die Restklasse nicht "
            "mehr wächst?",
            to=["prover-1"],
            open=True,
        ),
        9873: _msg(
            "werkstatt",
            9873,
            _at(24, 14, 46),
            "prover-gpt",
            MessageKind.ANSWER,
            "Unabhängig nachgerechnet: Fall C stimmt bis $2^{24}$, Laufzeit 4 min.",
            reply_to=9871,
        ),
        9874: _msg("werkstatt", 9874, _at(24, 14, 47), "prover-2", MessageKind.PASS, "passt"),
        9875: _msg(
            "werkstatt",
            9875,
            _at(24, 14, 52),
            "prover-2",
            MessageKind.PROOF,
            "Lemma 4.2 vollständig, siehe Anhang. Damit sind die Fälle A–C geschlossen, "
            "offen bleibt D.",
            refs=[
                AttachmentInfo(
                    name="lemma_4_2.md",
                    size=1_400_000,
                    path="groupchats/attachments/9875/lemma_4_2.md",
                )
            ],
        ),
    }
    gen_tail = _generated_message_factory(rid, "werkstatt", werk_members, _at(24, 14, 53), 20)
    for i in range(9876, 9883):
        m = gen_tail(i - 9875)
        werk.fixed[i] = m.model_copy(update={"id": i, "ts": _at(24, 14, 53) + (i - 9876) * 30})
    kritik_members = ["critic-1", "critic-2", "prover-1", "prover-2"]
    kritik = _Channel(
        info=ChannelInfo(
            name="kritik",
            purpose="Einwände sammeln",
            members=[ChannelMemberInfo(agent=a) for a in kritik_members],
            mode=ChannelMode.EVENT,
            open_questions=1,
            unread_for_user=3,
        ),
        generated=95,
        gen=_generated_message_factory(rid, "kritik", kritik_members, _at(24, 8, 5), 240),
    )
    kritik.fixed = {
        96: _msg(
            "kritik",
            96,
            _at(24, 14, 35),
            "critic-2",
            MessageKind.QUESTION,
            "Soll Fall D in diesem Run bearbeitet werden?",
            to=["user"],
            open=True,
        ),
    }
    bewertung = _Channel(
        info=ChannelInfo(
            name="bewertung",
            members=[ChannelMemberInfo(agent=a) for a in ["rater-1", "rater-2", "rater-gpt"]],
            mode=ChannelMode.ROUNDS,
        ),
        generated=22,
        gen=_generated_message_factory(
            rid, "bewertung", ["rater-1", "rater-2", "rater-gpt"], _at(24, 9, 0), 900
        ),
    )
    formal = _Channel(
        info=ChannelInfo(
            name="formal",
            members=[ChannelMemberInfo(agent="formalizer")],
            mode=ChannelMode.EVENT,
            open=False,
            opens_at_stage="Formalisierung",
        )
    )
    run.channels = {"werkstatt": werk, "kritik": kritik, "bewertung": bewertung, "formal": formal}
    run.stages = [
        StageInfo(
            key="strategy",
            index=1,
            label="Strategie",
            status=StageStatus.DONE,
            attempts=[Attempt(n=1, avg_score=8.0, pass_score=7, passed=True)],
        ),
        StageInfo(
            key="lemma-a",
            index=2,
            label="Lemma A",
            status=StageStatus.DONE,
            attempts=[Attempt(n=1, avg_score=7.5, pass_score=7, passed=True)],
        ),
        StageInfo(
            key="lemma-b",
            index=3,
            label="Lemma B",
            status=StageStatus.DONE,
            attempts=[
                Attempt(n=1, avg_score=5.5, pass_score=7, passed=False),
                Attempt(n=2, avg_score=8.0, pass_score=7, passed=True),
            ],
        ),
        StageInfo(
            key="main",
            index=4,
            label="Hauptbeweis",
            status=StageStatus.RUNNING,
            attempts=[Attempt(n=1)],
        ),
        StageInfo(
            key="formal",
            index=5,
            label="Formalisierung",
            status=StageStatus.TODO,
            agents=["formalizer"],
            detail="formalizer",
        ),
        StageInfo(
            key="summary",
            index=6,
            label="Zusammenfassung",
            status=StageStatus.TODO,
            agents=["summarizer"],
            detail="summarizer",
        ),
    ]
    run.results = [
        Result(
            id=i + 1,
            ts=_at(24, 9, 0) + i * 3600,
            agent="prover-1",
            stage=key,
            stage_label=lab,
            kind=ResultKind.PROOF,
            title=lab,
            attempt=att,
            avg_score=score,
            pass_score=7,
            status=ResultStatus.ACCEPTED if score >= 7 else ResultStatus.REWORK,
        )
        for i, (key, lab, att, score) in enumerate(
            [
                ("strategy", "Strategie", 1, 8.0),
                ("lemma-a", "Lemma A", 1, 7.5),
                ("lemma-b", "Lemma B", 1, 5.5),
                ("lemma-b", "Lemma B", 2, 8.0),
                ("main", "Hauptbeweis", 1, 0.0),
            ]
        )
    ]
    run.results[-1] = run.results[-1].model_copy(
        update={"avg_score": None, "status": ResultStatus.UNRATED}
    )
    run.transcripts["prover-1"] = _Transcript(
        total_generated=50_000,
        gen=_generated_transcript_factory(
            rid, "prover-1", 50_000, _at(24, 8, 0), _at(24, 14, 41), 31
        ),
    )
    for aid in run.agents:
        if aid != "prover-1":
            run.transcripts[aid] = _Transcript(
                total_generated=2_000,
                gen=_generated_transcript_factory(
                    rid, aid, 2_000, _at(24, 8, 0), _at(24, 14, 41), 12
                ),
            )


# --------------------------------------------------------------------------------------------
# Resolving a new run (docs/08 §5.4, simulated `estimate_run`)
# --------------------------------------------------------------------------------------------

_DECIDER_TEXT = {
    Decider.GATEKEEPER_THEN_USER.value: "Aufsichts-Agent, bei Zweifel du",
    Decider.USER.value: "immer du",
    Decider.GATEKEEPER.value: "nur Aufsichts-Agent",
}


def _num(value: float | None) -> str:
    """`16` → „16“, `12.5` → „12,5“ (for messages)."""
    if value is None:
        return "–"
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}".replace(".", ",")


def _limit_text(key: str, value: float) -> str:
    """A limit as the GUI writes it: „6 h“, „90 min“, „60“, „30 $“."""
    if key == "wall_time_s":
        return f"{_num(value / 3600)} h" if value % 1800 == 0 else f"{int(value // 60)} min"
    if key == "budget_usd":
        return f"{_num(value)} $"
    return _num(value)


def _id_group(ids: list[str]) -> str:
    """„judge-*“ when all IDs share a prefix with a number, else the IDs with commas."""
    prefixes = {i.rsplit("-", 1)[0] for i in ids}
    if len(ids) > 1 and len(prefixes) == 1:
        return f"{prefixes.pop()}-*"
    return ",".join(ids)


def _resolve_channels(skill: str | None, agents: list[RunStartAgent]) -> list[RunStartChannel]:
    """Channels of a skill with their resolved members (docs/06)."""
    ids = [a.id for a in agents]

    def having(*prefixes: str) -> list[str]:
        return [i for i in ids if i.startswith(prefixes)]

    if skill == "exploration":
        return [
            RunStartChannel(
                name="main", members=having("explorer", "critic"), mode=ChannelMode.EVENT
            ),
            RunStartChannel(
                name="ranking",
                members=having("ranker", "explorer"),
                mode=ChannelMode.ROUNDS,
                open_from="Bewertung",
            ),
        ]
    if skill == "proof":
        return [
            RunStartChannel(
                name="werkstatt", members=having("strategist", "prover"), mode=ChannelMode.EVENT
            ),
            RunStartChannel(
                name="bewertung", members=having("prover", "rater"), mode=ChannelMode.EVENT
            ),
        ]
    if skill == "proof-review":
        return [
            RunStartChannel(name="author-desk", members=["author"], mode=ChannelMode.EVENT),
            RunStartChannel(
                name="judges",
                members=having("judge", "meta-judge"),
                mode=ChannelMode.ROUNDS,
                anonymous=True,
                open_from="Diskussion",
            ),
        ]
    if skill == "roundtable":
        return [RunStartChannel(name="main", members=ids, mode=ChannelMode.ROUNDS)]
    return []


def _estimate(skill: str | None, params: dict[str, Any], agents: list[RunStartAgent]) -> Estimate:
    """Ranges for turns, cost and duration, like `estimate_run` (simulated)."""
    if skill == "exploration":
        ideas = len([i for i in params.get("ideas") or [] if str(i).strip()]) or 3
        work = ideas * int(params.get("turns_per_idea") or 6)
        turns = (work + 10, int(work * 1.5) + 20)
        cost = (round(work * 0.5), round(work * 0.9))
        minutes = (work * 5 // ideas, work * 10 // ideas)
    elif skill == "proof":
        rework = int(params.get("max_rework") or 0)
        turns = (40, 60 + 15 * rework)
        cost = (10, 14 + 2 * rework)
        minutes = (90, 150 + 30 * rework)
    elif skill == "proof-review":
        judges = int(params.get("judges") or 3)
        turns = (20 * judges, 30 * judges)
        cost = (round(2.7 * judges), round(4.7 * judges))
        minutes = (40, 70)
    elif skill == "roundtable":
        n = int(params.get("members") or 3) * int(params.get("rounds") or 3)
        turns = (n, 2 * n)
        cost = (round(n * 0.4), round(n * 0.8))
        minutes = (int(params.get("rounds") or 3) * 10, int(params.get("rounds") or 3) * 20)
    else:
        turns, cost, minutes = (10, 40), (1, 4), (10, 40)
    claude_sub = any(a.provider is Provider.CLAUDE for a in agents)
    return Estimate(
        turns=Range(min=turns[0], max=turns[1]),
        cost_usd=Range(min=cost[0], max=max(cost[0], cost[1])),
        cost_kind=CostKind.ESTIMATED if claude_sub else None,
        duration_s=Range(min=minutes[0] * 60, max=minutes[1] * 60),
    )


def _run_yaml(
    run_input: dict[str, Any],
    skill: SkillInfo | None,
    agents: list[RunStartAgent],
    settings: WorkfolderSettingsInfo,
) -> str:
    """The resolved `run.yaml` as the preview shows it (read only)."""
    limits = {**settings.limits.model_dump(), **(run_input.get("limits") or {})}
    lines = [
        "version: 1",
        f'name: "{run_input.get("name") or ""}"',
        f"project: {run_input.get('project') or ''}",
        f"skill: {skill.name if skill else '~'}",
        "topology:",
        f"  kind: {skill.topology.value if skill and skill.topology else 'solo'}",
    ]
    params = run_input.get("params") or {}
    if params:
        lines.append("  params:")
        lines += [
            f"    {k}: {v!r}" if isinstance(v, str) else f"    {k}: {v}" for k, v in params.items()
        ]
    wall = limits.get("wall_time_s")
    lines.append(
        "limits: { "
        + f"wall_time: {_limit_text('wall_time_s', wall) if wall else '~'}, "
        + f"turns_per_agent: {limits.get('turns_per_agent')}, "
        + f"budget_usd: {_num(limits.get('budget_usd'))} }}"
    )
    decider = run_input.get("decider") or settings.approvals.decider.value
    lines.append(f"approvals: {{ decider: {decider} }}")
    lines.append("agents:")
    for a in agents:
        lines += [
            f"  - id: {a.id}",
            f"    provider: {a.provider.value}",
            f"    model: {a.model}",
            f"    auth: {a.profile}",
            f"    tools: {{ shell: true, web: {str(a.tools.web).lower()} }}",
        ]
        if a.read_dirs:
            lines.append(f"    read_dirs: [{', '.join(a.read_dirs)}]")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------------
# Data set: acceptance test (docs/08 §9.2 Nr. 3)
# --------------------------------------------------------------------------------------------
# Same structure as the scenario, other names and contents, so that UX-Abnahme 2 measures
# understanding, not recognition. Built from the scenario: every run detail is built, then all
# texts pass through `_Translator` (exact texts first, then patterns for names and IDs); a few
# objects get their own values afterwards (`_abnahme_overrides`).


class _Translator:
    """Deep text replacement over models, dataclasses and containers (enums stay as they are)."""

    def __init__(self, exact: dict[str, str], patterns: list[tuple[str, str]]) -> None:
        self.exact = exact
        self.patterns = [(re.compile(p), r) for p, r in patterns]

    def text(self, value: str) -> str:
        if value in self.exact:
            return self.exact[value]
        for pattern, replacement in self.patterns:
            value = pattern.sub(replacement, value)
        return value

    def __call__(self, value: Any) -> Any:
        if value is None or isinstance(value, Enum | bool | int | float):
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, BaseModel):
            update = {name: self(getattr(value, name)) for name in type(value).model_fields}
            return value.model_copy(update=update)
        if isinstance(value, dict):
            return {
                (self.text(key) if isinstance(key, str) and not isinstance(key, Enum) else key): (
                    self(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self(item) for item in value)
        if isinstance(value, _Channel):
            return _Channel(
                info=self(value.info),
                fixed={k: self(m) for k, m in value.fixed.items()},
                generated=value.generated,
                gen=self.wrap(value.gen),
                extra=self(value.extra),
            )
        if isinstance(value, _Transcript):
            return _Transcript(
                total_generated=value.total_generated,
                gen=self.wrap(value.gen),
                fixed={k: self(e) for k, e in value.fixed.items()},
                extra=self(value.extra),
                older=value.older,
            )
        if isinstance(value, _Run):
            update = {}
            for f in dataclasses.fields(value):
                item = getattr(value, f.name)
                if f.name in ("agents", "channels", "transcripts"):
                    update[f.name] = {self.text(k): self(v) for k, v in item.items()}
                elif f.name == "running_tools":
                    update[f.name] = {self.text(k): v for k, v in item.items()}
                else:
                    update[f.name] = self(item)
            return dataclasses.replace(value, **update)
        return value

    def wrap(self, gen: Callable[[int], Any] | None) -> Callable[[int], Any] | None:
        """Translate what a generator of messages or transcript entries makes."""
        if gen is None:
            return None
        return lambda n: self(gen(n))


R_ABN_EXPLORE = "20261024-1412_goldbach-ternaer_exploration_a3f9"
R_ABN_RAMSEY = "20261024-1305_ramsey-graphen_proof_b2e4"
R_ABN_REVIEW = "20261024-1521_partitionen_proof-review_e81b"
R_ABN_KONGRUENZ = "20261024-0930_partitionen_proof_7c21"

_ABN_READ_DIR = "~/Documents/Partitionen-Daten"
_ABN_COMMAND = "wget https://primes.utm.edu/lists/small/millions/primes1.zip"

_ABN_REVIEW_TASK = """**Aussage:** Der Dyson-Rang teilt die Partitionen von $5n + 4$ in fünf \
gleich große Klassen.

**Beweis:** `tex/rang-lemma.tex` (Abschnitt 2).

**Schwerpunkte:** Vollständigkeit der Fallunterscheidung modulo 5, Notation des Rangs gegenüber \
der Kurbel (crank).

**Ergebnis:** Je Judge ein Gutachten mit Urteil (accept, minor, major, reject) und \
nummerierten Einwänden.

**Hinweise:** Die Definition des Rangs folgt Dyson (1944). Abschnitt 1 von `tex/rang-lemma.tex` \
ist nicht Teil der Prüfung.
"""

# Whole texts of the scenario and their counterpart (scenario text → acceptance text).
_ABN_EXACT = {
    # runs, projects, workfolder
    "Collatz – Ideen-Exploration": "Ternäre Goldbach-Darstellungen – Exploration",
    "R(4,5) ≤ 25 – Beweisweg ausarbeiten": "R(3,3,3)-Färbung – Beweis",
    "Lemma 3 – unabhängige Prüfung": "Rang-Lemma – unabhängige Prüfung",
    "Lemma 3 (Stoppzeit 4) – Beweis ausarbeiten": "Kongruenz p(5n+4) ≡ 0 mod 5 – Beweis",
    "proof-review: Lemma 3 – unabhängige Prüfung": "proof-review: Rang-Lemma – unabhängige Prüfung",
    "proof: Lemma 3 (Stoppzeit 4) – Beweis ausarbeiten": "proof: Kongruenz p(5n+4) ≡ 0 mod 5 – "
    "Beweis",
    "Primzahllücken – Roundtable": "Partitionen mod 7 – Roundtable",
    "Collatz – Zyklen mod 2ᵏ": "Goldbach – Ausnahmen bis 10¹²",
    "Terras-Lemma – Review": "Helfgott-Lemma – Review",
    "Stoppzeiten bis 10⁹ – solo": "Darstellungen bis 10⁹ – solo",
    "Collatz – erste Exploration": "Goldbach – erste Exploration",
    "Collatz – Stoppzeiten-Statistik": "Goldbach – Darstellungs-Statistik",
    "Terras-Satz – Beweisskizze": "Vinogradov-Satz – Beweisskizze",
    "Paritätsvektoren mod 2¹⁶": "Summanden modulo 30",
    "Collatz – 2-adische Ideen": "Goldbach – Kreismethode",
    "Zyklen der Länge k – Exploration": "Ausnahmemenge – Exploration",
    "Collatz – Heuristik ln(4/3)": "Goldbach – Heuristik nach Hardy–Littlewood",
    "R(4,5) – Strategien": "R(3,3,3) – Strategien",
    "R(3,3,3) – Färbungen": "R(4,4) – Färbungen",
    "Primzahllücken – Statistik": "Partitionen – Statistik",
    "Cramér-Vermutung – Review": "Crank-Vermutung – Review",
    "Collatz-Varianten": "Ternäre Goldbach-Darstellungen",
    "Ramsey-Zahlen, obere Schranken": "Ramsey-Graphen und Färbungen",
    "Primzahllücken": "Partitionen und Kongruenzen",
    "Mathe privat": "Forschung",
    "Uni": "Lehre",
    "~/Uni-Forschung": "~/Lehre",
    "arr wurde beendet · Protokoll bis 23:41": "arr wurde beendet · Protokoll bis 23:41",
    "Finde neue Zugänge zu Stoppzeiten der Collatz-Abbildung.": "Finde neue Zugänge zur Anzahl "
    "$r_3(n)$ der Darstellungen ungerader $n$ als Summe dreier Primzahlen.",
    # explorer agents → scouts
    "Idee „2-adische Darstellung“": "Idee „Kreismethode mit expliziten Konstanten“",
    "Idee „Stoppzeiten-Statistik“": "Idee „Statistik von r₃(n)“",
    "Idee „Terras-Paritätsvektoren“": "Idee „Restklassen der Summanden“",
    "python stoppzeiten.py --bis 10**8": "python darstellungen.py --bis 10**8",
    "paritaet/terras.md": "restklassen/summanden.md",
    # channel #labor (scenario #main)
    "Die mittlere Gesamtstoppzeit von $T$ für $n < 10^7$ liegt bei $6{,}95 \\cdot \\ln n$ "
    "(Abweichung unter 2 %). Das passt zur Heuristik $2 / \\ln(4/3) ≈ 6{,}95$.": "Für ungerade "
    "$n < 10^7$ liegt $r_3(n)$ im Mittel bei "
    "$0{,}98 \\cdot \\mathfrak{S}(n)\\, n^2 / (2 \\ln^3 n)$ "
    "(Abweichung unter 2 %). Das passt zur Asymptotik von Hardy und Littlewood.",
    "stoppzeiten_1e7.csv": "darstellungen_1e7.csv",
    "groupchats/attachments/2/stoppzeiten_1e7.csv": "groupchats/attachments/2/"
    "darstellungen_1e7.csv",
    "Auf $\\mathbb{Z}_2$ ist die Collatz-Abbildung $T$ stetig und maßerhaltend und zur "
    "Shift-Abbildung konjugiert (Lagarias 1985). Idee: Dichteaussagen über Stoppzeiten über "
    "diese Konjugation übertragen.": "Die Kreismethode mit den expliziten Konstanten von "
    "Helfgott (2013) liefert $r_3(n) > 0$ für alle ungeraden $n ≥ 10^{27}$. Idee: die Schranke "
    "für die Nebenbögen mit unseren Zählungen bis $10^8$ vergleichen.",
    "2adisch.md": "kreismethode.md",
    "groupchats/attachments/3/2adisch.md": "groupchats/attachments/3/kreismethode.md",
    "Terras (1976): Die Paritätsvektoren der Länge $k$ von $0, …, 2^k − 1$ sind paarweise "
    "verschieden. Für $k ≤ 20$ nachgerechnet.": "Für $n ≡ 3 \\pmod 6$ gibt es eine Darstellung "
    "mit $p_1 = 3$, sobald $n − 3$ eine Goldbach-Zerlegung hat. Für $n ≤ 10^6$ nachgerechnet.",
    "check_terras.py": "check_summanden.py",
    "groupchats/attachments/4/check_terras.py": "groupchats/attachments/4/check_summanden.py",
    "Eine Abweichung von 2 % bei $10^7$ sagt wenig, die Konvergenz ist logarithmisch langsam. "
    "Wie verhält sich der Fehler über die Dekaden $10^4 … 10^7$?": "Eine Abweichung von 2 % bei "
    "$10^7$ sagt wenig, der Fehlerterm fällt nur logarithmisch. Wie verhält sich die Abweichung "
    "über die Dekaden $10^4 … 10^7$?",
    "Bitte prüfe auch $k$ bis 24, wenn es unter 2 Minuten bleibt.": "Bitte prüfe auch $n$ bis "
    "$10^7$, wenn es unter 2 Minuten bleibt.",
    "Überträgt die Konjugation zum Shift wirklich Aussagen über die natürliche Dichte in "
    "$\\mathbb{N}$? Die Maß-Aussage gilt für das Haar-Maß auf $\\mathbb{Z}_2$, nicht für das "
    "Zählen in $\\mathbb{N}$.": "Gelten Helfgotts Konstanten auch für deine ungewichtete "
    "Zählung? Seine Schranke ist für die mit $\\log p_1 \\log p_2 \\log p_3$ gewichtete Summe "
    "formuliert, nicht für $r_3(n)$ selbst.",
    "Fehler über die Dekaden: $10^4$: 4,1 %, $10^5$: 3,0 %, $10^6$: 2,4 %, $10^7$: 1,9 %. Fällt "
    "ungefähr wie $1 / \\log n$, Tabelle im Anhang.": "Abweichung über die Dekaden: $10^4$: "
    "4,3 %, $10^5$: 3,1 %, $10^6$: 2,5 %, $10^7$: 1,9 %. Fällt ungefähr wie $1 / \\log n$, "
    "Tabelle im Anhang.",
    "fehlerterm.md": "abweichung.md",
    "groupchats/attachments/8/fehlerterm.md": "groupchats/attachments/8/abweichung.md",
    "$k ≤ 24$ geprüft (1 min 40 s), weiterhin bijektiv.": "$n ≤ 10^7$ geprüft (1 min 40 s), "
    "weiterhin ohne Ausnahme.",
    # transcript of scout-1
    "Stufe Exploration, Turn 6 von 8. Idee: 2-adische Darstellung.\nPosteingang: 2 neue "
    "Nachrichten in #main (explorer-gpt, critic). Keine offenen Fragen an dich.\nArbeite weiter "
    'und gib am Ende von Turn 8 dein Ergebnis mit submit_result(kind="exploration") ab.': "Stufe "
    "Exploration, Turn 6 von 8. Idee: Kreismethode mit expliziten Konstanten.\nPosteingang: 2 "
    "neue Nachrichten in #labor (scout-gpt, skeptic). Keine offenen Fragen an dich.\nArbeite "
    'weiter und gib am Ende von Turn 8 dein Ergebnis mit submit_result(kind="exploration") ab.',
    "Ich will die Stoppzeit-Verteilung aus der Shift-Konjugation mit den Werten aus OEIS A006577 "
    "vergleichen. Lokal habe ich nur meine eigenen Werte bis 10⁶. Zuerst prüfe ich, ob die "
    "Stoppzeiten aus den Paritätsvektoren mod 2¹⁶ mit der direkten Rechnung übereinstimmen. "
    "Danach brauche ich die b-Datei als unabhängige Referenz.": "Ich will meine Zählung von "
    "r₃(n) mit einer unabhängigen Primzahlliste prüfen. Lokal habe ich nur mein eigenes Sieb "
    "bis 10⁶. Zuerst prüfe ich, ob das Sieb mit der Zählung über Restklassen modulo 30 "
    "übereinstimmt. Danach brauche ich die Liste der ersten Million Primzahlen als Referenz.",
    "notizen/2adisch.md": "notizen/kreismethode.md",
    "# 2-adische Sicht\nT: ℤ₂ → ℤ₂ …": "# Kreismethode\nHauptbögen: q ≤ Q …",
    "python stoppzeit_2adisch.py --k 16": "python sieb_check.py --bis 10**6",
    "k=16: 65536 Restklassen geprüft\nStoppzeit aus Paritätsvektor = direkte Rechnung: 65536 "
    "von 65536\nLaufzeit 37,6 s": "78498 Primzahlen bis 10^6\nSieb = Zählung über Restklassen "
    "mod 30: 78498 von 78498\nLaufzeit 37,6 s",
    "Die Stoppzeiten aus den Paritätsvektoren stimmen mod 2¹⁶ mit der direkten Rechnung überein. "
    "Für den Vergleich mit großen n brauche ich die b-Datei aus OEIS.": "Mein Sieb stimmt bis "
    "10⁶ mit der Zählung über Restklassen überein. Für den Vergleich mit großen n brauche ich "
    "die Primzahlliste von primes.utm.edu.",
    "curl -sO https://oeis.org/A006577/b006577.txt": _ABN_COMMAND,
    "https://oeis.org/A006577/b006577.txt": "https://primes.utm.edu/lists/small/millions/"
    "primes1.zip",
    # approvals
    "Ich brauche die Tabelle A006577 (Schritte bis 1) für n ≤ 10 000, um meine "
    "Stoppzeit-Verteilung aus der 2-adischen Darstellung mit veröffentlichten Werten zu "
    "vergleichen. Die Datei ist reiner Text, etwa 90 kB.": "Ich brauche die Liste der ersten "
    "Million Primzahlen, um meine Zählung von r₃(n) mit einer unabhängigen Quelle zu "
    "vergleichen. Die Datei ist ein ZIP-Archiv mit reinem Text, etwa 1,7 MB.",
    "Keine Entscheidung bis 15:36: gilt als abgelehnt.": "Keine Entscheidung bis 16:09: gilt als "
    "abgelehnt.",
    "~/Documents/Mathe-Literatur/lagarias-3x+1.pdf": "~/Documents/Fachliteratur/"
    "helfgott-ternaer.pdf",
    "~/Documents/Collatz-Daten/orbits.csv": "~/Documents/Goldbach-Daten/darstellungen.csv",
    "Die Reduktion auf (4,5)-Graphen mit 24 Knoten ist richtig. Es fehlt aber, wie die "
    "verbleibenden Fälle ausgeschlossen werden.": "Die Färbung des K₁₆ über die Kubikreste im "
    "Körper mit 16 Elementen ist richtig angesetzt. Es fehlt aber, warum keine Farbklasse ein "
    "Dreieck enthält.",
    "Schritt 3 setzt R(3,5) = 14 voraus, ohne Quelle oder Beleg.": "Schritt 3 setzt voraus, dass "
    "jede Farbklasse ein Clebsch-Graph ist, ohne Beleg.",
    # finished run (scenario Lemma 3 → congruence)
    "Fallunterscheidung nach $n \\bmod 16$.": "Über die Erzeugendenfunktion und Eulers "
    "Pentagonalzahlensatz.",
    "Fall n ≡ 7 fehlt.": "Der Schritt modulo 5 ist nicht begründet.",
    "Rückrichtung unvollständig.": "Koeffizientenvergleich unvollständig.",
    "**Beweis.** Sei $n = 16m + 3$ mit $m ≥ 0$. Dann gilt "
    "$$T(n) = 24m + 5,\\ T^2(n) = 36m + 8,\\ T^3(n) = 18m + 4,\\ T^4(n) = 9m + 2 < n$$": "**Beweis."
    "** Nach Ramanujan gilt $$\\sum_{n ≥ 0} p(5n+4)\\, q^n = 5 \\frac{(q^5;q^5)_\\infty^5}"
    "{(q;q)_\\infty^6},$$ also sind alle Koeffizienten durch 5 teilbar.",
    "Vollständig und korrekt. Die Rückrichtung ließe sich in einer Tabelle der Restklassen "
    "zusammenfassen.": "Vollständig und korrekt. Die Herleitung der Identität ließe sich in einer "
    "Tabelle der Restklassen modulo 5 zusammenfassen.",
    "Korrekt. Bitte die vier Iterationen als Tabelle und die Definition von σ nach Terras "
    "ausdrücklich zitieren.": "Korrekt. Bitte die Identität mit Quelle (Ramanujan 1919) zitieren "
    "und den Schritt $(q;q)_\\infty^5 ≡ (q^5;q^5)_\\infty \\pmod 5$ ausschreiben.",
    "**Lemma 3 ist bewiesen:** Für $n > 1$ gilt $σ(n) = 4$ genau dann, wenn $n ≡ 3 \\pmod{16}$. "
    "Der Beweis rechnet die vier Schritte von $T$ für $n = 16m + 3$ aus und schließt die übrigen "
    "Restklassen modulo 16 aus.\n\n- **Strategie (8,0):** Fallunterscheidung nach $n \\bmod 16$."
    "\n- **Skizze:** Versuch 1 (6,0) übersah den Fall $n ≡ 7 \\pmod{16}$. Versuch 2 (8,5) "
    "ergänzt ihn.\n- **Beweis (8,0):** rater-2 wünscht eine Tabelle der vier Iterationen.\n- "
    "**Offen:** Verallgemeinerung auf $σ(n) = k$.": "**Die Kongruenz ist bewiesen:** Für alle "
    "$n ≥ 0$ gilt $p(5n+4) ≡ 0 \\pmod 5$. Der Beweis nutzt Ramanujans Identität für die "
    "Erzeugendenfunktion und $(q;q)_\\infty^5 ≡ (q^5;q^5)_\\infty \\pmod 5$.\n\n- **Strategie "
    "(8,0):** über die Erzeugendenfunktion.\n- **Skizze:** Versuch 1 (6,0) ließ den Schritt "
    "modulo 5 unbegründet. Versuch 2 (8,5) ergänzt ihn.\n- **Beweis (8,0):** rater-2 wünscht die "
    "Quelle der Identität.\n- **Offen:** die Kongruenzen modulo 7 und 11.",
    " und schlag vor, was wir nach tex/lemma3.tex übernehmen": " und schlag vor, was wir nach "
    "tex/kongruenz5.tex übernehmen",
    # review run (scenario proof-review → rank lemma)
    _LEMMA3_TASK: _ABN_REVIEW_TASK,
    # generated texts and the live stream
    "Für $n ≡ 3 \\pmod{16}$ fällt die Folge nach vier Schritten unter $n$.": "Für "
    "$n ≡ 3 \\pmod 6$ gibt es stets eine Darstellung mit $p_1 = 3$.",
    "Die Restklasse $27 \\bmod 32$ bleibt der schwierige Fall.": "Die Restklasse "
    "$n ≡ 1 \\bmod 30$ bleibt der schwierige Fall.",
    "Abschätzung $T^k(n) < n$ für alle $k ≥ 4$ nachgerechnet bis $2^{24}$.": "Abschätzung "
    "$r_3(n) > n^2 / (10 \\ln^3 n)$ nachgerechnet bis $2^{24}$.",
    "Sage-Rechnung bestätigt die Tabelle der Iterationen.": "Sage-Rechnung bestätigt die "
    "Tabelle der Darstellungen.",
    "Die Rechnung bestätigt $T^4(n) < n$ für alle $n ≡ 3 \\pmod{16}$ bis $2^{20}$.": "Die Rechnung "
    "bestätigt $r_3(n) > 0$ für alle ungeraden $n$ bis $2^{20}$.",
    "Als Nächstes prüfe ich die Restklasse $27 \\bmod 32$ getrennt.": "Als Nächstes prüfe ich "
    "die Restklasse $n ≡ 1 \\bmod 30$ getrennt.",
    "# Ideen\n1. Restklassen modulo 64\n2. Paritätsvektoren": "# Ideen\n1. Restklassen modulo 30"
    "\n2. Kreismethode",
    "python stoppzeiten.py --bis 10**9": "python darstellungen.py --bis 10**9",
    "Stoppzeiten bis 10^9 berechnet\nMittelwert 6,95 · ln n\nLaufzeit 41,2 s": "Darstellungen "
    "bis 10^9 gezählt\nkleinster Quotient 0,97\nLaufzeit 41,2 s",
}

# Names and IDs inside texts; applied in this order when no whole text matches.
_ABN_PATTERNS = [
    (r"(?<!\w)max-privat", "max-forschung"),
    (r"(?<!\w)chatgpt-privat", "chatgpt-forschung"),
    (r"(?<!\w)claude-api-uni", "claude-api-lehrstuhl"),
    (r"Mathe privat", "Forschung"),
    (r"~/Mathe(?![\w-])", "~/Forschung"),
    (r"Mathe-Literatur", "Fachliteratur"),
    (r"oeis\.org", "primes.utm.edu"),
    (r"explorer", "scout"),
    # plurals first, else „den Scoutn“, „Mehrere Scout“
    (r"Explorern", "Scouts"),
    (r"\b([Dd]ie|[Mm]ehrere|[Aa]lle) Explorer(?![\w-])", r"\1 Scouts"),
    (r"Explorer", "Scout"),
    (r"critic", "skeptic"),
    (r"Kritiker", "Skeptiker"),
    (r"(?<![\w-])main(?![\w-])", "labor"),
    (r"(?<![\w-])ranking(?![\w-])", "rangliste"),
    (r"(?<![a-z-])collatz(?![a-z])", "goldbach-ternaer"),
    (r"ramsey-schranken", "ramsey-graphen"),
    (r"primzahl-luecken", "partitionen"),
    (r"Collatz", "Goldbach"),
]

# Extra replacements for single runs of the scenario (checked before the general ones).
_ABN_RUN_EXACT = {
    R_LEMMA: {"collatz": "partitionen"},
    R_REVIEW: {"collatz": "partitionen", "tex/lemma3.tex": "tex/rang-lemma.tex"},
}
_ABN_RUN_PATTERNS = {
    R_LEMMA: [
        (r"_collatz_", "_partitionen_"),
        (r"tex/lemma3\.tex", "tex/kongruenz5.tex"),
        (r"notes/stoppzeit4\.md", "notes/ramanujan5.md"),
    ],
    R_REVIEW: [
        (r"_collatz_", "_partitionen_"),
        (r"~/Documents/Mathe-Literatur", _ABN_READ_DIR),
        (r"tex/lemma3\.tex", "tex/rang-lemma.tex"),
    ],
    R_RAMSEY: [(r"Skizze", "Konstruktion")],
}


def _abnahme_translator(run_id: str | None = None) -> _Translator:
    """The translator for the whole data set, or for the texts of one scenario run."""
    exact = {**_ABN_EXACT, **_ABN_RUN_EXACT.get(run_id or "", {})}
    patterns = [*_ABN_RUN_PATTERNS.get(run_id or "", []), *_ABN_PATTERNS]
    return _Translator(exact, patterns)


def _abnahme_data(base: str) -> tuple[dict[str, _Run], dict[str, ApprovalItem]]:
    """Runs (all details built) and inbox of the acceptance data set."""
    runs = _scenario_runs(SCENARIO_NOW)
    general = _abnahme_translator()
    for run in runs.values():
        if run.id not in _DETAIL_BUILDERS:
            run.name = general.text(run.name)  # the generic summary repeats the name
        _DETAIL_BUILDERS.get(run.id, _build_generic_detail)(run)
        run.detail_built = True
    new_runs = {}
    for run in runs.values():
        translated = _abnahme_translator(run.id)(run)
        new_runs[translated.id] = translated
    approvals = {}
    for item in _scenario_approvals(base).values():
        translated = _abnahme_translator(item.run.id)(item)
        approvals[translated.id] = translated
    _abnahme_overrides(new_runs, approvals)
    return new_runs, approvals


def _abnahme_overrides(runs: dict[str, _Run], approvals: dict[str, ApprovalItem]) -> None:
    """What the acceptance data set has differently from the scenario (docs/08 §9.2)."""
    # Permission of scout-1: domain not in the mandate; a longer deadline for the 50-minute test.
    deadline = _at(24, 16, 9)
    approvals["apr_0042"] = approvals["apr_0042"].model_copy(update={"deadline": deadline})
    scout = runs[R_ABN_EXPLORE].agents["scout-1"]
    assert scout.blocked_by is not None
    runs[R_ABN_EXPLORE].agents["scout-1"] = scout.model_copy(
        update={"blocked_by": scout.blocked_by.model_copy(update={"deadline": deadline})}
    )
    # Run start from Claude Code with two deviations: read folder and 6 h run time.
    review = runs[R_ABN_REVIEW]
    review.wall_time_s = 6 * 3600
    review.turns_per_agent = 40
    for aid, agent in list(review.agents.items()):
        review.agents[aid] = agent.model_copy(
            update={"tools": ToolAccess(shell=True, web=False), "turns_limit": 40}
        )
    item = approvals["apr_0041"]
    no_web = ToolAccess(shell=True, web=False)
    agents = [a.model_copy(update={"tools": no_web}) for a in item.agents]
    approvals["apr_0041"] = item.model_copy(
        update={
            "agents": agents,
            "limits": LimitsInfo(wall_time_s=6 * 3600, turns_per_agent=40, budget_usd=20),
            "estimate": Estimate(
                turns=Range(min=60, max=90),
                cost_usd=Range(min=8, max=14),
                cost_kind=CostKind.ESTIMATED,
                duration_s=Range(min=2 * 3600, max=4 * 3600),
            ),
            "deviations": [
                Deviation(
                    path="defaults.read_dirs",
                    label="Zusätzlicher Lese-Ordner",
                    value_text=f"{_ABN_READ_DIR} für alle Agenten",
                    default_text="keiner",
                    default=[],
                    value=[_ABN_READ_DIR],
                    sensitive=True,
                    providers=_shares(agents),
                ),
                Deviation(
                    path="limits.wall_time_s",
                    label="Laufzeit 6 h",
                    value_text="",
                    default_text="2 h",
                    default=2 * 3600,
                    value=6 * 3600,
                    sensitive=False,
                ),
            ],
            "standard_notes": [
                _copy_note("partitionen", agents),
                "Web-Zugriff",
                "Turns pro Agent",
                "Auth-Profile",
                "Entscheider für Berechtigungsanfragen",
                "Budget",
            ],
        }
    )


def _fit_storage(runs: dict[str, _Run], total_gb: float, freed_gb: float) -> None:
    """Scale run sizes so that the runs use `total_gb` and slimming frees `freed_gb`.

    The suggestion takes the 8 slimmable runs with the largest working copies (see
    `MockApi.get_storage`); their working copies are scaled to `freed_gb`, no other run keeps
    a larger one, and the protocols make up the rest per project.
    """
    finished = [r for r in runs.values() if not r.status.is_active]
    candidates = sorted(
        (r for r in finished if not (r.pinned or r.slimmed or r.archived) and r.size.work > 0),
        key=lambda r: r.size.work,
        reverse=True,
    )
    chosen, rest = candidates[:8], candidates[8:]
    before = {p: 0 for p in {r.project for r in runs.values()}}
    for r in runs.values():
        before[r.project] += r.size.actual_bytes
    factor = freed_gb * GB / sum(r.size.work for r in chosen)
    for r in chosen:
        r.size = r.size.model_copy(update={"work": int(r.size.work * factor)})
    smallest = min(r.size.work for r in chosen)
    for r in rest:
        if r.size.work >= smallest:
            r.size = r.size.model_copy(update={"work": int(smallest * 0.9)})
    scale = total_gb * GB / sum(before.values())
    for project, old in before.items():
        members = [r for r in finished if r.project == project]
        fixed = sum(r.size.actual_bytes for r in runs.values() if r.project == project) - sum(
            r.size.actual_bytes for r in members
        )
        parts = sum(r.size.work + r.size.output + r.size.archive for r in members)
        logs = sum(r.size.logs for r in members)
        target_logs = max(0, int(old * scale) - fixed - parts)
        for r in members:
            share = r.size.logs / logs if logs else 1 / len(members)
            new_logs = int(target_logs * share)
            actual = r.size.work + new_logs + r.size.output + r.size.archive
            ratio = r.size.logical_bytes / r.size.actual_bytes if r.size.actual_bytes else 1
            r.size = r.size.model_copy(
                update={
                    "logs": new_logs,
                    "actual_bytes": actual,
                    "logical_bytes": max(actual, int(actual * ratio)),
                }
            )


# --------------------------------------------------------------------------------------------
# The mock
# --------------------------------------------------------------------------------------------


_DETAIL_BUILDERS: dict[str, Callable[[_Run], None]] = {
    R_EXPLORE: _build_explore_detail,
    R_LEMMA: _build_lemma_detail,
    R_RAMSEY: _build_ramsey_detail,
    R_REVIEW: _build_review_detail,
    R_SATZ2: _build_satz2_detail,
}


def _requested_value(item: ApprovalItem, path: str) -> Any:
    """Value of a run start request at `path` (`agents.<id>.tools.web`, `limits.<key>` …)."""
    parts = path.split(".")
    if parts[0] == "agents" and len(parts) >= 3:
        agent = next((a for a in item.agents if a.id == parts[1]), None)
        if agent is None:
            return None
        if parts[-1] == "web":
            return agent.tools.web
        if parts[-1] == "read_dirs":
            return list(agent.read_dirs)
        if parts[-1] == "model":
            return agent.model
        return None
    if parts[0] == "limits" and item.limits is not None and len(parts) == 2:
        return getattr(item.limits, parts[1], None)
    if parts[0] == "decider":
        return item.decider.value if item.decider else None
    if parts[0] == "task_md":
        return item.task_md
    if parts[0] == "params" and len(parts) == 2:
        return item.params.get(parts[1])
    return None


def _change_label(path: str, old: Any, new: Any, skill: str | None = None) -> str:
    """German label of one change, e.g. „Web judge-1: an → aus“ (docs/08 §5.0 `changes`)."""
    parts = path.split(".")

    def dirs(value: Any) -> str:
        return ", ".join(value) if value else "keiner"

    if parts[0] == "agents" and parts[-1] == "web":
        return f"Web {parts[1]}: {'an' if old else 'aus'} → {'an' if new else 'aus'}"
    if parts[0] == "agents" and parts[-1] == "read_dirs":
        return f"Lese-Ordner {parts[1]}: {dirs(old)} → {dirs(new)}"
    if parts[0] == "agents" and parts[-1] == "model":
        return f"Modell {parts[1]}: {old} → {new}"
    if parts[0] == "limits" and len(parts) == 2:
        label = {"wall_time_s": "Laufzeit", "turns_per_agent": "Turns pro Agent"}.get(
            parts[1], "Budget"
        )
        old_text = _limit_text(parts[1], old) if isinstance(old, int | float) else "–"
        new_text = _limit_text(parts[1], new) if isinstance(new, int | float) else "–"
        return f"{label}: {old_text} → {new_text}"
    if parts[0] == "decider":
        return (
            f"Entscheider: {_DECIDER_TEXT.get(str(old), old)} → {_DECIDER_TEXT.get(str(new), new)}"
        )
    if parts[0] == "task_md":
        return "Aufgabe geändert"
    if parts[0] == "params" and len(parts) == 2:
        return f"{_param_label(skill, parts[1])}: {old} → {new}"
    return f"{path}: {old} → {new}"


def _size_text(num_bytes: int) -> str:
    """Decimal size like the GUI shows it (docs/08 §8.7): „46,2 GB“, „38 MB“."""
    if num_bytes >= GB and num_bytes % GB == 0:
        return f"{num_bytes // GB} GB"
    if num_bytes >= GB:
        return f"{num_bytes / GB:.1f} GB".replace(".", ",")
    return f"{round(num_bytes / MB)} MB"


def _usage_tokens(row: UsageRow) -> int:
    """All tokens of a usage row (input, output, cache, reasoning)."""
    return sum(
        v or 0 for v in (row.input, row.output, row.cache_read, row.cache_write, row.reasoning)
    )


def _add_usage(row: UsageRow, part: dict[str, Any]) -> UsageRow:
    """Add the numbers of `part` to `row`; a value stays „–“ (None) while no part delivers it."""
    update: dict[str, Any] = {}
    for name in ("input", "output", "cache_read", "cache_write", "reasoning"):
        value = part.get(name)
        if value is not None:
            update[name] = (getattr(row, name) or 0) + value
    cost = part.get("cost_usd")
    kind = part.get("cost_kind")
    if cost is not None:
        update["cost_usd"] = round((row.cost_usd or 0) + cost, 2)
        # mixed rows (billed + estimated) are marked as estimated („≈“)
        if row.cost_kind is CostKind.ESTIMATED or kind is CostKind.ESTIMATED:
            update["cost_kind"] = CostKind.ESTIMATED
        else:
            update["cost_kind"] = kind
    return row.model_copy(update=update)


def _same(value: Any) -> Any:
    """No translation (scenario and load data set)."""
    return value


def _not_found(what: str, ident: str) -> ApiException:
    return ApiException.of(ErrorCode.NOT_FOUND, f"{what} „{ident}“ nicht gefunden.")


class MockApi:
    """`ApiClient` on in-memory sample data (docs/08 §9.2), with a simulated live stream.

    Construct with a `Dataset`; `switch_dataset()` replaces all data and resets the clock.
    `start_simulation()` runs the live stream in the event loop; tests call `advance()`
    instead. Mock-only extras (`dataset`, `switch_dataset`, `advance`,
    `simulate_new_approval`) are not part of `ApiClient`.
    """

    def __init__(self, dataset: Dataset = Dataset.SCENARIO, *, base_url: str = DEFAULT_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.hub = EventHub()
        self._task: asyncio.Task[None] | None = None
        self.stream_rate = RUN_DETAIL_STREAM_RATE
        self.dataset = dataset
        self._load(dataset)

    # --- data set ---------------------------------------------------------------------------

    def _load(self, dataset: Dataset) -> None:
        self.dataset = dataset
        if dataset is Dataset.SCENARIO:
            self.clock = MockClock(SCENARIO_NOW)
            runs = _scenario_runs(SCENARIO_NOW)
            _fit_storage(runs, total_gb=46.2, freed_gb=14.9)
            approvals = _scenario_approvals(self.base_url)
            warnings = _scenario_warnings()
            wf = WorkfolderInfo(
                name="Mathe privat",
                path="~/Mathe",
                projects_count=3,
                runs_count=len(runs),
                disk_free_bytes=31 * GB,
                apfs=True,
                known=[
                    WorkfolderRef(
                        name="Mathe privat", path="~/Mathe", projects_count=3, active_runs=3
                    ),
                    WorkfolderRef(
                        name="Uni", path="~/Uni-Forschung", projects_count=1, active_runs=0
                    ),
                ],
                service_url=self.base_url,
            )
        elif dataset is Dataset.ACCEPTANCE:
            self.clock = MockClock(SCENARIO_NOW)
            runs, approvals = _abnahme_data(self.base_url)
            _fit_storage(runs, total_gb=47.8, freed_gb=11.2)
            tr = _abnahme_translator()
            warnings = [
                *tr(_scenario_warnings()),
                WarningInfo(
                    id="profile-claude-api-lehrstuhl",
                    kind=WarningKind.PROFILE_ERROR,
                    severity=Severity.ERROR,
                    title="claude-api-lehrstuhl funktioniert nicht",
                    text="Der API-Key wurde abgelehnt (401). Agenten mit diesem Profil starten "
                    "nicht.",
                    action=Action(label="Profil öffnen", url="/profile/claude-api-lehrstuhl"),
                ),
            ]
            wf = WorkfolderInfo(
                name="Forschung",
                path="~/Forschung",
                projects_count=3,
                runs_count=len(runs),
                disk_free_bytes=31 * GB,
                apfs=True,
                known=[
                    WorkfolderRef(
                        name="Forschung", path="~/Forschung", projects_count=3, active_runs=3
                    ),
                    WorkfolderRef(name="Lehre", path="~/Lehre", projects_count=1, active_runs=0),
                ],
                service_url=self.base_url,
            )
        else:
            self.clock = MockClock(LOAD_NOW)
            runs = _load_runs(LOAD_NOW)
            approvals = _load_approvals(self.base_url, runs)
            warnings = [
                *_scenario_warnings(),
                WarningInfo(
                    id="interrupted",
                    kind=WarningKind.RUNS_INTERRUPTED,
                    severity=Severity.ERROR,
                    title="20 Runs wurden abgebrochen",
                    text="arr wurde beendet, während sie liefen. Ihr Protokoll ist lesbar.",
                    action=Action(label="Runs ansehen", url="/runs?status=error"),
                ),
            ]
            wf = WorkfolderInfo(
                name="Mathe privat",
                path="~/Mathe",
                projects_count=3,
                runs_count=len(runs),
                disk_free_bytes=18 * GB,
                apfs=True,
                service_url=self.base_url,
            )
        self.state = _State(
            workfolder=wf,
            runs=runs,
            approvals=approvals,
            warnings=warnings,
            profiles=_profiles(self.clock.now()),
            projects=_projects(runs, self.clock.now()),
            skills=_skills(),
            user_rules=[
                FixedRule(
                    id="user-1",
                    effect=RuleEffect.ALLOW,
                    rule="Netzwerk zu www.lmfdb.org",
                    origin="user",
                    origin_text="von dir: Reichweite „immer“ bei einer Freigabe am 03.10.",
                    removable=True,
                    created_at=_at(3, 10, 12),
                )
            ],
            app_settings=AppSettings(
                port=int(self.base_url.rsplit(":", 1)[-1])
                if self.base_url.count(":") == 2
                else 8765,
                notifications={
                    NotificationEvent.APPROVAL: True,
                    NotificationEvent.RUN_FINISHED: True,
                    NotificationEvent.LIMIT_REACHED: True,
                    NotificationEvent.STORAGE_LOW: True,
                    NotificationEvent.MESSAGE_TO_USER: False,
                },
                workfolders=wf.known
                or [WorkfolderRef(name=wf.name, path=wf.path, projects_count=3, active_runs=5)],
            ),
            workfolder_settings=WorkfolderSettingsInfo(
                defaults={
                    Provider.CLAUDE: ProviderDefaultsInfo(
                        auth="max-privat", model="claude-opus-5-5", effort=Effort.HIGH
                    ),
                    Provider.CODEX: ProviderDefaultsInfo(
                        auth="chatgpt-privat", model="gpt-5.5", effort=Effort.MEDIUM
                    ),
                },
                limits=LimitsInfo(wall_time_s=2 * 3600, turns_per_agent=40, budget_usd=20),
                concurrency={"max-privat": 4, "chatgpt-privat": 3, "claude-api-uni": 2},
                toolchain=ToolchainInfo(python="~/.venvs/mathe"),
                approvals=ApprovalSettingsInfo(
                    decider=Decider.GATEKEEPER_THEN_USER,
                    timeout_s=20 * 60,
                    gatekeeper_model="claude-sonnet-5-5",
                    mandate="mandates/standard.md",
                ),
                retention=RetentionInfo(
                    slim_after_days=14, archive_after_days=90, max_runs_size_gb=50
                ),
            ),
            mandate_text=_MANDATE_TEXT,
            mandate_modified=_at(20, 9, 12),
        )
        self._tr: Callable[[Any], Any] = _same
        if dataset is Dataset.LOAD:
            assert self.state.workfolder_settings is not None
            self.state.workfolder_settings.retention.max_runs_size_gb = 500
        if dataset is Dataset.ACCEPTANCE:
            # names and texts of the acceptance data set also for what the mock makes later
            self._tr = tr = _abnahme_translator()
            st = self.state
            st.profiles = tr(st.profiles)
            st.skills = tr(st.skills)
            st.projects = tr(_projects(_scenario_runs(SCENARIO_NOW), self.clock.now()))
            st.app_settings = tr(st.app_settings)
            st.workfolder_settings = tr(st.workfolder_settings)
            st.mandate_text = tr(st.mandate_text)
        self.state.last_sim = self.clock.now()
        self._refresh_storage_warning(publish=False)

    def _retention(self) -> RetentionInfo:
        assert self.state.workfolder_settings is not None
        return self.state.workfolder_settings.retention

    def _refresh_storage_warning(self, *, publish: bool = True) -> None:
        """Warning „Speicher knapp“ from the current sizes (docs/08 §4.2: ≥ 90 % of the limit
        or less than 20 GB free); it disappears once enough is freed (R7)."""
        report = self._storage()
        limit = report.limit_bytes or 50 * GB
        others = [w for w in self.state.warnings if w.kind is not WarningKind.STORAGE_LOW]
        warnings = others
        if report.runs_actual >= 0.9 * limit or report.disk_free < 20 * GB:
            hint = "Nichts zu verschlanken. Älteste Runs archivieren?"
            if report.suggestion:
                n = len(report.suggestion.run_ids)
                hint = (
                    f"{n} fertige Runs lassen sich verschlanken "
                    f"({_size_text(report.suggestion.freed_bytes)})."
                )
            warning = WarningInfo(
                id="storage",
                kind=WarningKind.STORAGE_LOW,
                title="Speicher knapp",
                text=f"Runs belegen {_size_text(report.runs_actual)} von "
                f"{_size_text(limit)}. {hint}",
                action=Action(label="Speicher ansehen", url="/speicher"),
            )
            warnings = [warning, *others]
        if warnings == self.state.warnings:
            return
        self.state.warnings = warnings
        if publish:
            self._publish(WarningsChangedEvent(seq=0, ts=self.now(), warnings=warnings))
            self._publish_counts()

    def forced(self, state: "PageState | str", page: str = "") -> "ForcedStateApi":
        """This mock in a forced page state (`?zustand=leer|laedt|fehler`, W1-08) for `page`."""
        return ForcedStateApi(self, PageState(state), page)

    def datasets(self) -> list[tuple[str, str]]:
        """Data sets as `(value, German label)` for the switch in the navigation (mock only)."""
        return [(ds.value, DATASET_LABELS[ds]) for ds in Dataset]

    def switch_dataset(self, dataset: Dataset | str) -> None:
        """Replace all data with another data set; subscribers get fresh counters."""
        dataset = Dataset(dataset)
        self._load(dataset)
        self._publish_counts()
        self._publish(WarningsChangedEvent(seq=0, ts=self.now(), warnings=self.state.warnings))

    # --- clock and live stream ----------------------------------------------------------------

    def now(self) -> float:
        """Mock time (starts at the data set's moment, then runs in real time)."""
        return self.clock.now()

    def subscribe(self, run_id: str | None = None, agents: set[str] | None = None) -> Subscription:
        """Subscribe to the global stream or one run (see `ApiClient.subscribe`)."""
        return self.hub.add(run_id, agents)

    def _publish(self, event: LiveEvent) -> None:
        event.seq = self.hub.next_seq()
        self.hub.publish(event)

    def _publish_counts(self) -> None:
        self._publish(CountsChangedEvent(seq=0, ts=self.now(), counts=self._counts()))

    def _publish_run(self, run: _Run, previous: RunStatus | None = None) -> None:
        self._publish(
            RunStatusEvent(
                seq=0, ts=self.now(), run_id=run.id, run=self._summary(run), previous=previous
            )
        )

    def _publish_approval(self, kind: str, item: ApprovalItem) -> None:
        self._publish(ApprovalEvent(seq=0, ts=self.now(), run_id=item.run.id, type=kind, item=item))  # type: ignore[arg-type]

    async def start_simulation(self, interval: float = 1.0) -> None:
        """Run the live stream in the background until `stop_simulation()`."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._simulate(interval))

    async def stop_simulation(self) -> None:
        """Stop the background live stream."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _simulate(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            self.tick()

    def advance(self, seconds: float) -> None:
        """Move the mock clock forward and run the simulation for that time (tests)."""
        self.clock.advance(seconds)
        self.tick()

    def tick(self) -> None:
        """One simulation step: expiries, status transitions, usage, activity, messages."""
        st = self.state
        now = self.now()
        dt = max(0.0, now - st.last_sim)
        st.last_sim = now
        changed_counts = False
        # deadlines
        for item in list(st.approvals.values()):
            if item.status is ApprovalStatus.OPEN and item.deadline and item.deadline <= now:
                expired = item.model_copy(
                    update={
                        "status": ApprovalStatus.EXPIRED,
                        "status_text": f"abgelaufen, als abgelehnt gewertet "
                        f"({datetime.fromtimestamp(item.deadline).strftime('%H:%M')})",
                    }
                )
                st.approvals[item.id] = expired
                self._unblock_agent(expired, allowed=False)
                self._publish_approval("approval.expired", expired)
                run = st.runs.get(item.run.id)
                if run:
                    self._publish_run(run)
                changed_counts = True
        # scheduled transitions (queued → preparing → running, finalizing → done)
        due = [t for t in st.pending_transitions if t[0] <= now]
        st.pending_transitions = [t for t in st.pending_transitions if t[0] > now]
        for _, rid, status in due:
            run = st.runs.get(rid)
            if run is None:
                continue
            prev = run.status
            run.status = status
            run.status_since = now
            run.status_detail = None
            if status is RunStatus.RUNNING and run.started_at is None:
                run.started_at = now
            if status.is_final:
                run.ended_at = now
            self._publish_run(run, prev)
            changed_counts = True
        # paused runs: the running turns end after a moment
        for run in st.runs.values():
            if run.status is RunStatus.PAUSED and run.turns_end_at and run.turns_end_at <= now:
                run.turns_end_at = None
                self._workers_pause(run, now)
        # usage and activity of running runs
        for run in st.runs.values():
            if run.status is not RunStatus.RUNNING or dt == 0:
                continue
            if run.cost_rate:
                run.spent_usd = (run.spent_usd or 0.0) + run.cost_rate * dt
            if run.token_rate:
                run.tokens_total = int((run.tokens_total or 0) + run.token_rate * dt)
            limits = self._limits(run)
            self._publish(
                UsageTickEvent(
                    seq=0,
                    ts=now,
                    run_id=run.id,
                    elapsed_s=limits.elapsed_s,
                    spent_usd=run.spent_usd,
                    cost_kind=run.cost_kind,
                    tokens_total=run.tokens_total,
                )
            )
            if run.detail_built:
                self._simulate_run_activity(run, now, dt)
        if changed_counts:
            self._publish_counts()

    # --- live stream of the run detail (W1-07) ----------------------------------------------

    _SIM_TOOLS = [
        ("Bash", "python check.py --k 18", "k=18: 262144 Restklassen geprüft\nLaufzeit 2,4 s"),
        ("Bash", "sage teil3.sage", "Fall geprüft: 4096 von 4096\nkein Gegenbeispiel"),
        ("Read", "notes/ideen.md", "# Ideen\n1. Restklassen modulo 64\n2. Paritätsvektoren"),
        (
            "Bash",
            "python stoppzeiten.py --bis 10**9",
            "Stoppzeiten bis 10^9 berechnet\nMittelwert 6,95 · ln n\nLaufzeit 41,2 s",
        ),
        ("Edit", "tex/entwurf.tex", "3 Zeilen geändert"),
    ]
    _SIM_TEXTS = [
        "Die Rechnung bestätigt $T^4(n) < n$ für alle $n ≡ 3 \\pmod{16}$ bis $2^{20}$.",
        "Als Nächstes prüfe ich die Restklasse $27 \\bmod 32$ getrennt.",
        "Die Abweichung fällt ungefähr wie $1 / \\log n$, das passt zur Heuristik.",
        "Ich schreibe das Zwischenergebnis in den Kanal.",
    ]
    _SIM_THOUGHTS = [
        "Ich vergleiche die beiden Rechnungen, bevor ich etwas behaupte.",
        "Der Fall $k = 1$ braucht eine eigene Abschätzung.",
        "Lieber erst die kleinen Fälle per Hand prüfen, dann die Schranke.",
    ]
    _SIM_KINDS = [
        MessageKind.NOTE,
        MessageKind.CLAIM,
        MessageKind.ANSWER,
        MessageKind.IDEA,
        MessageKind.QUESTION,
        MessageKind.OBJECTION,
    ]

    def _sim_chance(self, per_second: float, dt: float) -> bool:
        """True with the probability of an event that happens `per_second` times a second."""
        return self.state.sim_rng.random() < min(1.0, per_second * self.stream_rate * dt)

    def _sim_append(self, run: _Run, agent: AgentState, ev: TranscriptEvent) -> None:
        ev = self._tr(ev)
        transcript = run.transcripts.setdefault(agent.id, _Transcript())
        transcript.extra.append(ev)
        self._publish(
            AgentTranscriptEvent(seq=0, ts=ev.ts, run_id=run.id, agent_id=agent.id, event=ev)
        )

    def _sim_activity(self, run: _Run, agent: AgentState, act: Activity, now: float) -> None:
        act = self._tr(act)
        run.agents[agent.id] = agent.model_copy(update={"activity": act})
        self._publish(
            AgentActivityEvent(seq=0, ts=now, run_id=run.id, agent_id=agent.id, activity=act)
        )

    def _simulate_run_activity(self, run: _Run, now: float, dt: float = 1.0) -> None:
        """Agents of a running run work: tool calls start and finish, text, thinking, turns,
        channel messages (docs/08 §6.6). The pace follows `stream_rate`."""
        rng = self.state.sim_rng
        working = [a for a in run.agents.values() if a.state is AgentDisplayState.WORKING]
        # running tool calls finish
        for agent_id, seq in list(run.running_tools.items()):
            transcript = run.transcripts.get(agent_id)
            ev = transcript.event(seq) if transcript else None
            agent = run.agents.get(agent_id)
            if ev is None or ev.tool is None or agent is None:
                run.running_tools.pop(agent_id, None)
                continue
            if not self._sim_chance(0.3, dt):
                continue
            run.running_tools.pop(agent_id)
            output = next(
                (
                    self._tr(o)
                    for n, a, o in self._SIM_TOOLS
                    if self._tr(a) == ev.tool.input_summary
                ),
                "ok",
            )
            done = ev.model_copy(
                update={
                    "tool": ev.tool.model_copy(
                        update={
                            "status": ToolStatus.OK,
                            "duration_ms": int(max(0.1, now - ev.ts) * 1000),
                            "output_excerpt": output,
                            "output_lines": output.count("\n") + 1,
                        }
                    )
                }
            )
            transcript.replace(done)  # type: ignore[union-attr]
            self._publish(
                AgentTranscriptEvent(seq=0, ts=now, run_id=run.id, agent_id=agent_id, event=done)
            )
            if agent.state is AgentDisplayState.WORKING:
                self._sim_activity(
                    run, agent, Activity(kind=ActivityKind.TEXT, label="schreibt", since=now), now
                )
        # new entries of a working agent without a running tool call
        idle_workers = [a for a in working if a.id not in run.running_tools]
        if idle_workers and self._sim_chance(0.35, dt):
            agent = run.agents[rng.choice(idle_workers).id]
            transcript = run.transcripts.setdefault(agent.id, _Transcript())
            seq, turn = transcript.total, agent.turn or 1
            roll = rng.random()
            if roll < 0.04:
                # the agent ends its turn and starts the next one
                turn += 1
                self._sim_append(
                    run,
                    agent,
                    _ev(seq, now, turn - 1, TranscriptKind.TURN_END, status="ok", cost_usd=0.11),
                )
                self._sim_append(
                    run,
                    agent,
                    _ev(
                        seq + 1,
                        now,
                        turn,
                        TranscriptKind.TURN_START,
                        prompt=f"Turn {turn}. Posteingang: 1 neue Nachricht.",
                        stage=None,
                    ),
                )
                agent = agent.model_copy(update={"turn": turn})
                run.agents[agent.id] = agent
                run.max_turns_used = max(run.max_turns_used, turn)
                self._publish(AgentStateEvent(seq=0, ts=now, run_id=run.id, agent=agent))
            elif roll < 0.6:
                name, arg, _out = rng.choice(self._SIM_TOOLS)
                self._sim_append(
                    run, agent, _tool_event(seq, now, turn, name, arg, ToolStatus.RUNNING)
                )
                run.running_tools[agent.id] = seq
                act = Activity(kind=ActivityKind.TOOL, label=name, arg=arg, since=now, running=True)
                self._sim_activity(run, agent, act, now)
            elif roll < 0.85:
                text = rng.choice(self._SIM_TEXTS)
                self._sim_append(run, agent, _ev(seq, now, turn, TranscriptKind.TEXT, text=text))
            else:
                text = rng.choice(self._SIM_THOUGHTS)
                self._sim_append(
                    run, agent, _ev(seq, now, turn, TranscriptKind.THINKING, text=text)
                )
                act = Activity(kind=ActivityKind.THINKING, label="denkt nach", since=now)
                self._sim_activity(run, agent, act, now)
        # channel messages
        if working and self._sim_chance(0.08, dt):
            open_channels = [c for c in run.channels.values() if c.info.open]
            if open_channels:
                ch = rng.choice(open_channels)
                members = [m.agent for m in ch.info.members]
                senders = [a.id for a in working if a.id in members]
                if senders:
                    sender = rng.choice(senders)
                    kind = rng.choice(self._SIM_KINDS)
                    others = [m for m in members if m != sender]
                    to = [rng.choice(others)] if kind is MessageKind.QUESTION and others else []
                    msg = self._tr(
                        Message(
                            id=ch.count + 1,
                            ts=now,
                            channel=ch.info.name,
                            sender=sender,
                            kind=kind,
                            body=rng.choice(_MATH_SNIPPETS),
                            to=to,
                            open=bool(to),
                        )
                    )
                    ch.extra.append(msg)
                    ch.info = ch.info.model_copy(
                        update={
                            "open_questions": ch.info.open_questions + int(msg.open),
                            "unread_for_user": ch.info.unread_for_user + 1,
                        }
                    )
                    self._publish(ChannelMessageEvent(seq=0, ts=now, run_id=run.id, message=msg))

    def simulate_new_approval(self, run_id: str | None = None) -> ApprovalItem:
        """Create a new open permission request (a simulated macOS notification, docs/08 §9.2)."""
        st = self.state
        run_id = run_id or next(
            (r.id for r in st.runs.values() if r.status is RunStatus.RUNNING), None
        )
        if run_id is None:
            raise _not_found("Laufender Run", "–")
        run = self._run(run_id)
        st.next_approval += 1
        aid = f"apr_{st.next_approval:04d}"
        now = self.now()
        item = ApprovalItem(
            id=aid,
            type=ApprovalType.PERMISSION,
            created_at=now,
            gui_url=f"{self.base_url}/freigaben/{aid}",
            title="explorer-2 möchte eine Datei von arxiv.org laden",
            run=ApprovalRunRef(
                id=run.id,
                name=run.name,
                project=run.project,
                gui_url=f"{self.base_url}/runs/{run.id}",
            ),
            deadline=now + 20 * 60,
            agent=ApprovalAgentRef(
                id="explorer-2",
                role_label="Explorer",
                model="claude-opus-5-5",
                model_label="Claude Opus 5.5",
                provider=Provider.CLAUDE,
                vendor=Vendor.ANTHROPIC,
            ),
            tool="Bash",
            command="curl -sO https://arxiv.org/e-print/2410.01234",
            reason="Ich brauche den Quelltext des Preprints für ein Zitat.",
            risk=RiskInfo(level=RiskLevel.MEDIUM, text="Netzwerkzugriff."),
            categories=[RequestCategory.NETWORK],
            escalated_by=EscalatedByInfo(
                decider=DecidedBy.GATEKEEPER,
                note="Der Aufsichts-Agent ist unsicher und fragt dich.",
            ),
            consequences=Consequences(
                allow="Der Download läuft einmal.",
                deny="explorer-2 arbeitet ohne den Quelltext weiter.",
                expire="Ohne Entscheidung gilt die Anfrage als abgelehnt.",
            ),
            rule_label="Netzwerk zu arxiv.org",
        )
        item = self._tr(item)
        st.approvals[aid] = item
        self._publish_approval("approval.created", item)
        self._publish_run(run)
        self._publish_counts()
        return item

    # --- internal helpers ---------------------------------------------------------------------

    def _run(self, run_id: str) -> _Run:
        run = self.state.runs.get(run_id)
        if run is None:
            raise _not_found("Run", run_id)
        return run

    def _detail(self, run_id: str) -> _Run:
        run = self._run(run_id)
        if not run.detail_built:
            _DETAIL_BUILDERS.get(run_id, _build_generic_detail)(run)
            run.detail_built = True
            if run_id not in _DETAIL_BUILDERS:
                self._agents_follow_approvals(run)
        return run

    def _open_items(self, run_id: str | None = None) -> list[ApprovalItem]:
        return [
            a
            for a in self.state.approvals.values()
            if a.status is ApprovalStatus.OPEN and (run_id is None or a.run.id == run_id)
        ]

    def _limits(self, run: _Run) -> RunLimits:
        now = self.now()
        if run.elapsed_frozen is not None:
            elapsed = run.elapsed_frozen
        elif run.started_at is None:
            elapsed = 0
        else:
            elapsed = (run.ended_at or now) - run.started_at
        return RunLimits(
            wall_time_s=run.wall_time_s,
            elapsed_s=int(elapsed),
            budget_usd=run.budget_usd,
            spent_usd=round(run.spent_usd, 2) if run.spent_usd is not None else None,
            turns_per_agent=run.turns_per_agent,
            max_turns_used=run.max_turns_used,
        )

    def _summary(self, run: _Run) -> RunSummary:
        items = self._open_items(run.id)
        open_escalation = run.status is RunStatus.PAUSED and any(
            i.type is ApprovalType.ESCALATION for i in items
        )
        limits = self._limits(run)
        by_state = AgentsByState()
        if run.detail_built or run.id in _DETAIL_BUILDERS:
            counts: dict[str, int] = {}
            for agent in self._detail(run.id).agents.values():
                counts[agent.state.value] = counts.get(agent.state.value, 0) + 1
            by_state = AgentsByState(**counts)
        duration = None
        if run.started_at is not None:
            duration = limits.elapsed_s
        return RunSummary(
            id=run.id,
            name=run.name,
            project=run.project,
            skill=run.skill,
            topology=run.topology,
            status=run.status,
            status_since=run.status_since,
            status_detail=run.status_detail,
            active=run.status.is_active,
            open_approvals=len(items),
            open_escalation=open_escalation,
            starter=run.starter,
            created_at=run.created_at,
            started_at=run.started_at,
            ended_at=run.ended_at,
            duration_s=duration,
            stage=run.stage,
            agents_by_state=by_state,
            limits=limits,
            tokens_total=run.tokens_total,
            cost_usd=round(run.spent_usd, 2) if run.spent_usd is not None else None,
            cost_kind=run.cost_kind if run.spent_usd is not None else None,
            size=run.size,
            pinned=run.pinned,
            slimmed=run.slimmed,
            archived=run.archived,
            best_result=run.best_result,
            last_message=self._last_message(run),
            error=run.error,
            info=run.info,
            adjusted_by_user=run.adjusted_by_user,
            changes=run.changes,
            gui_url=f"{self.base_url}/runs/{run.id}",
        )

    def _last_message(self, run: _Run) -> LastMessage | None:
        if not run.detail_built:
            return None
        best: Message | None = None
        unread = 0
        for ch in run.channels.values():
            if ch.count:
                m = ch.message(ch.count)
                if m and (best is None or m.ts > best.ts):
                    best = m
            unread += ch.info.unread_for_user
        if best is None:
            return None
        return LastMessage(
            channel=best.channel,
            sender=best.sender,
            excerpt=best.body[:80],
            ts=best.ts,
            unread_for_user=unread,
        )

    def _counts(self) -> GlobalCounts:
        runs = [self._summary(r) for r in self.state.runs.values()]
        return GlobalCounts(
            waiting=count_waiting(self.state.approvals.values()),
            active=count_active(runs),
            warnings=len(self.state.warnings),
        )

    def _unblock_agent(self, item: ApprovalItem, *, allowed: bool) -> None:
        if item.type is not ApprovalType.PERMISSION or item.agent is None:
            return
        run = self.state.runs.get(item.run.id)
        if run is None or not run.detail_built or item.agent.id not in run.agents:
            return
        agent = run.agents[item.agent.id]
        if agent.blocked_by is None or agent.blocked_by.approval_id != item.id:
            return
        now = self.now()
        act = Activity(
            kind=ActivityKind.TOOL if allowed else ActivityKind.TEXT,
            label=item.tool or "Bash" if allowed else "arbeitet ohne die Freigabe weiter",
            arg=item.command if allowed else None,
            since=now,
            running=allowed,
        )
        run.agents[agent.id] = agent.model_copy(
            update={
                "state": AgentDisplayState.WORKING,
                "blocked_by": None,
                "activity": act,
                "state_since": now,
            }
        )
        self._publish(AgentStateEvent(seq=0, ts=now, run_id=run.id, agent=run.agents[agent.id]))
        # the waiting tool call in the transcript runs now, or ends as denied (live stream)
        transcript = run.transcripts.get(agent.id)
        if transcript is None:
            return
        for seq in range(transcript.total - 1, max(-1, transcript.total - 400), -1):
            ev = transcript.event(seq)
            if ev is None or ev.tool is None or ev.tool.approval_id != item.id:
                continue
            tool = ev.tool.model_copy(
                update={"status": ToolStatus.RUNNING}
                if allowed
                else {
                    "status": ToolStatus.ERROR,
                    "output_excerpt": "Nicht erlaubt: Die Freigabe wurde abgelehnt.",
                    "output_lines": 1,
                }
            )
            changed = ev.model_copy(update={"tool": tool})
            transcript.replace(changed)
            if allowed:
                run.running_tools[agent.id] = seq
            self._publish(
                AgentTranscriptEvent(seq=0, ts=now, run_id=run.id, agent_id=agent.id, event=changed)
            )
            break

    def _workers_pause(self, run: _Run, now: float) -> None:
        """A paused run: working agents finish their turn and wait (docs/08 §6.7)."""
        if not run.detail_built:
            return
        for agent in list(run.agents.values()):
            if agent.state is not AgentDisplayState.WORKING:
                continue
            run.paused_workers.append(agent.id)
            run.running_tools.pop(agent.id, None)
            idle = agent.model_copy(
                update={
                    "state": AgentDisplayState.IDLE,
                    "state_since": now,
                    "activity": Activity(
                        kind=ActivityKind.WAIT_MESSAGES, label="Run pausiert, Turn beendet"
                    ),
                }
            )
            run.agents[agent.id] = idle
            self._publish(AgentStateEvent(seq=0, ts=now, run_id=run.id, agent=idle))

    def _workers_resume(self, run: _Run, now: float) -> None:
        """After „Fortsetzen“ the agents stopped by the pause work again."""
        for agent_id in run.paused_workers:
            agent = run.agents.get(agent_id)
            if agent is None or agent.state is not AgentDisplayState.IDLE:
                continue
            busy = agent.model_copy(
                update={
                    "state": AgentDisplayState.WORKING,
                    "state_since": now,
                    "activity": Activity(
                        kind=ActivityKind.TEXT, label="arbeitet weiter", since=now
                    ),
                }
            )
            run.agents[agent_id] = busy
            self._publish(AgentStateEvent(seq=0, ts=now, run_id=run.id, agent=busy))
        run.paused_workers = []

    def _agents_follow_approvals(self, run: _Run) -> None:
        """Agents with an open permission request wait for you (keeps generic runs consistent)."""
        for item in self._open_items(run.id):
            if item.type is not ApprovalType.PERMISSION or item.agent is None:
                continue
            blocked = BlockedBy(
                kind=BlockedKind.APPROVAL,
                approval_id=item.id,
                summary=item.title,
                deadline=item.deadline,
            )
            act = Activity(
                kind=ActivityKind.WAIT_APPROVAL,
                label=item.tool or "Anfrage",
                arg=item.command or item.path or item.url,
                since=item.created_at,
            )
            agent = run.agents.get(item.agent.id)
            if agent is None:
                agent = _agent(
                    run.id,
                    item.agent.id,
                    item.agent.role_label,
                    item.agent.model,
                    AgentDisplayState.WAITING_USER,
                    act,
                    blocked_by=blocked,
                    since=item.created_at,
                    turn=4,
                    channels=list(run.channels),
                )
                run.transcripts[agent.id] = _Transcript()
                start = _ev(
                    0,
                    item.created_at - 90,
                    4,
                    TranscriptKind.TURN_START,
                    prompt="Turn 4. Arbeite an deiner Idee weiter.",
                    stage=None,
                )
                run.transcripts[agent.id].extra.append(start)
            elif agent.blocked_by is None:
                agent = agent.model_copy(
                    update={
                        "state": AgentDisplayState.WAITING_USER,
                        "blocked_by": blocked,
                        "activity": act,
                        "state_since": item.created_at,
                    }
                )
            run.agents[agent.id] = agent
            # the request is the tool call the agent waits on (transcript and card agree)
            transcript = run.transcripts.setdefault(agent.id, _Transcript())
            transcript.extra.append(
                _tool_event(
                    transcript.total,
                    item.created_at,
                    agent.turn or 1,
                    item.tool or "Bash",
                    item.command or item.path or item.url or "",
                    ToolStatus.WAITING_APPROVAL,
                    approval_id=item.id,
                )
            )

    # --- ApiClient: workfolder, counts, warnings ----------------------------------------------

    async def get_workfolder(self) -> WorkfolderInfo:
        """Current workfolder."""
        wf = self.state.workfolder
        return wf.model_copy(update={"runs_count": len(self.state.runs)})

    async def get_counts(self) -> GlobalCounts:
        """Global counters (docs/08 §8.3)."""
        return self._counts()

    async def list_warnings(self) -> list[WarningInfo]:
        """Global warnings."""
        return list(self.state.warnings)

    # --- ApiClient: approvals ----------------------------------------------------------------

    async def list_approvals(
        self,
        status: Iterable[ApprovalStatus] | None = None,
        run_id: str | None = None,
        type: ApprovalType | None = None,  # noqa: A002 - mirrors the REST parameter
        since: float | None = None,
    ) -> list[ApprovalItem]:
        """Inbox items, filtered; open ones deadline first then oldest, others newest first."""
        wanted = set(status) if status is not None else None
        items = [
            a
            for a in self.state.approvals.values()
            if (wanted is None or a.status in wanted)
            and (run_id is None or a.run.id == run_id)
            and (type is None or a.type is type)
            and (since is None or (a.decision.decided_at if a.decision else a.created_at) >= since)
        ]
        open_items = sorted(
            (a for a in items if a.status is ApprovalStatus.OPEN),
            key=lambda a: (a.deadline is None, a.deadline or 0, a.created_at),
        )
        rest = sorted(
            (a for a in items if a.status is not ApprovalStatus.OPEN),
            key=lambda a: a.decision.decided_at if a.decision else (a.deadline or a.created_at),
            reverse=True,
        )
        return open_items + rest

    async def get_approval(self, approval_id: str) -> ApprovalItem:
        """One inbox item."""
        item = self.state.approvals.get(approval_id)
        if item is None:
            raise _not_found("Freigabe", approval_id)
        return item

    async def decide_approval(self, approval_id: str, request: DecisionRequest) -> DecisionResponse:
        """Decide an open inbox item and update runs, agents and counters."""
        item = await self.get_approval(approval_id)
        now = self.now()
        hhmm = datetime.fromtimestamp(now).strftime("%H:%M")
        if item.status is not ApprovalStatus.OPEN:
            raise ApiException.of(
                ErrorCode.CONFLICT,
                "Diese Freigabe ist schon entschieden oder nicht mehr nötig.",
                gui_url=item.gui_url,
            )
        allowed = {
            ApprovalType.PERMISSION: {DecisionAction.ALLOW, DecisionAction.DENY},
            ApprovalType.RUN_START: {
                DecisionAction.ALLOW,
                DecisionAction.DENY,
                DecisionAction.ADJUST,
            },
            ApprovalType.ESCALATION: {DecisionAction.OPTION},
        }[item.type]
        if request.action not in allowed:
            raise ApiException.of(
                ErrorCode.INVALID, f"„{request.action}“ passt nicht zu dieser Freigabe."
            )
        if item.type is not ApprovalType.PERMISSION and request.scope is not DecisionScope.ONCE:
            raise ApiException.of(ErrorCode.INVALID, "Run-Starts und Eskalationen gelten einmalig.")
        run = self._run(item.run.id)
        changes: list[ChangeInfo] = []
        if request.action is DecisionAction.ADJUST:
            for path, value in (request.config or {}).items():
                old = _requested_value(item, path)
                if old != value:
                    changes.append(
                        ChangeInfo(
                            path=path,
                            from_=old,
                            to=value,
                            label=_change_label(path, old, value, item.skill),
                        )
                    )
            if not changes:
                raise ApiException.of(
                    ErrorCode.INVALID,
                    "Du hast nichts geändert. Nimm „Freigeben“, wenn der Run so starten soll.",
                )
        decision = DecisionInfo(
            action=request.action,
            scope=request.scope,
            option_key=request.option_key,
            changes=changes,
            comment=request.comment or None,
            decided_by=DecidedBy.USER,
            decided_at=now,
        )
        revocable = item.type is ApprovalType.PERMISSION and request.scope in (
            DecisionScope.AGENT,
            DecisionScope.RUN,
        )
        decided = item.model_copy(
            update={"status": ApprovalStatus.DECIDED, "decision": decision, "revocable": revocable}
        )
        self.state.approvals[item.id] = decided
        previous = run.status
        message = self._apply_decision(decided, run, request, changes, hhmm)
        self._publish_approval("approval.decided", decided)
        self._publish_run(run, previous)
        self._publish_counts()
        return DecisionResponse(item=decided, run_id=run.id, changes=changes, message=message)

    def _apply_decision(
        self,
        item: ApprovalItem,
        run: _Run,
        request: DecisionRequest,
        changes: list[ChangeInfo],
        hhmm: str,
    ) -> str:
        now = self.now()
        action = request.action
        if item.type is ApprovalType.PERMISSION:
            allowed = action is DecisionAction.ALLOW
            self._unblock_agent(item, allowed=allowed)
            who = item.agent.id if item.agent else "Agent"
            target = item.rule_label or item.command or ""
            if request.scope is DecisionScope.ALWAYS and allowed:
                self.state.user_rules.append(
                    FixedRule(
                        id=f"user-{len(self.state.user_rules) + 1}",
                        effect=RuleEffect.ALLOW,
                        rule=target,
                        origin="user",
                        origin_text="von dir: Reichweite „immer“ bei einer Freigabe am "
                        f"{datetime.fromtimestamp(self.now()):%d.%m.}",
                        removable=True,
                        created_at=self.now(),
                    )
                )
                return f"Erlaubt (immer): neue feste Regel „{target}“"
            if not allowed:
                return f"Abgelehnt: {who} · {target}"
            scope_text = {
                DecisionScope.ONCE: "einmalig",
                DecisionScope.AGENT: f"für {who} in diesem Run",
                DecisionScope.RUN: "für diesen Run",
                DecisionScope.ALWAYS: "immer",
            }[request.scope]
            return f"Erlaubt ({scope_text}): {who} · {target}"
        if item.type is ApprovalType.RUN_START:
            if action is DecisionAction.DENY:
                run.status = RunStatus.STOPPED
                run.status_since = now
                run.ended_at = now
                run.info = "Run-Start von dir abgelehnt"
                return "Run-Start abgelehnt. Claude Code erhält deine Nachricht."
            if action is DecisionAction.ADJUST:
                run.adjusted_by_user = True
                run.changes = changes
                self._apply_changes(run, changes)
            run.status = RunStatus.QUEUED
            run.status_since = now
            run.status_detail = self._tr("wartet auf 2 Plätze bei max-privat")
            run.info = None
            self.state.pending_transitions += [
                (now + 5, run.id, RunStatus.PREPARING),
                (now + 12, run.id, RunStatus.RUNNING),
            ]
            if action is DecisionAction.ADJUST:
                return f"Angepasst und freigegeben: {run.name}"
            return f"Run freigegeben: {run.name}"
        # escalation
        option = request.option_key
        if option is EscalationOption.END:
            run.status = RunStatus.FINALIZING
            run.status_since = now
            run.elapsed_frozen = None
            self.state.pending_transitions.append((now + 8, run.id, RunStatus.DONE))
            return f"Run wird beendet: {run.name}. Die Zusammenfassung wird erstellt."
        run.status = RunStatus.RUNNING
        run.status_since = now
        if run.elapsed_frozen is not None and run.started_at is not None:
            run.started_at = now - run.elapsed_frozen
            run.elapsed_frozen = None
        run.cost_rate = run.cost_rate or 0.001
        if run.detail_built:
            stages = []
            for s in run.stages:
                if s.status is StageStatus.WAITING_USER:
                    if option is EscalationOption.RETRY:
                        s = s.model_copy(
                            update={
                                "status": StageStatus.RUNNING,
                                "attempts": [*s.attempts, Attempt(n=len(s.attempts) + 1)],
                            }
                        )
                    else:
                        s = s.model_copy(update={"status": StageStatus.FAILED})
                stages.append(s)
            if option is EscalationOption.CONTINUE:
                for i, s in enumerate(stages):
                    if s.status is StageStatus.TODO:
                        stages[i] = s.model_copy(update={"status": StageStatus.RUNNING})
                        break
            run.stages = stages
        if option is EscalationOption.RETRY:
            run.info = "Noch ein Versuch läuft"
            worker = item.comment_to.id if item.comment_to else "Der Beweiser"
            return f"Noch ein Versuch: {worker} überarbeitet die Stufe „{item.stage}“."
        run.info = "Trotzdem weiter"
        return "Trotzdem weiter: Die nächste Stufe startet."

    def _apply_changes(self, run: _Run, changes: list[ChangeInfo]) -> None:
        """Your changes from „Anpassen“ go into the run (docs/08 §5.2: same run ID)."""
        detail = self._detail(run.id)
        for change in changes:
            parts = change.path.split(".")
            if parts[0] == "limits" and len(parts) == 2 and hasattr(run, parts[1]):
                setattr(run, parts[1], change.to)
            elif parts[0] == "decider" and change.to:
                run.decider = Decider(change.to)
            elif parts[0] == "task_md":
                run.task_md = str(change.to or "")
            elif parts[0] == "agents" and parts[1] in detail.agents:
                agent = detail.agents[parts[1]]
                if parts[-1] == "web":
                    agent = agent.model_copy(
                        update={"tools": agent.tools.model_copy(update={"web": bool(change.to)})}
                    )
                elif parts[-1] == "read_dirs":
                    agent = agent.model_copy(update={"read_dirs": list(change.to or [])})
                detail.agents[agent.id] = agent

    async def revoke_approval(self, approval_id: str) -> ApprovalItem:
        """Withdraw a revocable decision (gatekeeper, or user with scope agent/run)."""
        item = await self.get_approval(approval_id)
        if not item.revocable:
            raise ApiException.of(
                ErrorCode.NOT_ALLOWED,
                "Diese Entscheidung lässt sich nicht zurückziehen. Entscheidungen der "
                "Vollmacht-Prüfung änderst du über die Vollmacht.",
                action=Action(label="Vollmacht ansehen", url="/einstellungen#vollmacht"),
            )
        hhmm = datetime.fromtimestamp(self.now()).strftime("%H:%M")
        revoked = item.model_copy(
            update={
                "status": ApprovalStatus.REVOKED,
                "revocable": False,
                "status_text": f"zurückgezogen um {hhmm}",
            }
        )
        self.state.approvals[item.id] = revoked
        self._publish_approval("approval.revoked", revoked)
        return revoked

    # --- ApiClient: runs -----------------------------------------------------------------------

    async def list_runs(
        self,
        status: RunFilter = RunFilter.ALL,
        project: str | None = None,
        starter: Starter | None = None,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RunsPage:
        """Runs page; active first (by start, newest first), then finished by end."""
        summaries = [self._summary(r) for r in self.state.runs.values()]
        base = [
            s
            for s in summaries
            if (project is None or s.project == project)
            and (starter is None or s.starter == starter)
        ]
        needle = (q or "").strip().lower()
        if needle:
            base = [s for s in base if needle in s.name.lower() or needle in s.id.lower()]
        counts = RunCounts(
            all=len(base),
            active=count_active(base),
            waiting=count_runs_waiting(base),
            done=sum(1 for s in base if s.status is RunStatus.DONE),
            error=sum(1 for s in base if s.status in (RunStatus.FAILED, RunStatus.INTERRUPTED)),
            archived=sum(1 for s in base if s.archived),
        )
        test = {
            RunFilter.ALL: lambda s: True,
            RunFilter.ACTIVE: lambda s: s.active,
            RunFilter.WAITING: lambda s: s.open_approvals > 0,
            RunFilter.DONE: lambda s: s.status is RunStatus.DONE,
            RunFilter.ERROR: lambda s: s.status in (RunStatus.FAILED, RunStatus.INTERRUPTED),
            RunFilter.ARCHIVED: lambda s: s.archived,
        }[status]
        rows = [s for s in base if test(s)]
        rows.sort(key=lambda s: (not s.active, -(s.created_at if s.active else (s.ended_at or 0))))
        offset = int(cursor or 0)
        page = rows[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(rows) else None
        return RunsPage(
            items=page,
            counts=counts,
            total=len(rows),
            next_cursor=next_cursor,
            projects=sorted({s.project for s in summaries}),
        )

    async def get_run(self, run_id: str) -> RunDetail:
        """Run detail head with banner data, stages and allowed actions."""
        run = self._detail(run_id)
        summary = self._summary(run)
        items = await self.list_approvals(status=[ApprovalStatus.OPEN], run_id=run_id)
        top = None
        if items:
            first = items[0]
            top = ApprovalTop(
                id=first.id,
                summary=first.title,
                risk=first.risk,
                deadline=first.deadline,
                more=len(items) - 1,
                gui_url=first.gui_url,
            )
        questions = sum(
            1
            for ch in run.channels.values()
            for mid in ch.fixed
            if (m := ch.fixed[mid]).open and "user" in m.to
        )
        if run.status.is_final:
            actions = [
                RunAction.PIN if not run.pinned else RunAction.UNPIN,
                RunAction.SLIM,
                RunAction.ARCHIVE,
                RunAction.DELETE,
            ]
        elif run.status is RunStatus.PAUSED:
            actions = (
                [RunAction.STOP] if summary.open_escalation else [RunAction.RESUME, RunAction.STOP]
            )
        elif run.status is RunStatus.RUNNING:
            actions = [RunAction.PAUSE, RunAction.STOP]
        else:
            actions = [RunAction.STOP]
        handoff = (
            f"Run {run.id} ist fertig. Lies die Zusammenfassung und die Ergebnisse "
            f"(read_run_summary, read_results){run.handoff_extra}."
            if run.status.is_final
            else f"Run {run.id} läuft noch. Schau mit run_status nach, wie weit er ist."
        )
        return RunDetail(
            run=summary,
            open_approvals_top=top,
            questions_to_user=questions,
            decider=run.decider,
            actions_allowed=actions,
            stages=run.stages,
            handoff_text=handoff,
            run_dir=f"{self.state.workfolder.path}/runs/{run.id}",
            task_md=run.task_md,
        )

    async def run_action(
        self, run_id: str, action: RunAction, dry_run: bool = False
    ) -> ActionResult:
        """Pause, resume, stop, slim, archive, delete, pin or unpin a run."""
        run = self._run(run_id)
        now = self.now()
        hhmm = datetime.fromtimestamp(now).strftime("%H:%M")
        prev = run.status
        if action is RunAction.RESUME:
            if any(i.type is ApprovalType.ESCALATION for i in self._open_items(run_id)):
                item = next(
                    i for i in self._open_items(run_id) if i.type is ApprovalType.ESCALATION
                )
                raise ApiException.of(
                    ErrorCode.NOT_ALLOWED,
                    "Der Run wartet auf eine Entscheidung in der GUI. Weiter geht es nur über die "
                    "Eskalation.",
                    gui_url=item.gui_url,
                    action=Action(label="Eskalation ansehen", url=f"/freigaben/{item.id}"),
                )
            if run.status is not RunStatus.PAUSED:
                raise ApiException.of(ErrorCode.NOT_ALLOWED, "Der Run ist nicht pausiert.")
        if action in (RunAction.SLIM, RunAction.ARCHIVE, RunAction.DELETE) and run.status.is_active:
            raise ApiException.of(
                ErrorCode.NOT_ALLOWED,
                "Aktive Runs lassen sich nicht verschlanken, archivieren oder "
                "löschen. Stoppe den Run zuerst.",
            )
        if action is RunAction.PAUSE and run.status is not RunStatus.RUNNING:
            raise ApiException.of(ErrorCode.NOT_ALLOWED, "Nur laufende Runs lassen sich pausieren.")
        if action is RunAction.STOP and not run.status.is_active:
            raise ApiException.of(ErrorCode.NOT_ALLOWED, "Der Run ist schon beendet.")
        freed = run.size.work if action is RunAction.SLIM else None
        if dry_run and action is RunAction.SLIM:
            return ActionResult(
                ok=True,
                message=f"Frei werden bis zu {_size_text(freed or 0)}: Arbeitskopien und Snapshot."
                "\nEs bleiben Transkripte, Roh-Events, Diffs, Gruppenchats, Output und Usage.",
                freed_bytes=freed,
            )
        if dry_run:
            return ActionResult(
                ok=True, message=f"Vorschau: {action.value} für „{run.name}“", freed_bytes=freed
            )
        if action is RunAction.PAUSE:
            run.status = RunStatus.PAUSED
            run.status_since = now
            run.elapsed_frozen = self._limits(run).elapsed_s
            run.turns_end_at = now + PAUSE_TURNS_END_S
            message = f"Pausiert: {run.name}"
        elif action is RunAction.RESUME:
            run.status = RunStatus.RUNNING
            run.turns_end_at = None
            self._workers_resume(run, now)
            if run.elapsed_frozen is not None:
                run.started_at = now - run.elapsed_frozen
                run.elapsed_frozen = None
            message = f"Fortgesetzt: {run.name}"
        elif action is RunAction.STOP:
            run.elapsed_frozen = self._limits(run).elapsed_s if run.started_at else None
            run.status = RunStatus.STOPPED
            run.status_since = now
            run.ended_at = now
            if run.started_at is not None and run.elapsed_frozen is not None:
                run.started_at = now - run.elapsed_frozen
            run.elapsed_frozen = None
            for item in self._open_items(run_id):
                obsolete = item.model_copy(
                    update={
                        "status": ApprovalStatus.OBSOLETE,
                        "status_text": f"Nicht mehr nötig: Der Run wurde um {hhmm} gestoppt.",
                    }
                )
                self.state.approvals[item.id] = obsolete
                self._publish_approval("approval.expired", obsolete)
            message = f"Gestoppt: {run.name}"
        elif action in (RunAction.PIN, RunAction.UNPIN):
            run.pinned = action is RunAction.PIN
            message = "Run angeheftet" if run.pinned else "Nicht mehr angeheftet"
        elif action is RunAction.SLIM:
            run.slimmed = True
            run.size = run.size.model_copy(
                update={"actual_bytes": run.size.actual_bytes - run.size.work, "work": 0}
            )
            wf = self.state.workfolder
            self.state.workfolder = wf.model_copy(
                update={"disk_free_bytes": wf.disk_free_bytes + (freed or 0)}
            )
            message = f"Verschlankt: {run.name}"
        elif action is RunAction.ARCHIVE:
            run.archived = True
            message = f"Archiviert: {run.name}"
        else:
            del self.state.runs[run_id]
            self._publish_counts()
            self._refresh_storage_warning()
            return ActionResult(ok=True, message=f"In den Papierkorb verschoben: {run.name}")
        self._publish_run(run, prev)
        self._publish_counts()
        if action in (RunAction.SLIM, RunAction.ARCHIVE):
            self._refresh_storage_warning()
        return ActionResult(ok=True, message=message, run=self._summary(run), freed_bytes=freed)

    async def list_channels(self, run_id: str) -> list[ChannelInfo]:
        """Channels of a run with message counts."""
        run = self._detail(run_id)
        return [
            ch.info.model_copy(
                update={"message_count": ch.count, "last_message_id": ch.count or None}
            )
            for ch in run.channels.values()
        ]

    async def get_messages(
        self,
        run_id: str,
        channel: str,
        before_id: int | None = None,
        since_id: int | None = None,
        limit: int = 50,
    ) -> MessagesPage:
        """Messages, oldest first within the page (default: the newest `limit`)."""
        run = self._detail(run_id)
        ch = run.channels.get(channel)
        if ch is None:
            raise _not_found("Kanal", f"#{channel}")
        total = ch.count
        if since_id is not None:
            first, last = since_id + 1, min(total, since_id + limit)
        else:
            last = min(total, (before_id - 1) if before_id else total)
            first = max(1, last - limit + 1)
        items = [m for i in range(first, last + 1) if (m := ch.message(i)) is not None]
        return MessagesPage(
            channel=channel, items=items, total=total, older_count=max(0, first - 1)
        )

    async def post_message(self, run_id: str, channel: str, request: PostMessageRequest) -> Message:
        """Post as the user; delivery depends on the addressees' state (docs/08 §6.5)."""
        run = self._detail(run_id)
        ch = run.channels.get(channel)
        if ch is None:
            raise _not_found("Kanal", f"#{channel}")
        if run.status.is_final:
            raise ApiException.of(
                ErrorCode.NOT_ALLOWED,
                "Der Run ist beendet. Nachrichten gehen nur an laufende Runs.",
            )
        now = self.now()
        targets = request.to or [m.agent for m in ch.info.members]
        states = [run.agents[a].state for a in targets if a in run.agents]
        if ch.info.mode is ChannelMode.ROUNDS or run.status is RunStatus.PAUSED:
            status = DeliveryStatus.NEXT_INBOX
        elif any(s is AgentDisplayState.WORKING for s in states):
            status = DeliveryStatus.HINT_MID_TURN
        elif states:
            status = DeliveryStatus.WOKEN
        else:
            status = DeliveryStatus.PENDING
        msg = Message(
            id=ch.count + 1,
            ts=now,
            channel=channel,
            sender="user",
            via=MessageVia.GUI,
            kind=request.kind,
            body=request.body,
            to=request.to,
            open=request.kind is MessageKind.QUESTION and bool(request.to),
            delivery=DeliveryInfo(status=status, ts=now),
        )
        ch.extra.append(msg)
        self._publish(ChannelMessageEvent(seq=0, ts=now, run_id=run.id, message=msg))
        return msg

    async def list_agents(self, run_id: str) -> list[AgentState]:
        """Agent cards in the order of the data set."""
        return list(self._detail(run_id).agents.values())

    async def get_transcript(
        self,
        run_id: str,
        agent_id: str,
        from_seq: int | None = None,
        limit: int = 200,
        kinds: Iterable[TranscriptKind] | None = None,
    ) -> TranscriptPage:
        """Transcript page; without `from_seq` the latest `limit` entries."""
        run = self._detail(run_id)
        t = run.transcripts.get(agent_id)
        if t is None:
            if agent_id not in run.agents:
                raise _not_found("Agent", agent_id)
            t = run.transcripts.setdefault(agent_id, _Transcript())
        limit = max(1, min(limit, 200))
        wanted = set(kinds) if kinds is not None else None
        total = t.total
        start = max(0, total - limit) if from_seq is None else from_seq
        items: list[TranscriptEvent] = []
        seq = start
        while seq < total and len(items) < limit:
            ev = t.event(seq)
            if ev is not None and (wanted is None or ev.kind in wanted):
                items.append(ev)
            seq += 1
        return TranscriptPage(
            agent_id=agent_id,
            items=items,
            total=total,
            next_from_seq=seq if seq < total else None,
            older=t.older,
        )

    async def get_results(self, run_id: str) -> RunResults:
        """Results with ratings and the summary."""
        run = self._detail(run_id)
        return RunResults(
            items=run.results,
            summary_md=run.summary_md,
            summary_path="output/summary.md" if run.summary_md else None,
            empty_text=run.results_empty_text,
        )

    async def get_log(self, run_id: str) -> list[LogEntry]:
        """Orchestrator log [S]."""
        return list(self._detail(run_id).log)

    async def get_changes(self, run_id: str) -> list[AgentChanges]:
        """File changes per agent [S]."""
        return list(self._detail(run_id).file_changes)

    # --- ApiClient: profiles, projects, skills ------------------------------------------------

    async def list_profiles(self) -> list[ProfileInfo]:
        """Auth profiles (never a secret)."""
        return list(self.state.profiles)

    async def test_profile(self, name: str) -> ProfileInfo:
        """Simulated test: refreshes `last_test.ts`, keeps the outcome."""
        for i, p in enumerate(self.state.profiles):
            if p.name == name:
                test = (p.last_test or ProfileTest(ts=0, ok=True)).model_copy(
                    update={"ts": self.now()}
                )
                self.state.profiles[i] = p.model_copy(update={"last_test": test})
                return self.state.profiles[i]
        raise _not_found("Profil", name)

    async def list_projects(self) -> list[ProjectInfo]:
        """Projects with run counters."""
        return [await self.get_project(p.name) for p in self.state.projects]

    async def get_project(self, name: str) -> ProjectInfo:
        """One project with current run counters."""
        for p in self.state.projects:
            if p.name == name:
                summaries = [
                    self._summary(r) for r in self.state.runs.values() if r.project == name
                ]
                return p.model_copy(
                    update={
                        "runs": ProjectRuns(
                            total=len(summaries),
                            active=count_active(summaries),
                            waiting=count_runs_waiting(summaries),
                        )
                    }
                )
        raise _not_found("Projekt", name)

    async def list_skills(self, kind: SkillKind | None = None) -> list[SkillInfo]:
        """Skills, optionally of one kind, with their use in the runs („Benutzt von“)."""
        return [self._with_usage(s) for s in self.state.skills if kind is None or s.kind is kind]

    async def get_skill(self, name: str) -> SkillInfo:
        """One skill."""
        for s in self.state.skills:
            if s.name == name:
                return self._with_usage(s)
        raise _not_found("Skill", name)

    def _with_usage(self, skill: SkillInfo) -> SkillInfo:
        if skill.kind is not SkillKind.GROUP:
            return skill
        runs = [r for r in self.state.runs.values() if r.skill == skill.name]
        if not runs:
            return skill.model_copy(update={"used_by": SkillUsage()})
        usage = SkillUsage(
            projects=sorted({r.project for r in runs}),
            runs_count=len(runs),
            last_run_at=max(r.created_at for r in runs),
        )
        return skill.model_copy(update={"used_by": usage})

    # --- ApiClient: usage, storage, new run, setup --------------------------------------------

    async def get_usage(
        self,
        from_ts: float | None = None,
        to_ts: float | None = None,
        group_by: UsageGroupBy = UsageGroupBy.PROFILE,
        project: str | None = None,
    ) -> UsageReport:
        """Usage of the runs in the period, split by profile kind (docs/08 §5.9).

        Subscriptions count at the API list price („≈“, `estimated`), API keys are billed,
        Codex reports no price („–“). Each run is split over the profiles in fixed shares.
        """
        if group_by is UsageGroupBy.AGENT:
            raise ApiException.of(
                ErrorCode.INVALID, "Die Aufschlüsselung nach Agent kommt in einer späteren Version."
            )
        now = self.now()
        from_ts = (
            from_ts
            if from_ts is not None
            else datetime.fromtimestamp(now).replace(day=1, hour=0, minute=0, second=0).timestamp()
        )
        to_ts = to_ts if to_ts is not None else now
        runs = [
            r
            for r in self.state.runs.values()
            if from_ts <= r.created_at <= to_ts and (project is None or r.project == project)
        ]
        rows: dict[str, UsageRow] = {}
        run_sets: dict[str, set[str]] = {}
        for r in runs:
            for part in self._usage_parts(r):
                key, label = {
                    UsageGroupBy.PROFILE: (part["profile"], part["profile_label"]),
                    UsageGroupBy.MODEL: (part["model"], _model_label(part["model"])),
                    UsageGroupBy.PROJECT: (r.project, r.project),
                    UsageGroupBy.RUN: (r.id, r.name),
                }[group_by]
                row = rows.get(key) or UsageRow(key=key, label=label)
                rows[key] = _add_usage(row, part)
                run_sets.setdefault(key, set()).add(r.id)
        for key, row in rows.items():
            rows[key] = row.model_copy(update={"runs": len(run_sets[key])})
        total = UsageRow(key="total", label="Gesamt")
        for row in rows.values():
            total = _add_usage(total, row.model_dump())
        total = total.model_copy(update={"runs": len(runs)})
        estimated_parts = [
            p["cost_usd"]
            for r in runs
            for p in self._usage_parts(r)
            if p["cost_kind"] is CostKind.ESTIMATED and p["cost_usd"] is not None
        ]
        billed_parts = [
            p["cost_usd"]
            for r in runs
            for p in self._usage_parts(r)
            if p["cost_kind"] is CostKind.BILLED and p["cost_usd"] is not None
        ]
        billed = round(sum(billed_parts), 2)
        read = total.cache_read or 0
        fed = read + (total.input or 0) + (total.cache_write or 0)
        events = [
            RateLimitEvent(
                ts=_at(24, 15, 5),
                profile="chatgpt-privat",
                status="warning",
                utilization=0.82,
                run=R_EXPLORE,
                agent="explorer-gpt",
                consequence="keine, arbeitet weiter",
            ),
            RateLimitEvent(
                ts=_at(22, 11, 9),
                profile="chatgpt-privat",
                status="error",
                run="20261022-1105_primzahl-luecken_roundtable_19ab",
                agent="alle",
                consequence="Run fehlgeschlagen (Anmeldung abgelaufen)",
            ),
            RateLimitEvent(
                ts=_at(19, 17, 5),
                profile="max-privat",
                status="throttled",
                utilization=1.0,
                run="20261019-1640_collatz_proof-review_0f3c",
                agent="judge-1, judge-3",
                consequence="12 min gewartet, dann weiter",
            ),
        ]
        events = [
            e
            for e in self._tr(events)
            if from_ts <= e.ts <= to_ts and (e.run is None or e.run in {r.id for r in runs})
        ]
        return UsageReport(
            from_ts=from_ts,
            to_ts=to_ts,
            group_by=group_by,
            rows=sorted(rows.values(), key=lambda row: -_usage_tokens(row)),
            totals=total,
            billed_usd=billed,
            estimated_usd=round(sum(estimated_parts), 2),
            cache_read_ratio=round(read / fed, 2) if fed else None,
            ratelimits=events,
        )

    def _usage_parts(self, run: _Run) -> list[dict[str, Any]]:
        """Fixed shares of a run per profile: Claude subscription, ChatGPT, sometimes an API key."""
        tokens = run.tokens_total or 0
        spent = run.spent_usd or 0.0
        if not tokens:
            return []
        api_key = int(run.id[-4:], 16) % 5 == 0 if run.id[-4:].isalnum() else False
        shares = [
            ("max-privat", "Claude-Abo", "claude-opus-5-5", 0.58, 0.72, CostKind.ESTIMATED),
            ("max-privat", "Claude-Abo", "claude-sonnet-5-5", 0.16, 0.16, CostKind.ESTIMATED),
            ("chatgpt-privat", "ChatGPT-Abo", "gpt-5.5", 0.2, None, None),
        ]
        if api_key:
            shares.append(("claude-api-uni", "API-Key", "claude-opus-5-5", 0.06, 0.12, None))
        parts = []
        for profile, _kind, model, share, cost_share, kind in shares:
            t = int(tokens * share)
            codex = model.startswith("gpt")
            parts.append(
                {
                    "profile": self._tr(profile),
                    "profile_label": self._tr(profile),
                    "model": model,
                    "input": int(t * 0.2),
                    "output": int(t * 0.05),
                    "cache_read": int(t * 0.7),
                    "cache_write": None if codex else int(t * 0.05),
                    "reasoning": int(t * 0.05) if codex else None,
                    "cost_usd": None if cost_share is None else round(spent * cost_share, 2),
                    "cost_kind": CostKind.BILLED
                    if profile == "claude-api-uni"
                    else (None if codex else kind),
                }
            )
        return parts

    async def get_storage(self) -> StorageReport:
        """Disk use of the runs with a slimming suggestion."""
        return self._storage()

    def _storage(self) -> StorageReport:
        summaries = [self._summary(r) for r in self.state.runs.values()]
        actual = sum(s.size.actual_bytes for s in summaries)
        logical = sum(s.size.logical_bytes for s in summaries)
        parts = StorageParts(
            work=sum(s.size.work for s in summaries),
            logs=sum(s.size.logs for s in summaries),
            output=sum(s.size.output for s in summaries),
            archive=sum(s.size.archive for s in summaries),
        )
        items = []
        for s in sorted(summaries, key=lambda s: s.size.actual_bytes, reverse=True):
            reason = "aktiv" if s.active else "angeheftet" if s.pinned else None
            items.append(
                StorageItem(
                    run=RunRef(id=s.id, name=s.name, project=s.project, gui_url=s.gui_url),
                    actual=s.size.actual_bytes,
                    logical=s.size.logical_bytes,
                    by_part=StorageParts(
                        work=s.size.work,
                        logs=s.size.logs,
                        output=s.size.output,
                        archive=s.size.archive,
                    ),
                    slimmable=reason is None and not s.slimmed and s.size.work > 0,
                    archivable=reason is None and not s.archived,
                    protected_reason=reason,
                )
            )
        slim = sorted((i for i in items if i.slimmable), key=lambda i: i.by_part.work, reverse=True)
        slim = slim[:8]
        by_project: dict[str, ProjectStorage] = {}
        for s in summaries:
            p = by_project.get(s.project) or ProjectStorage(
                project=s.project, actual=0, logical=0, runs=0
            )
            by_project[s.project] = p.model_copy(
                update={
                    "actual": p.actual + s.size.actual_bytes,
                    "logical": p.logical + s.size.logical_bytes,
                    "runs": p.runs + 1,
                }
            )
        return StorageReport(
            runs_actual=actual,
            runs_logical=logical,
            by_part=parts,
            limit_bytes=int((self._retention().max_runs_size_gb or 50) * GB),
            disk_free=self.state.workfolder.disk_free_bytes,
            disk_total=494 * GB,
            by_project=sorted(by_project.values(), key=lambda p: p.actual, reverse=True),
            suggestion=StorageSuggestion(
                action=RunAction.SLIM,
                run_ids=[i.run.id for i in slim],
                freed_bytes=sum(i.by_part.work for i in slim),
                excluded_pinned=sum(1 for s in summaries if s.pinned),
                excluded_active=sum(1 for s in summaries if s.active),
                keeps=["Transkripte", "Gruppenchats", "Ergebnisse", "Zusammenfassung"],
                removes=["Arbeitskopien", "Snapshots"],
            )
            if slim
            else None,
            runs=items,
        )

    async def resolve_run(self, run_input: dict[str, Any]) -> ResolveResult:
        """Resolve and check a run input like `estimate_run` (docs/08 §5.4, simulated).

        `run_input`: `project`, `skill` (None: without skill), `name`, `task_md`, `params`,
        `agents` (overrides per agent: `model`, `web`, `read_dirs`), `limits` (`wall_time_s`,
        `turns_per_agent`, `budget_usd`), `decider`. Errors carry the path of their field.
        """
        assert self.state.workfolder_settings is not None
        settings = self.state.workfolder_settings
        errors: list[FieldIssue] = []
        warnings: list[str] = []
        project_name = str(run_input.get("project") or "")
        skill_name = run_input.get("skill") or None
        params = dict(run_input.get("params") or {})
        if not project_name:
            errors.append(FieldIssue(path="project", field="project", message="Wähle ein Projekt."))
        else:
            project = next((p for p in self.state.projects if p.name == project_name), None)
            if project is None:
                errors.append(
                    FieldIssue(
                        path="project",
                        field="project",
                        message=f"Projekt „{project_name}“ gibt es nicht. Wähle ein anderes.",
                    )
                )
            elif not project.setup_ok:
                warnings.append(
                    f"{project_name} ist unvollständig eingerichtet. Der Run startet trotzdem, "
                    "Claude Code arbeitet dort erst nach „Reparieren“ mit."
                )
        skill = None
        if skill_name:
            skill = next((s for s in self.state.skills if s.name == skill_name), None)
            if skill is None or skill.kind is not SkillKind.GROUP:
                errors.append(
                    FieldIssue(
                        path="skill",
                        field="skill",
                        message=f"„{skill_name}“ ist kein startbarer Group-Skill.",
                    )
                )
                skill = None
            elif not skill.valid:
                issue = skill.issues[0] if skill.issues else None
                where = f"{issue.file}, Zeile {issue.line}: " if issue and issue.line else ""
                errors.append(
                    FieldIssue(
                        path="skill",
                        field="skill",
                        message=f"Skill {skill_name} ist ungültig: {where}"
                        f"{issue.message if issue else 'Prüfung fehlgeschlagen.'}",
                        action=Action(label="Skill öffnen", url=f"/skills/{skill_name}"),
                    )
                )
        for p in skill.params if skill else []:
            value = params.get(p.name, p.default)
            if p.type not in ("int", "float") or not isinstance(value, int | float):
                continue
            if (p.min is not None and value < p.min) or (p.max is not None and value > p.max):
                errors.append(
                    FieldIssue(
                        path=f"params.{p.name}",
                        field=p.name,
                        message=f"Erlaubt sind {_num(p.min)}–{_num(p.max)}.",
                    )
                )
        if not str(run_input.get("task_md") or "").strip():
            errors.append(
                FieldIssue(
                    path="task_md",
                    field="task_md",
                    message="Die Aufgabe fehlt. Schreib, woran die Agenten arbeiten sollen.",
                )
            )
        agents = self._resolve_agents(skill_name, params, run_input.get("agents") or {})
        channels = self._tr(_resolve_channels(skill_name, agents))
        agents = self._tr(agents)
        profiles = {p.name: p for p in self.state.profiles}
        for agent in agents:
            profile = profiles.get(agent.profile)
            if profile is None:
                errors.append(
                    FieldIssue(
                        path=f"agents.{agent.id}.profile",
                        field=agent.id,
                        message=f"Für {agent.id} fehlt ein Auth-Profil ({agent.profile}). Lege "
                        "es unter Auth-Profile an.",
                        action=Action(label="Auth-Profile", url="/profile"),
                    )
                )
            elif profile.last_test and not profile.last_test.ok:
                errors.append(
                    FieldIssue(
                        path=f"agents.{agent.id}.profile",
                        field=agent.id,
                        message=f"Profil „{profile.name}“ von {agent.id} funktioniert nicht.",
                        action=Action(label="Profil öffnen", url=f"/profile/{profile.name}"),
                    )
                )
        estimate = _estimate(skill_name, params, agents)
        limits_in = run_input.get("limits") or {}
        budget = limits_in.get("budget_usd", settings.limits.budget_usd)
        if isinstance(budget, int | float) and budget < estimate.cost_usd.max:
            cost_param = _COST_PARAM.get(skill_name or "")
            hint = (
                f"nimm weniger {_param_label(skill_name, cost_param)}"
                if cost_param
                else "nimm weniger Agenten"
            )
            errors.append(
                FieldIssue(
                    path="limits.budget_usd",
                    field="budget_usd",
                    message=f"Zu knapp: Die Schätzung liegt bei ≈ {_num(estimate.cost_usd.min)}–"
                    f"{_num(estimate.cost_usd.max)} $. Setze mindestens "
                    f"{_num(estimate.cost_usd.max)} $ oder {hint}.",
                )
            )
        capacity = []
        for name in dict.fromkeys(a.profile for a in agents):
            profile = profiles.get(name)
            if profile is None:
                continue
            needed = sum(1 for a in agents if a.profile == name)
            free = max(0, profile.concurrency.limit - profile.concurrency.in_use)
            capacity.append(
                CapacityInfo(
                    profile=name, free=free, needed=needed, limit=profile.concurrency.limit
                )
            )
            if needed > free:
                warnings.append(
                    f"{name}: {free} von {profile.concurrency.limit} Plätzen frei, der Run "
                    f"braucht {needed}. Er wartet in der Warteschlange, bis Plätze frei werden."
                )
        deviations = self._deviations(agents, limits_in, run_input.get("decider"))
        return ResolveResult(
            resolved_run_yaml=_run_yaml(run_input, skill, agents, settings),
            agents=agents,
            channels=channels,
            errors=errors,
            warnings=warnings,
            estimate=estimate,
            capacity=capacity,
            deviations=deviations,
        )

    def _resolve_agents(
        self, skill: str | None, params: dict[str, Any], overrides: dict[str, Any]
    ) -> list[RunStartAgent]:
        """Agents of a skill with the standard profiles, then the overrides of the form."""
        assert self.state.workfolder_settings is not None
        defaults = self.state.workfolder_settings.defaults
        specs: list[tuple[str, str, str]] = []
        if skill == "exploration":
            ideas = [i for i in params.get("ideas") or [] if str(i).strip()]
            for n in range(1, max(1, len(ideas) or 3) + 1):
                specs.append(
                    (
                        f"explorer-{n}" if n < 3 else "explorer-gpt" if n == 3 else f"explorer-{n}",
                        "Explorer",
                        "gpt-5.5" if n == 3 else "claude-opus-5-5",
                    )
                )
            specs += [
                ("critic", "Kritiker", "claude-sonnet-5-5"),
                ("ranker", "Ranker", "claude-sonnet-5-5"),
            ]
        elif skill == "proof":
            specs = [
                ("strategist", "Stratege", "claude-opus-5-5"),
                ("prover", "Beweiser", "claude-opus-5-5"),
                ("rater-1", "Rater", "claude-sonnet-5-5"),
                ("rater-2", "Rater", "gpt-5.5"),
            ]
        elif skill == "proof-review":
            specs = [("author", "Autor", "claude-opus-5-5")]
            for n in range(1, int(params.get("judges") or 3) + 1):
                specs.append(
                    (f"judge-{n}", "Judge", "gpt-5.5" if n % 2 == 0 else "claude-opus-5-5")
                )
            if params.get("verdict") == "meta_judge":
                specs.append(("meta-judge", "Meta-Judge", "claude-opus-5-5"))
        elif skill == "roundtable":
            for n in range(1, int(params.get("members") or 3) + 1):
                specs.append(
                    (f"member-{n}", "Mitglied", "gpt-5.5" if n % 3 == 0 else "claude-opus-5-5")
                )
        else:
            specs = [("solver", "Löser", "claude-opus-5-5")]
        agents = []
        for agent_id, role, model in specs:
            change = overrides.get(self._tr(agent_id)) or overrides.get(agent_id) or {}
            model = str(change.get("model") or model)
            provider = _provider_for(model)
            profile = (defaults.get(provider) or ProviderDefaultsInfo()).auth or ""
            agents.append(
                RunStartAgent(
                    id=agent_id,
                    role_label=role,
                    provider=provider,
                    vendor=_vendor(provider),
                    model=model,
                    model_label=_model_label(model),
                    profile=str(change.get("profile") or profile),
                    tools=ToolAccess(shell=True, web=bool(change.get("web", False))),
                    read_dirs=list(change.get("read_dirs") or []),
                    workspace_mode=WorkspaceMode.EMPTY
                    if agent_id == "ranker"
                    else WorkspaceMode.COPY,
                )
            )
        return agents

    def _deviations(
        self, agents: list[RunStartAgent], limits: dict[str, Any], decider: Any
    ) -> list[Deviation]:
        """Differences from your standards (docs/07 §5, docs/08 §5.0 `deviations`)."""
        assert self.state.workfolder_settings is not None
        settings = self.state.workfolder_settings
        out: list[Deviation] = []
        web = [a.id for a in agents if a.tools.web]
        if web:
            out.append(
                Deviation(
                    path=f"agents.{_id_group(web)}.tools.web",
                    label="Web-Zugriff an",
                    value_text=f"für {', '.join(web)}",
                    default_text="aus",
                    default=False,
                    value=True,
                    sensitive=True,
                )
            )
        folders: dict[str, list[RunStartAgent]] = {}
        for a in agents:
            for folder in a.read_dirs:
                folders.setdefault(folder, []).append(a)
        for folder, readers in folders.items():
            who = (
                "alle Agenten" if len(readers) == len(agents) else ", ".join(a.id for a in readers)
            )
            out.append(
                Deviation(
                    path="defaults.read_dirs",
                    label="Zusätzlicher Lese-Ordner",
                    value_text=f"{folder} für {who}",
                    default_text="keiner",
                    default=[],
                    value=[folder],
                    sensitive=True,
                    providers=_shares(readers),
                )
            )
        std = settings.limits
        for key, label in (
            ("wall_time_s", "Laufzeit"),
            ("turns_per_agent", "Turns pro Agent"),
            ("budget_usd", "Budget"),
        ):
            value = limits.get(key)
            default = getattr(std, key)
            if isinstance(value, int | float) and default is not None and value > default:
                out.append(
                    Deviation(
                        path=f"limits.{key}",
                        label=f"{label} {_limit_text(key, value)}",
                        value_text="",
                        default_text=_limit_text(key, default),
                        default=default,
                        value=value,
                        sensitive=False,
                    )
                )
        if decider and str(decider) != settings.approvals.decider.value:
            weaker = str(decider) == Decider.GATEKEEPER.value
            out.append(
                Deviation(
                    path="decider",
                    label="Entscheider für Berechtigungsanfragen",
                    value_text=_DECIDER_TEXT.get(str(decider), str(decider)),
                    default_text=_DECIDER_TEXT[settings.approvals.decider.value],
                    default=settings.approvals.decider.value,
                    value=str(decider),
                    sensitive=weaker,
                )
            )
        standard = {p: d.auth for p, d in settings.defaults.items()}
        other = [
            a for a in agents if standard.get(a.provider) and a.profile != standard[a.provider]
        ]
        if other:
            out.append(
                Deviation(
                    path="agents.profile",
                    label="Anderes Auth-Profil",
                    value_text=", ".join(f"{a.id}: {a.profile}" for a in other),
                    default_text=", ".join(sorted({standard[a.provider] or "" for a in other})),
                    sensitive=True,
                )
            )
        return out

    async def start_run(self, run_input: dict[str, Any]) -> StartRunResponse:
        """Create a queued run from the input (simulated preparation follows)."""
        resolved = await self.resolve_run(run_input)
        if resolved.errors:
            raise ApiException.of(
                ErrorCode.CONFIG_INVALID, resolved.errors[0].message, issues=resolved.errors
            )
        assert self.state.workfolder_settings is not None
        std = self.state.workfolder_settings.limits
        now = self.now()
        project = str(run_input["project"])
        skill = run_input.get("skill") or None
        topology = (
            next((s.topology for s in self.state.skills if s.name == skill), None)
            or TopologyKind.SOLO
        )
        limits = run_input.get("limits") or {}
        rid = (
            datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M")
            + f"_{project}_{skill or 'custom'}_"
            + (f"{random.Random(now).randrange(16**4):04x}")
        )
        run = _Run(
            id=rid,
            name=str(run_input.get("name") or f"{skill or 'Run'} in {project}"),
            project=project,
            skill=skill,
            topology=topology,
            status=RunStatus.QUEUED,
            starter=Starter.GUI,
            created_at=now,
            status_since=now,
            size=SizeInfo(actual_bytes=0, logical_bytes=0),
            wall_time_s=int(limits.get("wall_time_s") or std.wall_time_s or 7200),
            budget_usd=float(limits.get("budget_usd") or std.budget_usd or 20),
            turns_per_agent=int(limits.get("turns_per_agent") or std.turns_per_agent or 40),
            decider=Decider(run_input.get("decider") or Decider.GATEKEEPER_THEN_USER),
            task_md=str(run_input.get("task_md") or ""),
            cost_rate=0.001,
            token_rate=40.0,
        )
        self.state.runs[rid] = run
        self.state.pending_transitions += [
            (now + 3, rid, RunStatus.PREPARING),
            (now + 8, rid, RunStatus.RUNNING),
        ]
        self._publish_run(run)
        self._publish_counts()
        return StartRunResponse(
            run_id=rid, status=run.status, gui_url=f"{self.base_url}/runs/{rid}"
        )

    # --- ApiClient: settings, mandate, rules, profiles, projects, skill files, workfolder ----

    async def get_app_settings(self) -> AppSettings:
        """App settings."""
        assert self.state.app_settings is not None
        return self.state.app_settings

    async def update_app_settings(self, settings: AppSettings) -> AppSettings:
        """Save app settings (validated again, like at the HTTP boundary)."""
        settings = AppSettings.model_validate(settings.model_dump())
        self.state.app_settings = settings
        return settings

    async def get_workfolder_settings(self) -> WorkfolderSettingsInfo:
        """Workfolder settings with the number of running runs."""
        assert self.state.workfolder_settings is not None
        running = sum(1 for r in self.state.runs.values() if r.status is RunStatus.RUNNING)
        return self.state.workfolder_settings.model_copy(update={"running_runs": running})

    async def update_workfolder_settings(
        self, settings: WorkfolderSettingsInfo
    ) -> WorkfolderSettingsInfo:
        """Save workfolder settings; unknown profiles in `concurrency` are refused."""
        settings = WorkfolderSettingsInfo.model_validate(settings.model_dump())
        known = {p.name for p in self.state.profiles}
        for name in settings.concurrency:
            if name not in known:
                raise ApiException.of(
                    ErrorCode.INVALID,
                    f"Profil „{name}“ gibt es nicht. Wähle eines der Auth-Profile.",
                    field=f"concurrency.{name}",
                )
        self.state.workfolder_settings = settings
        return await self.get_workfolder_settings()

    async def get_mandate(self, name: str = "standard") -> MandateInfo:
        """The mandate with the parts arr checks itself."""
        if name != "standard":
            raise _not_found("Vollmacht", name)
        running = sum(1 for r in self.state.runs.values() if r.status is RunStatus.RUNNING)
        return MandateInfo(
            name=name,
            path=f"mandates/{name}.md",
            text=self.state.mandate_text,
            modified_at=self.state.mandate_modified,
            checked=MandateChecks(
                domains=["pypi.org", "files.pythonhosted.org", "arxiv.org"],
                read_dirs=self._tr(["~/Documents/Mathe-Literatur"]),
                packages=["sympy", "numpy", "scipy", "networkx", "python-flint", "mpmath"],
            ),
            running_runs=running,
            example=self._tr(_MANDATE_TEXT),
        )

    async def update_mandate(self, name: str, update: MandateUpdate) -> MandateInfo:
        """Replace the mandate text (effective at once)."""
        await self.get_mandate(name)
        self.state.mandate_text = update.text
        self.state.mandate_modified = self.now()
        return await self.get_mandate(name)

    async def list_fixed_rules(self) -> list[FixedRule]:
        """Standard rules (fixed) and own rules (removable)."""
        builtin = [
            FixedRule(
                id=f"std-{i + 1}",
                effect=effect,
                rule=rule,
                origin="builtin",
                origin_text="Standard",
                removable=False,
            )
            for i, (effect, rule) in enumerate(_BUILTIN_RULES)
        ]
        return builtin + list(self.state.user_rules)

    async def delete_fixed_rule(self, rule_id: str) -> list[FixedRule]:
        """Remove an own rule."""
        rules = {r.id: r for r in await self.list_fixed_rules()}
        if rule_id not in rules:
            raise _not_found("Regel", rule_id)
        if not rules[rule_id].removable:
            raise ApiException.of(
                ErrorCode.NOT_ALLOWED,
                "Standardregeln sind fest. Du kannst nur Regeln entfernen, die du mit „immer“ "
                "angelegt hast.",
            )
        self.state.user_rules = [r for r in self.state.user_rules if r.id != rule_id]
        return await self.list_fixed_rules()

    async def test_toolchain(self, python: str | None = None) -> ToolchainTestResult:
        """Simulated check of the agents' Python environment."""
        assert self.state.workfolder_settings is not None
        python = python or self.state.workfolder_settings.toolchain.python or "System-Python"
        if python == "System-Python":
            return ToolchainTestResult(
                ok=False,
                python=python,
                python_version="3.12.0",
                packages={"sympy": False, "numpy": False},
                message="sympy fehlt in System-Python. Wähle eine venv mit sympy, z. B. "
                "~/.venvs/mathe, oder lass Agenten sympy in die Run-venv installieren.",
                duration_ms=900,
            )
        return ToolchainTestResult(
            ok=True,
            python=python,
            python_version="3.12.0",
            packages={"sympy": True, "numpy": True, "sage": False},
            message=f"{python} funktioniert: Python 3.12.0, sympy und numpy vorhanden.",
            duration_ms=1200,
        )

    async def create_profile(self, request: CreateProfileRequest) -> ProfileInfo:
        """Create a profile (the key would go to the keychain; the mock keeps only a flag)."""
        if any(p.name == request.name for p in self.state.profiles):
            raise ApiException.of(
                ErrorCode.CONFLICT,
                f"Ein Profil „{request.name}“ gibt es schon. Wähle einen anderen Namen.",
                field="name",
            )
        provider = (
            Provider.CODEX
            if request.kind in (AuthKind.CODEX_CHATGPT, AuthKind.OPENAI_API_KEY)
            else Provider.CLAUDE
        )
        has_key = request.api_key is not None
        profile = ProfileInfo(
            name=request.name,
            kind=request.kind,
            provider=provider,
            concurrency=ProfileConcurrency(limit=request.concurrency, in_use=0),
            secret_present=has_key,
            keychain_label=f"arr/{request.name}"
            if request.kind is not AuthKind.CODEX_CHATGPT
            else None,
            login_dir=f"~/.arr/codex-homes/{request.name}"
            if request.kind is AuthKind.CODEX_CHATGPT
            else None,
            last_test=ProfileTest(
                ts=self.now(),
                ok=True,
                duration_ms=1800,
                models=["claude-opus-5-5", "claude-sonnet-5-5"],
            )
            if has_key
            else None,
        )
        self.state.profiles.append(profile)
        return profile

    async def delete_profile(self, name: str) -> None:
        """Delete a profile; profiles in use by an active run are refused."""
        if not any(p.name == name for p in self.state.profiles):
            raise _not_found("Profil", name)
        if any(p.name == name and p.concurrency.in_use for p in self.state.profiles):
            raise ApiException.of(
                ErrorCode.NOT_ALLOWED,
                f"„{name}“ wird gerade von laufenden Runs genutzt. Warte, bis sie fertig sind.",
            )
        self.state.profiles = [p for p in self.state.profiles if p.name != name]

    async def start_login(self, name: str) -> LoginStart:
        """Simulated login: done 3 s after the start (mock clock)."""
        profile = next((p for p in self.state.profiles if p.name == name), None)
        if profile is None:
            raise _not_found("Profil", name)
        now = self.now()
        self.state.logins[name] = now
        poll = f"{self.base_url}/api/profiles/{name}/login"
        if profile.kind is AuthKind.CLAUDE_OAUTH:
            return LoginStart(
                profile=name,
                method="browser",
                browser_url="https://claude.ai/oauth/authorize?arr=mock",
                poll_url=poll,
                expires_at=now + 600,
                note="arr führt dafür claude setup-token aus.",
            )
        if profile.kind is AuthKind.CODEX_CHATGPT:
            return LoginStart(
                profile=name,
                method="device_code",
                device_code="ARR-4K7Q",
                poll_url=poll,
                expires_at=now + 900,
                note="arr meldet Codex im Login-Ordner des Profils an.",
            )
        raise ApiException.of(
            ErrorCode.INVALID,
            "Profile mit API-Key melden sich nicht an. Ersetze den Key unter „Bearbeiten“.",
        )

    async def get_login_status(self, name: str) -> LoginStatus:
        """Waiting during the first 3 s after `start_login`, then done with a tested profile."""
        started = self.state.logins.get(name)
        if started is None:
            return LoginStatus(
                profile=name,
                state=LoginState.CANCELLED,
                message="Keine Anmeldung läuft. Starte sie erneut.",
            )
        if self.now() - started < 3:
            return LoginStatus(
                profile=name,
                state=LoginState.WAITING,
                message="Warte auf die Anmeldung im Browser …",
            )
        del self.state.logins[name]
        for i, p in enumerate(self.state.profiles):
            if p.name == name:
                self.state.profiles[i] = p.model_copy(
                    update={
                        "secret_present": True,
                        "last_test": ProfileTest(
                            ts=self.now(),
                            ok=True,
                            duration_ms=2100,
                            models=p.models or ["claude-opus-5-5"],
                        ),
                    }
                )
                return LoginStatus(
                    profile=name,
                    state=LoginState.DONE,
                    message="Angemeldet und getestet.",
                    profile_info=self.state.profiles[i],
                )
        raise _not_found("Profil", name)

    async def cancel_login(self, name: str) -> LoginStatus:
        """Cancel a running login."""
        self.state.logins.pop(name, None)
        return LoginStatus(
            profile=name,
            state=LoginState.CANCELLED,
            message="Die Anmeldung wurde abgebrochen. Versuche es erneut oder nimm einen API-Key.",
            action=Action(label="Erneut anmelden", url=f"/profile/{name}"),
        )

    async def create_project(self, request: CreateProjectRequest) -> ProjectSetupResult:
        """Create a project from a template or set up an existing folder."""
        if any(p.name == request.name for p in self.state.projects):
            raise ApiException.of(
                ErrorCode.CONFLICT,
                f"Ein Projekt „{request.name}“ gibt es schon. Wähle einen anderen Ordnernamen.",
                field="name",
            )
        path = request.existing_path or f"{self.state.workfolder.path}/projects/{request.name}"
        arr_files = [
            "CLAUDE.md (arr-Block)",
            "AGENTS.md",
            ".mcp.json",
            "project.yaml",
            ".claude/skills/ (7 Links)",
        ]
        created = arr_files if request.existing_path else [*arr_files, "notes/", "tex/", "code/"]
        project = ProjectInfo(
            name=request.name,
            title=request.title or request.name,
            path=path,
            setup_ok=True,
            size_bytes=0,
            modified_at=self.now(),
            runs=ProjectRuns(total=0, active=0, waiting=0),
            checks=[
                ProjectCheck(key="claude_md", ok=True, message="CLAUDE.md mit arr-Block"),
                ProjectCheck(
                    key="mcp_json", ok=True, message=".mcp.json verbindet Claude Code mit arr"
                ),
                ProjectCheck(key="skill_links", ok=True, message="Skill-Links: 7 von 7"),
            ],
        )
        self.state.projects.append(project)
        verb = "eingerichtet" if request.existing_path else "angelegt"
        return ProjectSetupResult(
            project=project, created=created, message=f"Projekt „{request.name}“ {verb}."
        )

    async def setup_project(self, name: str) -> ProjectSetupResult:
        """Repair missing arr files (the mock fixes failed checks)."""
        project = await self.get_project(name)
        missing = [c.message.removesuffix(" fehlt") for c in project.checks if not c.ok]
        fixed = project.model_copy(
            update={
                "setup_ok": True,
                "checks": [
                    c.model_copy(update={"ok": True, "action": None}) for c in project.checks
                ],
            }
        )
        self.state.projects = [fixed if p.name == name else p for p in self.state.projects]
        message = f"„{name}“ repariert." if missing else f"„{name}“ ist vollständig eingerichtet."
        return ProjectSetupResult(project=fixed, created=missing, message=message)

    async def get_skill_file(self, name: str, path: str) -> SkillFile:
        """A file of a skill (sample content)."""
        skill = await self.get_skill(name)
        if path not in skill.files:
            raise _not_found("Datei", f"{name}/{path}")
        content = self._tr(_skill_file_text(skill, path))
        language = "yaml" if path.endswith(".yaml") else "markdown"
        return SkillFile(
            skill=name, path=path, content=content, language=language, size=len(content.encode())
        )

    async def select_workfolder(self, request: WorkfolderRequest) -> WorkfolderInfo:
        """Open a known or create a new workfolder (the mock keeps its data set)."""
        path = request.path.rstrip("/")
        if not path.startswith(("~/", "/")):
            raise ApiException.of(
                ErrorCode.INVALID, "Gib einen Ordner an, z. B. ~/Mathe.", field="path"
            )
        known = {w.path: w for w in self.state.workfolder.known}
        if path in known or path == self.state.workfolder.path:
            return await self.get_workfolder()
        if not request.create:
            raise ApiException.of(
                ErrorCode.NOT_FOUND,
                f"Workfolder {path} nicht gefunden. Wurde er verschoben? Wähle einen anderen "
                "Ordner oder lege ihn neu an.",
                field="path",
            )
        name = request.name or path.rsplit("/", 1)[-1]
        ref = WorkfolderRef(name=name, path=path, projects_count=0, active_runs=0)
        self.state.workfolder = self.state.workfolder.model_copy(
            update={"known": [*self.state.workfolder.known, ref]}
        )
        return WorkfolderInfo(
            name=name,
            path=path,
            projects_count=0,
            runs_count=0,
            disk_free_bytes=self.state.workfolder.disk_free_bytes,
            apfs=True,
            known=self.state.workfolder.known,
            service_url=self.base_url,
        )

    async def get_setup_state(self) -> SetupState:
        """State for the setup assistant (sample values)."""
        return SetupState(
            workfolder=await self.get_workfolder(),
            profiles=list(self.state.profiles),
            toolchain_candidates=[
                ToolchainCandidate(
                    path="~/.venvs/mathe",
                    python_version="3.12.0",
                    packages={"sympy": True, "numpy": True, "sage": False},
                ),
                ToolchainCandidate(
                    path="System-Python", python_version="3.12.0", packages={"sympy": False}
                ),
            ],
            projects=[p.name for p in self.state.projects],
        )


# --------------------------------------------------------------------------------------------
# Forced page states for reviews and the UX acceptance test (W1-08, docs/08 §5 „Zustände“)
# --------------------------------------------------------------------------------------------


class PageState(StrEnum):
    """States a page can be forced into with `?zustand=…` (mock only)."""

    EMPTY = "leer"
    LOADING = "laedt"
    ERROR = "fehler"


FORCED_LOADING_S = 600.0
"""How long content requests take in the forced state „lädt“ (a page stays in it)."""

_FRAME_METHODS = {"now", "subscribe", "get_counts", "get_workfolder"}
_SETTINGS_ERRORS = {"get_workfolder_settings", "get_mandate"}
_INDEX_ERROR = (
    "Run-Ordner nicht lesbar: meta.json fehlt in 20261012-0915_collatz_custom_77aa.",
    "Vermutlich wurde der Ordner außerhalb von arr verändert. Der Index lässt sich aus den "
    "Run-Ordnern neu aufbauen.",
    "Index neu aufbauen",
)
_ERRORS = {
    "get_usage": (
        "Usage-Index nicht lesbar.",
        "Er lässt sich aus den Run-Ordnern neu aufbauen.",
        "Neu aufbauen aus den Run-Ordnern",
    ),
    "get_workfolder_settings": (
        "arr.yaml, Zeile 14: Einrückung passt nicht zu „limits“.",
        "Die Datei wurde außerhalb von arr geändert. Öffne sie oder stell den Standard wieder her.",
        "Datei öffnen",
    ),
    "get_mandate": (
        "mandates/standard.md ist nicht lesbar.",
        "Die Datei fehlt oder ist leer. Stell den Standard wieder her.",
        "Standard wiederherstellen",
    ),
}


class ForcedStateApi:
    """`ApiClient` view of a `MockApi` in a forced page state (`MockApi.forced`).

    - leer: no runs, projects, skills, profiles, approvals, warnings or usage; counters 0
    - laedt: content requests take `FORCED_LOADING_S` (frame counters stay real)
    - fehler: content requests fail as the service would (docs/08 §8.4); data errors that
      pages show themselves (invalid skill, failed profile tests, missing project folder,
      a run whose size cannot be measured, check errors in „Neuer Run“) come as data
    """

    def __init__(self, api: "MockApi", state: PageState, page: str = "") -> None:
        self._api = api
        self.state = state
        self.page = page

    @property
    def base(self) -> "MockApi":
        """The mock without the forced state (for the mock-only controls of the frame)."""
        return self._api

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._api, name)
        if name in _FRAME_METHODS or not callable(target) or name.startswith("_"):
            return target
        if self.state is PageState.LOADING and asyncio.iscoroutinefunction(target):

            async def slow(*args: Any, **kwargs: Any) -> Any:
                await asyncio.sleep(FORCED_LOADING_S)
                return await target(*args, **kwargs)

            return slow
        if self.state is PageState.ERROR and name in _ERRORS:
            if name in _SETTINGS_ERRORS and self.page != "einstellungen":
                return target  # a broken arr.yaml is the error state of the settings only
            return self._fail(*_ERRORS[name])
        return target

    def _fail(self, message: str, detail: str, label: str) -> Callable[..., Any]:
        async def fail(*_args: Any, **_kwargs: Any) -> Any:
            raise ApiException.of(
                ErrorCode.UNAVAILABLE, message, detail=detail, action=Action(label=label, url="")
            )

        return fail

    # --- leer ---------------------------------------------------------------------------------

    def subscribe(self, run_id: str | None = None, agents: set[str] | None = None) -> Subscription:
        if self.state is PageState.EMPTY:
            return EventHub().add(run_id, agents)  # nothing is ever published
        return self._api.subscribe(run_id, agents)

    async def get_counts(self) -> GlobalCounts:
        if self.state is PageState.EMPTY:
            return GlobalCounts(waiting=0, active=0, warnings=0)
        return await self._api.get_counts()

    async def get_workfolder(self) -> WorkfolderInfo:
        wf = await self._api.get_workfolder()
        if self.state is PageState.EMPTY:
            return wf.model_copy(update={"projects_count": 0, "runs_count": 0})
        return wf

    async def list_warnings(self) -> list[WarningInfo]:
        if self.state is PageState.EMPTY:
            return []
        return await self.__getattr__("list_warnings")()

    async def list_approvals(self, *args: Any, **kwargs: Any) -> list[ApprovalItem]:
        if self.state is PageState.EMPTY:
            return []
        return await self.__getattr__("list_approvals")(*args, **kwargs)

    async def list_runs(self, *args: Any, **kwargs: Any) -> RunsPage:
        if self.state is PageState.EMPTY:
            return RunsPage(
                items=[],
                counts=RunCounts(all=0, active=0, waiting=0, done=0, error=0, archived=0),
                total=0,
            )
        if self.state is PageState.ERROR and self.page not in ("projekte", "speicher"):
            # the error states of Projekte and Speicher are one folder; the runs stay readable
            raise self._index_error()
        return await self.__getattr__("list_runs")(*args, **kwargs)

    async def list_projects(self) -> list[ProjectInfo]:
        if self.state is PageState.EMPTY:
            return []
        projects = await self.__getattr__("list_projects")()
        if self.state is PageState.ERROR and projects:
            first = projects[0]
            missing = ProjectCheck(
                key="folder",
                ok=False,
                severity=Severity.ERROR,
                message=f"{first.name} fehlt unter {first.path.rsplit('/', 1)[0]}. "
                f"{first.runs.total} Runs bleiben lesbar.",
                detail="Wurde der Ordner verschoben oder umbenannt?",
                action=Action(label="Ordner suchen", url=f"/projekte/{first.name}"),
            )
            projects[0] = first.model_copy(
                update={"setup_ok": False, "checks": [missing, *first.checks]}
            )
        return projects

    async def get_project(self, name: str) -> ProjectInfo:
        if self.state is PageState.EMPTY:
            raise _not_found("Projekt", name)
        if self.state is PageState.ERROR:
            for project in await self.list_projects():
                if project.name == name:
                    return project
        return await self.__getattr__("get_project")(name)

    async def list_skills(self, kind: SkillKind | None = None) -> list[SkillInfo]:
        if self.state is PageState.EMPTY:
            return []
        skills = await self.__getattr__("list_skills")(kind)
        return [self._broken(s) for s in skills] if self.state is PageState.ERROR else skills

    async def get_skill(self, name: str) -> SkillInfo:
        if self.state is PageState.EMPTY:
            raise _not_found("Skill", name)
        skill = await self.__getattr__("get_skill")(name)
        return self._broken(skill) if self.state is PageState.ERROR else skill

    def _broken(self, skill: SkillInfo) -> SkillInfo:
        if skill.name != "exploration":
            return skill
        issue = SkillIssue(
            file="group.yaml",
            line=31,
            message="Rolle roles/critic.md nicht gefunden. Lege sie an oder entferne den "
            "Agenten critic.",
        )
        return skill.model_copy(update={"valid": False, "issues": [issue]})

    async def list_profiles(self) -> list[ProfileInfo]:
        if self.state is PageState.EMPTY:
            return []
        profiles = await self.__getattr__("list_profiles")()
        if self.state is not PageState.ERROR:
            return profiles
        failures = {
            AuthKind.CLAUDE_OAUTH: (
                "Die Claude-Anmeldung wurde abgebrochen. Versuche es erneut oder nimm einen "
                "Claude-API-Key.",
                "Erneut anmelden",
            ),
            AuthKind.CODEX_CHATGPT: (
                "Die ChatGPT-Anmeldung ist abgelaufen. Melde dich neu an.",
                "Neu anmelden",
            ),
        }
        out = []
        for profile in profiles:
            if profile.kind in failures and profile.last_test is not None:
                message, label = failures[profile.kind]
                test = profile.last_test.model_copy(
                    update={
                        "ok": False,
                        "message": message,
                        "action": Action(label=label, url=f"/profile/{profile.name}"),
                    }
                )
                profile = profile.model_copy(update={"last_test": test})
            out.append(profile)
        return out

    async def get_usage(self, *args: Any, **kwargs: Any) -> UsageReport:
        if self.state is PageState.EMPTY:
            report = await self._api.get_usage(*args, **kwargs)
            return report.model_copy(
                update={
                    "rows": [],
                    "totals": UsageRow(key="total", label="Gesamt"),
                    "billed_usd": None,
                    "estimated_usd": None,
                    "cache_read_ratio": None,
                    "ratelimits": [],
                }
            )
        return await self.__getattr__("get_usage")(*args, **kwargs)

    async def get_storage(self) -> StorageReport:
        if self.state is PageState.EMPTY:
            report = await self._api.get_storage()
            return report.model_copy(
                update={
                    "runs_actual": 0,
                    "runs_logical": 0,
                    "by_part": StorageParts(),
                    "by_project": [],
                    "suggestion": None,
                    "runs": [],
                }
            )
        report = await self.__getattr__("get_storage")()
        if self.state is PageState.ERROR and report.runs:
            # one run folder is not readable: 0 B with the reason (W3-01: a field of its own)
            last = report.runs[-1].model_copy(
                update={
                    "actual": 0,
                    "logical": 0,
                    "by_part": StorageParts(),
                    "slimmable": False,
                    "archivable": False,
                    "protected_reason": "Ordner nicht lesbar",
                }
            )
            report = report.model_copy(update={"runs": [*report.runs[:-1], last]})
        return report

    async def get_setup_state(self) -> SetupState:
        state = await self.__getattr__("get_setup_state")()
        if self.state is PageState.EMPTY:
            return state.model_copy(update={"workfolder": None, "profiles": [], "projects": []})
        return state

    async def resolve_run(self, run_input: dict[str, Any]) -> ResolveResult:
        result = await self.__getattr__("resolve_run")(run_input)
        if self.state is PageState.ERROR and run_input.get("skill") == "exploration":
            issue = FieldIssue(
                path="skill",
                field="skill",
                message="Rolle für critic nicht gefunden: skills/exploration/roles/critic.md. "
                "Lege die Datei an oder wähle eine vorhandene Rolle (explorer, ranker).",
                action=Action(label="Skill öffnen", url="/skills/exploration"),
            )
            return result.model_copy(update={"errors": [issue, *result.errors]})
        return result

    async def start_login(self, name: str) -> LoginStart:
        return await self.__getattr__("start_login")(name)

    async def get_login_status(self, name: str) -> LoginStatus:
        if self.state is PageState.ERROR:
            await self._api.cancel_login(name)
            return LoginStatus(
                profile=name,
                state=LoginState.FAILED,
                message="Die Claude-Anmeldung wurde abgebrochen. Versuche es erneut oder nimm "
                "einen Claude-API-Key.",
                action=Action(label="Erneut anmelden", url=f"/profile/{name}"),
            )
        return await self.__getattr__("get_login_status")(name)

    def _index_error(self) -> ApiException:
        message, detail, label = _INDEX_ERROR
        return ApiException.of(
            ErrorCode.UNAVAILABLE, message, detail=detail, action=Action(label=label, url="")
        )
