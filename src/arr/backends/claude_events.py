"""Normalize the messages of `claude-agent-sdk` into `AgentEvent`s (docs/02 §6, S1 §3.2).

One `ClaudeEvents` per agent keeps the state that spans turns: message IDs already counted,
and the cumulative cost and token totals of the client session. Per turn it maps

- `AssistantMessage` blocks → `thinking` (summarized), `text`, `tool_call`; its `usage` once
  per `message_id` → `usage` (informational, docs/02 §6)
- `UserMessage` tool results → `tool_result`; a call without result gets a synthetic error
  result at the end of the turn, so every `tool_call` has its `tool_result`
- `RateLimitEvent` → `rate_limit`, `ResultMessage` → `turn_end` (plus `error` on failure)
- `SystemMessage` `compact_boundary` → `compacted` (the backend then resends `context.md`)

**Tokens and cost of a turn** come from the same source: the difference of the cumulative
`model_usage` (all models, incl. helper requests) and `total_cost_usd` to the previous
`ResultMessage` of the session. `ResultMessage.usage` covers only the main loop (W1-04
review: 8 vs. 1 213 input tokens in the fixture), and the `usage` of streamed assistant
messages carries start values (output tokens of the first chunk), so neither matches the
cost. A reset of the session totals (compaction, `/clear`) counts the new totals as the delta.
"""

from collections.abc import Mapping
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import ServerToolResultBlock, ServerToolUseBlock

from arr.backends.base import AgentEvent, CostKind, EventKind, TurnStatus, Usage, UsageData

MISSING_RESULT = "Kein Ergebnis: Der Turn endete, bevor das Werkzeug fertig war."
NO_RESULT = "Der Turn endete ohne Ergebnis der Claude-CLI."
LIMIT_SUBTYPES = frozenset({"error_max_turns", "error_max_budget_usd"})
_PRIVATE_KEYS = frozenset({"account", "email", "organization", "organizationName"})


def strip_private(value: Any) -> Any:
    """Copy of `value` without account data (e-mail, organization) for `raw.jsonl`."""
    if isinstance(value, Mapping):
        return {k: strip_private(v) for k, v in value.items() if k not in _PRIVATE_KEYS}
    if isinstance(value, list | tuple):
        return [strip_private(v) for v in value]
    return value


def _raw(msg: Any) -> Any:
    """Raw form of an SDK message: dataclass fields as a dict, account data removed."""
    fields = getattr(msg, "__dataclass_fields__", None)
    data = {name: getattr(msg, name) for name in fields} if fields else msg
    if isinstance(data, dict):
        data = {"type": type(msg).__name__, **data}
    return strip_private(data)


def _raw_once(events: list[AgentEvent]) -> list[AgentEvent]:
    """Keep the raw message only on the first event of one SDK message (W2-01.1, B7), so
    `raw.jsonl` holds every message once and not once per block."""
    for event in events[1:]:
        event.raw = None
    return events


def result_text(content: Any) -> str | None:
    """Text of a tool result: a string, or the text parts of a content list."""
    if content is None or isinstance(content, str):
        return content
    parts = []
    for part in content if isinstance(content, list) else [content]:
        if isinstance(part, Mapping) and part.get("type") == "text":
            parts.append(str(part.get("text", "")))
        elif isinstance(part, Mapping):
            parts.append(f"[{part.get('type', 'Inhalt')}]")
        else:
            parts.append(str(part))
    return "\n".join(parts)


def _model_totals(model_usage: Mapping[str, Any] | None) -> Usage:
    """Sum of the cumulative per-model counters of a `ResultMessage`."""
    total = Usage()
    for entry in (model_usage or {}).values():
        thinking = entry.get("thinkingTokens")
        total = total + Usage(
            input=entry.get("inputTokens"),
            output=entry.get("outputTokens"),
            cache_read=entry.get("cacheReadInputTokens"),
            cache_write=entry.get("cacheCreationInputTokens"),
            reasoning=thinking if isinstance(thinking, int) else None,
        )
    return total


