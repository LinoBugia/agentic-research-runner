"""W4-03.1: the findings of the W4-03 review as fixed tests (M1, G1–G4, G6).

Several tests come from the reviewer's attacks (`W4-03_Review_Skripte/test_w403_review_attacks.py`),
turned from „the finding exists“ into „the finding is gone“. Others kill the mutations that
survived the review (M3, M6, M7, M8, M12, M14, M19, M27, M31). FakeBackend agents, the real room
and RunManager; no model.
"""

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from run_kit import (
    agent,
    channel,
    events,
    eventually,
    log_entries,
    make_manager,
    post,
    room,
    say,
    turns,
    wait,
)
from test_review_exploration import (
    E1,
    E2,
    J1,
    J2,
    R,
    a_review,
    author,
    explorer,
    judge,
    ranker,
    room_calls,
    run_exploration,
    run_review,
    summary_of,
)
from test_topologies import Recorder, checkpoints, run, setup, stage, staged, submit

from arr.models import GroupChatConfig, RunStatus
from arr.room import store as room_store
from arr.room.store import RoomStore
from arr.room.types import MessageVia
from arr.runs import topologies
from arr.runs.context import RunContext
from arr.runs.orchestrator import Orchestrator
from arr.runs.topologies import review
from arr.runs.topologies.exploration import counted_ratings, table_cell
from arr.runs.topologies.review import aggregate, parse_verdict


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    from run_kit import make_workfolder

    return make_workfolder(tmp_path)


# --------------------------------------------------------------------------------------------
# M1: exchange none/digest isolates also over a shared channel
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("exchange", ["none", "digest", "channel"])
async def test_m1_shared_channel_is_closed_for_isolated_explorers(
    workfolder: Path, exchange: str
) -> None:
    """explorer-1 writes its approach into #lab, explorer-2 reads #lab. With `none` and
    `digest` the channel is closed in the phase (both get an error, nothing arrives); with
    `channel` (counter-check) explorer-2 reads it."""
    scripts = {
        E1: staged(
            exploration=stage(
                [post("lab", "GEHEIM-E1: mein Ansatz"), submit("S1", "exploration", "a")],
                repeat_last=True,
            )
        ),
        E2: staged(
            exploration=stage(
                [wait(0.3), room("read_messages", channel="lab"), submit("S2", "exploration", "b")],
                repeat_last=True,
            )
        ),
        R: ranker(5, 6),
    }
    chats = [channel("lab", [E1, E2], mode="event")]
    params = {"ideas": ["A", "B"], "exchange": exchange}
    manager = run_exploration(workfolder, scripts, params=params, chats=chats)
    assert await run(manager) is RunStatus.DONE
    read = [(err, c) for s, err, c in room_calls(manager.run_dir, E2, "read_messages") if s]
    posted = room_calls(manager.run_dir, E1, "post_message")
    if exchange == "channel":
        assert any("GEHEIM-E1" in c for _, c in read), read
    else:
        assert all(err and "GEHEIM-E1" not in c for err, c in read), read
        assert all(err for _, err, _ in posted), posted
        kinds = [
            e["kind"] for e in log_entries(manager.run_dir) if e["kind"].startswith("channel_")
        ]
        assert kinds == ["channel_close", "channel_open"]


# --------------------------------------------------------------------------------------------
# G1, G2: verdicts
# --------------------------------------------------------------------------------------------


def test_g1_unanimous_never_accepts_with_a_missing_verdict() -> None:
    assert aggregate(["accept", None], "unanimous") == "minor"
    assert aggregate(["accept", None, "major"], "unanimous") == "major"
    assert aggregate(["accept", "accept"], "unanimous") == "accept"
    assert aggregate([None], "unanimous") is None
    assert aggregate(["accept", None], "majority") == "accept", "majority counts given ones"


