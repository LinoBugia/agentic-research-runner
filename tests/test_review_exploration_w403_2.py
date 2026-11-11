"""W4-03.2: the small findings of the recheck (G-N1, G-N2, G-N4, G-N5) as fixed tests.

Several come from the reviewer's `test_w4031_nachpruefung.py`. FakeBackend agents, the real
room and RunManager; no model.
"""

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from run_kit import agent, channel, eventually, log_entries, room
from test_review_exploration import (
    E1,
    E2,
    J1,
    J2,
    R,
    a_review,
    author,
    judge,
    ranker,
    resolved_run,
    room_calls,
    run_exploration,
    run_review,
    summary_of,
)
from test_topologies import checkpoints, run, stage, staged, submit

from arr.errors import ConfigError
from arr.models import RunStatus
from arr.runs.topologies import check_topology, review


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    from run_kit import make_workfolder

    return make_workfolder(tmp_path)


def children() -> set[int]:
    """PIDs of the descendants of this process (from `ps`: PID and parent only)."""
    ps = subprocess.Popen(["/bin/ps", "-A", "-o", "pid=,ppid="], stdout=subprocess.PIPE, text=True)
    out, _ = ps.communicate()
    parent = {}
    for row in out.splitlines():
        pid, ppid = (int(x) for x in row.split())
        parent[pid] = ppid
    me, found = os.getpid(), set()
    for pid in parent:
        p = pid
        while p in parent and p not in (0, 1):
            p = parent[p]
            if p == me and pid != ps.pid:
                found.add(pid)
                break
    return found


def ff_lines(size: int, step: int, sep: str = "\x0c") -> str:
    """Repeated short lines separated by a character `splitlines` splits on but which is not
    "\\n" (the review's way past the old line limit)."""
    out, total, i = [], 0, 0
    while total < size:
        line = f"{(i * step + 3) % 150}{sep}"
        out.append(line)
        total += len(line)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------------------------
# G-N1: isolated explorers share no working folder and read no other explorer's
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("exchange", ["none", "digest", "channel"])
def test_g_n1_isolated_explorers_share_no_folder(exchange: str) -> None:
    resolved = resolved_run(
        "exploration",
        [
            agent(E1, workspace={"mode": "shared", "name": "x"}),
            agent(E2, workspace={"mode": "shared", "name": "x"}),
            agent(
                "explorer-3", workspace={"mode": "shared", "name": "y"}, read_dirs=["run:shared/x"]
            ),
        ],
        params={"ideas": ["A", "B", "C"], "exchange": exchange},
        groupchats=[channel("lab", [E1, E2, "explorer-3"])],
    )
    if exchange == "channel":  # counter-check: exchanging is the point there
        check_topology(resolved)
        return
    with pytest.raises(ConfigError) as caught:
        check_topology(resolved)
    text = str(caught.value)
    assert "teilen den Arbeitsordner „x“" in text
    assert "„explorer-3“ liest den Arbeitsordner von explorer-1, explorer-2" in text


# --------------------------------------------------------------------------------------------
# G-N2: the line limit of the diff counts every line break; G-N5: the diff process ends
# --------------------------------------------------------------------------------------------


async def test_g_n2_the_line_limit_counts_every_line_break(workfolder: Path) -> None:
    """From the review: versions of 190 KB whose lines end with \\x0c. The limit counts them
    as lines now: a note at once, no diff process, the run ends quickly."""
    v1, v2 = ff_lines(190 * 1024, 1), ff_lines(190 * 1024, 7)
    scripts = {
        "author": author(v1, v2),
        J1: judge([a_review("major", "x")], [a_review("accept")]),
        J2: judge([a_review("major")], [a_review("accept")]),
    }
    before = children()
    manager = run_review(workfolder, scripts, params={"discussion_rounds": 0})
    started = time.monotonic()
    assert await asyncio.wait_for(manager.run(), 60) is RunStatus.DONE
    assert time.monotonic() - started < review.DIFF_TIMEOUT_S
    [revision] = checkpoints(manager, "revision")
    diff = (manager.run_dir / revision["diff"]).read_text()
    assert diff.startswith("Kein Diff von v1 zu v2: Eine Fassung ist größer als 2000 Zeilen")
    assert not children() - before