def _delta(now: int | None, before: int | None) -> int | None:
    if now is None:
        return None
    if before is None or now < before:  # first result, or totals were reset
        return now
    return now - before


def message_usage(usage: Mapping[str, Any], model: str | None) -> UsageData:
    """`usage` of one model request in the shape of `UsageData`."""
    details = usage.get("output_tokens_details") or {}
    data = UsageData(
        input=usage.get("input_tokens"),
        output=usage.get("output_tokens"),
        cache_read=usage.get("cache_read_input_tokens"),
        cache_write=usage.get("cache_creation_input_tokens"),
        reasoning=details.get("thinking_tokens") if isinstance(details, Mapping) else None,
    )
    if model:
        data["model"] = model
    return data


class ClaudeEvents:
    """Stateful mapper from SDK messages to `AgentEvent`s for one agent."""

    def __init__(self, run_id: str, agent_id: str, model: str, cost_kind: CostKind | None) -> None:
        self.run_id = run_id
        self.agent_id = agent_id
        self.model = model
        self.cost_kind = cost_kind
        self.compacted = False
        self.session_id: str | None = None
        self.turn = 0
        self._seen: set[str] = set()
        self._totals = Usage()
        self._cost_total: float | None = None
        self._open: dict[str, str] = {}
        self._result: ResultMessage | None = None

    def new_session(self) -> None:
        """Forget the session: a new CLI counts usage and cost from zero again (W4-03 F5)."""
        self.session_id = None
        self.compacted = False
        self._totals = Usage()
        self._cost_total = None

    def event(self, kind: EventKind, data: Mapping[str, Any], raw: Any = None) -> AgentEvent:
        return AgentEvent(self.run_id, self.agent_id, self.turn, kind, dict(data), raw=raw)

    def start_turn(self, turn: int) -> None:
        self.turn = turn
        self._open = {}
        self._result = None

    @property
    def result(self) -> ResultMessage | None:
        return self._result

    def feed(self, msg: Any) -> list[AgentEvent]:
        """Events for one SDK message (often none)."""
        if isinstance(msg, AssistantMessage):
            return self._assistant(msg)
        if isinstance(msg, UserMessage):
            return self._user(msg)
        if isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            data = {
                "status": info.status,
                "utilization": info.utilization,
                "resets_at": float(info.resets_at) if info.resets_at is not None else None,
            }
            return [self.event(EventKind.RATE_LIMIT, data, _raw(msg))]
        if isinstance(msg, ResultMessage):
            self._result = msg
            self.session_id = msg.session_id or self.session_id
            return []
        if isinstance(msg, SystemMessage):
            if msg.subtype == "compact_boundary":
                self.compacted = True
            elif msg.subtype == "init":
                self.session_id = msg.data.get("session_id") or self.session_id
        return []

    def _assistant(self, msg: AssistantMessage) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        raw = _raw(msg)
        if msg.error:
            data = {"message": f"Fehler der Claude-API: {msg.error}", "type": msg.error}
            events.append(self.event(EventKind.ERROR, {**data, "recoverable": True}, raw))
        for block in msg.content:
            if isinstance(block, ThinkingBlock):
                data = {"text": block.thinking, "summarized": True}
                events.append(self.event(EventKind.THINKING, data, raw))
            elif isinstance(block, TextBlock):
                events.append(self.event(EventKind.TEXT, {"text": block.text}, raw))
            elif isinstance(block, ToolUseBlock):
                self._open[block.id] = block.name
                data = {"id": block.id, "name": block.name, "input": block.input}
                events.append(self.event(EventKind.TOOL_CALL, data, raw))
            elif isinstance(block, ServerToolUseBlock):
                self._open[block.id] = block.name
                data = {"id": block.id, "name": block.name, "input": block.input}
                data["source_type"] = "server_tool_use"
                events.append(self.event(EventKind.TOOL_CALL, data, raw))
            elif isinstance(block, ServerToolResultBlock):
                events.append(self._tool_result(block.tool_use_id, False, block.content, raw))
        if msg.usage and msg.message_id and msg.message_id not in self._seen:
            self._seen.add(msg.message_id)
            data = message_usage(msg.usage, msg.model or self.model)
            events.append(self.event(EventKind.USAGE, data, raw))
        return _raw_once(events)

    def _user(self, msg: UserMessage) -> list[AgentEvent]:
        if not isinstance(msg.content, list):
            return []
        raw = _raw(msg)
        return _raw_once(
            [
                self._tool_result(block.tool_use_id, bool(block.is_error), block.content, raw)
                for block in msg.content
                if isinstance(block, ToolResultBlock)
            ]
        )

    def _tool_result(self, call_id: str, is_error: bool, content: Any, raw: Any) -> AgentEvent:
        self._open.pop(call_id, None)
        data = {"id": call_id, "is_error": is_error, "content": result_text(content), "blob": None}
        return self.event(EventKind.TOOL_RESULT, data, raw)

    def finish(self, *, interrupted: bool = False, error: str | None = None) -> list[AgentEvent]:
        """Close the turn: results for open calls, `error` if needed, then `turn_end`."""
        events = [
            self.event(
                EventKind.TOOL_RESULT,
                {"id": call_id, "is_error": True, "content": MISSING_RESULT, "blob": None},
            )
            for call_id in self._open
        ]
        self._open = {}
        result = self._result
        status, message = self._status(result, interrupted, error)
        if message is not None:
            data = {"message": message, "type": "turn", "recoverable": True}
            events.append(self.event(EventKind.ERROR, data, _raw(result) if result else None))
        usage, cost = self._turn_usage(result)
        data = {
            "status": str(status),
            "usage": {**usage.tokens(), "model": self.model},
            "cost_usd": cost,
            "cost_kind": str(self.cost_kind) if cost is not None and self.cost_kind else None,
            "session": self.session_id,
            "num_turns": result.num_turns if result else None,
        }
        if result is not None:
            data["stop_reason"] = result.stop_reason
            data["terminal_reason"] = result.terminal_reason
            data["permission_denials"] = strip_private(result.permission_denials or [])
        events.append(self.event(EventKind.TURN_END, data, _raw(result) if result else None))
        return events

    def _status(
        self, result: ResultMessage | None, interrupted: bool, error: str | None
    ) -> tuple[TurnStatus, str | None]:
        if error is not None:
            return TurnStatus.ERROR, error
        terminal = (result.terminal_reason or "") if result else ""
        if interrupted or terminal.startswith("aborted"):
            return TurnStatus.INTERRUPTED, None
        if result is None:
            return TurnStatus.ERROR, NO_RESULT
        if result.subtype in LIMIT_SUBTYPES or terminal == "max_turns":
            return TurnStatus.LIMIT, None
        if result.is_error or result.subtype != "success":
            detail = "; ".join(result.errors or []) or result.result or result.subtype
            return TurnStatus.ERROR, f"Der Turn ist fehlgeschlagen: {detail}"
        return TurnStatus.OK, None

    def _turn_usage(self, result: ResultMessage | None) -> tuple[Usage, float | None]:
        if result is None:
            return Usage(), None
        totals = _model_totals(result.model_usage)
        before = self._totals
        usage = Usage(
            input=_delta(totals.input, before.input),
            output=_delta(totals.output, before.output),
            cache_read=_delta(totals.cache_read, before.cache_read),
            cache_write=_delta(totals.cache_write, before.cache_write),
            reasoning=_delta(totals.reasoning, before.reasoning),
        )
        self._totals = totals
        cost = None
        if result.total_cost_usd is not None:
            previous = self._cost_total
            reset = previous is None or result.total_cost_usd < previous
            cost = result.total_cost_usd if reset else result.total_cost_usd - previous
            self._cost_total = result.total_cost_usd
        return usage, cost
