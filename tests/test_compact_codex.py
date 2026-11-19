"""W4b-05b with the Codex engine: fill level, window and its own compaction (A1, A2, A9).

The fill level is `last.inputTokens` of the latest `thread/tokenUsage/updated` (in arr `input +
cache_read`), the window `tokenUsage.modelContextWindow`, reported with every request. The
engine's compaction comes as the item `contextCompaction` or the notification
`thread/compacted`; both make one event `compaction`.

Whether `thread/compacted` reaches the turn's stream (W4b-00 §9 left it open) is shown here in
the SDK itself: its router hands a notification with `turnId` to that turn's subscription,
which is what `AsyncTurnHandle.stream()` reads.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from openai_codex._message_router import MessageRouter
from openai_codex.generated.notification_registry import NOTIFICATION_MODELS
from openai_codex.models import Notification
from test_codex_backend import (  # noqa: F401 - `setup` is a fixture
    THREAD,
    TURN,
    note,
    play,
    setup,
    simple_turn,
)

from arr.backends.base import EventKind
from arr.backends.codex import _payload
from arr.backends.codex_events import TurnEvents

FIXTURES = Path(__file__).parent / "fixtures" / "codex"


def notifications(name: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        msg = json.loads(line)["msg"]
        if json.loads(line)["dir"] == "in" and "method" in msg and "id" not in msg:
            out.append((msg["method"], msg.get("params") or {}))
    return out


def test_a1_codex_fill_and_window_from_the_fixture() -> None:
    name = "turn_developer_vs_base_instructions.jsonl"
    state = TurnEvents(model="gpt-6-luna")
    updates = []
    for method, params in notifications(name):
        events = state.feed(method, params)
        if method == "thread/tokenUsage/updated":
            usage = params["tokenUsage"]
            updates.append(usage)
            (event,) = [data for kind, data in events if kind is EventKind.USAGE]
            assert event["context_window"] == usage["modelContextWindow"]
            assert state.context_tokens == usage["last"]["inputTokens"]
            # in arr's terms: input + cache_read of the request
            assert state.context_tokens == event["input"] + event["cache_read"]
    assert updates and state.measured
    assert state.context_window == updates[-1]["modelContextWindow"]
    assert state.context_window in (258_400, 475_000)


async def test_a1_codex_backend_fill_over_turns(setup) -> None:  # noqa: F811
    def usage(last_in: int, window: int | None) -> dict[str, Any]:
        counts = {"inputTokens": last_in, "cachedInputTokens": last_in // 2, "outputTokens": 5}
        token_usage: dict[str, Any] = {"total": counts, "last": counts}
        if window is not None:
            token_usage["modelContextWindow"] = window
        return note("thread/tokenUsage/updated", tokenUsage=token_usage)

    def turn(*middle: dict[str, Any]) -> list[dict[str, Any]]:
        first, *_, last = simple_turn()
        return [first, *middle, last]

    compaction = note("item/completed", item={"type": "contextCompaction", "id": "c1"})
    backend, _, _ = setup(
        [
            turn(usage(1_000, 258_400), usage(4_000, 258_400)),
            turn(),  # no request: the fill level stays
            turn(compaction),
            turn(usage(3_000, None)),  # a new thread after reset
            turn(usage(5_000, None)),
            turn(),  # after reset, no request yet (M40)
        ]
    )
    await backend.start()
    one, two, three = [await play(backend) for _ in range(3)]
    await backend.reset()
    four = await play(backend)
    five = await play(backend)
    await backend.reset()
    six = await play(backend)
    ends = [events[-1].data for events in (one, two, three, four, five, six)]
    assert [(e["context_tokens"], e["context_window"]) for e in ends] == [
        (4_000, 258_400),
        (4_000, 258_400),
        (None, 258_400),  # compacted, no request since
        (3_000, 258_400),  # the window stays known
        (5_000, 258_400),
        (None, 258_400),  # reset forgets the fill level of the old thread
    ]
    assert [e.kind for e in three].count(EventKind.COMPACTION) == 1


def test_a9_codex_compaction_once_per_compaction() -> None:
    state = TurnEvents(model="gpt-6-luna")
    item = {"type": "contextCompaction", "id": "c1"}
    params = {"threadId": THREAD, "turnId": TURN}
    seen = [
        *state.feed("item/started", {**params, "item": item}),
        *state.feed("item/completed", {**params, "item": item}),
        *state.feed("thread/compacted", params),
    ]
    assert seen == [(EventKind.COMPACTION, {"trigger": None, "tokens_before": None})]
    assert state.context_tokens is None and state.measured
    usage = {"total": {"inputTokens": 7}, "last": {"inputTokens": 7}}
    state.feed("thread/tokenUsage/updated", {**params, "tokenUsage": usage})
    assert state.context_tokens == 7
    # a second compaction after a request is a new event; `thread/compacted` alone suffices
    assert state.feed("thread/compacted", params) == seen
    assert state.context_tokens is None


def test_a9_thread_compacted_reaches_the_turn_stream() -> None:
    """The SDK routes `thread/compacted` by its `turnId` into the turn's subscription (the
    queue `AsyncTurnHandle.stream()` reads), not to the global notifications."""
    router = MessageRouter()
    subscription = router.subscribe_turn(TURN)
    raw = {"threadId": THREAD, "turnId": TURN}
    payload = NOTIFICATION_MODELS["thread/compacted"].model_validate(raw)
    router.route_notification(Notification(method="thread/compacted", payload=payload))
    other = NOTIFICATION_MODELS["thread/compacted"].model_validate({**raw, "turnId": "other"})
    router.route_notification(Notification(method="thread/compacted", payload=other))
    got = subscription.next()
    assert got.method == "thread/compacted"
    params = _payload(got)  # as the backend's pump passes it on
    assert params == raw
    state = TurnEvents(model="gpt-6-luna")
    assert [kind for kind, _ in state.feed(got.method, params)] == [EventKind.COMPACTION]
    subscription.close()


# --------------------------------------------------------------------------------------------
# W4b-05b.1 point 3 (M3, G10): one compaction is one event
# --------------------------------------------------------------------------------------------

P = {"threadId": THREAD, "turnId": TURN}


def usage_note(last: int, total: int) -> tuple[str, dict[str, Any]]:
    counts = {"inputTokens": last, "cachedInputTokens": 0, "outputTokens": 1}
    totals = {"inputTokens": total, "cachedInputTokens": 0, "outputTokens": 1}
    token_usage = {"last": counts, "total": totals, "modelContextWindow": 258_400}
    return "thread/tokenUsage/updated", {**P, "tokenUsage": token_usage}


def lab_turn(item_id: str, old: int, new: int, total: int) -> list[tuple[str, dict[str, Any]]]:
    """The order the real app-server sent in the review lab (`probe_codex_lab.txt`): the item
    starts, three usage updates (the old `last`, the summary request, `last` = 0), the item
    completes, then the next request."""
    item = {"type": "contextCompaction", "id": item_id}
    return [
        ("item/started", {**P, "item": item}),
        usage_note(old, total),
        usage_note(old + 1, total + old + 1),
        usage_note(0, total + old + 1),
        ("item/completed", {**P, "item": item}),
        usage_note(new, total + old + 1 + new),
    ]


def compactions(state: TurnEvents, notes: list[tuple[str, dict[str, Any]]]) -> int:
    return sum(
        kind is EventKind.COMPACTION
        for method, params in notes
        for kind, _ in state.feed(method, params)
    )


def test_m3_the_recorded_order_gives_one_event_per_compaction() -> None:
    turn2, turn3 = TurnEvents(model="gpt-6-luna"), TurnEvents(model="gpt-6-luna")
    assert compactions(turn2, lab_turn("c1", 200_001, 200_003, 200_001)) == 1
    assert compactions(turn3, lab_turn("c2", 200_003, 200_005, 600_006)) == 1
    assert (turn2.context_tokens, turn3.context_tokens) == (200_003, 200_005)


def test_g10_zero_input_right_after_a_compaction_is_unknown() -> None:
    state = TurnEvents(model="gpt-6-luna")
    notes = lab_turn("c1", 200_001, 200_003, 200_001)[:4]  # ends at `last` = 0
    assert compactions(state, notes) == 1
    assert state.context_tokens is None and state.measured


@pytest.mark.parametrize(
    "order",
    [
        ["started", "completed"],  # only the item
        ["notification"],  # only the notification
        ["started", "completed", "notification"],
        ["notification", "started", "completed"],
        ["started", "notification", "completed"],
    ],
)
def test_m3_item_and_notification_in_any_order(order: list[str]) -> None:
    item = {"type": "contextCompaction", "id": "c1"}
    feeds = {
        "started": ("item/started", {**P, "item": item}),
        "completed": ("item/completed", {**P, "item": item}),
        "notification": ("thread/compacted", P),
    }
    state = TurnEvents(model="gpt-6-luna")
    assert compactions(state, [feeds[step] for step in order]) == 1
    # two compactions in one turn are two events, also with their notifications
    second = {"type": "contextCompaction", "id": "c2"}
    more = [("item/completed", {**P, "item": second}), ("thread/compacted", P)]
    assert compactions(state, more) == 1
