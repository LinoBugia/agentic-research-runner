"""Carry-overs of Codeplan §8 for W3-01 (A9), each with a test that fails on the old state:
`ProfileGate` shared across runs of a process, atomic take-over of a reserved run folder,
clean-up of orphaned `queued` folders, lock probe without exclusive lock and retry on
`acquire`, appearance also on the frame root, skills per agent in `RunStartAgent`."""

import asyncio
import fcntl
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from run_kit import (
    agent,
    events,
    make_manager,
    make_settings,
    make_workfolder,
    say,
    turns,
    wait,
    write_profiles,
)
from test_gui import gui_user

from arr.errors import ArrError
from arr.models import RunStatus
from arr.runs.context import RunContext
from arr.runs.inspect import RunLock, run_lock_held
from arr.runs.launch import discard_orphaned_reservations
from arr.runs.orchestrator import ProfileGate
from arr.service.api_models import Appearance, AppSettings, RunStartAgent, Vendor
from arr.service.gui.app import STATIC_DIR
from arr.service.mock_api import MockApi
from arr.service.real_api import RealApi


def intervals(run_dir: Path, agent_id: str) -> list[tuple[float, float]]:
    out = []
    start = None
    for record in events(run_dir, agent_id):
        if record["kind"] == "turn_start":
            start = record["ts"]
        elif record["kind"] == "turn_end" and start is not None:
            out.append((start, record["ts"]))
    return out


def overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


# --- G6: ProfileGate shared across runs ---------------------------------------------------------


async def test_profile_gate_is_shared_across_runs_of_one_process(tmp_path: Path) -> None:
    workfolder = make_workfolder(tmp_path)
    shared: dict[str, ProfileGate] = {}
    settings = make_settings(**{"fake-auth": 1})

    async def topology(ctx: RunContext) -> None:
        await ctx.turn("a", "x")

    managers = [
        make_manager(
            workfolder,
            topology,
            {"a": turns([say("los"), wait(0.4)])},
            settings=settings,
            gates=shared,
        )
        for _ in range(2)
    ]
    statuses = await asyncio.gather(*(m.run() for m in managers))
    assert statuses == [RunStatus.DONE, RunStatus.DONE]
    assert set(shared) == {"fake-auth"} and shared["fake-auth"].limit == 1
    [first] = intervals(managers[0].run_dir, "a")
    [second] = intervals(managers[1].run_dir, "a")
    assert not overlap(first, second), (first, second)


# --- G7: atomic take-over of a reserved folder --------------------------------------------------


def reserve(workfolder: Path, run_id: str) -> Path:
    folder = workfolder / "runs" / run_id
    folder.mkdir(parents=True)
    meta = {
        "run_id": run_id,
        "status": "queued",
        "starter": "user_agent",
        "project": "demo",
        "created": time.time(),
        "arr_version": "0",
    }
    (folder / "meta.json").write_text(json.dumps(meta))
    return folder


HOLDER = (
    "import fcntl, sys, time\n"
    "f = open(sys.argv[1], 'ab')\n"
    "fcntl.flock(f, fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "time.sleep(60)\n"
)


def test_reserved_folder_held_by_another_process_is_not_prepared_twice(tmp_path: Path) -> None:
    workfolder = make_workfolder(tmp_path)
    run_id = "20261029-1200_demo_custom_abcd"
    folder = reserve(workfolder, run_id)
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(folder / "run.lock")], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"

        async def topology(ctx: RunContext) -> None:
            await ctx.turn("a", "x")

        manager = make_manager(workfolder, topology, {"a": turns([say("y")])}, run_id=run_id)
        with pytest.raises(ArrError) as error:
            asyncio.run(manager.run())
        assert "anderer arr-Prozess" in str(error.value)
        assert {p.name for p in folder.iterdir()} == {"meta.json", "run.lock"}
        assert json.loads((folder / "meta.json").read_text())["status"] == "queued"
    finally:
        holder.kill()
        holder.wait()
    # once the holder is gone the same manager setup takes the folder over and holds the lock
    manager = make_manager(workfolder, topology, {"a": turns([say("y")])}, run_id=run_id)
    assert asyncio.run(manager.run()) is RunStatus.DONE
    assert (folder / "run.lock").exists() and not run_lock_held(folder)


# --- N2: orphaned `queued` folders --------------------------------------------------------------


def test_orphaned_reservations_are_removed_at_start(tmp_path: Path) -> None:
    workfolder = make_workfolder(tmp_path)
    old = reserve(workfolder, "20261029-1100_demo_custom_0001")
    meta = json.loads((old / "meta.json").read_text())
    (old / "meta.json").write_text(json.dumps({**meta, "created": time.time() - 120}))
    (old / "run.lock").touch()
    fresh = reserve(workfolder, "20261029-1101_demo_custom_0002")
    prepared = reserve(workfolder, "20261029-1102_demo_custom_0003")
    (prepared / "meta.json").write_text(
        json.dumps(
            {**json.loads((prepared / "meta.json").read_text()), "created": time.time() - 120}
        )
    )
    (prepared / "task.md").write_text("x")
    held = reserve(workfolder, "20261029-1103_demo_custom_0004")
    (held / "meta.json").write_text(
        json.dumps({**json.loads((held / "meta.json").read_text()), "created": time.time() - 120})
    )
    lock = RunLock(held)
    lock.acquire()
    try:
        removed = discard_orphaned_reservations(workfolder)
    finally:
        lock.release()
    assert removed == [old.name]
    assert not old.exists() and fresh.exists() and prepared.exists() and held.exists()


