"""RunManager (W2-03): lifecycle, stop, limits, concurrency, rate limits, crash recovery,
escalation, checkpoints, reserved folders and no orphaned turns. FakeBackend + real room."""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from run_kit import (
    agent,
    cost,
    events,
    eventually,
    log_entries,
    make_manager,
    make_settings,
    make_workfolder,
    room,
    say,
    turns,
    wait,
)

from arr.backends.base import TurnStatus
from arr.errors import ArrError, ConfigError
from arr.models import RunStatus, Toolchain
from arr.permissions.types import (
    ApprovalStatus,
    ApprovalType,
    DecidedBy,
    Decision,
    DecisionAction,
    DecisionScope,
    EscalationOption,
    PermissionRequest,
)
from arr.runs import manager as manager_module
from arr.runs.context import RunContext, TurnJob
from arr.runs.manager import (
    RunManager,
    base_interpreter,
    create_run_venv,
    mark_interrupted_runs,
)
from arr.runs.orchestrator import Orchestrator, ProfileGate
from arr.runs.prepare import PreparedRun, load_run_meta


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


class Finalizer:
    """Records the calls and the status in `meta.json` at that moment."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, RunStatus]] = []

    async def __call__(self, prepared: PreparedRun) -> None:
        self.calls.append((prepared.run_id, load_run_meta(prepared.run_dir).status))


def statuses(run_dir: Path) -> list[str]:
    return [e["data"]["status"] for e in log_entries(run_dir, "status")]


# --------------------------------------------------------------------------------------------
# A1 lifecycle
# --------------------------------------------------------------------------------------------


async def test_lifecycle_writes_every_status_to_meta_json(workfolder: Path) -> None:
    seen: list[RunStatus] = []
    finalizer = Finalizer()
    manager: RunManager

    async def topology(ctx: RunContext) -> None:
        seen.append(load_run_meta(manager.run_dir).status)
        outcome = await ctx.turn("solo", "Rechne.", stage="arbeit", context={"attempt": 2})
        assert outcome.status is TurnStatus.OK and outcome.text == "fertig"
        assert [r.title for r in outcome.results] == ["Ergebnis"]
        assert outcome.results[0].attempt == 2 and outcome.usage.cost_usd == 0.01
        manager.pause()
        seen.append(load_run_meta(manager.run_dir).status)
        manager.resume()
        seen.append(load_run_meta(manager.run_dir).status)

    script = turns(
        [
            say("fertig"),
            room("submit_result", kind="idea", title="Ergebnis", body="x"),
            cost(0.01),
        ]
    )
    manager = make_manager(workfolder, topology, {"solo": script}, finalizer=finalizer)
    assert await manager.run() is RunStatus.DONE

    run_dir = manager.run_dir
    assert seen == [RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.RUNNING]
    assert statuses(run_dir) == [
        "preparing",
        "running",
        "paused",
        "running",
        "finalizing",
        "done",
    ]
    meta = load_run_meta(run_dir)
    assert meta.status is RunStatus.DONE
    assert meta.started is not None and meta.ended is not None and meta.ended >= meta.started
    assert finalizer.calls == [(manager.prepared.run_id, RunStatus.FINALIZING)]
    assignment = manager.room.assignment("solo")
    assert (assignment.stage, assignment.prompt, assignment.context) == (
        "arbeit",
        "Rechne.",
        {"attempt": 2},
    )
    session = json.loads((run_dir / "agents" / "solo" / "session.json").read_text())
    assert session == {"session_id": "fake-solo"}
    kinds = [e["kind"] for e in events(run_dir, "solo")]
    assert kinds[0] == "turn_start" and kinds[-1] == "turn_end"


async def test_topology_error_ends_failed_with_finalizer(workfolder: Path) -> None:
    finalizer = Finalizer()

    async def topology(ctx: RunContext) -> None:
        await ctx.turn("solo", "los")
        raise RuntimeError("Topologie kaputt")

    manager = make_manager(workfolder, topology, {"solo": turns([say("x")])}, finalizer=finalizer)
    assert await manager.run() is RunStatus.FAILED
    assert isinstance(manager.error, RuntimeError)
    assert statuses(manager.run_dir)[-2:] == ["finalizing", "failed"]
    assert finalizer.calls == [(manager.prepared.run_id, RunStatus.FINALIZING)]
    [error] = log_entries(manager.run_dir, "error")
    assert error["data"]["message"] == "Topologie kaputt"


async def test_backend_start_failure_ends_failed_with_finalizer(workfolder: Path) -> None:
    finalizer = Finalizer()

    async def topology(ctx: RunContext) -> None:
        raise AssertionError("never runs")

    manager = make_manager(workfolder, topology, {"solo": turns([say("x")])}, finalizer=finalizer)
    original = manager.backend_factory

    def factory(prepared: Any) -> Any:
        backend = original(prepared)

        async def broken() -> None:
            raise OSError("Claude-Prozess startet nicht")

        backend.start = broken
        return backend

    manager.backend_factory = factory
    assert await manager.run() is RunStatus.FAILED
    assert statuses(manager.run_dir) == ["preparing", "finalizing", "failed"]
    assert len(finalizer.calls) == 1


async def test_finalizer_error_fails_the_run(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        pass

    async def finalizer(prepared: PreparedRun) -> None:
        raise ValueError("Export kaputt")

    manager = make_manager(workfolder, topology, {"solo": turns()}, finalizer=finalizer)
    assert await manager.run() is RunStatus.FAILED
    assert log_entries(manager.run_dir, "error")[0]["data"]["where"] == "finalizer"


async def test_run_venv_is_created_for_overlay(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, str | None, bool]] = []

    def fake_venv(run_dir: Path, base: str | None, *, with_pip: bool = True) -> Path:
        calls.append((run_dir, base, with_pip))
        return run_dir / ".venv"

    monkeypatch.setattr(manager_module, "create_run_venv", fake_venv)
    settings = make_settings().model_copy(update={"toolchain": Toolchain(run_venv="overlay")})

    async def topology(ctx: RunContext) -> None:
        pass

    manager = make_manager(workfolder, topology, {"solo": turns()}, settings=settings)
    assert await manager.run() is RunStatus.DONE
    assert calls == [(manager.run_dir, None, True)]
    assert (manager.run_dir / "tmp").is_dir()


def test_create_run_venv_sees_the_base_venv(tmp_path: Path) -> None:
    import subprocess

    base = tmp_path / "base-venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(base)], check=True)
    [site] = base.glob("lib/python*/site-packages")
    (site / "arr_basemod.py").write_text("WERT = 42\n")
    assert base_interpreter(str(base)) == base / "bin" / "python3"

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    venv = create_run_venv(run_dir, str(base), with_pip=False)
    python = venv / "bin" / "python"
    probe = "import arr_basemod, sys; print(arr_basemod.WERT, sys.prefix)"
    out = subprocess.run([python, "-c", probe], check=True, capture_output=True, text=True)
    value, prefix = out.stdout.split()
    assert value == "42" and Path(prefix).resolve() == venv.resolve()
    assert "include-system-site-packages = true" in (venv / "pyvenv.cfg").read_text()


def test_base_interpreter_for_an_interpreter_prefix_and_without_toolchain(tmp_path: Path) -> None:
    prefix = tmp_path / "usr"
    (prefix / "bin").mkdir(parents=True)
    python = prefix / "bin" / "python3"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    assert base_interpreter(str(prefix)) == python  # toolchain.python: /usr/bin/python3 → /usr
    system = base_interpreter(None)
    assert system.is_relative_to(Path(sys.base_prefix)) or system == Path(sys.executable)


# --------------------------------------------------------------------------------------------
# A2 stop mid-run, A11 no orphaned turns
# --------------------------------------------------------------------------------------------


def network_request(run_id: str, agent_id: str) -> PermissionRequest:
    """A request in the grey zone (network host), so it waits in the inbox."""
    return PermissionRequest(
        run_id=run_id,
        agent_id=agent_id,
        provider="claude",
        tool="SandboxNetworkAccess",
        hosts=["oeis.org"],
        reason="Folge A006577 nachschlagen",
    )


async def test_stop_mid_run_cancels_approvals_interrupts_turns_and_finalizes(
    workfolder: Path,
) -> None:
    finalizer = Finalizer()
    outcomes: list[Any] = []
    approval: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        orch = ctx
        assert isinstance(orch, Orchestrator)

        async def ask() -> None:  # agent b waits for an approval in its turn, like a backend
            approval["decision"] = await orch.inbox.handle(network_request(ctx.run_id, "b"))

        asking = asyncio.create_task(ask())
        outcomes.extend(await ctx.turns([("a", "lang"), ("b", "hängt")]))
        await asking
        assert ctx.stop_requested
        outcomes.append(await ctx.turn("a", "noch einer?"))

    scripts = {
        "a": turns([say("beginne"), wait(30), say("nie")]),
        "b": turns([say("beginne"), {"do": "hang", "seconds": 0.3}, say("nie")]),
    }
    manager = make_manager(workfolder, topology, scripts, finalizer=finalizer)
    running = asyncio.create_task(manager.run())
    await eventually(lambda: manager.inbox is not None and len(manager.inbox.pending()) == 1)
    await eventually(lambda: len(manager.orchestrator._active) == 2)
    started = time.monotonic()
    manager.stop()
    assert await running is RunStatus.STOPPED
    assert time.monotonic() - started < 5, "the 30 s wait was interrupted"

    assert [o.status for o in outcomes] == [TurnStatus.INTERRUPTED] * 3
    assert outcomes[2].turn == 2 and outcomes[2].error == "Der Run wurde gestoppt."
    assert approval["decision"].action is DecisionAction.DENY
    [item] = manager.inbox.items()
    assert item.status is ApprovalStatus.OBSOLETE
    assert finalizer.calls == [(manager.prepared.run_id, RunStatus.FINALIZING)]
    assert statuses(manager.run_dir)[-3:] == ["running", "finalizing", "stopped"]
    for agent_id in ("a", "b"):
        log = events(manager.run_dir, agent_id)
        assert [e["kind"] for e in log] == ["turn_start", "text", "turn_end"]
        assert log[-1]["data"]["status"] == "interrupted"
    assert log_entries(manager.run_dir, "stop")[0]["data"]["reason"] == "user"


async def test_hanging_turn_is_cancelled_after_the_grace_time(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        await ctx.turn("a", "hängt lange")

    scripts = {"a": turns([say("beginne"), {"do": "hang", "seconds": 30}])}
    manager = make_manager(workfolder, topology, scripts, stop_grace_s=0.2)
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: manager.orchestrator is not None and "a" in manager.orchestrator._active
    )
    manager.stop()
    assert await asyncio.wait_for(running, 5) is RunStatus.STOPPED

    log = events(manager.run_dir, "a")
    assert [e["kind"] for e in log] == ["turn_start", "text", "error", "turn_end"]
    assert log[-1]["data"]["status"] == "interrupted" and log[-2]["data"]["type"] == "orchestrator"
    assert "abgebrochen" in log_entries(manager.run_dir, "error")[0]["data"]["message"]
    backend = manager.backends["a"]
    assert not backend._running and backend._closed, "no orphaned turn (aclosing)"
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert others == [], "no hanging tasks after the stop"


async def test_stop_before_running_skips_the_topology(workfolder: Path) -> None:
    finalizer = Finalizer()
    ran = []

    async def topology(ctx: RunContext) -> None:
        ran.append(True)

    manager = make_manager(workfolder, topology, {"solo": turns()}, finalizer=finalizer)
    manager.stop()
    assert await manager.run() is RunStatus.STOPPED
    assert ran == [] and len(finalizer.calls) == 1
    assert statuses(manager.run_dir) == ["preparing", "finalizing", "stopped"]


# --------------------------------------------------------------------------------------------
# A3 limits, concurrency, rate limits
# --------------------------------------------------------------------------------------------


async def test_wall_time_limit_interrupts_and_finalizes(workfolder: Path) -> None:
    finalizer = Finalizer()
    outcomes = []

    async def topology(ctx: RunContext) -> None:
        outcomes.append(await ctx.turn("solo", "lang"))
        assert ctx.stop_requested

    manager = make_manager(
        workfolder,
        topology,
        {"solo": turns([wait(30)])},
        finalizer=finalizer,
        run_extra={"limits": {"wall_time": 0.3}},
    )
    assert await asyncio.wait_for(manager.run(), 10) is RunStatus.DONE
    assert outcomes[0].status is TurnStatus.LIMIT
    assert log_entries(manager.run_dir, "limit")[0]["data"]["limit"] == "wall_time"
    assert events(manager.run_dir, "solo")[-1]["data"]["status"] == "interrupted"
    assert len(finalizer.calls) == 1


async def test_turns_per_agent_limit(workfolder: Path) -> None:
    outcomes = []

    async def topology(ctx: RunContext) -> None:
        while not ctx.stop_requested:
            outcomes.append(await ctx.turn("solo", "weiter"))

    manager = make_manager(
        workfolder,
        topology,
        {"solo": turns([say("x")], repeat_last=True)},
        run_extra={"limits": {"turns_per_agent": 2}},
    )
    assert await manager.run() is RunStatus.DONE
    assert [o.status for o in outcomes] == [TurnStatus.OK, TurnStatus.OK, TurnStatus.LIMIT]
    assert manager.orchestrator.agents["solo"].turns_used == 2
    [limit] = log_entries(manager.run_dir, "limit")
    assert limit["data"] == {"limit": "turns_per_agent", "agent": "solo"}


async def test_budget_limit_sums_turn_costs_and_interrupts_others(workfolder: Path) -> None:
    outcomes: dict[str, list[Any]] = {"a": [], "b": []}

    async def spend(ctx: RunContext) -> None:
        for _ in range(3):  # the third turn comes after the budget is used up
            outcomes["a"].append(await ctx.turn("a", "rechne"))

    async def topology(ctx: RunContext) -> None:
        async def slow() -> None:
            outcomes["b"].append(await ctx.turn("b", "lang"))

        await asyncio.gather(spend(ctx), slow())

    scripts = {
        "a": turns([cost(0.03)], repeat_last=True),
        "b": turns([wait(30)]),
    }
    manager = make_manager(
        workfolder, topology, scripts, run_extra={"limits": {"budget_usd": 0.05}}
    )
    assert await asyncio.wait_for(manager.run(), 10) is RunStatus.DONE
    assert [o.status for o in outcomes["a"]] == [TurnStatus.OK, TurnStatus.OK, TurnStatus.LIMIT]
    assert outcomes["b"][0].status is TurnStatus.LIMIT, "running turns are interrupted"
    assert manager.orchestrator.cost_usd == pytest.approx(0.06)
    assert log_entries(manager.run_dir, "limit")[0]["data"]["limit"] == "budget_usd"


def intervals(run_dir: Path, agent_id: str) -> list[tuple[float, float]]:
    log = events(run_dir, agent_id)
    starts = [e["ts"] for e in log if e["kind"] == "turn_start"]
    ends = [e["ts"] for e in log if e["kind"] == "turn_end"]
    return list(zip(starts, ends, strict=True))


def overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


async def test_concurrency_per_auth_profile(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        await ctx.turns([("a", "x"), ("b", "x"), ("c", "x")])

    scripts = {name: turns([wait(0.2)]) for name in ("a", "b", "c")}
    agents = [agent("a"), agent("b"), agent("c", auth="other")]
    manager = make_manager(
        workfolder, topology, scripts, agents=agents, settings=make_settings(**{"fake-auth": 1})
    )
    assert await manager.run() is RunStatus.DONE
    [a], [b], [c] = (intervals(manager.run_dir, x) for x in ("a", "b", "c"))
    assert not overlap(a, b), "profile fake-auth: one turn at a time"
    assert overlap(a, c) or overlap(b, c), "profile other is not limited"


async def test_rate_limit_pauses_the_profile_until_resets_at(workfolder: Path) -> None:
    resets: list[float] = []

    def limited() -> dict[str, Any]:
        resets.append(time.time() + 0.5)
        return turns([{"do": "rate_limit", "status": "rejected", "resets_at": resets[0]}])

    async def topology(ctx: RunContext) -> None:
        await ctx.turn("a", "x")
        await ctx.turn("b", "x")

    agents = [agent("a"), agent("b"), agent("c", auth="other")]
    scripts = {"a": limited, "b": turns([say("y")]), "c": turns()}
    manager = make_manager(workfolder, topology, scripts, agents=agents)
    assert await manager.run() is RunStatus.DONE
    [b] = intervals(manager.run_dir, "b")
    assert b[0] >= resets[0] - 0.01, "b waited until the profile was free again"
    [entry] = log_entries(manager.run_dir, "rate_limit")
    assert entry["data"]["action"] == "pause" and entry["data"]["profile"] == "fake-auth"


async def test_profile_gate_throttles_pauses_and_aborts() -> None:
    now = [1000.0]
    gate = ProfileGate("p", 3, clock=lambda: now[0])
    assert await gate.acquire(lambda: False) and await gate.acquire(lambda: False)
    assert gate.report({"status": "allowed", "resets_at": None}) is None
    assert gate.report({"status": "allowed_warning", "resets_at": 1100.0}) == ("throttle", 1100.0)
    third = asyncio.create_task(gate.acquire(lambda: False))
    await asyncio.sleep(0.01)
    assert not third.done(), "throttled to one turn at a time"
    gate.release()
    gate.release()
    assert await asyncio.wait_for(third, 1)
    gate.release()
    assert gate.report({"status": "rejected", "resets_at": None}) == ("pause", 1060.0)
    stop = [False]
    waiting = asyncio.create_task(gate.acquire(lambda: stop[0]))
    await asyncio.sleep(0.01)
    assert not waiting.done(), "paused"
    stop[0] = True
    gate.kick()
    assert await asyncio.wait_for(waiting, 1) is False
    now[0] = 1200.0
    assert await gate.acquire(lambda: False), "after resets_at the profile is free"


# --------------------------------------------------------------------------------------------
# A4 crash recovery
# --------------------------------------------------------------------------------------------


def test_mark_interrupted_runs(workfolder: Path) -> None:
    runs = workfolder / "runs"
    for status in RunStatus:
        folder = runs / f"20261028-1200_demo_custom_{list(RunStatus).index(status):04x}"
        folder.mkdir(parents=True)
        meta = {
            "run_id": folder.name,
            "status": status.value,
            "starter": "cli",
            "project": "demo",
            "created": 1.0,
            "arr_version": "0",
        }
        (folder / "meta.json").write_text(json.dumps(meta))
        (folder / "agents" / "a").mkdir(parents=True)
        (folder / "agents" / "a" / "transcript.jsonl").write_text('{"seq":1,"kind":"turn_start"}\n')
    (runs / "kaputt").mkdir()
    (runs / "kaputt" / "meta.json").write_text("{")

    before = {p.name: load_run_meta(p).status for p in runs.iterdir() if p.name != "kaputt"}
    changed = mark_interrupted_runs(workfolder)
    after = {p.name: load_run_meta(p) for p in runs.iterdir() if p.name != "kaputt"}
    active = ("preparing", "running", "paused", "finalizing")
    expected = [r for r, s in before.items() if s in active]
    assert sorted(changed) == sorted(expected) and len(changed) == 4
    assert all(after[r].status is RunStatus.INTERRUPTED for r in changed)
    for run_id in changed:
        folder = runs / run_id
        assert log_entries(folder, "status")[-1]["data"]["previous"] in (
            "preparing",
            "running",
            "paused",
            "finalizing",
        )
        assert load_run_meta(folder).ended is not None
        assert (folder / "agents" / "a" / "transcript.jsonl").read_text().startswith('{"seq":1')
    untouched = [m.status for m in after.values() if m.run_id not in changed]
    assert RunStatus.DONE in untouched and RunStatus.QUEUED in untouched
    assert mark_interrupted_runs(workfolder) == []


# --------------------------------------------------------------------------------------------
# A8 escalation, A9 checkpoints
# --------------------------------------------------------------------------------------------


async def test_escalate_pauses_waits_for_the_user_and_continues(workfolder: Path) -> None:
    chosen = []
    later: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        async def other_turn() -> None:
            await asyncio.sleep(0.05)
            later["outcome"] = await ctx.turn("b", "während der Eskalation")

        pending = asyncio.create_task(other_turn())
        options = [EscalationOption.RETRY, EscalationOption.CONTINUE, EscalationOption.END]
        chosen.append(await ctx.escalate("beweis", "Ø 5,5 nach 2 Versuchen", options))
        await pending

    manager = make_manager(workfolder, topology, {"a": turns(), "b": turns([say("ok")])})
    running = asyncio.create_task(manager.run())
    await eventually(lambda: manager.inbox is not None and len(manager.inbox.pending()) == 1)
    [item] = manager.inbox.pending()
    assert item.type is ApprovalType.ESCALATION and item.stage == "beweis"
    assert load_run_meta(manager.run_dir).status is RunStatus.PAUSED
    await asyncio.sleep(0.2)
    assert "outcome" not in later, "new turns wait while the run is paused"

    with pytest.raises(ValueError, match="nur der User"):
        Decision(
            action=DecisionAction.OPTION,
            option=EscalationOption.END,
            decided_by=DecidedBy.GATEKEEPER,
        )
    run_scope = Decision(
        action=DecisionAction.OPTION,
        option=EscalationOption.END,
        decided_by=DecidedBy.USER,
        scope=DecisionScope.RUN,
    )
    with pytest.raises(ArrError, match="einmalig"):
        manager.inbox.resolve(item.id, run_scope)
    decided_at = time.time()
    manager.inbox.resolve(
        item.id,
        Decision(
            action=DecisionAction.OPTION,
            option=EscalationOption.CONTINUE,
            decided_by=DecidedBy.USER,
        ),
    )
    assert await running is RunStatus.DONE
    assert chosen == [EscalationOption.CONTINUE]
    assert later["outcome"].status is TurnStatus.OK
    [b_turn] = intervals(manager.run_dir, "b")
    assert b_turn[0] >= decided_at
    assert statuses(manager.run_dir)[1:4] == ["running", "paused", "running"]
    [decided] = log_entries(manager.run_dir, "escalation_decided")
    assert decided["data"] == {"stage": "beweis", "option": "continue", "by_user": True}


async def test_escalate_ends_with_end_on_stop(workfolder: Path) -> None:
    chosen = []

    async def topology(ctx: RunContext) -> None:
        chosen.append(await ctx.escalate("s", "x", [EscalationOption.RETRY]))

    manager = make_manager(workfolder, topology, {"a": turns()})
    running = asyncio.create_task(manager.run())
    await eventually(lambda: manager.inbox is not None and len(manager.inbox.pending()) == 1)
    manager.stop()
    assert await running is RunStatus.STOPPED
    assert chosen == [EscalationOption.END]
    assert manager.inbox.items()[0].status is ApprovalStatus.OBSOLETE


async def test_checkpoint_plan_at_start_and_further_checkpoints(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        await ctx.checkpoint("strategie", attempt=1)
        await ctx.checkpoint("beweis", accepted=[3])

    manager = make_manager(workfolder, topology, {"a": turns()}, stages=["strategie", "beweis"])
    assert await manager.run() is RunStatus.DONE
    checkpoints = [e["data"] for e in log_entries(manager.run_dir, "checkpoint")]
    assert checkpoints == [
        {"stage": "plan", "stages": ["strategie", "beweis"]},
        {"stage": "strategie", "attempt": 1},
        {"stage": "beweis", "accepted": [3]},
    ]
    entries = log_entries(manager.run_dir)
    first_checkpoint = next(i for i, e in enumerate(entries) if e["kind"] == "checkpoint")
    assert entries[first_checkpoint - 1]["data"] == {"status": "running"}


async def test_default_plan_comes_from_run_yaml_stages(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        pass

    stages = [{"name": "skizze", "agent": "a"}, {"name": "beweis", "agent": "a"}]
    manager = make_manager(workfolder, topology, {"a": turns()}, run_extra={"stages": stages})
    assert await manager.run() is RunStatus.DONE
    plan = log_entries(manager.run_dir, "checkpoint")[0]["data"]
    assert plan == {"stage": "plan", "stages": ["skizze", "beweis"]}


# --------------------------------------------------------------------------------------------
# A10 reserved run folders
# --------------------------------------------------------------------------------------------


def reserve(workfolder: Path, run_id: str) -> Path:
    folder = workfolder / "runs" / run_id
    folder.mkdir(parents=True)
    meta = {
        "run_id": run_id,
        "status": "queued",
        "starter": "user_agent",
        "project": "demo",
        "created": 1761390000.0,
        "arr_version": "0",
    }
    (folder / "meta.json").write_text(json.dumps(meta))
    return folder


async def test_reserved_run_folder_runs_in_place(workfolder: Path) -> None:
    run_id = "20261028-1200_demo_custom_abcd"
    folder = reserve(workfolder, run_id)

    async def topology(ctx: RunContext) -> None:
        await ctx.turn("a", "x")

    manager = make_manager(workfolder, topology, {"a": turns([say("y")])}, run_id=run_id)
    assert await manager.run() is RunStatus.DONE
    assert manager.run_dir == folder
    assert load_run_meta(folder).starter.value == "user_agent"


async def test_reserved_run_that_cannot_be_prepared_fails_and_keeps_meta(workfolder: Path) -> None:
    run_id = "20261028-1200_demo_custom_abcd"
    folder = reserve(workfolder, run_id)

    async def topology(ctx: RunContext) -> None:
        raise AssertionError("never runs")

    manager = make_manager(
        workfolder,
        topology,
        {"a": turns()},
        agents=[agent("a", role="roles/fehlt.md")],
        run_id=run_id,
    )
    with pytest.raises(ConfigError):
        await manager.run()
    assert [p.name for p in folder.iterdir()] == ["meta.json"]
    assert load_run_meta(folder).status is RunStatus.FAILED


# --------------------------------------------------------------------------------------------
# A11 no orphaned turns
# --------------------------------------------------------------------------------------------


async def test_cancelled_topology_leaves_no_turn_running(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        task = asyncio.create_task(ctx.turn("a", "x"))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        outcome = await ctx.turn("a", "danach")
        assert outcome.status is TurnStatus.OK and outcome.turn == 2

    scripts = {"a": turns([wait(30)], [say("wieder da")])}
    manager = make_manager(workfolder, topology, scripts)
    assert await manager.run() is RunStatus.DONE
    log = events(manager.run_dir, "a")
    assert [e["kind"] for e in log] == [
        "turn_start",
        "error",
        "turn_end",
        "turn_start",
        "text",
        "turn_end",
    ]
    assert log[2]["data"]["status"] == "interrupted"


async def test_parallel_turns_of_one_agent_run_one_after_the_other(workfolder: Path) -> None:
    async def topology(ctx: RunContext) -> None:
        outcomes = await ctx.turns([TurnJob("a", "1"), TurnJob("a", "2", {"k": 1})])
        assert [o.turn for o in outcomes] == [1, 2]

    manager = make_manager(workfolder, topology, {"a": turns([wait(0.1)], [wait(0.1)])})
    assert await manager.run() is RunStatus.DONE
    first, second = intervals(manager.run_dir, "a")
    assert not overlap(first, second)


# --------------------------------------------------------------------------------------------
# W2-03.1: stop in finalizing (G1), crash in preparing (G3), parallel interrupt (G4)
# --------------------------------------------------------------------------------------------


async def test_w2_03_1_stop_during_finalizing_keeps_done(workfolder: Path) -> None:
    """G1: a stop that comes while the finalizer runs is ignored; the run stays `done`."""
    seen: list[RunStatus] = []

    async def topology(ctx: RunContext) -> None:
        await ctx.turn("solo", "los")

    async def finalizer(prepared: PreparedRun) -> None:
        seen.append(load_run_meta(prepared.run_dir).status)
        manager.stop()
        await asyncio.sleep(0.05)  # room for a stop task, as on the old code

    manager = make_manager(workfolder, topology, {"solo": turns([say("x")])}, finalizer=finalizer)
    assert await manager.run() is RunStatus.DONE
    assert seen == [RunStatus.FINALIZING]
    assert statuses(manager.run_dir)[-2:] == ["finalizing", "done"]
    assert log_entries(manager.run_dir, "stop") == []
    manager.stop()  # after the end: ignored as well
    assert load_run_meta(manager.run_dir).status is RunStatus.DONE


def test_w2_03_1_crash_in_preparing_becomes_interrupted(workfolder: Path) -> None:
    """G3: a run left in `preparing` (arr went down while preparing) becomes `interrupted`."""
    folder = workfolder / "runs" / "20261028-1200_demo_custom_beef"
    folder.mkdir(parents=True)
    meta = {
        "run_id": folder.name,
        "status": "preparing",
        "starter": "cli",
        "project": "demo",
        "created": 1.0,
        "arr_version": "0",
    }
    (folder / "meta.json").write_text(json.dumps(meta))
    assert mark_interrupted_runs(workfolder) == [folder.name]
    assert load_run_meta(folder).status is RunStatus.INTERRUPTED
    assert log_entries(folder, "status")[-1]["data"] == {
        "status": "interrupted",
        "previous": "preparing",
    }


async def test_w2_03_1_interrupts_run_in_parallel_after_cancelling_approvals(
    workfolder: Path,
) -> None:
    """G4: three agents whose `interrupt()` takes 1 s each stop in well under 2 s."""
    pending_at_interrupt: list[int] = []

    async def topology(ctx: RunContext) -> None:
        await ctx.turns([("a", "x"), ("b", "x"), ("c", "x")])

    scripts = {name: turns([say("beginne"), wait(30)]) for name in ("a", "b", "c")}
    manager = make_manager(workfolder, topology, scripts)
    original = manager.backend_factory

    def factory(prepared: Any) -> Any:
        backend = original(prepared)
        interrupt = backend.interrupt

        async def slow_interrupt() -> None:
            if not backend._running:
                return  # no-op when idle (contract), e.g. from close()
            assert manager.inbox is not None
            pending_at_interrupt.append(len(manager.inbox.pending()))
            await asyncio.sleep(1.0)
            await interrupt()

        backend.interrupt = slow_interrupt
        return backend

    manager.backend_factory = factory
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: manager.orchestrator is not None and len(manager.orchestrator._active) == 3
    )
    assert manager.orchestrator is not None
    waiting = asyncio.create_task(
        manager.orchestrator.inbox.handle(network_request(manager.prepared.run_id, "a"))
    )
    await eventually(lambda: len(manager.inbox.pending()) == 1)
    started = time.monotonic()
    manager.stop()
    assert await running is RunStatus.STOPPED
    elapsed = time.monotonic() - started
    assert elapsed < 1.8, f"stop took {elapsed:.2f} s, interrupts ran one after the other"
    assert pending_at_interrupt == [0, 0, 0], "approvals are cancelled before interrupt()"
    assert (await waiting).action is DecisionAction.DENY
