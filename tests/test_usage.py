"""Usage aggregation and `usage.json` (W1-04 A5): sums, `cost_kind`, spike fixture values."""

import json
from pathlib import Path
from typing import Any

import pytest

from arr.backends.base import CostKind, EventKind
from arr.backends.fake import FakeBackend
from arr.models import AgentConfig
from arr.runs.eventlog import EventLogger
from arr.usage import (
    UNKNOWN,
    RunUsage,
    UsageRecord,
    aggregate_usage,
    compute_run_usage,
    usage_records,
    write_usage_json,
)

FIXTURES = Path(__file__).parent / "fixtures"


def config(agent_id: str, provider: str = "claude", auth: str = "max-privat", model: str = "m-1"):
    return AgentConfig(id=agent_id, base_id=agent_id, provider=provider, model=model, auth=auth)


def turn_end(
    agent: str,
    turn: int,
    *,
    input: int | None = None,
    output: int | None = None,
    cache_read: int | None = None,
    cost: float | None = None,
    kind: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "input": input,
        "output": output,
        "cache_read": cache_read,
        "cache_write": None,
        "reasoning": None,
    }
    if model:
        usage["model"] = model
    data = {"status": "ok", "usage": usage, "cost_usd": cost, "session": None, "num_turns": 1}
    if kind is not None:
        data["cost_kind"] = kind
    return {"seq": turn, "ts": 1.0 + turn, "run": "r", "agent": agent, "turn": turn,
            "kind": "turn_end", "data": data}  # fmt: skip


