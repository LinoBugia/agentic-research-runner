"""Codex app-server notifications → normalized `AgentEvent` data (docs/spikes/S2_codex.md §15).

Pure data, no SDK import: `TurnEvents.feed(method, params)` takes one notification of a turn
(the JSON params as in `tests/fixtures/codex/`) and returns `(kind, data)` pairs; the backend
wraps them into `AgentEvent`s with the raw notification attached.

- Items: `agentMessage` → `text`, `reasoning` with a summary → `thinking` (empty summaries
  produce nothing), `commandExecution` / `fileChange` / `mcpToolCall` / `webSearch` →
  `tool_call` on `item/started` and `tool_result` on `item/completed`.
- Usage: `thread/tokenUsage/updated` after every model call. `last` becomes a `usage` event,
  the turn's usage is the difference of `total` (S2 §5). `inputTokens` includes
  `cachedInputTokens`, so `input` is the uncached part and `cache_read` the cached one.
- Rate limits: `account/rateLimits/updated` → `rate_limit` with the RunManager's statuses
  (`rate_limit_status`), only when the status or the rounded utilization changes.
- Blocked commands: the sandbox's blocked commands can be missing in the stream, completely
  or with `item/started` only. They are in the rollout (`thread.path`); `TurnEvents.reconcile`
  compares both by command text and adds only what the stream lacks: a whole call, or just
  the result of a started item. Nothing is logged twice (S2 §15).
"""

import json
import re
import shlex
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arr.backends.base import (
    ErrorData,
    EventKind,
    RateLimitData,
    TextData,
    ThinkingData,
    ToolCallData,
    ToolResultData,
    TurnStatus,
    Usage,
    UsageData,
)

__all__ = [
    "RATE_LIMIT_WARNING",
    "RolloutCall",
    "TurnEvents",
    "missing_commands",
    "rate_limit_status",
    "read_rollout",
    "rollout_calls",
    "unwrap_shell",
]

RATE_LIMIT_WARNING = 80.0
"""From this `usedPercent` of a window on the profile is throttled (`allowed_warning`)."""

Event = tuple[EventKind, dict[str, Any]]

_TOOL_ITEMS = ("commandExecution", "fileChange", "mcpToolCall", "webSearch", "dynamicToolCall")
_TURN_STATUS = {
    "completed": TurnStatus.OK,
    "interrupted": TurnStatus.INTERRUPTED,
    "failed": TurnStatus.ERROR,
}