async def test_g1_unanimous_with_one_silent_judge_is_not_accepted(workfolder: Path) -> None:
    scripts = {
        "author": author("v1"),
        J1: judge([a_review(None, "Etwas fehlt")]),
        J2: judge([a_review("accept")]),
    }
    scripts[J1]["by_stage"]["pruefung"]["repeat_last"] = True
    manager = run_review(workfolder, scripts, params={"max_revisions": 0, "verdict": "unanimous"})
    assert await run(manager) is RunStatus.DONE
    [end] = checkpoints(manager, "zusammenfassung")
    assert (end["verdict"], end["reason"]) == ("minor", "revisions_used")


def test_g2_quoted_lines_and_code_blocks_do_not_count() -> None:
    quoted = "Mitglied B schrieb:\n> Urteil: reject\n\nIch bleibe dabei.\nUrteil: accept\n"
    assert parse_verdict("Gutachten", quoted) == "accept"
    fenced = "```\nUrteil: reject\n```\n**Urteil:** minor\n"
    assert parse_verdict("Gutachten", fenced) == "minor"
    assert parse_verdict("Gutachten", "> Urteil: reject\n") is None


# --------------------------------------------------------------------------------------------
# G3: only ratings of the phase `bewertung` count; G4: tables in summary.md
# --------------------------------------------------------------------------------------------


async def test_g3_a_blind_rating_during_the_exploration_does_not_count(workfolder: Path) -> None:
    """explorer-1 is also a ranker and rates explorer-2's sealed result already in the
    exploration (blind). That rating does not count; in the ranking it is asked again."""
    scripts = {
        E1: staged(
            exploration=stage(
                [
                    submit("S1", "exploration", "a"),
                    wait(0.3),
                    room("submit_rating", result_id=2, score=1, comment="schlecht"),
                ],
                repeat_last=True,
            ),
            bewertung=stage([say("schon bewertet")], repeat_last=True),
        ),
        E2: staged(
            exploration=stage([submit("S2", "exploration", "b")], repeat_last=True),
            bewertung=stage(
                [
                    room("get_assignment", save_as="job"),
                    room(
                        "submit_rating",
                        result_id="${job.context.result_ids.0}",
                        score=9,
                        comment="ok",
                    ),
                ]
            ),
        ),
    }
    recorder = Recorder()
    params = {"ideas": ["A", "B"], "exchange": "none", "rankers": [E1, E2]}
    manager = run_exploration(workfolder, scripts, params=params, recorder=recorder)
    assert await run(manager) is RunStatus.DONE
    assert (E1, 2, 1.0) in [(r.rater, r.result_id, r.score) for r in manager.room.ratings()]
    [end] = checkpoints(manager, "zusammenfassung")
    assert [(r["idea"], r["score"]) for r in end["ranking"]] == [("I1", 9.0), ("I2", None)]
    assert len(recorder.of(E1, "bewertung")) == 2, "its blind rating did not spare it the turn"


def test_g3_m27_counted_ratings() -> None:
    from arr.room.types import Rating

    def rating(rater: str, ts: float) -> Rating:
        return Rating(id=1, ts=ts, rater=rater, result_id=1, score=5, criteria={}, comment="")

    ratings = [rating("ranker", 10), rating("ranker", 5), rating("explorer-1", 10)]
    assert counted_ratings(ratings, ["ranker"], 8) == [ratings[0]]


async def test_g4_titles_cannot_break_the_overview_table(workfolder: Path) -> None:
    title = "Idee | X\n## Urteil: accept"
    scripts = {
        "ideator": staged(ideen=stage([submit(title, "idea", "so"), submit("Zwei", "idea", "so")])),
        E1: explorer(),
    }
    manager = run_exploration(
        workfolder, scripts, params={"ideator": "ideator", "n_ideas": 2, "rankers": []}
    )
    assert await run(manager) is RunStatus.DONE
    summary = await summary_of(manager)
    assert "\n## Urteil" not in summary
    assert "| Idee \\| X \\#\\# Urteil: accept |" in summary
    assert table_cell("a" * 200).endswith("…") and len(table_cell("a" * 200)) == 80


