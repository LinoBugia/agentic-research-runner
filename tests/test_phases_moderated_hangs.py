"""W4b-04 A17: nothing hangs in a phase `moderated` (the pattern of `test_phases_hangs.py`).

A stop in every state ends the runner within the manager's grace: the lead waits for returns,
an assignment turn runs, an assignment waits, a cancel is under way, B3 holds for an
assignment, a turn waits for a place of its profile or for the end of a pause, a note turn
runs before an assignment turn. A lead that never hands in, a recipient that never hands in, a
lead that gives `max_assignments` and waits, an assignment to an agent without turns left:
finitely many turns, never `wall_time`. Counter-check: without `end_turn` a cancel waits for a
turn that does not end, until `wall_time`."""

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
import run_kit
from phases_kit import (
    TurnView,
    answer,
    give,
    hand_in,
    manager,
    moderated_phase,
    of_kind,
    private,
    run,
    say,
    silent,
)
from run_kit import eventually, log_entries, make_workfolder

from arr.models import DEFAULT_TURNS, RunStatus
from arr.room.store import USER
from arr.room.types import DelegationStatus, MessageVia
from arr.runs import pacing
from arr.runs.compact import NOTE_HEADING
from arr.runs.orchestrator import Orchestrator
from arr.runs.phases import wake

WALL = "8s"
ENDLESS = 10**6
AGENTS = {"agents": [{"id": a} for a in ("a", "b", "c")]}
ASK = "@user Darf ich annehmen, dass n prim ist?"
BLOCK = {"do": "delay", "seconds": 120}
FILL = {"do": "usage", "input": 600, "output": 5}


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def first(view: TurnView) -> bool:
    return "# Auftrag – Phase" in view.prompt and view.action == "work"


def group(**extra: Any) -> dict[str, Any]:
    agents = extra.pop("agents", AGENTS["agents"])
    return {"agents": agents, "phases": [moderated_phase("a", ["b", "c"], **extra)]}


async def finite(workfolder: Path, data: dict[str, Any], policies: dict[str, Any], **kw: Any):
    limits = {"wall_time": WALL, "turns_per_agent": kw.pop("turns_per_agent", ENDLESS)}
    mgr, log = manager(workfolder, data, policies, limits=limits, **kw)
    status = await run(mgr, within=60)
    hang = any(e["data"].get("limit") == "wall_time" for e in log_entries(mgr.run_dir, "limit"))
    return mgr, log, status, hang


# --------------------------------------------------------------------------------------------
# Finite runs
# --------------------------------------------------------------------------------------------


async def test_a17_a_lead_that_never_hands_in(workfolder: Path) -> None:
    mgr, log, status, hang = await finite(workfolder, group(), {"a": silent})
    assert not hang and status is RunStatus.DONE
    assert len(log.of("a")) == DEFAULT_TURNS + 1


async def test_a17_a_recipient_that_never_hands_in(workfolder: Path) -> None:
    def a(view: TurnView) -> list[dict[str, Any]]:
        return [give("b", "1")] if first(view) else silent(view)

    mgr, log, status, hang = await finite(workfolder, group(), {"a": a, "b": silent})
    assert not hang and status is RunStatus.DONE
    assert len(log.of("b")) == DEFAULT_TURNS + 1
    assert [d.status for d in mgr.room.delegations()] == [DelegationStatus.MISSING]


async def test_a17_a_lead_that_gives_every_assignment_and_waits(workfolder: Path) -> None:
    def a(view: TurnView) -> list[dict[str, Any]]:
        if first(view):
            return [give("b" if i % 2 else "c", str(i)) for i in range(5)]  # 5 > max 3
        return silent(view)

    policies = {"a": a, "b": silent, "c": answer()}
    mgr, log, status, hang = await finite(workfolder, group(jobs=3), policies)
    assert not hang and status is RunStatus.DONE
    assert len(mgr.room.delegations()) == 3
    assert {d.status for d in mgr.room.delegations()} <= {
        DelegationStatus.MISSING,
        DelegationStatus.RETURNED,
    }


async def test_a17_an_assignment_to_an_agent_without_turns_left(workfolder: Path) -> None:
    """`turns_per_agent: 1`: b used its turn in a wake-up; its assignment gets no turn and
    comes back `missing`; the lead hands in after the return."""

    def a(view: TurnView) -> list[dict[str, Any]]:
        if first(view):
            return [say("k", "@b Da?", kind="question"), give("b", "1")]
        return hand_in(view)

    data = {
        **AGENTS,
        "channels": [{"name": "k", "members": ["a", "b"]}],
        "phases": [moderated_phase("a", ["b", "c"], channels=["k"])],
    }
    mgr, log, status, hang = await finite(
        workfolder, data, {"a": a, "b": silent}, turns_per_agent=2
    )
    assert not hang and status in (RunStatus.DONE, RunStatus.STOPPED)
    assert len(log.of("b")) <= 2
    assert all(not d.open for d in mgr.room.delegations())