@pytest.mark.parametrize("ending", ["deadline", "stop"])
async def test_g_n5_the_diff_process_is_gone_after_its_deadline_and_a_stop(ending: str) -> None:
    """The diff process does not outlive `diff_elsewhere` (mutation N7 of the recheck: without
    the kill it ran on for 18 min)."""
    big = "".join(f"{(i * 7 + 3) % 150}\n" for i in range(40_000))
    other = "".join(f"{i % 150}\n" for i in range(40_000))
    before = children()
    seen: list[set[int]] = []

    def stopped() -> bool:
        seen.append(children() - before)
        return ending == "stop" and len(seen) > 3

    timeout = 0.6 if ending == "deadline" else 60
    result = await asyncio.wait_for(
        review.diff_elsewhere(big, other, ("a", "b"), timeout_s=timeout, stopped=stopped), 30
    )
    assert result is None
    assert any(seen), "the diff ran in a process of its own"
    assert not children() - before, "no diff process is left"


async def test_g_n5_a_stop_during_the_diff_ends_the_run_and_its_process(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From the review: limits off, deadline 60 s; a stop in the middle of the diff ends the run
    at once and leaves no process."""
    monkeypatch.setattr(review, "DIFF_TIMEOUT_S", 60.0)
    monkeypatch.setattr(review, "MAX_DIFF_LINES", 10**9)
    monkeypatch.setattr(review, "MAX_DIFF_BYTES", 10**9)
    v1, v2 = ff_lines(190 * 1024, 1, "\n"), ff_lines(190 * 1024, 7, "\n")
    scripts = {
        "author": author(v1, v2),
        J1: judge([a_review("major", "x")], [a_review("accept")]),
        J2: judge([a_review("major")], [a_review("accept")]),
    }
    before = children()
    manager = run_review(workfolder, scripts, params={"discussion_rounds": 0})
    task = asyncio.create_task(manager.run())
    await eventually(lambda: bool(children() - before) or task.done(), within=30)
    await eventually(
        lambda: (
            task.done()
            or any(
                e["data"].get("stage") == "revision" for e in log_entries(manager.run_dir, "turn")
            )
        ),
        within=30,
    )
    await asyncio.sleep(0.5)
    started = time.monotonic()
    manager.stop()
    status = await asyncio.wait_for(task, 30)
    assert status is RunStatus.STOPPED and time.monotonic() - started < 5
    await eventually(lambda: not children() - before, within=5)


# --------------------------------------------------------------------------------------------
# G-N4: objection texts in summary.md are masked like titles
# --------------------------------------------------------------------------------------------


async def test_g_n4_objection_texts_cannot_break_the_overview(workfolder: Path) -> None:
    hostile = "Lücke | in **Lemma** 2 <b>x</b> [link](http://x)"
    scripts = {
        "author": author("v1"),
        J1: judge([a_review("major", hostile)]),
        J2: judge([a_review("major")]),
    }
    manager = run_review(workfolder, scripts, params={"max_revisions": 0})
    assert await run(manager) is RunStatus.DONE
    summary = await summary_of(manager)
    assert (
        "- E1 (offen, judge-1 (Mitglied A)): Lücke \\| in \\*\\*Lemma\\*\\* 2 \\<b\\>x\\</b\\>"
        in summary
    )
    assert "**Lemma**" not in summary


# --------------------------------------------------------------------------------------------
# M1 from the review: a channel with open_from `bewertung` stays closed in the exploration
# --------------------------------------------------------------------------------------------


async def test_m1_a_channel_opening_at_bewertung_stays_closed_in_the_exploration(
    workfolder: Path,
) -> None:
    scripts = {
        E1: staged(
            exploration=stage(
                [room("read_messages", channel="lab"), submit("S1", "exploration", "a")],
                repeat_last=True,
            )
        ),
        E2: staged(exploration=stage([submit("S2", "exploration", "b")], repeat_last=True)),
        R: ranker(5, 6),
    }
    chats = [channel("lab", [E1, E2], mode="event", open_from="bewertung")]
    manager = run_exploration(
        workfolder, scripts, params={"ideas": ["A", "B"], "exchange": "none"}, chats=chats
    )
    assert await run(manager) is RunStatus.DONE
    calls = room_calls(manager.run_dir, E1, "read_messages")
    assert calls and all(err for _, err, _ in calls), calls


async def test_n2_the_run_manager_makes_a_run_tmp_for_every_agent(workfolder: Path) -> None:
    """Every agent has its `tmp/<id>/` from the start, also one whose backend makes none."""
    seen: dict[str, bool] = {}

    async def topology(ctx: Any) -> None:
        for agent_id in ctx.agents:
            seen[agent_id] = (ctx.run_folder / "tmp" / agent_id).is_dir()

    from run_kit import make_manager, say, turns

    manager = make_manager(workfolder, topology, {"a": turns([say("x")]), "b": turns([say("y")])})
    assert await run(manager) is RunStatus.DONE
    assert seen == {"a": True, "b": True}