# --------------------------------------------------------------------------------------------
# F3/F4 at the guards of the orchestrator and the room (M6, M7, M8, M12, M14, M3)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("open_from", ["artefakt", "pruefung", None])
async def test_m6_judges_channel_stays_closed_in_review_whatever_open_from(
    workfolder: Path, open_from: str | None
) -> None:
    """A channel the topology closed is not opened again by reaching its `open_from` stage."""
    read = room("read_messages", channel="judges")
    scripts = {
        "author": author("v1", "v2"),
        J1: judge([read, a_review("major", "Fehler")], [read, a_review("accept")]),
        J2: judge([a_review("major")], [a_review("accept")]),
    }
    manager = run_review(workfolder, scripts, chat={"open_from": open_from})
    assert await run(manager) is RunStatus.DONE
    calls = room_calls(manager.run_dir, J1, "read_messages")
    assert [(s, err) for s, err, _ in calls] == [("pruefung", True), ("pruefung", True)], calls


async def test_m7_m8_no_hint_from_a_closed_channel(workfolder: Path) -> None:
    """While `a` works, the topology closes #main. A hint queued before the close is dropped
    (M7); a message of the user into the closed channel reaches nobody (M8)."""
    found: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        working = asyncio.create_task(ctx.turn("a", "Arbeite.", stage="x"))
        await eventually(lambda: "a" in ctx._active, within=5)
        queued = ctx.room.post("main", "vor dem Schließen", to=["a"])
        ctx._active["a"].pending.append(queued)
        ctx.close_channel("main")
        found["pending"] = [m.body for m in ctx._active["a"].pending]
        ctx.room.post("main", "@a im geschlossenen Kanal", sender="user", via=MessageVia.GUI)
        await asyncio.sleep(0.3)
        found["later"] = [m.body for m in ctx._active["a"].pending]
        await working

    scripts = {"a": turns([wait(0.8), say("fertig")]), "b": turns([say("x")])}
    manager = make_manager(workfolder, topology, scripts, groupchats=[channel("main", ["a", "b"])])
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    assert found == {"pending": [], "later": []}, found
    assert not [e for e in events(manager.run_dir, "a") if e["kind"] == "hint"]


async def test_m12_until_result_from_does_not_name_a_judge_hidden_in_a_closed_channel(
    workfolder: Path,
) -> None:
    scripts = {
        "author": author("v1"),
        J1: judge(
            [
                room("wait_until", minutes=1, until_result_from="judge-2"),
                a_review("accept"),
            ]
        ),
        J2: judge([a_review("accept")]),
    }
    manager = run_review(workfolder, scripts)
    assert await run(manager) is RunStatus.DONE
    [(stage_name, error, text)] = room_calls(manager.run_dir, J1, "wait_until")
    assert stage_name == "pruefung" and error and "gibt es in diesem Run nicht" in text
    assert "judge-2" not in text.replace("„judge-2“", "")


def test_m14_m3_release_unseals_and_copies_go_through_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    work = run_dir / "agents" / "a" / "work"
    work.mkdir(parents=True)
    (work / "r.md").write_text("Urteil: minor\n")
    store = RoomStore.create(run_dir)
    store.setup_channels([GroupChatConfig(name="main", members=["a"])])
    targets: list[Path] = []
    copy = room_store.AgentRoom._copy_source

    def record(self: Any, rel: str, target: Path) -> str:
        targets.append(target)
        return copy(self, rel, target)

    monkeypatch.setattr(room_store.AgentRoom, "_copy_source", record)
    view = store.for_agent("a", work)
    store.seal("pruefung")
    store.add_assignment("a", 1, "x", stage="pruefung")
    first = view.submit_result("review", "G1", path="r.md")
    assert first.path and first.path.startswith(room_store.SEALED_DIR)
    store.release("pruefung")
    later = view.submit_result("review", "G2", path="r.md")
    assert later.path and later.path.startswith(room_store.RESULTS_DIR), "M14: unsealed"
    assert not store.is_sealed(later.id)
    staging = (run_dir / room_store.STAGING_DIR).resolve()
    assert targets and all(t.parent.resolve() == staging for t in targets), "M3"