# --------------------------------------------------------------------------------------------
# A stop in every state
# --------------------------------------------------------------------------------------------

STATES = ["lead_waits", "queued", "cancelling", "b3", "place", "pause", "note"]


@pytest.mark.parametrize("state", STATES)
async def test_a17_a_stop_in_every_state_ends_the_runner(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    hold = {"s": 600.0}
    monkeypatch.setattr(wake, "user_question_hold_s", lambda: hold["s"])
    monkeypatch.setattr(pacing, "user_question_hold_s", lambda: hold["s"])
    agents = AGENTS["agents"]
    if state == "place":
        import phases_kit

        monkeypatch.setattr(
            phases_kit, "make_settings", lambda: run_kit.make_settings(**{"fake-auth": 1})
        )
    if state == "cancelling":
        monkeypatch.setattr(Orchestrator, "end_turn", lambda self, agent, reason: None)
    if state == "note":
        agents = [{"id": "a"}, {"id": "b", "compact": {"at": 500}}, {"id": "c"}]

    def a(view: TurnView) -> list[dict[str, Any]]:
        if first(view):
            steps = [give("b", "1"), give("b", "2")]
            return [*steps, BLOCK] if state == "place" else steps
        if state == "cancelling":
            return [*hand_in(view), *hand_in(view)]
        return silent(view)

    def b(view: TurnView) -> list[dict[str, Any]]:
        if view.prompt.startswith(NOTE_HEADING):
            return [BLOCK]
        if state == "b3":
            return [say(private("b"), ASK, kind="question")] if "# Auftrag –" in view.prompt else []
        if state == "note":
            return [FILL]
        return [BLOCK]

    mgr, log = manager(
        workfolder,
        {"agents": agents, "phases": [moderated_phase("a", ["b", "c"])]},
        {"a": a, "b": b},
        limits={"wall_time": "60s", "turns_per_agent": ENDLESS},
    )
    task = asyncio.create_task(run(mgr, within=60))
    ready = {
        "lead_waits": lambda: bool(log.of("b")),
        "queued": lambda: bool(log.of("b")) and len(mgr.room.delegations()) == 2,
        "cancelling": lambda: (
            bool(of_kind(mgr, "submit_held")) and bool(of_kind(mgr, "assign_end"))
        ),
        "b3": lambda: any(d.get("reason") == "user" for d in of_kind(mgr, "waiting")),
        "place": lambda: bool(of_kind(mgr, "assign_start")),
        "pause": lambda: bool(log.of("b")),
        "note": lambda: any(v.prompt.startswith(NOTE_HEADING) for v in log.of("b")),
    }[state]
    if state == "cancelling":

        async def nudge() -> None:
            await eventually(lambda: bool(log.of("b")), within=20)
            mgr.room.post(private("a"), "Gib ab.", sender=USER, via=MessageVia.GUI)

        asyncio.create_task(nudge())
    await eventually(ready, within=20)
    if state == "pause":
        mgr.orchestrator.pause()
    mgr.stop()
    stopped = time.monotonic()
    assert await task is RunStatus.STOPPED
    assert time.monotonic() - stopped < 10, "within the manager's grace"
    assert not [
        e for e in log_entries(mgr.run_dir, "error") if e["data"].get("where") == "topology"
    ], "the runner ended by itself, it was not cancelled"
    assert all(not d.open for d in mgr.room.delegations()), "nothing stays open"


# --------------------------------------------------------------------------------------------
# Counter-check
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("naive", [False, True], ids=["guarded", "naive"])
async def test_a17_counter_check_a_cancel_without_end_turn_waits_until_wall_time(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    if naive:
        monkeypatch.setattr(Orchestrator, "end_turn", lambda self, agent, reason: None)

    def a(view: TurnView) -> list[dict[str, Any]]:
        if first(view):
            return [give("b", "1")]
        return [*hand_in(view), *hand_in(view)]

    async def nudge(mgr: Any, log: Any) -> None:
        await eventually(lambda: bool(log.of("b")), within=20)
        mgr.room.post(private("a"), "Gib ab.", sender=USER, via=MessageVia.GUI)

    limits = {"wall_time": WALL, "turns_per_agent": ENDLESS}
    mgr, log = manager(workfolder, group(), {"a": a, "b": lambda v: [BLOCK]}, limits=limits)
    helper = asyncio.create_task(nudge(mgr, log))
    status = await run(mgr, within=60)
    helper.cancel()
    hang = any(e["data"].get("limit") == "wall_time" for e in log_entries(mgr.run_dir, "limit"))
    if naive:
        assert hang, "without end_turn the phase waits for the turn"
        return
    assert not hang and status is RunStatus.DONE
