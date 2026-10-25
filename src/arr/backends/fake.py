"""FakeBackend: an `AgentBackend` that plays a script instead of calling a model.

It lets the orchestrator and the topologies be tested without tokens (docs/01-architektur.md §3,
Codeplan W1-04). The behavior comes from a `FakeScript`, given as YAML or as Python data:
one entry per turn, each a list of steps (text, thinking, tool call with result, room action,
usage, rate limit, error, delay). The events are the same `AgentEvent`s as those of the real
backends; room actions go through the `RoomAPI` of the agent and also appear as tool calls
named `mcp__arr-room__<action>`, like the MCP tools of a real agent.

Script format (YAML)::

    version: 1
    cost_kind: estimated        # default for `usage` steps with a cost (null = unknown)
    repeat_last: false          # play the last turn again when the script is used up
    turns:
      - steps:
          - {do: thinking, text: "Ich zerlege die Aufgabe."}
          - {do: text, text: "Erster Ansatz …"}
          - {do: tool, name: Bash, input: {command: "python check.py"}, result: "ok"}
          - do: room
            action: post_message
            args: {channel: main, body: "Idee 1", kind: idea}
            save_as: posted         # later: reply_to: "${posted.id}"
          - {do: usage, input: 1200, output: 300, cost_usd: 0.02}
          - {do: delay, seconds: 0.5}

String values of room `args` that consist of exactly `${name.path}` are replaced by a value
saved earlier with `save_as` (dotted path; list items by index).
"""

import asyncio
import inspect
import json
import re
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from arr.backends.base import (
    AgentEvent,
    CostKind,
    ErrorData,
    EventKind,
    HintData,
    RateLimitData,
    TextData,
    ThinkingData,
    ToolCallData,
    ToolResultData,
    TurnEndData,
    TurnStartData,
    TurnStatus,
    Usage,
    UsageData,
)
from arr.errors import ArrError
from arr.models import AgentConfig, AgentStatus, Provider, StrictModel
from arr.resolve import load_yaml_model, validate_model
from arr.room.types import MessageKind, RoomAPI, RoomError, VoteChoice, VoteRule

ROOM_TOOL_PREFIX = "mcp__arr-room__"
"""Tool name prefix of room actions in the event log (MCP server `arr-room`, docs/01 §5)."""


class FakeScriptError(ArrError):
    """The script cannot be played, e.g. it has no entry for this turn or a bad reference."""


# --------------------------------------------------------------------------------------------
# Script model
# --------------------------------------------------------------------------------------------


class TextStep(StrictModel):
    """Answer text of the agent (`text` event)."""

    do: Literal["text"]
    text: str


class ThinkingStep(StrictModel):
    """Reasoning (`thinking` event)."""

    do: Literal["thinking"]
    text: str
    summarized: bool = True


class ToolStep(StrictModel):
    """Tool call with its result: a `tool_call`, after `duration_s` the `tool_result`."""

    do: Literal["tool"]
    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    result: str | None = ""
    is_error: bool = False
    id: str | None = Field(default=None, description="Tool call ID; default `toolu_fake_<n>`")
    duration_s: float = Field(default=0, ge=0)


class RoomAction(StrEnum):
    """Methods of `RoomAPI` that a script can call (docs/06-gruppenchats.md §9)."""

    INBOX = "inbox"
    LIST_CHANNELS = "list_channels"
    READ_MESSAGES = "read_messages"
    POST_MESSAGE = "post_message"
    GET_ATTACHMENT = "get_attachment"
    VOTE = "vote"
    SET_STATUS = "set_status"
    SUBMIT_RESULT = "submit_result"
    SUBMIT_RATING = "submit_rating"
    GET_ASSIGNMENT = "get_assignment"
    GIVE_FLOOR = "give_floor"
    CALL_VOTE = "call_vote"
    PIN = "pin"
    CLOSE_DISCUSSION = "close_discussion"


_ENUM_ARGS: dict[str, dict[str, type[StrEnum]]] = {
    RoomAction.POST_MESSAGE: {"kind": MessageKind},
    RoomAction.VOTE: {"choice": VoteChoice},
    RoomAction.SET_STATUS: {"status": AgentStatus},
    RoomAction.CALL_VOTE: {"rule": VoteRule},
}


class RoomStep(StrictModel):
    """A call of the agent's `RoomAPI`: message, question, answer, result, rating, vote, status …

    `action` is the method name, `args` its arguments (enum values as strings). A `RoomError`
    becomes a failed tool result and the turn goes on, as the agent would see it.
    """

    do: Literal["room"]
    action: RoomAction
    args: dict[str, Any] = Field(default_factory=dict)
    save_as: str | None = Field(default=None, description="Keep the return value under this name")


