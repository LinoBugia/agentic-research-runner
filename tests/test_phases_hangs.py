"""W4b-02b A20: nothing hangs (the pattern of `test_topology_hangs.py`, docs/09 principle 1).

Every case runs the real RunManager with FakeBackend agents under a `wall_time` far longer
than the case needs; it must end by itself, never by `wall_time`. A stop in every state ends
the runner within the manager's grace. The counter-check (`naive`) replaces `read.enter` by one
that ignores `max_visits`: then a loop runs until `wall_time`.
"""

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from phases_kit import (
    Choose,
    TurnView,
    answer,
    errors,
    hand_in,
    judge_gate,
    manager,
    parallel,
    run,
    silent,
    solo,
)
from run_kit import eventually, log_entries, make_workfolder

from arr.models import RunStatus
from arr.permissions.types import EscalationOption
from arr.plan.read import Entry
from arr.runs.orchestrator import Orchestrator
from arr.runs.phases import runner as phase_runner

WALL = "8s"
ENDLESS = 10**6
AGENTS = {"agents": [{"id": a} for a in ("a", "b", "c", "r")]}


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


async def case(
    workfolder: Path, group: dict[str, Any], policies: dict[str, Any], **kwargs: Any
) -> tuple[Any, Any, dict[str, Any]]:
    mgr, log = manager(
        workfolder,
        group,
        policies,
        limits={"wall_time": WALL, "turns_per_agent": ENDLESS},
        **kwargs,
    )
    started = time.monotonic()
    status = await run(mgr, within=60)
    limits = [e["data"].get("limit") for e in log_entries(mgr.run_dir, "limit")]
    return (
        mgr,
        log,
        {"status": status, "hang": "wall_time" in limits, "took": time.monotonic() - started},
    )


both = pytest.mark.parametrize("naive", [False, True], ids=["guarded", "naive"])


@both
async def test_w1_an_agent_that_never_hands_in_ends(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    if naive:
        monkeypatch.setattr(phase_runner, "enter", lambda run, phase, visits: Entry(phase))
    group = {
        **AGENTS,
        "phases": [
            {
                **solo("x", "a", turns=2),
                "max_visits": 3,
                "on_exhausted": "next",
                "gate": {
                    "type": "signal",
                    "field": "status",
                    "routes": {"done": "next", "not_solved": "retry"},
                },
            },
            solo("y", "b"),
        ],
    }
    mgr, log, found = await case(workfolder, group, {"a": silent, "b": answer()})
    if naive:
        assert found["hang"], found
        return
    assert not found["hang"] and found["status"] is RunStatus.DONE, found
    assert len(log.of("a")) == 3 * 3, "three passes of two turns and one follow-up question"
    assert len(log.of("b")) == 1


async def test_w2_a_rater_that_never_rates_ends(workfolder: Path) -> None:
    group = {
        **AGENTS,
        "phases": [
            {
                **parallel("x", ["a", "b"]),
                "gate": judge_gate("r", "rank", routes={"ranked": "next"}),
            }
        ],
    }
    decider = Choose(EscalationOption.CONTINUE)
    mgr, log, found = await case(
        workfolder, group, {"a": answer(), "b": answer(), "r": silent}, decider=decider
    )
    assert not found["hang"] and found["status"] is RunStatus.DONE, found
    assert len(log.of("r")) == 2 and len(decider.asked) == 1


async def test_w3_a_list_that_never_comes_ends(workfolder: Path) -> None:
    group = {
        **AGENTS,
        "phases": [
            {
                **solo("ideen", "c", "list", name="ideen", turns=1),
                "gate": {
                    "type": "signal",
                    "field": "status",
                    "routes": {"done": "next", "not_solved": "next"},
                },
            },
            {**parallel("x", ["a", "b"]), "over": "ideen"},
        ],
    }
    decider = Choose(EscalationOption.RETRY, EscalationOption.CONTINUE)
    mgr, log, found = await case(
        workfolder, group, {"a": answer(), "b": answer(), "c": silent}, decider=decider
    )
    assert not found["hang"] and found["status"] is RunStatus.DONE, found
    assert len(decider.asked) == 2 and log.of("a") == []


def blocking(view: TurnView) -> list[dict[str, Any]]:
    return [{"do": "delay", "seconds": 120}]


async def stop_when(mgr: Any, ready: Any) -> float:
    await eventually(ready, within=20)
    await asyncio.sleep(0.1)
    mgr.stop()
    return time.monotonic()


@pytest.mark.parametrize("state", ["turn", "wait", "question", "rating"])
async def test_w4_a_stop_in_every_state_ends_the_runner(workfolder: Path, state: str) -> None:
    phase: dict[str, Any] = solo("x", "a")
    policies: dict[str, Any] = {"a": answer(), "b": answer(), "r": answer()}
    decider = None
    asked: list[Any] = []
    if state == "turn":
        policies["a"] = blocking
    elif state == "wait":

        def waiting(view: TurnView) -> list[dict[str, Any]]:
            from run_kit import room

            return [room("wait_until", minutes=10)] if view.turn == 1 else hand_in(view)

        policies["a"] = waiting
    elif state == "question":
        phase["gate"] = {"type": "user"}

        class Away:
            async def decide(self, item: Any) -> Any:
                asked.append(item)
                await asyncio.Event().wait()

        decider = Away()
    else:
        phase["gate"] = judge_gate("r", "rank", routes={"ranked": "next"})
        policies["r"] = blocking
    group = {**AGENTS, "phases": [phase, solo("y", "b")]}
    mgr, log = manager(
        workfolder,
        group,
        policies,
        decider=decider,
        limits={"wall_time": "60s", "turns_per_agent": ENDLESS},
    )
    task = asyncio.create_task(run(mgr, within=60))
    ready = {
        "turn": lambda: bool(log.of("a")),
        "wait": lambda: bool(log.of("a")),
        "question": lambda: bool(asked),
        "rating": lambda: bool(log.of("r")),
    }[state]
    stopped = await stop_when(mgr, ready)
    assert await task is RunStatus.STOPPED, errors(mgr)
    assert time.monotonic() - stopped < 10, "within the manager's grace"
    assert log.of("b") == []
    assert not [
        e for e in log_entries(mgr.run_dir, "error") if e["data"].get("where") == "topology"
    ], "the runner ended by itself, it was not cancelled"


async def test_w5_a_stop_during_release_ends_the_runner(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder: dict[str, Any] = {}
    original = Orchestrator.release_results

    def release_results(self: Any, stage: str) -> Any:
        holder["mgr"].stop()
        time.sleep(0.3)  # the room moves files meanwhile
        return original(self, stage)

    monkeypatch.setattr(Orchestrator, "release_results", release_results)
    group = {
        **AGENTS,
        "phases": [
            {
                **parallel("x", ["a", "b"]),
                "isolated": True,
                "gate": judge_gate("r", "rank", routes={"ranked": "next"}),
            },
            solo("y", "c"),
        ],
    }
    mgr, log = manager(
        workfolder, group, {"a": answer(), "b": answer(), "r": answer(), "c": answer()}
    )
    holder["mgr"] = mgr
    started = time.monotonic()
    assert await run(mgr) is RunStatus.STOPPED
    assert time.monotonic() - started < 15
    assert log.of("r") == [] and log.of("c") == []
