"""AgentBackend protocol plus the normalized AgentEvent, Usage and TurnOutcome types.

Every backend (Claude, Codex, the FakeBackend for tests) emits the same `AgentEvent`s; the
EventLogger writes them to `transcript.jsonl` (normalized) and `raw.jsonl` (raw SDK objects).
See docs/01-architektur.md §3-4 and docs/02-workfolder-und-runs.md §6.
"""

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NotRequired, Protocol, Self, TypedDict, runtime_checkable

from pydantic import Field

from arr.models import AgentConfig, Record
from arr.room.types import Result


class EventKind(StrEnum):
    """Kind of a normalized agent event (docs/02-workfolder-und-runs.md §6).

    `hint` records a chat hint delivered during a running turn (docs/06-gruppenchats.md §4).
    """

    TURN_START = "turn_start"
    TEXT = "text"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    RATE_LIMIT = "rate_limit"
    ERROR = "error"
    TURN_END = "turn_end"
    HINT = "hint"


class TurnStatus(StrEnum):
    """How a turn ended, `data.status` of `turn_end` (docs/02 §6, docs/01 §6).

    `limit`: a per-turn limit of the SDK (budget, max turns) stopped the turn.
    """

    OK = "ok"
    INTERRUPTED = "interrupted"
    LIMIT = "limit"
    ERROR = "error"


class CostKind(StrEnum):
    """Whether a cost is billed (API key) or estimated (subscription, „≈“), docs/08 §5.0.

    `None` in a `Usage` means unknown.
    """

    BILLED = "billed"
    ESTIMATED = "estimated"


# --------------------------------------------------------------------------------------------
# `data` schemas per event kind (docs/02 §6). Codex items that cannot be mapped cleanly carry
# `source_type`; the full object is always in `raw.jsonl`.
# --------------------------------------------------------------------------------------------


class TurnStartData(TypedDict):
    """`data` of `turn_start` (docs/02 §6)."""

    prompt: str
    stage: str | None


class TextData(TypedDict):
    """`data` of `text` (docs/02 §6)."""

    text: str
    source_type: NotRequired[str]


class ThinkingData(TypedDict):
    """`data` of `thinking`: summarized reasoning where the SDK provides it (docs/02 §6)."""

    text: str
    summarized: bool


class ToolCallData(TypedDict):
    """`data` of `tool_call` (docs/02 §6)."""

    id: str
    name: str
    input: dict[str, Any]
    source_type: NotRequired[str]


class ToolResultData(TypedDict):
    """`data` of `tool_result`; output > 1 MB goes to `blobs/`, `blob` is its path (docs/02 §6)."""

    id: str
    is_error: bool
    content: str | None
    blob: str | None


class UsageData(TypedDict):
    """`data` of `usage`: token counts of one model request (docs/02 §6, §8)."""

    input: int | None
    output: int | None
    cache_read: int | None
    cache_write: int | None
    reasoning: int | None
    model: NotRequired[str]


class RateLimitData(TypedDict):
    """`data` of `rate_limit` (docs/02 §6); `resets_at` is a Unix timestamp."""

    status: str
    utilization: float | None
    resets_at: float | None


class ErrorData(TypedDict):
    """`data` of `error` (docs/02 §6)."""

    message: str
    type: str
    recoverable: bool


class TurnEndData(TypedDict):
    """`data` of `turn_end` (docs/02 §6); `usage` without cost, cost separately."""

    status: str
    usage: UsageData
    cost_usd: float | None
    session: str | None
    num_turns: int | None


class HintData(TypedDict):
    """`data` of `hint`: text delivered mid-turn and how (docs/06-gruppenchats.md §4)."""

    text: str
    channel: str | None
    method: str  # "post_tool_use" (Claude hook) | "steer" (Codex)


# --------------------------------------------------------------------------------------------
# Events, usage, outcome
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class AgentEvent:
    """One normalized event of an agent; one line in `transcript.jsonl` (docs/02 §6).

    `seq` is the running number in the agent's log and is assigned by the EventLogger;
    backends may leave it at 0. `raw` is the original SDK object for `raw.jsonl` and is not
    part of the normalized record.
    """

    run_id: str
    agent_id: str
    turn: int
    kind: EventKind
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    ts: float = field(default_factory=time.time)
    raw: Any = None

    def to_record(self) -> dict[str, Any]:
        """Return the normalized line for `transcript.jsonl` (keys as in docs/02 §6)."""
        return {
            "seq": self.seq,
            "ts": self.ts,
            "run": self.run_id,
            "agent": self.agent_id,
            "turn": self.turn,
            "kind": str(self.kind),
            "data": self.data,
        }


