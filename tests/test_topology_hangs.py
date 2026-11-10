"""Hang cases of `review` and `exploration` (W4-03 §3.5, A8), by the rules of docs/09.

Every case runs a real RunManager with FakeBackend agents that behave like real models do when
they go wrong, under a short `wall_time`. The yardstick (docs/09 principle 1): no state ends
only by `wall_time`. Each guard has a counter-check (`naive`) that switches it off and shows
the hang it prevents:

- W1 a judge never gives a verdict, W2 the author never revises, W4 an explorer stays silent,
  W5 a ranker never rates: asked again `ASK_AGAIN` times, then the topology goes on with a
  note (naive: asked without end).
- W3 a discussion without contributions ends after its rounds (naive: no round limit).
- W6 a fresh judge after `close` waits in the closed channel for messages: L7 ends its turn,
  the topology asks once more (naive: no L7 limit).

W4-03.1 H2: hostile versions and reviews do not hold the event loop (naive: the diff in the
loop without limits, the old verdict pattern); a stop during a long diff ends the run at once.
"""

import asyncio
import re
import time
from pathlib import Path
from typing import Any

import pytest
from run_kit import agent, channel, eventually, log_entries, room, say
from test_review_exploration import E1, J1, J2, R, a_review, author, explorer, judge, ranker
from test_topologies import Recorder, checkpoints, setup, stage, staged, submit

from arr.models import RunStatus
from arr.room import waiting
from arr.runs import topologies
from arr.runs.topologies import review

WALL = "6s"
ENDLESS = 10**6


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    from run_kit import make_workfolder

    return make_workfolder(tmp_path)


async def hang_case(
    workfolder: Path,
    kind: str,
    scripts: dict[str, Any],
    *,
    params: dict[str, Any],
    groupchats: list[dict[str, Any]] | None = None,
    recorder: Recorder | None = None,
) -> tuple[Any, dict[str, Any]]:
    manager = setup(
        workfolder,
        kind,
        scripts,
        params=params,
        recorder=recorder,
        agents=[agent(a) for a in scripts],
        groupchats=groupchats or [],
        limits={"wall_time": WALL, "turns_per_agent": ENDLESS},  # only the wall time is left
    )
    started = time.monotonic()
    status = await asyncio.wait_for(manager.run(), 60)
    limits = [e["data"].get("limit") for e in log_entries(manager.run_dir, "limit")]
    return manager, {
        "status": status,
        "hang": "wall_time" in limits,
        "took_s": time.monotonic() - started,
    }


def judges_chat(**fields: Any) -> list[dict[str, Any]]:
    base = {"mode": "rounds", "anonymous": True, "open_from": "diskussion"}
    return [channel("judges", [J1, J2], **(base | fields))]


def guard(monkeypatch: pytest.MonkeyPatch, naive: bool) -> None:
    if naive:
        monkeypatch.setattr(topologies, "ASK_AGAIN", ENDLESS)


def check(found: dict[str, Any], naive: bool) -> None:
    if naive:
        assert found["hang"], found
    else:
        assert not found["hang"] and found["status"] is RunStatus.DONE, found


both = pytest.mark.parametrize("naive", [False, True], ids=["guarded", "naive"])