class UsageStep(StrictModel):
    """Token usage of one model request (`usage` event); summed into `turn_end`."""

    do: Literal["usage"]
    input: int | None = Field(default=None, ge=0)
    output: int | None = Field(default=None, ge=0)
    cache_read: int | None = Field(default=None, ge=0)
    cache_write: int | None = Field(default=None, ge=0)
    reasoning: int | None = Field(default=None, ge=0)
    model: str | None = Field(default=None, description="Default: the model of the agent")
    cost_usd: float | None = Field(default=None, ge=0)
    cost_kind: CostKind | None = Field(default=None, description="Default: of the script")


class RateLimitStep(StrictModel):
    """Rate limit report (`rate_limit` event)."""

    do: Literal["rate_limit"]
    status: str = "allowed_warning"
    utilization: float | None = Field(default=None, ge=0)
    resets_at: float | None = None


class DelayStep(StrictModel):
    """Wait; `interrupt()` ends the wait at once."""

    do: Literal["delay"]
    seconds: float = Field(ge=0)


class ErrorStep(StrictModel):
    """`error` event. Not recoverable: the turn ends with status `error`; else it goes on."""

    do: Literal["error"]
    message: str
    type: str = "fake_error"
    recoverable: bool = False


Step = Annotated[
    TextStep
    | ThinkingStep
    | ToolStep
    | RoomStep
    | UsageStep
    | RateLimitStep
    | DelayStep
    | ErrorStep,
    Field(discriminator="do"),
]


class TurnScript(StrictModel):
    """Steps of one turn, played in order."""

    steps: list[Step] = Field(default_factory=list)


class FakeScript(StrictModel):
    """Behavior of a FakeBackend agent: entry n of `turns` is played for turn n."""

    version: Literal[1] = 1
    cost_kind: CostKind | None = CostKind.ESTIMATED
    repeat_last: bool = False
    turns: list[TurnScript] = Field(default_factory=list)


def load_script(source: "str | Path | Mapping[str, Any] | FakeScript") -> FakeScript:
    """Load a script from a YAML file, plain data or an existing `FakeScript`.

    Invalid scripts raise `ConfigError` (German, with the path to the faulty step).
    """
    if isinstance(source, FakeScript):
        return source
    if isinstance(source, Mapping):
        return validate_model(FakeScript, dict(source), source="FakeBackend-Skript")
    return load_yaml_model(source, FakeScript)


# --------------------------------------------------------------------------------------------
# References and plain data
# --------------------------------------------------------------------------------------------

_REF = re.compile(r"^\$\{([^}]+)\}$")