def _add_optional[T: (int, float)](a: T | None, b: T | None) -> T | None:
    if a is None:
        return b
    if b is None:
        return a
    return a + b


class Usage(Record):
    """Token usage and cost of a request, turn or aggregate (docs/02 §6, §8).

    Token fields are `None` when the SDK does not report them. `cost_kind` says whether
    `cost_usd` is billed (API key) or estimated (subscription); `None` means unknown.
    """

    input: int | None = Field(default=None, ge=0)
    output: int | None = Field(default=None, ge=0)
    cache_read: int | None = Field(default=None, ge=0)
    cache_write: int | None = Field(default=None, ge=0)
    reasoning: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    cost_kind: CostKind | None = None

    def __add__(self, other: Self) -> Self:
        """Sum field by field; `None + x = x`. Mixed cost kinds give `estimated`."""
        if self.cost_kind == other.cost_kind or other.cost_usd is None:
            kind = self.cost_kind
        elif self.cost_usd is None:
            kind = other.cost_kind
        else:
            kind = CostKind.ESTIMATED
        return type(self)(
            input=_add_optional(self.input, other.input),
            output=_add_optional(self.output, other.output),
            cache_read=_add_optional(self.cache_read, other.cache_read),
            cache_write=_add_optional(self.cache_write, other.cache_write),
            reasoning=_add_optional(self.reasoning, other.reasoning),
            cost_usd=_add_optional(self.cost_usd, other.cost_usd),
            cost_kind=kind,
        )

    def tokens(self) -> UsageData:
        """Token counts in the shape of `UsageData` (for `usage` / `turn_end` events)."""
        return UsageData(
            input=self.input,
            output=self.output,
            cache_read=self.cache_read,
            cache_write=self.cache_write,
            reasoning=self.reasoning,
        )


class TurnOutcome(Record):
    """Summary of one finished turn, returned by `RunContext.turn` (docs/01 §6).

    `results` are the results the agent submitted during this turn. `session` is the
    session/thread ID after the turn (docs/02 §6 `turn_end.session`).
    """

    agent_id: str
    turn: int = Field(ge=1)
    status: TurnStatus
    text: str | None = Field(default=None, description="Final answer text of the turn")
    usage: Usage = Field(default_factory=Usage)
    results: list[Result] = Field(default_factory=list)
    session: str | None = None
    num_turns: int | None = Field(default=None, description="Model round trips (SDK count)")
    duration_s: float | None = Field(default=None, ge=0)
    error: str | None = None


@runtime_checkable
class AgentBackend(Protocol):
    """Adapter from arr's agent model to a native harness (docs/01-architektur.md §3).

    One instance per agent. Lifecycle: `start()` once, then `run_turn()` per assignment
    (one at a time), `close()` at the end. `interrupt()` and `deliver_hint()` may be called
    while a turn runs.
    """

    config: AgentConfig

    async def start(self) -> None:
        """Start the SDK client / subprocess for this agent."""
        ...

    def run_turn(self, prompt: str, *, stage: str | None = None) -> AsyncIterator[AgentEvent]:
        """Run one turn; yield events from `turn_start` to `turn_end`.

        The iterator always ends with a `turn_end` event, also after `interrupt()` (status
        `interrupted`) and after errors (status `error`, preceded by an `error` event).
        """
        ...

    async def interrupt(self) -> None:
        """Stop the running turn (stop from the GUI or a limit reached); no-op if idle."""
        ...

    async def deliver_hint(self, text: str) -> bool:
        """Deliver a short chat hint into the running turn (docs/06-gruppenchats.md §4).

        Returns False when no turn is running or the backend cannot deliver it now; the hint
        then stays in the inbox for the next turn.
        """
        ...

    def session_ref(self) -> dict[str, str]:
        """Return session/thread IDs for `session.json`, e.g. `{"session_id": …}`."""
        ...

    async def close(self) -> None:
        """Release the SDK client and subprocesses; safe to call more than once."""
        ...
