"""Normalization of Codex notifications and the rollout reconciliation, against the recorded
fixtures of S2 and W2-02 (no model, no SDK)."""

import json
from pathlib import Path
from typing import Any

import pytest

from arr.backends.base import EventKind, TurnStatus
from arr.backends.codex_events import (
    TurnEvents,
    missing_commands,
    rate_limit_status,
    rollout_calls,
    unwrap_shell,
)

FIXTURES = Path(__file__).parent / "fixtures" / "codex"


def notifications(name: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        msg = entry["msg"]
        if entry["dir"] == "in" and "method" in msg and "id" not in msg:
            out.append((msg["method"], msg.get("params") or {}))
    return out


def replay(name: str, turn: int = 0) -> tuple[TurnEvents, list[tuple[EventKind, dict[str, Any]]]]:
    """Map the notifications of the `turn`-th turn of a fixture."""
    turns: list[list[tuple[str, dict[str, Any]]]] = []
    for method, params in notifications(name):
        if method == "turn/started":
            turns.append([])
        if turns:
            turns[-1].append((method, params))
    state = TurnEvents(model="gpt-6-luna")
    events = [event for method, params in turns[turn] for event in state.feed(method, params)]
    return state, events


def of(events: list[tuple[EventKind, dict[str, Any]]], kind: EventKind) -> list[dict[str, Any]]:
    return [data for k, data in events if k is kind]


def test_write_and_run() -> None:
    state, events = replay("turn_write_and_run.jsonl")
    assert state.status is TurnStatus.OK and state.turn_id
    texts = of(events, EventKind.TEXT)
    assert len(texts) == 2 and texts[-1]["text"]
    calls, results = of(events, EventKind.TOOL_CALL), of(events, EventKind.TOOL_RESULT)
    assert [c["name"] for c in calls] == ["commandExecution"]
    assert calls[0]["input"]["command"].startswith("/bin/zsh -lc")
    assert results[0]["id"] == calls[0]["id"] and results[0]["is_error"] is False
    assert not of(events, EventKind.THINKING)


def test_usage_is_the_difference_of_total_without_double_cache() -> None:
    state, events = replay("turn_write_and_run.jsonl")
    per_call = of(events, EventKind.USAGE)
    assert len(per_call) == 2 and per_call[0]["model"] == "gpt-6-luna"
    assert per_call[0]["input"] == 11484 and per_call[0]["cache_read"] == 0
    turn = state.usage()
    # total inputTokens 23141 include 11264 cached tokens
    assert (turn.input, turn.cache_read, turn.output) == (23141 - 11264, 11264, 212)
    assert sum(u["input"] + u["cache_read"] for u in per_call) == 23141


def test_usage_after_resume_counts_only_the_new_turn() -> None:
    state, _ = replay("thread_resume_after_restart.jsonl")  # the turn after the restart
    turn = state.usage()
    assert turn.input + turn.cache_read == 22211 - 11093
    assert turn.output == 11 - 5


def test_patch_and_mcp_tools() -> None:
    _, events = replay("turn_reasoning_patch.jsonl")
    calls = of(events, EventKind.TOOL_CALL)
    assert [c["name"] for c in calls] == ["fileChange", "commandExecution"]
    patch = next(r for r in of(events, EventKind.TOOL_RESULT) if r["id"] == calls[0]["id"])
    assert "91 = 7" in patch["content"]
    _, events = replay("turn_mcp_thread.jsonl")
    names = [c["name"] for c in of(events, EventKind.TOOL_CALL)]
    assert names and all(n.startswith("mcp__") for n in names)


def test_empty_reasoning_gives_no_thinking() -> None:
    _, events = replay("turn_reasoning_medium_hard.jsonl")
    assert not of(events, EventKind.THINKING)


def test_reasoning_summary_becomes_thinking() -> None:
    state = TurnEvents()
    item = {"type": "reasoning", "id": "rs", "summary": ["Erst prüfen.", {"text": "Dann."}]}
    assert state.feed("item/completed", {"item": item}) == [
        (EventKind.THINKING, {"text": "Erst prüfen.\n\nDann.", "summarized": True})
    ]


def test_tool_call_once_also_without_start() -> None:
    state = TurnEvents()
    item = {
        "type": "commandExecution",
        "id": "x",
        "command": "ls",
        "status": "failed",
        "exitCode": 1,
        "aggregatedOutput": "nope",
    }
    started = state.feed("item/started", {"item": {**item, "status": "inProgress"}})
    done = state.feed("item/completed", {"item": item})
    assert [k for k, _ in started + done] == [EventKind.TOOL_CALL, EventKind.TOOL_RESULT]
    assert done[0][1]["is_error"] is True
    assert [k for k, _ in TurnEvents().feed("item/completed", {"item": item})] == [
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
    ]


def test_error_notification() -> None:
    params = {
        "error": {"message": "Verbindung weg", "codexErrorInfo": {"httpConnectionFailed": {}}},
        "willRetry": True,
    }
    assert TurnEvents().feed("error", params) == [
        (
            EventKind.ERROR,
            {"message": "Verbindung weg", "type": "httpConnectionFailed", "recoverable": True},
        )
    ]


@pytest.mark.parametrize(
    ("primary", "reached", "status"),
    [
        (0, None, "allowed"),
        (85, None, "allowed_warning"),
        (100, None, "rejected"),
        (40, "rateLimitReached", "rejected"),
    ],
)
def test_rate_limit_status(primary: float, reached: str | None, status: str) -> None:
    params = {
        "rateLimits": {
            "primary": {"usedPercent": primary, "resetsAt": 1793472850},
            "secondary": {"usedPercent": 0, "resetsAt": 1},
            "rateLimitReachedType": reached,
        }
    }
    data = rate_limit_status(params)
    assert data == {"status": status, "utilization": primary / 100, "resets_at": 1793472850.0}


def test_rate_limit_only_on_change() -> None:
    state = TurnEvents()
    params = {"rateLimits": {"primary": {"usedPercent": 3, "resetsAt": 5}}}
    assert len(state.feed("account/rateLimits/updated", params)) == 1
    assert state.feed("account/rateLimits/updated", params) == []
    assert rate_limit_status({"rateLimits": {"primary": None}}) is None


def test_rate_limits_of_the_fixture() -> None:
    _, events = replay("thread_resume_after_restart.jsonl")
    assert of(events, EventKind.RATE_LIMIT) == [
        {"status": "allowed", "utilization": 0.0, "resets_at": 1793472850.0}
    ]


# -- rollout reconciliation ---------------------------------------------------------------


def blocked() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    stream, rollout = [], []
    for line in (FIXTURES / "blocked_commands.jsonl").read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        (stream if entry["source"] == "stream" else rollout).append(entry["line"])
    return stream, rollout


def test_unwrap_shell() -> None:
    assert unwrap_shell("/bin/zsh -lc 'cat a.txt'") == "cat a.txt"
    assert unwrap_shell('/bin/bash -c "echo 1"') == "echo 1"
    assert unwrap_shell("python3 x.py") == "python3 x.py"


def test_rollout_adds_only_the_commands_missing_in_the_stream() -> None:
    """Strict run of E19: the sandbox's blocked commands are missing in the stream."""
    stream, rollout = blocked()
    state = TurnEvents()
    for line in stream:
        state.feed(line["msg"]["method"], line["msg"]["params"])
    calls = rollout_calls(rollout, state.turn_id or "")
    missing = missing_commands(calls, state.commands)
    assert len(state.commands) == 11 and len(calls) > 20
    assert len(missing) == len(calls) - len(state.commands)
    assert not {m.command for m in missing} & set(state.commands)  # nothing twice
    l5 = next(m for m in missing if m.command.startswith(": L5;"))
    assert l5.exit_code == 1 and "Operation not permitted" in (l5.output or "")
    assert l5.call_id.startswith("call_")


def test_rollout_of_another_turn_is_ignored() -> None:
    _, rollout = blocked()
    assert rollout_calls(rollout, "other-turn") == []


def test_repeated_command_is_counted() -> None:
    from arr.backends.codex_events import RolloutCall

    calls = [RolloutCall("a", "ls", "", 0), RolloutCall("b", "ls", "", 0)]
    assert [c.call_id for c in missing_commands(calls, ["ls"])] == ["b"]


def test_started_but_unfinished_command_gets_only_its_result() -> None:
    """Live smoke: a sandbox-blocked `pip install` showed `item/started` only."""
    from arr.backends.codex_events import RolloutCall

    state = TurnEvents()
    item = {
        "type": "commandExecution",
        "id": "exec-1",
        "status": "inProgress",
        "command": "/bin/zsh -lc 'pip install tinyrecord'",
    }
    state.feed("item/started", {"item": item})
    calls = [RolloutCall("call_1", "pip install tinyrecord", "network blocked", 1)]
    events = state.reconcile(calls)
    assert events == [
        (
            EventKind.TOOL_RESULT,
            {"id": "exec-1", "is_error": True, "content": "network blocked", "blob": None},
        )
    ]
    # the next turn's call without a started item becomes a whole call
    again = state.reconcile(calls)
    assert [k for k, _ in again] == [EventKind.TOOL_CALL, EventKind.TOOL_RESULT]
    assert again[0][1]["source_type"] == "rollout" and again[1][1]["id"] == "call_1"


def test_reconcile_of_the_strict_run() -> None:
    stream, rollout = blocked()
    state = TurnEvents()
    for line in stream:
        state.feed(line["msg"]["method"], line["msg"]["params"])
    events = state.reconcile(rollout_calls(rollout, state.turn_id or ""))
    calls = [d for k, d in events if k is EventKind.TOOL_CALL]
    results = [d for k, d in events if k is EventKind.TOOL_RESULT]
    assert len(calls) == len(results) > 10
    assert all(c["source_type"] == "rollout" for c in calls)
    assert {c["id"] for c in calls} == {r["id"] for r in results}
