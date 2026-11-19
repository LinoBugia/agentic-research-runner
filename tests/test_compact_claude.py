"""W4b-05b with the Claude engine: fill level, window and its own compaction (A1, A2, A9).

From the recorded S1 fixtures (`tests/fixtures/claude/`): the fill level is `input +
cache_read + cache_write` of the last request of the agent's own model, the window comes
from `ResultMessage.model_usage` at the end of a turn. A `compact_boundary` of the CLI becomes
the event `compaction`; the backend then sends `context.md` again (as before).
"""

from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from claude_agent_sdk._internal.message_parser import parse_message
from test_claude_backend import (  # noqa: F401 - `setup` is a fixture
    Setup,
    collect,
    fixture_messages,
    make_backend,
    setup,
)

from arr.backends.base import CostKind, EventKind
from arr.backends.claude_events import ClaudeEvents, context_window
from arr.runs.eventlog import render_transcript

HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"


def own_fill(messages: list[Any], model: str) -> int | None:
    """Expected fill level: input of the last request (message ID) of `model`."""
    fill = None
    seen: set[str] = set()
    for msg in messages:
        if isinstance(msg, AssistantMessage) and msg.usage and msg.message_id not in seen:
            seen.add(msg.message_id or "")
            if (msg.model or model) == model:
                u = msg.usage
                fill = sum(
                    u.get(k) or 0
                    for k in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
    return fill


def test_a1_claude_fill_and_window_from_the_fixture() -> None:
    queries = fixture_messages("solo_three_turns_thinking_tools.jsonl")
    mapper = ClaudeEvents("r1", "e1", HAIKU, CostKind.ESTIMATED)
    ends = []
    for number, messages in enumerate(queries, start=1):
        mapper.start_turn(number)
        for msg in messages:
            mapper.feed(msg)
            if number == 1:
                assert mapper.context_window is None  # unknown before the end of turn 1
        end = mapper.finish()[-1]
        assert end.kind is EventKind.TURN_END
        ends.append(end.data)
        expected = own_fill(messages, HAIKU)
        assert expected and end.data["context_tokens"] == expected
    result = queries[0][-1]
    assert isinstance(result, ResultMessage)
    window = result.model_usage[HAIKU]["contextWindow"]
    assert window == 1_000_000
    assert [e["context_window"] for e in ends] == [window] * 3
    # the fill level grows with the session
    assert ends[0]["context_tokens"] < ends[1]["context_tokens"] < ends[2]["context_tokens"]


def test_a1_claude_counts_only_the_agents_model() -> None:
    """A helper request of another model (haiku next to a sonnet agent) does not count."""
    (messages,) = fixture_messages("can_use_tool_deny_after_wait.jsonl")
    result = messages[-1]
    assert isinstance(result, ResultMessage) and HAIKU in result.model_usage  # helper requests
    helper = AssistantMessage(
        [TextBlock("Titel")],
        HAIKU,
        message_id="helper-1",
        usage={"input_tokens": 900_000, "cache_read_input_tokens": 5, "output_tokens": 3},
    )
    mapper = ClaudeEvents("r1", "e1", SONNET, CostKind.ESTIMATED)
    mapper.start_turn(1)
    for msg in [*messages[:-1], helper, result]:
        mapper.feed(msg)
    end = mapper.finish()[-1].data
    assert end["context_tokens"] == own_fill(messages, SONNET)
    assert end["context_tokens"] < 900_000
    assert end["context_window"] == result.model_usage[SONNET]["contextWindow"]


def test_a1_window_by_key_or_canonical_model() -> None:
    usage = {"x-haiku-alias": {"canonicalModel": HAIKU, "contextWindow": 200_000}}
    assert context_window(usage, HAIKU) == 200_000
    assert context_window({HAIKU: {"contextWindow": 5}}, HAIKU) == 5
    assert context_window({HAIKU: {"contextWindow": True}}, HAIKU) is None
    assert context_window({}, HAIKU) is None and context_window(None, HAIKU) is None


def boundary(pre_tokens: int = 150_000) -> Any:
    """`compact_boundary` as the CLI sends it, through the SDK's own parser."""
    return parse_message(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "session_id": "s1",
            "uuid": "u1",
            "compact_metadata": {"trigger": "auto", "pre_tokens": pre_tokens},
        }
    )