def unwrap_shell(command: str) -> str:
    """`/bin/zsh -lc 'cmd'` → `cmd`; other commands unchanged (for matching only)."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return command.strip()
    if len(argv) == 3 and argv[1] in ("-c", "-lc") and Path(argv[0]).name in ("zsh", "bash", "sh"):
        return argv[2].strip()
    return command.strip()


def rate_limit_status(params: dict[str, Any]) -> RateLimitData | None:
    """Map `account/rateLimits/updated` to `allowed` / `allowed_warning` / `rejected`.

    The fuller window counts. A reached limit (`rateLimitReachedType`, 100 %) is `rejected`
    until `resetsAt`; from `RATE_LIMIT_WARNING` % on `allowed_warning`. Returns None
    without any window.
    """
    limits = params.get("rateLimits") or {}
    windows = [w for w in (limits.get("primary"), limits.get("secondary")) if w]
    if not windows:
        return None
    worst = max(windows, key=lambda w: w.get("usedPercent") or 0)
    used = float(worst.get("usedPercent") or 0)
    if limits.get("rateLimitReachedType") or used >= 100:
        status = "rejected"
    elif used >= RATE_LIMIT_WARNING:
        status = "allowed_warning"
    else:
        status = "allowed"
    resets = worst.get("resetsAt")
    return RateLimitData(
        status=status,
        utilization=round(used / 100, 4),
        resets_at=float(resets) if isinstance(resets, int | float) else None,
    )


def _usage(counts: dict[str, Any] | None) -> Usage:
    counts = counts or {}
    total_in = counts.get("inputTokens") or 0
    cached = counts.get("cachedInputTokens") or 0
    return Usage(
        input=max(total_in - cached, 0),
        output=counts.get("outputTokens") or 0,
        cache_read=cached,
        cache_write=counts.get("cacheWriteInputTokens") or 0,
        reasoning=counts.get("reasoningOutputTokens") or 0,
    )


def _diff(end: Usage, start: Usage) -> Usage:
    def minus(a: int | None, b: int | None) -> int | None:
        return None if a is None else max(a - (b or 0), 0)

    return Usage(
        input=minus(end.input, start.input),
        output=minus(end.output, start.output),
        cache_read=minus(end.cache_read, start.cache_read),
        cache_write=minus(end.cache_write, start.cache_write),
        reasoning=minus(end.reasoning, start.reasoning),
    )


def _summary_text(item: dict[str, Any]) -> str:
    parts = []
    for part in item.get("summary") or []:
        text = part.get("text") if isinstance(part, dict) else part
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def _error_type(info: Any) -> str:
    """`codexErrorInfo` is a string or a one-key object, e.g. `{"httpConnectionFailed": …}`."""
    if isinstance(info, str):
        return info
    if isinstance(info, dict) and info:
        return str(next(iter(info)))
    return "error"


def _tool_name(item: dict[str, Any]) -> str:
    if item["type"] == "mcpToolCall":
        return f"mcp__{item.get('server')}__{item.get('tool')}"
    return str(item["type"])


def _tool_input(item: dict[str, Any]) -> dict[str, Any]:
    match item["type"]:
        case "commandExecution":
            return {"command": item.get("command"), "cwd": item.get("cwd")}
        case "fileChange":
            changes = item.get("changes") or []
            return {"changes": [{"path": c.get("path"), "kind": c.get("kind")} for c in changes]}
        case "mcpToolCall":
            return dict(item.get("arguments") or {})
        case "webSearch":
            return {"query": item.get("query")}
        case _:
            return {k: v for k, v in item.items() if k not in ("id", "type", "status")}


def _tool_result(item: dict[str, Any]) -> ToolResultData:
    status = item.get("status")
    match item["type"]:
        case "commandExecution":
            content = item.get("aggregatedOutput")
            error = status != "completed" or item.get("exitCode") not in (0, None)
        case "fileChange":
            content = "\n".join(c.get("diff") or "" for c in item.get("changes") or []) or None
            error = status != "completed"
        case "mcpToolCall":
            failure = item.get("error")
            result = item.get("result")
            if failure:
                content = failure.get("message") if isinstance(failure, dict) else str(failure)
            else:
                content = json.dumps(result, ensure_ascii=False) if result is not None else None
            error = bool(failure) or status == "failed"
        case _:
            content = json.dumps(item, ensure_ascii=False)
            error = status in ("failed", "declined")
    return ToolResultData(id=item["id"], is_error=bool(error), content=content, blob=None)


@dataclass
class TurnEvents:
    """State of one turn while its notifications are mapped (see module docstring)."""

    model: str | None = None
    turn_id: str | None = None
    status: TurnStatus | None = None
    error: str | None = None
    duration_ms: int | None = None
    requests: int = 0
    commands: list[str] = field(default_factory=list)
    """Commands (unwrapped) of the stream's completed `commandExecution` items."""
    open_commands: dict[str, str] = field(default_factory=dict)
    """Started `commandExecution` items without completion: item ID → command."""
    _started: set[str] = field(default_factory=set)
    _start: Usage | None = None
    _total: Usage | None = None
    _rate: tuple[str, float | None] | None = None

    def usage(self) -> Usage:
        """Usage of this turn: difference of the thread's `total` (zero without updates)."""
        if self._total is None or self._start is None:
            return Usage(input=0, output=0, cache_read=0, cache_write=0, reasoning=0)
        return _diff(self._total, self._start)

    def feed(self, method: str, params: dict[str, Any] | None) -> list[Event]:
        """Map one notification; returns zero or more `(kind, data)` pairs."""
        params = params or {}
        match method:
            case "turn/started":
                self.turn_id = (params.get("turn") or {}).get("id")
            case "item/started":
                return list(self._item_started(params.get("item") or {}))
            case "item/completed":
                return list(self._item_completed(params.get("item") or {}))
            case "thread/tokenUsage/updated":
                return self._token_usage(params.get("tokenUsage") or {})
            case "account/rateLimits/updated":
                return self._rate_limits(params)
            case "error":
                failure = params.get("error") or {}
                data = ErrorData(
                    message=str(failure.get("message") or "Codex-Fehler"),
                    type=_error_type(failure.get("codexErrorInfo")),
                    recoverable=bool(params.get("willRetry")),
                )
                return [(EventKind.ERROR, dict(data))]
            case "turn/completed":
                turn = params.get("turn") or {}
                self.status = _TURN_STATUS.get(turn.get("status"), TurnStatus.ERROR)
                failure = turn.get("error")
                if failure:
                    self.error = (
                        failure.get("message") if isinstance(failure, dict) else str(failure)
                    )
                self.duration_ms = turn.get("durationMs")
        return []

    def _item_started(self, item: dict[str, Any]) -> Iterator[Event]:
        if item.get("type") in _TOOL_ITEMS and item.get("id") not in self._started:
            self._started.add(item["id"])
            if item["type"] == "commandExecution" and item.get("command"):
                self.open_commands[item["id"]] = unwrap_shell(item["command"])
            yield EventKind.TOOL_CALL, self._call(item)

    def _call(self, item: dict[str, Any]) -> dict[str, Any]:
        return dict(
            ToolCallData(
                id=item["id"],
                name=_tool_name(item),
                input=_tool_input(item),
                source_type=item["type"],
            )
        )

    def _item_completed(self, item: dict[str, Any]) -> Iterator[Event]:
        kind = item.get("type")
        if kind == "agentMessage":
            if item.get("text"):
                yield EventKind.TEXT, dict(TextData(text=item["text"]))
        elif kind == "reasoning":
            text = _summary_text(item)
            if text:
                yield EventKind.THINKING, dict(ThinkingData(text=text, summarized=True))
        elif kind in _TOOL_ITEMS:
            if item.get("id") not in self._started:  # completed without a start notification
                self._started.add(item["id"])
                yield EventKind.TOOL_CALL, self._call(item)
            if kind == "commandExecution" and item.get("command"):
                self.commands.append(unwrap_shell(item["command"]))
                self.open_commands.pop(item["id"], None)
            yield EventKind.TOOL_RESULT, dict(_tool_result(item))

    def reconcile(self, calls: Iterable["RolloutCall"]) -> list[Event]:
        """Events for the rollout calls the stream missed (call this after `turn/completed`).

        A call whose item was started but never completed gets only its `tool_result`, under
        the stream's item ID; a call the stream never showed gets `tool_call` + `tool_result`
        with `source_type: rollout`.
        """
        out: list[Event] = []
        for call in missing_commands(calls, self.commands):
            result = ToolResultData(
                id=call.call_id, is_error=call.exit_code != 0, content=call.output, blob=None
            )
            started = next((i for i, c in self.open_commands.items() if c == call.command), None)
            if started is not None:
                del self.open_commands[started]
                out.append((EventKind.TOOL_RESULT, dict(result, id=started)))
                continue
            tool = ToolCallData(
                id=call.call_id,
                name="commandExecution",
                input={"command": call.command},
                source_type="rollout",
            )
            out += [(EventKind.TOOL_CALL, dict(tool)), (EventKind.TOOL_RESULT, dict(result))]
        return out

    def _token_usage(self, usage: dict[str, Any]) -> list[Event]:
        total, last = _usage(usage.get("total")), _usage(usage.get("last"))
        if self._start is None:
            # before the first call of this turn; also right after a resume (total restarts)
            self._start = _diff(total, last)
        self._total = total
        self.requests += 1
        data = UsageData(**last.tokens())
        if self.model:
            data["model"] = self.model
        return [(EventKind.USAGE, dict(data))]

    def _rate_limits(self, params: dict[str, Any]) -> list[Event]:
        data = rate_limit_status(params)
        if data is None:
            return []
        key = (data["status"], round(data["utilization"] or 0, 2))
        if key == self._rate:
            return []
        self._rate = key
        return [(EventKind.RATE_LIMIT, dict(data))]