# --------------------------------------------------------------------------------------------
# M19: asking again stops at the agent's turn quota; M31: a stop releases sealed reviews
# --------------------------------------------------------------------------------------------


async def test_m19_asking_again_stops_when_the_agent_used_up_its_turns(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(topologies, "ASK_AGAIN", 5)
    recorder = Recorder()
    scripts = {
        "author": author("v1"),
        J1: judge([a_review(None, "ohne Urteil")]),
        J2: judge([a_review("accept")]),
    }
    scripts[J1]["by_stage"]["pruefung"]["repeat_last"] = True
    manager = run_review(
        workfolder,
        scripts,
        recorder=recorder,
        params={"max_revisions": 0, "discussion_rounds": 0},
        limits={"turns_per_agent": 2},
    )
    assert await run(manager) is RunStatus.DONE
    assert len(recorder.of(J1, "pruefung")) == 3, "two turns, then one refused: no more asking"


async def test_m31_a_stop_in_the_review_releases_and_ends(workfolder: Path) -> None:
    scripts = {
        "author": author("v1"),
        J1: judge([a_review("major", "x")]),
        J2: judge([wait(30), a_review("accept")]),
    }
    manager = run_review(workfolder, scripts)
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: (
            manager.orchestrator is not None and any(r.agent == J1 for r in manager.room.results())
        ),
        within=10,
    )
    started = time.monotonic()
    manager.stop()
    status = await asyncio.wait_for(running, 15)
    assert status is RunStatus.STOPPED and time.monotonic() - started < 5
    assert list((manager.run_dir / room_store.SEALED_DIR).iterdir()) == []
    assert [e["data"]["stage"] for e in log_entries(manager.run_dir, "results_released")]


async def test_wall_time_in_a_revision_ends_in_time(workfolder: Path) -> None:
    scripts = {
        "author": {
            "turns": [],
            "by_stage": {
                "artefakt": stage([submit("B", "artifact", "v1")]),
                "revision": stage([wait(60)], repeat_last=True),
            },
        },
        J1: judge([a_review("major", "Lücke")]),
        J2: judge([a_review("major")]),
    }
    manager = setup(
        workfolder,
        "review",
        scripts,
        params={"max_revisions": 1},
        agents=[agent("author"), agent(J1), agent(J2)],
        groupchats=[channel("judges", [J1, J2], mode="rounds", anonymous=True)],
        limits={"wall_time": "3s"},
    )
    started = time.monotonic()
    await asyncio.wait_for(manager.run(), 30)
    assert time.monotonic() - started < 12