# --- lock probe and retry -----------------------------------------------------------------------


def test_lock_probe_does_not_block_a_starting_run_and_probes_share(tmp_path: Path) -> None:
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / "run.lock").touch()
    probing = threading.Event()
    release = threading.Event()

    def probe_hold() -> None:  # what a probe does, held a little longer
        with (folder / "run.lock").open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            probing.set()
            release.wait(5)
            fcntl.flock(handle, fcntl.LOCK_UN)

    thread = threading.Thread(target=probe_hold)
    thread.start()
    probing.wait(5)
    assert not run_lock_held(folder), "a probe never looks like an executing process"
    timer = threading.Timer(0.3, release.set)
    timer.start()
    started = time.monotonic()
    lock = RunLock(folder)
    lock.acquire()  # old state: fails at once with ArrError
    try:
        assert time.monotonic() - started >= 0.2
        assert run_lock_held(folder)
    finally:
        lock.release()
        thread.join(5)
    assert not run_lock_held(folder)
    with pytest.raises(ArrError):
        other = RunLock(folder)
        lock.acquire()
        try:
            other.acquire(retries=2, wait_s=0.01)
        finally:
            lock.release()


# --- W1-08.2: appearance also on the frame root -------------------------------------------------


async def test_appearance_is_set_on_the_frame_root_and_arr_js_applies_it() -> None:
    api = MockApi()
    await api.update_app_settings(AppSettings(appearance=Appearance.DARK))
    async with gui_user(api) as user:
        await user.open("/runs")
        with user.client:
            from nicegui import ElementFilter

            [root] = [e for e in ElementFilter(kind=None) if "app" in e.classes]  # type: ignore[arg-type]
        assert root._props.get("data-theme") == "dark"
    script = (STATIC_DIR / "arr.js").read_text("utf-8")
    assert "arr.applyTheme" in script and "arr-theme" in script and "dataset.theme" in script


# --- W1-08 §4: skills per agent -----------------------------------------------------------------


def test_run_start_agent_carries_the_agents_skills(
    tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_model = RunStartAgent(
        id="a",
        role_label="A",
        provider="claude",  # type: ignore[arg-type]
        vendor=Vendor.ANTHROPIC,
        model="m",
        model_label="M",
        profile="p",
        skills=["sympy-check"],
    )
    assert agent_model.skills == ["sympy-check"]
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    workfolder = make_workfolder(tmp_path)
    (workfolder / "arr.yaml").write_text("version: 1\ntoolchain: {run_venv: off}\n")
    skill = workfolder / "skills" / "sympy-check"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: sympy-check\ndescription: Prüft Rechnungen.\n---\n# SymPy\n"
    )
    api = RealApi(workfolder, base_url="http://127.0.0.1:8765")
    result = asyncio.run(
        api.resolve_run(
            {
                "project": "demo",
                "task": "x",
                "topology": {"kind": "solo"},
                "defaults": {"provider": "claude", "model": "fake-model", "auth": "fake-auth"},
                "agents": [agent("a", skills=["sympy-check"])],
            }
        )
    )
    assert not result.errors, result.errors
    assert [a.skills for a in result.agents] == [["sympy-check"]]


# --- N2, part 2: the clean-up is called at the start of `arr run` and of the service ---------


def test_arr_run_and_the_service_remove_an_old_orphan_at_start(
    tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails when `run_launch` or `RealApi.start` no longer call the clean-up (W3-01.1 G4)."""
    from run_kit import fake_builders

    from arr.models import RunInput
    from arr.runs.launch import plan_run_input, run_launch
    from arr.service.real_api import RealApi

    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    workfolder = make_workfolder(tmp_path)
    (workfolder / "arr.yaml").write_text("version: 1\ntoolchain: {run_venv: off}\n")

    def orphan(name: str) -> Path:
        folder = reserve(workfolder, name)
        meta = json.loads((folder / "meta.json").read_text())
        (folder / "meta.json").write_text(json.dumps({**meta, "created": time.time() - 3600}))
        return folder

    first = orphan("20261029-1000_demo_custom_0a01")
    api = RealApi(workfolder, base_url="http://127.0.0.1:1")
    asyncio.run(api.start())
    asyncio.run(api.close())
    assert not first.exists(), "RealApi.start() must remove an old orphaned reservation"

    second = orphan("20261029-1001_demo_custom_0a02")
    run_input = {
        "project": "demo",
        "task": "x",
        "topology": {"kind": "solo"},
        "defaults": {"provider": "claude", "model": "fake-model", "auth": "fake-auth"},
        "agents": [agent("a")],
    }
    launch = plan_run_input(RunInput.model_validate(run_input), workfolder=workfolder)
    status = asyncio.run(
        run_launch(
            launch,
            decider=None,
            report=lambda line: None,
            builders=fake_builders({"a": turns([say("hallo")])}),
            signals=False,
            scan=False,
        )
    ).status
    assert status is RunStatus.DONE
    assert not second.exists(), "run_launch must remove an old orphaned reservation"