# --------------------------------------------------------------------------------------------
# Rollout reconciliation
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RolloutCall:
    """A shell call of one turn as recorded in the rollout."""

    call_id: str
    command: str
    output: str | None
    exit_code: int | None


_JS_CMD = re.compile(r"exec_command\(\s*\{\s*cmd\s*:\s*(\"(?:[^\"\\]|\\.)*\")")


def _rollout_command(payload: dict[str, Any]) -> str | None:
    if payload.get("type") == "custom_tool_call" and payload.get("name") == "exec":
        match = _JS_CMD.search(str(payload.get("input") or ""))
        return json.loads(match.group(1)).strip() if match else None
    if payload.get("type") == "function_call" and payload.get("name") in (
        "exec_command",
        "shell",
        "local_shell",
    ):
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except json.JSONDecodeError:
            return None
        cmd = args.get("cmd") or args.get("command")
        if isinstance(cmd, list):
            return unwrap_shell(shlex.join(cmd))
        return cmd.strip() if isinstance(cmd, str) else None
    return None


def _rollout_output(payload: dict[str, Any]) -> tuple[str | None, int | None]:
    output = payload.get("output")
    texts = []
    if isinstance(output, list):
        texts = [part.get("text", "") for part in output if isinstance(part, dict)]
    elif isinstance(output, str):
        texts = [output]
    for text in reversed(texts):
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and ("exit_code" in data or "output" in data):
            return data.get("output"), data.get("exit_code")
    return ("\n".join(texts) or None), None