def write_transcript(run_dir: Path, agent: str, records: list[dict[str, Any]]) -> None:
    folder = run_dir / "agents" / agent
    folder.mkdir(parents=True)
    (folder / "transcript.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------


def test_only_turn_end_events_count_and_usage_events_are_not_summed_twice() -> None:
    events = [
        {"kind": "turn_start", "data": {}},
        {"kind": "usage", "agent": "a", "turn": 1, "data": {"input": 999, "output": 999}},
        turn_end("a", 1, input=100, output=10),
    ]
    records = usage_records(events, config("a"))
    assert len(records) == 1 and records[0].usage.input == 100
    assert (records[0].profile, records[0].provider, records[0].model) == (
        "max-privat",
        "claude",
        "m-1",
    )


def test_model_comes_from_turn_end_else_from_config() -> None:
    records = usage_records(
        [turn_end("a", 1, input=1, model="opus"), turn_end("a", 2, input=1)], config("a")
    )
    assert [r.model for r in records] == ["opus", "m-1"]
    assert usage_records([turn_end("a", 1, input=1)])[0].model is None  # no config, no model


def test_damaged_turn_end_is_skipped_not_fatal(caplog: pytest.LogCaptureFixture) -> None:
    bad = turn_end("a", 1, input=-5)
    worse = turn_end("a", 2)
    worse["data"]["usage"] = "kaputt"
    records = usage_records([bad, worse, turn_end("a", 3, input=7)], config("a"))
    assert [r.turn for r in records] == [3]
    assert "ungültiger Usage" in caplog.text


def test_unknown_cost_kind_value_becomes_none() -> None:
    record = usage_records([turn_end("a", 1, cost=1.0, kind="geschenkt")])[0]
    assert record.usage.cost_usd == 1.0 and record.usage.cost_kind is None


# --------------------------------------------------------------------------------------------
# Sums
# --------------------------------------------------------------------------------------------


def rec(agent: str, turn: int, profile: str | None, model: str | None, **kw: Any) -> UsageRecord:
    (event,) = [turn_end(agent, turn, **kw)]
    (record,) = usage_records([event], config(agent, auth=profile or "x", model=model or "x"))
    return record.model_copy(update={"profile": profile, "model": model})


def test_sums_per_run_agent_model_and_profile() -> None:
    records = [
        rec("a-1", 1, "max", "sonnet", input=100, output=10, cost=0.5, kind="estimated"),
        rec("a-1", 2, "max", "sonnet", input=200, output=20, cost=0.25, kind="estimated"),
        rec("a-2", 1, "max", "opus", input=50, output=5, cost=1.0, kind="estimated"),
        rec("b-1", 1, "api", "sonnet", input=1, output=2, cost=4.0, kind="billed"),
    ]
    usage = aggregate_usage(records, run_id="run-1")
    assert usage.run == "run-1"
    assert (usage.total.usage.input, usage.total.usage.output, usage.total.turns) == (351, 37, 4)
    assert usage.total.usage.cost_usd == pytest.approx(5.75)
    assert set(usage.by_agent) == {"a-1", "a-2", "b-1"}
    assert usage.by_agent["a-1"].usage.input == 300 and usage.by_agent["a-1"].turns == 2
    assert usage.by_agent["a-1"].profile == "max"
    assert set(usage.by_agent["a-1"].models) == {"sonnet"}
    assert usage.by_model["sonnet"].usage.input == 301 and usage.by_model["sonnet"].turns == 3
    assert usage.by_model["opus"].usage.cost_usd == 1.0
    assert usage.by_profile["max"].usage.cost_usd == pytest.approx(1.75)
    assert usage.by_profile["api"].usage.cost_usd == 4.0


def test_cost_kind_billed_estimated_and_mixed() -> None:
    records = [
        rec("sub", 1, "max", "m", cost=1.0, kind="estimated"),
        rec("sub", 2, "max", "m", cost=2.0, kind="estimated"),
        rec("key", 1, "api", "m", cost=3.0, kind="billed"),
        rec("key", 2, "api", "m", cost=4.0, kind="billed"),
    ]
    usage = aggregate_usage(records)
    assert usage.by_agent["sub"].usage.cost_kind is CostKind.ESTIMATED
    assert usage.by_agent["key"].usage.cost_kind is CostKind.BILLED
    assert usage.by_profile["max"].usage.cost_kind is CostKind.ESTIMATED
    assert usage.by_profile["api"].usage.cost_kind is CostKind.BILLED
    assert usage.total.usage.cost_usd == 10.0
    assert usage.total.usage.cost_kind is CostKind.ESTIMATED  # billed + estimated


def test_unknown_cost_does_not_pollute_the_kind_but_unknown_kind_does() -> None:
    codex = aggregate_usage([rec("cx", 1, "gpt", "g", input=10), rec("cx", 2, "gpt", "g", input=5)])
    assert codex.total.usage.input == 15
    assert codex.total.usage.cost_usd is None and codex.total.usage.cost_kind is None

    mixed = aggregate_usage(
        [
            rec("cl", 1, "max", "m", cost=1.0, kind="estimated"),
            rec("cx", 1, "gpt", "g", input=10),  # no cost: ignored for the kind
        ]
    )
    assert mixed.total.usage.cost_usd == 1.0
    assert mixed.total.usage.cost_kind is CostKind.ESTIMATED

    unsure = aggregate_usage(
        [rec("a", 1, "x", "m", cost=1.0, kind="estimated"), rec("a", 2, "x", "m", cost=2.0)]
    )
    assert unsure.total.usage.cost_usd == 3.0 and unsure.total.usage.cost_kind is None


def test_missing_profile_or_model_is_grouped_under_unknown() -> None:
    usage = aggregate_usage([rec("a", 1, None, None, input=1)])
    assert UNKNOWN in usage.by_model and UNKNOWN in usage.by_profile


def test_agents_without_turns_appear_with_zero_rows() -> None:
    usage = aggregate_usage([], agents=["a", "b"])
    assert set(usage.by_agent) == {"a", "b"} and usage.total.turns == 0
    assert usage.total.usage.input is None


# --------------------------------------------------------------------------------------------
# Spike fixtures
# --------------------------------------------------------------------------------------------


def claude_turn_ends() -> list[dict[str, Any]]:
    """`turn_end` events from the three `result` messages of the Claude spike fixture.

    Mapping (illustrative, W2-01 owns the real one): per-query `usage` for the tokens, cost
    as the difference of the session-cumulative `total_cost_usd` (docs/01 §4.1).
    """
    path = FIXTURES / "claude" / "solo_three_turns_thinking_tools.jsonl"
    results = [
        row["msg"]
        for row in map(json.loads, path.read_text(encoding="utf-8").splitlines())
        if row["dir"] == "in" and row["msg"].get("type") == "result"
    ]
    events, previous = [], 0.0
    for number, msg in enumerate(results, start=1):
        usage = msg["usage"]
        events.append(
            turn_end(
                "claude-1",
                number,
                input=usage["input_tokens"],
                output=usage["output_tokens"],
                cache_read=usage["cache_read_input_tokens"],
                cost=msg["total_cost_usd"] - previous,
                kind="estimated",
                model="claude-haiku-5-5",
            )
        )
        events[-1]["data"]["usage"]["cache_write"] = usage["cache_creation_input_tokens"]
        events[-1]["data"]["usage"]["reasoning"] = usage["output_tokens_details"]["thinking_tokens"]
        previous = msg["total_cost_usd"]
    return events


def codex_turn_end() -> dict[str, Any]:
    """`turn_end` from the last `thread/tokenUsage/updated` of a Codex spike turn (`total`).

    The thread starts empty, so the turn usage is `total` itself (docs/01 §4.2); Codex reports
    no cost.
    """
    path = FIXTURES / "codex" / "turn_write_and_run.jsonl"
    updates = [
        row["msg"]["params"]["tokenUsage"]["total"]
        for row in map(json.loads, path.read_text(encoding="utf-8").splitlines())
        if row["msg"].get("method") == "thread/tokenUsage/updated"
    ]
    total = updates[-1]
    event = turn_end(
        "codex-1",
        1,
        input=total["inputTokens"],
        output=total["outputTokens"],
        cache_read=total["cachedInputTokens"],
        model="gpt-fixture",
    )
    event["data"]["usage"]["reasoning"] = total["reasoningOutputTokens"]
    return event


def test_claude_spike_fixture_values() -> None:
    events = claude_turn_ends()
    usage = aggregate_usage(usage_records(events, config("claude-1", model="claude-haiku-5-5")))
    assert usage.total.turns == 3
    assert usage.total.usage.output == 641 + 418 + 166
    assert usage.total.usage.input == 2 + 4 + 2
    assert usage.total.usage.cache_read == 0 + 12487 + 7265
    assert usage.total.usage.cache_write == 5863 + 1402 + 98
    assert usage.total.usage.reasoning == 496 + 87 + 107
    assert usage.total.usage.cost_usd == pytest.approx(0.00241492)  # = last cumulative cost
    assert usage.total.usage.cost_kind is CostKind.ESTIMATED


def test_codex_spike_fixture_values_have_no_cost() -> None:
    usage = aggregate_usage(
        usage_records([codex_turn_end()], config("codex-1", "codex", "chatgpt", "gpt-fixture"))
    )
    assert usage.total.turns == 1
    assert usage.total.usage.input and usage.total.usage.output
    assert usage.total.usage.cost_usd is None and usage.total.usage.cost_kind is None


def test_mixed_run_from_both_spikes_and_usage_json(tmp_path: Path) -> None:
    configs = [
        config("claude-1", model="claude-haiku-5-5"),
        config("codex-1", "codex", "chatgpt-privat", "gpt-fixture"),
        config("keyed-1", "claude", "api-key", "claude-sonnet-5-5"),
        config("silent-1"),
    ]
    run_dir = tmp_path / "run-1"
    write_transcript(run_dir, "claude-1", claude_turn_ends())
    write_transcript(run_dir, "codex-1", [codex_turn_end()])
    write_transcript(
        run_dir, "keyed-1", [turn_end("keyed-1", 1, input=10, output=2, cost=0.3, kind="billed")]
    )
    (run_dir / "agents" / "silent-1").mkdir()  # an agent that never ran

    path = write_usage_json(run_dir, configs)
    assert path == run_dir / "usage.json"
    usage = RunUsage.model_validate_json(path.read_text(encoding="utf-8"))
    data = json.loads(path.read_text(encoding="utf-8"))

    assert usage.run == "run-1" and data["version"] == 1
    assert set(usage.by_agent) == {"claude-1", "codex-1", "keyed-1", "silent-1"}
    assert usage.by_agent["claude-1"].usage.cost_kind is CostKind.ESTIMATED
    assert usage.by_agent["keyed-1"].usage.cost_kind is CostKind.BILLED
    assert usage.by_agent["codex-1"].usage.cost_kind is None
    assert usage.by_agent["silent-1"].turns == 0
    assert usage.total.turns == 5
    assert usage.total.usage.cost_usd == pytest.approx(0.00241492 + 0.3)
    assert usage.total.usage.cost_kind is CostKind.ESTIMATED  # estimated + billed
    assert set(usage.by_profile) == {"max-privat", "chatgpt-privat", "api-key"}
    assert set(usage.by_model) == {"claude-haiku-5-5", "gpt-fixture", "claude-sonnet-5-5"}
    assert data["by_profile"]["api-key"]["usage"]["cost_kind"] == "billed"
    assert data["by_agent"]["codex-1"]["usage"]["cost_usd"] is None
    assert not list(run_dir.glob("*.tmp"))


def test_agent_folders_without_config_are_still_counted(tmp_path: Path) -> None:
    write_transcript(tmp_path, "ghost-1", [turn_end("ghost-1", 1, input=4)])
    usage = compute_run_usage(tmp_path, [])
    assert usage.by_agent["ghost-1"].usage.input == 4 and usage.by_agent["ghost-1"].profile is None
    assert UNKNOWN in usage.by_profile


def test_a_crashed_transcript_still_yields_usage(tmp_path: Path) -> None:
    write_transcript(tmp_path, "a", [turn_end("a", 1, input=4, output=1)])
    with (tmp_path / "agents" / "a" / "transcript.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"seq":2,"kind":"turn_e')  # torn last line
    assert compute_run_usage(tmp_path, [config("a")]).total.usage.input == 4


# --------------------------------------------------------------------------------------------
# End to end with the FakeBackend and the EventLogger
# --------------------------------------------------------------------------------------------


async def test_fake_backend_through_logger_into_usage_json(tmp_path: Path) -> None:
    script = {
        "cost_kind": "estimated",
        "turns": [
            {"steps": [{"do": "usage", "input": 1000, "output": 100, "cost_usd": 0.5}]},
            {
                "steps": [
                    {"do": "usage", "input": 500, "output": 50, "cost_usd": 0.25},
                    {"do": "usage", "input": 20, "cost_usd": 0.01, "cost_kind": "billed"},
                ]
            },
        ],
    }
    cfg = config("explorer-1")
    run_dir = tmp_path / "run-x"
    backend = FakeBackend(cfg, script, run_id="run-x")
    await backend.start()
    with EventLogger(run_dir / "agents" / "explorer-1") as logger:
        for prompt in ("eins", "zwei"):
            async for event in backend.run_turn(prompt):
                logger.log(event)
    usage = RunUsage.model_validate_json(
        write_usage_json(run_dir, [cfg]).read_text(encoding="utf-8")
    )
    assert usage.total.turns == 2
    assert (usage.total.usage.input, usage.total.usage.output) == (1520, 150)
    assert usage.total.usage.cost_usd == pytest.approx(0.76)
    assert usage.total.usage.cost_kind is CostKind.ESTIMATED
    assert usage.by_model == {"m-1": usage.by_model["m-1"]} and usage.by_model["m-1"].turns == 2
    assert EventKind.TURN_END.value == "turn_end"