def _plain(value: Any) -> Any:
    """Convert models, enums and paths to plain JSON-compatible data."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value


def _lookup(path: str, saved: Mapping[str, Any]) -> Any:
    name, *rest = path.split(".")
    if name not in saved:
        raise FakeScriptError(
            f"Skript: „${{{path}}}“ verweist auf „{name}“, das vorher nicht mit save_as "
            "gespeichert wurde."
        )
    value = saved[name]
    for part in rest:
        try:
            value = value[int(part)] if isinstance(value, list) else value[part]
        except (KeyError, IndexError, ValueError, TypeError):
            raise FakeScriptError(
                f"Skript: „${{{path}}}“ ist nicht auflösbar, „{part}“ fehlt."
            ) from None
    return value


def _resolve_refs(value: Any, saved: Mapping[str, Any]) -> Any:
    if isinstance(value, str) and (match := _REF.match(value)):
        return _lookup(match.group(1), saved)
    if isinstance(value, list):
        return [_resolve_refs(item, saved) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_refs(item, saved) for key, item in value.items()}
    return value


# --------------------------------------------------------------------------------------------
# Backend
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _TurnState:
    n: int
    usage: Usage = field(default_factory=Usage)
    models: set[str] = field(default_factory=set)
    tool_calls: int = 0
    saved: dict[str, Any] = field(default_factory=dict)
    status: TurnStatus = TurnStatus.OK


class FakeBackend:
    """Plays a `FakeScript`; implements `AgentBackend` (docs/01 §3).

    `run_id` goes into the events. `room` is the `RoomAPI` view of this agent for room
    actions; it can be set after construction. Room methods are synchronous and are called
    directly (fast, deterministic). `interrupt()` ends a running turn at the next step or
    during a delay with `turn_end` (`interrupted`); `deliver_hint()` queues a hint that is
    emitted as a `hint` event between steps.
    """

    def __init__(
        self,
        config: AgentConfig,
        script: "FakeScript | Mapping[str, Any] | str | Path",
        *,
        run_id: str = "fake-run",
        room: RoomAPI | None = None,
    ) -> None:
        self.config = config
        self.script = load_script(script)
        self.run_id = run_id
        self.room = room
        self._session = f"fake-{config.id}"
        self._turn = 0
        self._started = False
        self._closed = False
        self._running = False
        self._interrupted = asyncio.Event()
        self._wake = asyncio.Event()
        self._hints: list[str] = []

    # -- lifecycle ------------------------------------------------------------------------

    async def start(self) -> None:
        """Mark the backend as started (nothing to launch)."""
        if self._closed:
            raise RuntimeError("FakeBackend ist geschlossen")
        self._started = True

    async def close(self) -> None:
        """Stop a running turn and close; safe to call more than once."""
        await self.interrupt()
        self._closed = True

    async def interrupt(self) -> None:
        """Stop the running turn; no-op if idle."""
        if self._running:
            self._interrupted.set()
            self._wake.set()

    async def deliver_hint(self, text: str) -> bool:
        """Queue a hint for the running turn; False when idle (the hint stays in the inbox)."""
        if not self._running:
            return False
        self._hints.append(text)
        self._wake.set()
        return True

    def session_ref(self) -> dict[str, str]:
        """Session / thread ID for `session.json`, named like the real backend's."""
        if not self._started:
            return {}
        key = "thread_id" if self.config.provider is Provider.CODEX else "session_id"
        return {key: self._session}

    # -- turn -----------------------------------------------------------------------------

    async def run_turn(self, prompt: str, *, stage: str | None = None) -> AsyncIterator[AgentEvent]:
        """Play the next turn of the script; ends with `turn_end` also after interrupt/errors."""
        if not self._started or self._closed:
            raise RuntimeError("FakeBackend: start() fehlt oder close() ist schon erfolgt")
        if self._running:
            raise RuntimeError("FakeBackend: es läuft schon ein Turn")
        self._running = True
        self._interrupted.clear()
        self._wake.clear()
        self._hints.clear()
        self._turn += 1
        state = _TurnState(self._turn)
        try:
            yield self._event(
                state, EventKind.TURN_START, TurnStartData(prompt=prompt, stage=stage)
            )
            try:
                async for event in self._play(state):
                    yield event
            except Exception as exc:  # script or room bug: report it, keep the turn contract
                state.status = TurnStatus.ERROR
                yield self._event(
                    state,
                    EventKind.ERROR,
                    ErrorData(message=str(exc), type=type(exc).__name__, recoverable=False),
                )
            if self._interrupted.is_set() and state.status is TurnStatus.OK:
                state.status = TurnStatus.INTERRUPTED
            tail = list(self._take_hints(state))
            self._running = False  # no hint is accepted after the last drain
            for event in tail:
                yield event
            yield self._turn_end(state)
        finally:
            self._running = False

    async def _play(self, state: _TurnState) -> AsyncIterator[AgentEvent]:
        for step in self._turn_script(state.n).steps:
            for event in self._take_hints(state):
                yield event
            if self._interrupted.is_set():
                return
            async for event in self._step(step, state):
                yield event
            if state.status is not TurnStatus.OK:
                return

    def _turn_script(self, n: int) -> TurnScript:
        turns = self.script.turns
        if n <= len(turns):
            return turns[n - 1]
        if self.script.repeat_last and turns:
            return turns[-1]
        raise FakeScriptError(
            f"Das Skript für „{self.config.id}“ hat keinen Eintrag für Turn {n} "
            f"(es hat {len(turns)}). Ergänze einen Turn oder setze repeat_last: true."
        )

    async def _step(self, step: Step, state: _TurnState) -> AsyncIterator[AgentEvent]:
        raw = {"fake_step": step.model_dump(mode="json")}

        def event(kind: EventKind, data: Mapping[str, Any]) -> AgentEvent:
            return self._event(state, kind, data, raw)

        match step:
            case TextStep():
                yield event(EventKind.TEXT, TextData(text=step.text))
            case ThinkingStep():
                yield event(
                    EventKind.THINKING, ThinkingData(text=step.text, summarized=step.summarized)
                )
            case ToolStep():
                state.tool_calls += 1
                call_id = step.id or f"toolu_fake_{state.n}_{state.tool_calls}"
                yield event(
                    EventKind.TOOL_CALL,
                    ToolCallData(id=call_id, name=step.name, input=step.input),
                )
                async for hint in self._wait(step.duration_s, state):
                    yield hint
                if self._interrupted.is_set():
                    return  # no result for an aborted tool call
                yield event(
                    EventKind.TOOL_RESULT,
                    ToolResultData(
                        id=call_id, is_error=step.is_error, content=step.result, blob=None
                    ),
                )
            case RoomStep():
                state.tool_calls += 1
                call_id = f"toolu_fake_{state.n}_{state.tool_calls}"
                args = _resolve_refs(step.args, state.saved)
                yield event(
                    EventKind.TOOL_CALL,
                    ToolCallData(id=call_id, name=ROOM_TOOL_PREFIX + step.action.value, input=args),
                )
                content, is_error = self._call_room(step, args, state)
                yield event(
                    EventKind.TOOL_RESULT,
                    ToolResultData(id=call_id, is_error=is_error, content=content, blob=None),
                )
            case UsageStep():
                model = step.model or self.config.model
                state.models.add(model)
                cost_kind = step.cost_kind or self.script.cost_kind
                state.usage += Usage(
                    input=step.input,
                    output=step.output,
                    cache_read=step.cache_read,
                    cache_write=step.cache_write,
                    reasoning=step.reasoning,
                    cost_usd=step.cost_usd,
                    cost_kind=cost_kind if step.cost_usd is not None else None,
                )
                tokens = UsageData(
                    input=step.input,
                    output=step.output,
                    cache_read=step.cache_read,
                    cache_write=step.cache_write,
                    reasoning=step.reasoning,
                    model=model,
                )
                yield event(EventKind.USAGE, tokens)
            case RateLimitStep():
                yield event(
                    EventKind.RATE_LIMIT,
                    RateLimitData(
                        status=step.status, utilization=step.utilization, resets_at=step.resets_at
                    ),
                )
            case DelayStep():
                async for hint in self._wait(step.seconds, state):
                    yield hint
            case ErrorStep():
                yield event(
                    EventKind.ERROR,
                    ErrorData(message=step.message, type=step.type, recoverable=step.recoverable),
                )
                if not step.recoverable:
                    state.status = TurnStatus.ERROR

    def _call_room(
        self, step: RoomStep, args: dict[str, Any], state: _TurnState
    ) -> tuple[str, bool]:
        """Call the room; return the tool result text and whether it is an error."""
        if self.room is None:
            raise FakeScriptError(
                f"Das Skript ruft das Room auf („{step.action}“), aber dem FakeBackend von "
                f"„{self.config.id}“ ist kein RoomAPI zugewiesen (Parameter room)."
            )
        method = getattr(self.room, step.action.value)
        try:
            inspect.signature(getattr(RoomAPI, step.action.value)).bind(None, **args)
        except TypeError as exc:
            raise FakeScriptError(
                f"Room-Aktion „{step.action}“: ungültige Argumente ({exc})."
            ) from None
        kwargs = dict(args)
        for name, enum in _ENUM_ARGS.get(step.action, {}).items():
            if isinstance(kwargs.get(name), str):
                try:
                    kwargs[name] = enum(kwargs[name])
                except ValueError:
                    raise FakeScriptError(
                        f"Room-Aktion „{step.action}“: „{kwargs[name]}“ ist kein gültiger Wert "
                        f"für „{name}“."
                    ) from None
        try:
            result = method(**kwargs)
        except RoomError as exc:
            return str(exc), True
        plain = _plain(result)
        if step.save_as:
            state.saved[step.save_as] = plain
        return ("ok" if plain is None else json.dumps(plain, ensure_ascii=False)), False

    async def _wait(self, seconds: float, state: _TurnState) -> AsyncIterator[AgentEvent]:
        """Sleep up to `seconds`, emitting hints meanwhile; return early on `interrupt()`."""
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while True:
            for event in self._take_hints(state):
                yield event
            remaining = end - loop.time()
            if self._interrupted.is_set() or remaining <= 0:
                return
            try:
                await asyncio.wait_for(self._wake.wait(), remaining)
            except TimeoutError:
                return
            self._wake.clear()

    # -- events ---------------------------------------------------------------------------

    def _event(
        self,
        state: _TurnState,
        kind: EventKind,
        data: Mapping[str, Any],
        raw: Any = None,
    ) -> AgentEvent:
        return AgentEvent(
            run_id=self.run_id,
            agent_id=self.config.id,
            turn=state.n,
            kind=kind,
            data=dict(data),
            raw=raw,
        )

    def _take_hints(self, state: _TurnState) -> Iterator[AgentEvent]:
        method = "steer" if self.config.provider is Provider.CODEX else "post_tool_use"
        hints, self._hints = self._hints, []
        for text in hints:
            yield self._event(
                state,
                EventKind.HINT,
                HintData(text=text, channel=None, method=method),
                {"fake_hint": text},
            )

    def _turn_end(self, state: _TurnState) -> AgentEvent:
        usage = state.usage
        model = next(iter(state.models)) if len(state.models) == 1 else self.config.model
        tokens = UsageData(**usage.tokens(), model=model)
        data = TurnEndData(
            status=state.status.value,
            usage=tokens,
            cost_usd=usage.cost_usd,
            cost_kind=usage.cost_kind.value if usage.cost_kind else None,
            session=self._session,
            num_turns=1 + state.tool_calls,
        )
        return self._event(state, EventKind.TURN_END, data)