@both
async def test_w1_a_judge_never_gives_a_verdict(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    guard(monkeypatch, naive)
    recorder = Recorder()
    scripts = {
        "author": author("v1"),
        J1: judge([a_review(None, "Etwas fehlt")]),  # repeats: never a verdict
        J2: judge([a_review("accept")]),
    }
    scripts[J1]["by_stage"]["pruefung"]["repeat_last"] = True
    manager, found = await hang_case(
        workfolder,
        "review",
        scripts,
        params={"max_revisions": 0},
        groupchats=judges_chat(),
        recorder=recorder,
    )
    check(found, naive)
    if not naive:
        assert len(recorder.of(J1, "pruefung")) == 2, "asked once more"
        [end] = [c for c in checkpoints(manager, "pruefung") if c.get("phase") == "end"]
        assert end["missing"] == [J1] and end["reviews"][J1] is not None
        assert [c["verdict"] for c in checkpoints(manager, "urteil")] == ["accept"]


@both
async def test_w2_the_author_never_revises(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    guard(monkeypatch, naive)
    recorder = Recorder()
    scripts = {
        "author": {
            "turns": [],
            "by_stage": {
                "artefakt": stage([room("submit_result", kind="artifact", title="B", body="v1")]),
                "revision": stage([say("Ich überlege noch.")], repeat_last=True),
            },
        },
        J1: judge([a_review("major", "Lücke")]),
        J2: judge([a_review("major")]),
    }
    manager, found = await hang_case(
        workfolder,
        "review",
        scripts,
        params={"max_revisions": 2},
        groupchats=judges_chat(),
        recorder=recorder,
    )
    check(found, naive)
    if not naive:
        assert len(recorder.of("author", "revision")) == 2
        [end] = checkpoints(manager, "zusammenfassung")
        assert end["reason"] == "no_revision" and end["verdict"] == "major"
        [revision] = checkpoints(manager, "revision")
        assert revision["reason"] == "no_revision"


@both
async def test_w3_a_discussion_without_contributions(workfolder: Path, naive: bool) -> None:
    silent = [say("Ich lese nur mit.")]
    scripts = {
        "author": author("v1"),
        J1: judge([a_review("accept")], discussion=silent),
        J2: judge([a_review("minor")], discussion=silent),
    }
    manager, found = await hang_case(
        workfolder,
        "review",
        scripts,
        params={"max_revisions": 0, "discussion_rounds": ENDLESS if naive else 2},
        groupchats=judges_chat(mode="event", idle_after_s=0.2, quiet_after_s=300),
    )
    check(found, naive)
    if not naive:
        [end] = [e["data"] for e in log_entries(manager.run_dir, "discussion_end")]
        assert (end["ended_by"], end["rounds"]) == ("rounds", 2)
        assert [c["verdict"] for c in checkpoints(manager, "urteil")] == ["minor"]


@both
async def test_w4_an_explorer_stays_silent(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    guard(monkeypatch, naive)
    recorder = Recorder()
    scripts = {
        E1: staged(exploration=stage([say("Ich denke nach.")], repeat_last=True)),
        "explorer-2": explorer(),
        R: ranker(6),
    }
    manager, found = await hang_case(
        workfolder,
        "exploration",
        scripts,
        params={"ideas": ["A", "B"]},
        recorder=recorder,
    )
    check(found, naive)
    if not naive:
        assert len(recorder.of(E1, "exploration")) == 2, "one turn, asked once more"
        [end] = [c for c in checkpoints(manager, "exploration") if c.get("phase") == "end"]
        assert end["missing"] == ["I1"]
        [summary] = checkpoints(manager, "zusammenfassung")
        assert [(r["idea"], r["score"]) for r in summary["ranking"]] == [("I2", 6.0), ("I1", None)]


@both
async def test_w5_a_ranker_never_rates(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    guard(monkeypatch, naive)
    recorder = Recorder()
    scripts = {
        E1: explorer(),
        R: staged(bewertung=stage([say("Mache ich später.")], repeat_last=True)),
    }
    manager, found = await hang_case(
        workfolder,
        "exploration",
        scripts,
        params={"ideas": ["A", "B"]},
        recorder=recorder,
    )
    check(found, naive)
    if not naive:
        assert len(recorder.of(R, "bewertung")) == 2
        [end] = [c for c in checkpoints(manager, "bewertung") if c.get("phase") == "end"]
        assert end["ratings"] == 0 and len(end["missing"][R]) == 2
        [summary] = checkpoints(manager, "zusammenfassung")
        assert [r["score"] for r in summary["ranking"]] == [None, None]


@both
async def test_w6_a_fresh_judge_after_close_waits_in_the_closed_channel(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """Round 2, fresh session, #judges closed again: the judge waits for messages that cannot
    come. L7 ends the wait and the turn; the topology asks once more."""
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 1e9 if naive else 1.0)
    loop = [room("wait_for_messages", seconds=0.5) for _ in range(60)]  # 30 s of waiting
    scripts = {
        "author": author("v1", "v2"),
        J1: judge([a_review("major", "Lücke")], loop, [a_review("accept")]),
        J2: judge([a_review("major")], [a_review("accept")]),
    }
    manager, found = await hang_case(
        workfolder,
        "review",
        scripts,
        params={"max_revisions": 1, "judge_memory": "fresh"},
        groupchats=judges_chat(),
    )
    check(found, naive)
    if not naive:
        resets = [e["data"]["agent"] for e in log_entries(manager.run_dir, "session_reset")]
        assert sorted(resets) == [J1, J2]
        ended = [e["data"] for e in log_entries(manager.run_dir, "end_turn")]
        assert [e["agent"] for e in ended] == [J1] and ended[0]["reason"] == "nobody"
        assert [c["verdict"] for c in checkpoints(manager, "urteil")] == ["major", "accept"]


# --------------------------------------------------------------------------------------------
# W4-03.1 H2: no hostile text holds the event loop
# --------------------------------------------------------------------------------------------


def hostile_version(size: int, step: int) -> str:
    """Lines from only 150 different values: the worst case of `difflib` (review W4-03)."""
    lines, total, i = [], 0, 0
    while total < size:
        line = f"{(i * step + 3) % 150}\n"
        lines.append(line)
        total += len(line)
        i += 1
    return "".join(lines)


async def longest_stall(task: "asyncio.Task[Any]") -> float:
    """The longest time the event loop did not come back to this coroutine while `task` ran."""
    worst, last = 0.0, time.monotonic()
    while not task.done():
        await asyncio.sleep(0.02)
        now = time.monotonic()
        worst, last = max(worst, now - last), now
    return worst


def run_in_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counter-check of H2: the diff runs in the event loop again, without size limits."""

    async def inline(before: str, after: str, names: Any, **_: Any) -> str:
        return review.unified_diff(before, after, *names)

    monkeypatch.setattr(review, "MAX_DIFF_LINES", ENDLESS)
    monkeypatch.setattr(review, "MAX_DIFF_BYTES", ENDLESS)
    monkeypatch.setattr(review, "diff_elsewhere", inline)


def revision_with(workfolder: Path, v1: str, v2: str) -> Any:
    scripts = {
        "author": author(v1, v2),
        J1: judge([a_review("major", "x")], [a_review("accept")]),
        J2: judge([a_review("major")], [a_review("accept")]),
    }
    return setup(
        workfolder,
        "review",
        scripts,
        params={"max_revisions": 1, "discussion_rounds": 0},
        agents=[agent(a) for a in scripts],
        groupchats=judges_chat(),
    )


@both
async def test_h2_hostile_versions_do_not_hold_the_event_loop(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """Two 20 KB versions of 150 repeated lines: the diff alone takes about a second (measured
    here). Guarded, arr writes a note instead and the loop never stops for long; naive (the
    diff in the loop, no limits), the loop stands still that long."""
    v1, v2 = hostile_version(20 * 1024, 1), hostile_version(20 * 1024, 7)
    started = time.monotonic()
    review.unified_diff(v1, v2, "a", "b")
    cost = time.monotonic() - started
    if naive:
        run_in_the_loop(monkeypatch)
    manager = revision_with(workfolder, v1, v2)
    task = asyncio.create_task(manager.run())
    stall = await longest_stall(task)
    assert await task is RunStatus.DONE
    diff = (manager.run_dir / "output/artifacts/v2/diff.patch").read_text()
    if naive:
        assert stall > cost / 2, (stall, cost)
    else:
        assert stall < cost / 3, (stall, cost)
        assert diff.startswith("Kein Diff von v1 zu v2: Eine Fassung ist größer als 2000 Zeilen")


@both
async def test_h2_a_stop_during_a_long_diff_ends_the_run_in_time(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """Limits off, so the diff of hostile versions runs long: guarded in a thread, a stop ends
    the run at once; naive (in the loop) the stop waits for the diff."""
    v1, v2 = hostile_version(20 * 1024, 1), hostile_version(20 * 1024, 7)
    started = time.monotonic()
    review.unified_diff(v1, v2, "a", "b")
    cost = time.monotonic() - started
    monkeypatch.setattr(review, "MAX_DIFF_LINES", ENDLESS)
    monkeypatch.setattr(review, "MAX_DIFF_BYTES", ENDLESS)
    if naive:
        run_in_the_loop(monkeypatch)
    begun: list[float] = []
    original = review.diff_elsewhere

    async def diff(*args: Any, **kwargs: Any) -> Any:
        begun.append(time.monotonic())
        return await original(*args, **kwargs)

    monkeypatch.setattr(review, "diff_elsewhere", diff)
    manager = revision_with(workfolder, v1, v2)
    task = asyncio.create_task(manager.run())
    await eventually(lambda: bool(begun) or task.done(), within=30)
    manager.stop()
    status = await asyncio.wait_for(task, 60)
    latency = time.monotonic() - begun[0]
    if naive:
        assert latency > cost / 2, (latency, cost)
    else:
        assert status is RunStatus.STOPPED and latency < cost / 2, (status, latency, cost)


_OLD_VERDICT_LINE = re.compile(
    r"^[\s*_>#-]*(?:urteil|verdict)[\s*_]*[:=][\s*_]*(accept|minor|major|reject)\b",
    re.IGNORECASE | re.MULTILINE,
)
"""The pattern of W4-03 before the review: quadratic on a text without a verdict (H2b)."""


def old_parse_verdict(title: str, text: str) -> str | None:
    found = _OLD_VERDICT_LINE.search(title) or _OLD_VERDICT_LINE.search(text)
    return found.group(1).lower() if found else None


@both
async def test_h2_a_hostile_review_text_does_not_hold_the_event_loop(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """A review of 12 000 line breaks and a heading full of blanks, without a verdict: arr reads
    it several times (check, asking again). Guarded each read is linear; naive (the old
    pattern) the loop stands still for every read."""
    hostile = "\n" * 12_000 + "kein Urteil\n# a" + " " * 12_000 + "b\n"
    started = time.monotonic()
    old_parse_verdict("Gutachten", hostile)
    cost = time.monotonic() - started
    if naive:
        monkeypatch.setattr(review, "parse_verdict", old_parse_verdict)
    scripts = {
        "author": author("v1"),
        J1: judge([submit("Gutachten", "review", hostile)]),
        J2: judge([a_review("accept")]),
    }
    scripts[J1]["by_stage"]["pruefung"]["repeat_last"] = True
    manager = setup(
        workfolder,
        "review",
        scripts,
        params={"max_revisions": 0},
        agents=[agent(a) for a in scripts],
        groupchats=judges_chat(),
    )
    task = asyncio.create_task(manager.run())
    stall = await longest_stall(task)
    assert await task is RunStatus.DONE
    if naive:
        assert stall > cost / 2, (stall, cost)
    else:
        assert stall < max(cost / 5, 0.15), (stall, cost)