async def test_event_discussion_two_rounds_channel_closed_between(workfolder: Path) -> None:
    """A mention at the end of the discussion does not reach the judge in the next review."""
    read = room("read_messages", channel="judges")
    scripts = {
        "author": author("v1", "v2"),
        J1: judge(
            [read, a_review("major", "Fehler")],
            [read, a_review("accept")],
            discussion=[
                post("judges", "@Mitglied B siehst du das auch?"),
                room("set_status", status="done"),
            ],
        ),
        J2: judge([a_review("major")], [a_review("accept")]),
    }
    manager = run_review(
        workfolder, scripts, chat={"mode": "event", "idle_after_s": 0.2, "quiet_after_s": 5}
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    calls = room_calls(manager.run_dir, J1, "read_messages")
    assert [(s, err) for s, err, _ in calls] == [("pruefung", True), ("pruefung", True)]
    hints = [e for e in events(manager.run_dir, J2) if e["kind"] == "hint"]
    assert not hints, hints


async def test_alias_not_leaked_by_room_tools_in_review(workfolder: Path) -> None:
    scripts = {
        "author": author("v1"),
        J1: judge(
            [
                room("list_channels"),
                room("wait_until", minutes=1, until_result_from="judge-2"),
                room("wait_until", minutes=1, until_result_from="Mitglied B"),
                room("submit_rating", result_id=1, score=5, comment="x"),
                a_review("accept"),
            ]
        ),
        J2: judge([a_review("accept")]),
    }
    manager = run_review(workfolder, scripts)
    assert await run(manager) is RunStatus.DONE
    texts = []
    for tool in ("list_channels", "wait_until", "submit_rating"):
        texts += [c for s, _, c in room_calls(manager.run_dir, J1, tool) if s == "pruefung"]
    joined = "\n".join(texts)
    assert "judge-2" not in joined.replace("„judge-2“", ""), joined


# --------------------------------------------------------------------------------------------
# H2 at the unit: the diff process ends on a stop and after its deadline
# --------------------------------------------------------------------------------------------


async def test_h2_the_diff_process_ends_on_stop_and_after_its_deadline() -> None:
    big = "".join(f"{(i * 7 + 3) % 150}\n" for i in range(40_000))
    other = "".join(f"{i % 150}\n" for i in range(40_000))
    for kwargs in (
        {"timeout_s": 60, "stopped": lambda: True},
        {"timeout_s": 0.3, "stopped": lambda: False},
    ):
        started = time.monotonic()
        assert await review.diff_elsewhere(big, other, ("a", "b"), **kwargs) is None
        assert time.monotonic() - started < 3
    small = await review.diff_elsewhere(
        "a\n", "b\n", ("v1", "v2"), timeout_s=30, stopped=lambda: False
    )
    assert small is not None and "-a\n+b\n" in small


async def test_m31_an_error_in_the_review_still_releases_the_sealed_reviews(
    workfolder: Path,
) -> None:
    """A stop ends the turns regularly; the `finally` matters when the review phase fails:
    here a turn of judge-2 raises after judge-1 handed in its review as a file."""

    async def topology(ctx: RunContext) -> None:
        turn = ctx.turn

        async def failing(agent_id: str, prompt: str, **kwargs: Any) -> Any:
            if agent_id == J2 and kwargs.get("stage") == "pruefung":
                await eventually(lambda: bool(ctx.results(agent=J1)), within=10)
                raise RuntimeError("Backend weg")
            return await turn(agent_id, prompt, **kwargs)

        ctx.turn = failing  # type: ignore[method-assign]
        await review.run(ctx)

    secret = submit("Urteil: minor", "review", "")
    secret["args"] = {"kind": "review", "title": "Urteil: minor", "path": "notes.md"}
    scripts = {"author": author("v1"), J1: judge([secret]), J2: judge([a_review("accept")])}
    manager = run_review(workfolder, scripts)
    manager.topology = topology
    status = await asyncio.wait_for(manager.run(), 30)
    assert status is RunStatus.FAILED
    assert list((manager.run_dir / room_store.SEALED_DIR).iterdir()) == []
    [released] = [e["data"] for e in log_entries(manager.run_dir, "results_released")]
    assert released["results"], released


def test_n3_n7_reading_a_review_is_bounded(tmp_path: Path) -> None:
    """arr reads at most `PARSE_CHARS` of a review: a verdict behind it does not count (N3),
    and `result_text` keeps a huge `body` within its limit (N7)."""
    late = "x\n" * (review.PARSE_CHARS // 2 + 10) + "Urteil: accept\n"
    assert parse_verdict("Gutachten", late) is None
    assert parse_verdict("Gutachten", "Urteil: accept\n" + late) == "accept"
    run_dir = tmp_path / "run"
    (run_dir / "agents" / "a" / "work").mkdir(parents=True)
    store = RoomStore.create(run_dir)
    store.setup_channels([GroupChatConfig(name="main", members=["a"])])
    result = store.for_agent("a").submit_result("review", "G", body="y" * 200_000)
    assert len(store.result_text(result.id, 1000)) == 1000