def rollout_calls(lines: Iterable[dict[str, Any]], turn_id: str) -> list[RolloutCall]:
    """Shell calls of turn `turn_id` from rollout lines (already parsed JSON)."""
    current: str | None = None
    calls: dict[str, tuple[str, dict[str, Any] | None]] = {}
    order: list[str] = []
    for line in lines:
        payload = line.get("payload") or {}
        kind = payload.get("type")
        if kind == "task_started":
            current = payload.get("turn_id")
            continue
        meta = payload.get("internal_chat_message_metadata_passthrough") or {}
        if (meta.get("turn_id") or current) != turn_id:
            continue
        command = _rollout_command(payload)
        if command is not None and payload.get("call_id"):
            calls[payload["call_id"]] = (command, None)
            order.append(payload["call_id"])
        elif kind in ("custom_tool_call_output", "function_call_output"):
            call_id = payload.get("call_id")
            if call_id in calls:
                calls[call_id] = (calls[call_id][0], payload)
    result = []
    for call_id in order:
        command, out = calls[call_id]
        output, code = _rollout_output(out) if out else (None, None)
        result.append(RolloutCall(call_id, command, output, code))
    return result


def read_rollout(path: str | Path) -> list[dict[str, Any]]:
    """Parsed lines of a rollout file; unreadable lines are skipped."""
    lines = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    for raw in text.splitlines():
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return lines


def missing_commands(calls: Iterable[RolloutCall], seen: Iterable[str]) -> list[RolloutCall]:
    """Rollout calls whose command the stream did not report (counted, so repeats match)."""
    left = Counter(seen)
    missing = []
    for call in calls:
        if left[call.command] > 0:
            left[call.command] -= 1
        else:
            missing.append(call)
    return missing