def test_a9_claude_compaction_is_an_event_and_the_fill_level_unknown() -> None:
    (messages,) = fixture_messages("solo_three_turns_thinking_tools.jsonl")[:1]
    mapper = ClaudeEvents("r1", "e1", HAIKU, CostKind.ESTIMATED)
    mapper.start_turn(1)
    for msg in messages[:-1]:
        mapper.feed(msg)
    assert mapper.context_tokens
    events = mapper.feed(boundary(123_456))
    assert [e.kind for e in events] == [EventKind.COMPACTION]
    assert events[0].data == {"trigger": "auto", "tokens_before": 123_456}
    assert events[0].raw["subtype"] == "compact_boundary"
    assert mapper.compacted and mapper.context_tokens is None
    end = (mapper.feed(messages[-1]), mapper.finish()[-1].data)[1]
    assert end["context_tokens"] is None  # no request after the compaction
    # a request after it makes the fill level known again
    mapper.start_turn(2)
    mapper.feed(
        AssistantMessage(
            [TextBlock("weiter")],
            HAIKU,
            message_id="after-1",
            usage={"input_tokens": 10, "cache_read_input_tokens": 2_000},
        )
    )
    assert mapper.context_tokens == 2_010
    mapper.new_session()  # reset
    assert mapper.context_tokens is None and mapper.context_window == 1_000_000


async def test_a9_claude_backend_sends_context_again_and_logs_the_compaction(
    setup: Setup,  # noqa: F811
) -> None:
    def result(session: str = "s1") -> ResultMessage:
        return ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=session,
            total_cost_usd=0.001,
            model_usage={HAIKU: {"inputTokens": 5, "contextWindow": 1_000_000}},
        )

    turn1 = [
        AssistantMessage([TextBlock("a")], HAIKU, message_id="m1", usage={"input_tokens": 100}),
        boundary(),
        result(),
    ]
    backend, clients = make_backend(setup, [turn1, [result()], [result()]])
    try:
        first = await collect(backend, "Auftrag 1")
        second = await collect(backend, "Auftrag 2")
        await backend.reset()
        third = await collect(backend, "Auftrag 3")
    finally:
        await backend.close()
    kinds = [e.kind for e in first]
    assert EventKind.COMPACTION in kinds and kinds[-1] is EventKind.TURN_END
    assert first[-1].data["context_tokens"] is None
    assert first[-1].data["context_window"] == 1_000_000
    queries = [q for c in clients for q in c.queries]
    assert "Kontext-Marker-77" in queries[1]  # resent after the compaction, as before
    assert second[-1].data["context_window"] == 1_000_000
    assert third[-1].data["context_tokens"] is None  # new session, no request yet
    md = render_transcript([e.to_record() for e in first])
    assert "Die Engine hat die Sitzung verdichtet" in md and "150 k" in md


# --------------------------------------------------------------------------------------------
# W4b-05b.1 point 2 (M2, G1): the main session, whatever the spelling of the model
# --------------------------------------------------------------------------------------------

USAGE_SONNET = {
    SONNET: {
        "inputTokens": 1,
        "outputTokens": 1,
        "contextWindow": 1_000_000,
        "canonicalModel": SONNET,
    }
}


def request(model: str, mid: str, tokens: int, *, parent: str | None = None) -> AssistantMessage:
    return AssistantMessage(
        [TextBlock("t")],
        model,
        parent_tool_use_id=parent,
        message_id=mid,
        usage={
            "input_tokens": tokens,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 700,
            "output_tokens": 3,
        },
    )


def finish_with(agent_model: str, messages: list[Any]) -> dict[str, Any]:
    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
        total_cost_usd=0.01,
        model_usage=USAGE_SONNET,
    )
    mapper = ClaudeEvents("r", "a", agent_model, CostKind.ESTIMATED)
    mapper.start_turn(1)
    for msg in [*messages, result]:
        mapper.feed(msg)
    data = mapper.finish()[-1].data
    return {k: data[k] for k in ("context_tokens", "context_window")}


def test_m2_short_names_suffix_and_case_give_the_same_fill_level() -> None:
    messages = [request(SONNET, "m1", 5_000)]
    full = finish_with(SONNET, messages)
    assert full == {"context_tokens": 5_800, "context_window": 1_000_000}  # positive control
    for configured in ("sonnet", "claude-sonnet-5-5[1m]", "Claude-Sonnet-5-5"):
        assert finish_with(configured, messages) == full, configured


def test_g1_subagent_and_helper_requests_do_not_count() -> None:
    main = request(SONNET, "m1", 90_000)
    for configured in (SONNET, "sonnet"):
        subagent = request(SONNET, "sub1", 2_000, parent="toolu_task_1")  # same model
        assert finish_with(configured, [main, subagent])["context_tokens"] == 90_800
        big = request(SONNET, "sub2", 600_000, parent="toolu_task_1")
        assert finish_with(configured, [main, big])["context_tokens"] == 90_800
        helper = request(HAIKU, "h1", 900_000)  # another model, main session
        assert finish_with(configured, [main, helper])["context_tokens"] == 90_800
    # counter-check: a later main request of the agent's model does count
    later = request(SONNET, "m2", 120_000)
    assert finish_with("sonnet", [main, later])["context_tokens"] == 120_800
