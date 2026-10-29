"""Start service of `arr run` (W2-05 A2, A3, A6) with FakeBackend agents: workfolder lookup,
`--param`, profiles, `check_topology` before `prepare_run`, backend factory per provider, crash
recovery, Ctrl-C (call and real signal), `stop.request` and the German progress lines.
W2-05.1: the run lock (M1), signal handlers until after the scan (G1), texts in `finalizing`
(G2) and the repeated signal of `uv run` (point 7)."""

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from run_kit import (
    eventually,
    fake_builders,
    log_entries,
    make_workfolder,
    say,
    turns,
    wait,
    write_profiles,
)

from arr.errors import ArrError, ConfigError
from arr.models import RunStatus
from arr.runs import launch as launch_module
from arr.runs.launch import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_STOPPED,
    STOP_REQUEST,
    StopControl,
    build_manager,
    describe_approval,
    describe_entry,
    exit_code,
    find_workfolder,
    load_profiles,
    parse_params,
    plan_launch,
    request_stop,
    run_launch,
)
from arr.runs.manager import mark_interrupted_runs
from arr.runs.prepare import PreparedRun, load_run_meta


class Clock:
    """A clock for `StopControl` that the test moves."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


SOLO = """\
version: 1
project: demo
task: Teste arr.
topology: {kind: solo}
limits: {wall_time: 2m}
defaults: {provider: claude, model: fake-model, auth: fake-auth}
agents: [{id: a}]
groupchats: [{name: main, members: [a]}]
"""

MIXED = """\
version: 1
project: demo
task: Teste arr.
topology: {kind: solo, params: {agent: a}}
limits: {wall_time: 2m, budget_usd: 0.5}
defaults: {provider: claude, model: fake-model, auth: fake-auth}
agents:
  - {id: a}
  - {id: gpt, provider: codex, model: fake-gpt, auth: fake-codex}
"""


@pytest.fixture
def workfolder(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    wf = make_workfolder(tmp_path)
    (wf / "arr.yaml").write_text("version: 1\ntoolchain: {run_venv: off}\n", encoding="utf-8")
    write_profiles(arr_home, {"fake-auth": "claude_oauth", "fake-codex": "codex_chatgpt"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return wf


def write_run(workfolder: Path, text: str = SOLO, name: str = "run.yaml") -> Path:
    path = workfolder / name
    path.write_text(text, encoding="utf-8")
    return path


def no_report(line: str) -> None:
    return None


# --------------------------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------------------------


def test_find_workfolder_upwards_and_with_the_option(workfolder: Path, tmp_path: Path) -> None:
    deep = workfolder / "projects" / "demo" / "sub"
    deep.mkdir()
    assert find_workfolder(start=deep) == workfolder
    assert find_workfolder(workfolder) == workfolder
    with pytest.raises(ArrError, match="kein arr-Workfolder"):
        find_workfolder(tmp_path)
    with pytest.raises(ArrError, match="Kein arr-Workfolder gefunden"):
        find_workfolder(start=tmp_path)


def test_parse_params_reads_yaml_scalars() -> None:
    params = parse_params(["pass_score=6", "on_fail=stop", "flag=true", "text= a b ", "e="])
    assert params == {"pass_score": 6, "on_fail": "stop", "flag": True, "text": "a b", "e": ""}
    with pytest.raises(ArrError, match="kein Parameter"):
        parse_params(["pass_score"])
    with pytest.raises(ArrError, match="kein Parameter"):
        parse_params(["=6"])


def test_load_profiles_without_the_file_gives_a_hint(arr_home: Path) -> None:
    with pytest.raises(ArrError, match="profiles.yaml fehlt") as info:
        load_profiles()
    assert "kind: claude_oauth" in str(info.value) and "codex-login" in str(info.value)
    write_profiles(arr_home, {"max": "claude_oauth"})
    assert [p.name for p in load_profiles().profiles] == ["max"]


def test_plan_checks_the_topology_before_any_folder_exists(workfolder: Path) -> None:
    broken = SOLO.replace("topology: {kind: solo}", "topology: {kind: waterfall}")
    broken += "stages: [{name: s, agent: a}]\nraters: [a]\n"
    run_file = write_run(workfolder, broken)
    with pytest.raises(ConfigError) as info:
        plan_launch(run_file, cwd=workfolder)
    assert "params.pass_score" in str(info.value)
    assert list((workfolder / "runs").iterdir()) == [], "nothing was prepared"


def test_plan_loads_the_group_skill_and_the_param_overrides(workfolder: Path) -> None:
    skill = workfolder / "skills" / "demo-group"
    (skill / "roles").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo-group\ndescription: Demo-Gruppe.\n---\n\nDemo.\n", encoding="utf-8"
    )
    (skill / "roles" / "worker.md").write_text("Du arbeitest.\n", encoding="utf-8")
    (skill / "group.yaml").write_text(
        "kind: research-group\nversion: 1\ntopology: solo\n"
        "params:\n  max_turns: {default: 3, min: 1, max: 5}\n"
        "defaults: {provider: claude, model: fake-model, auth: fake-auth}\n"
        "agents: [{id: a, role: roles/worker.md}]\n",
        encoding="utf-8",
    )
    run_file = write_run(workfolder, "version: 1\nproject: demo\nskill: demo-group\ntask: Los.\n")
    launch = plan_launch(run_file, params={"max_turns": 2}, cwd=workfolder)
    assert launch.group_spec is not None and launch.resolved.params == {"max_turns": 2}
    assert launch.resolved.agents[0].role == "roles/worker.md"
    with pytest.raises(ConfigError, match="max_turns"):
        plan_launch(run_file, params={"max_turns": 9}, cwd=workfolder)
    (skill / "group.yaml").unlink()
    with pytest.raises(ConfigError, match="kein Group-Skill"):
        plan_launch(run_file, cwd=workfolder)


def test_plan_reports_missing_file_project_and_profile(workfolder: Path, arr_home: Path) -> None:
    with pytest.raises(ArrError, match="gibt es nicht"):
        plan_launch(workfolder / "fehlt.yaml", cwd=workfolder)
    run_file = write_run(workfolder, SOLO.replace("project: demo", "project: nope"))
    with pytest.raises(ArrError, match="Projekt „nope“"):
        plan_launch(run_file, cwd=workfolder)
    run_file = write_run(workfolder)
    launch = plan_launch(run_file, cwd=workfolder / "projects")  # found upwards
    assert launch.workfolder == workfolder and launch.task_base == workfolder
    assert [p.name for p in launch.used_profiles()] == ["fake-auth"]


# --------------------------------------------------------------------------------------------
# A6: factory per provider, finalizer, crash recovery
# --------------------------------------------------------------------------------------------


async def test_factory_builds_the_backend_per_provider_with_profile_and_limits(
    workfolder: Path,
) -> None:
    builders = fake_builders({"a": turns([say("ok")]), "gpt": turns([say("ok")])})
    launch = plan_launch(write_run(workfolder, MIXED), cwd=workfolder)
    manager = build_manager(launch, decider=None, builders=builders)
    assert await manager.run() is RunStatus.DONE
    calls = {(provider, agent_id): kwargs for provider, agent_id, kwargs in builders["calls"]}
    assert set(calls) == {("claude", "a"), ("codex", "gpt")}
    for kwargs in calls.values():
        assert kwargs["run_id"] == manager.prepared.run_id and kwargs["inbox"] is manager.inbox
    assert calls[("claude", "a")]["auth"].name == "fake-auth"
    assert calls[("codex", "gpt")]["auth"].kind == "codex_chatgpt"
    assert calls[("claude", "a")]["limits"].budget_usd == 0.5
    assert manager.finalizer is not None, "finalizer always set"
    assert (manager.run_dir / "output" / "summary.md").is_file()
    assert manager.stages == [], "no own stages= (plan comes from run.yaml)"


async def test_run_launch_marks_interrupted_runs_and_scans(workfolder: Path) -> None:
    stale = workfolder / "runs" / "20261029-0900_demo_custom_dead"
    stale.mkdir(parents=True)
    meta = {
        "run_id": stale.name,
        "status": "running",
        "starter": "cli",
        "project": "demo",
        "created": 1.0,
        "arr_version": "0",
    }
    (stale / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    lines: list[str] = []
    launch = plan_launch(write_run(workfolder), cwd=workfolder)
    result = await run_launch(
        launch,
        decider=None,
        report=lines.append,
        builders=fake_builders({"a": turns([say("ok")])}),
        signals=False,
        interval_s=0.02,
    )
    assert load_run_meta(stale).status is RunStatus.INTERRUPTED
    assert any(
        "20261029-0900_demo_custom_dead" in line and "unterbrochen" in line for line in lines
    )
    assert result.status is RunStatus.DONE and result.summary_file.is_file()
    assert result.scan is not None and result.scan.clean
    [scan] = log_entries(result.run_dir, "secret_scan")
    assert scan["data"]["clean"] is True
    text = "\n".join(lines)
    assert f"Run {result.run_id}" in text and f"Ordner: {result.run_dir}" in text
    assert "▶ a: Turn 1" in text and "■ a: Turn 1 beendet (ok)" in text
    assert "Status: abgeschlossen (done)" in text
    assert f"Run {result.run_id} beendet: abgeschlossen (done)." in text
    assert "Grund: Die Topologie ist regulär durchgelaufen." in text
    assert "Secret-Scan: sauber" in text
    assert exit_code(result.status) == EXIT_OK


# --------------------------------------------------------------------------------------------
# A2: Ctrl-C
# --------------------------------------------------------------------------------------------


async def test_stop_control_first_stops_with_finalizer_second_aborts_hard(
    workfolder: Path,
) -> None:
    lines: list[str] = []
    exits: list[int] = []
    clock = Clock()
    launch = plan_launch(write_run(workfolder), cwd=workfolder)
    manager = build_manager(
        launch, decider=None, builders=fake_builders({"a": turns([say("x"), wait(30)])})
    )
    control = StopControl(manager, lines.append, hard_exit=exits.append, clock=clock)
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: manager.orchestrator is not None and "a" in manager.orchestrator._active
    )
    control.on_interrupt()
    assert "Strg-C: Der Run wird gestoppt" in lines[-1]
    assert await asyncio.wait_for(running, 10) is RunStatus.STOPPED
    assert (manager.run_dir / "output" / "summary.md").is_file(), "finalizer ran"
    assert exits == [] and exit_code(RunStatus.STOPPED) == EXIT_STOPPED
    clock.now = 2.0
    control.on_interrupt()
    assert exits == [130] and "harter Abbruch" in lines[-1]
    assert load_run_meta(manager.run_dir).status is RunStatus.STOPPED, "a finished run stays"


async def test_second_interrupt_during_finalizing_aborts_hard(workfolder: Path) -> None:
    lines: list[str] = []
    exits: list[int] = []
    seen: list[RunStatus] = []

    async def slow_finalizer(prepared: PreparedRun) -> None:
        seen.append(load_run_meta(prepared.run_dir).status)
        await asyncio.sleep(0.6)

    clock = Clock()
    launch = plan_launch(write_run(workfolder), cwd=workfolder)
    manager = build_manager(
        launch,
        decider=None,
        builders=fake_builders({"a": turns([say("x"), wait(30)])}),
        finalizer=slow_finalizer,
    )
    control = StopControl(manager, lines.append, hard_exit=exits.append, clock=clock)
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: manager.orchestrator is not None and "a" in manager.orchestrator._active
    )
    control.on_interrupt()
    await eventually(lambda: seen == [RunStatus.FINALIZING])
    clock.now = 2.0
    control.on_interrupt()
    assert exits == [130] and "Zweites Strg-C: harter Abbruch" in lines[-1]
    assert load_run_meta(manager.run_dir).status is RunStatus.INTERRUPTED
    assert log_entries(manager.run_dir, "status")[-1]["data"] == {
        "status": "interrupted",
        "previous": "hard_abort",
    }
    await asyncio.wait_for(running, 10)  # only in the test: the real process is gone


async def test_a_finished_run_is_not_stopped_by_a_late_interrupt(workfolder: Path) -> None:
    lines: list[str] = []
    launch = plan_launch(write_run(workfolder), cwd=workfolder)
    manager = build_manager(launch, decider=None, builders=fake_builders({"a": turns([say("x")])}))
    assert await manager.run() is RunStatus.DONE
    control = StopControl(manager, lines.append, hard_exit=lambda code: None)
    control.on_interrupt()
    assert "schon beendet" in lines[-1]
    assert load_run_meta(manager.run_dir).status is RunStatus.DONE


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_real_sigint_stops_the_run_and_the_finalizer_runs(workfolder: Path) -> None:
    lines: list[str] = []
    launch = plan_launch(write_run(workfolder), cwd=workfolder)

    async def interrupt_when_active() -> None:
        await eventually(lambda: any(line.startswith("▶ a: Turn 1") for line in lines))
        os.kill(os.getpid(), signal.SIGINT)

    killer = asyncio.create_task(interrupt_when_active())
    result = await run_launch(
        launch,
        decider=None,
        report=lines.append,
        builders=fake_builders({"a": turns([say("x"), wait(30)])}),
        hard_exit=lambda code: None,
        interval_s=0.02,
    )
    await killer
    assert result.status is RunStatus.STOPPED and result.summary_file.is_file()
    assert any(line.startswith("Strg-C") for line in lines)
    assert "Grund: Der User hat den Run gestoppt." in lines
    assert exit_code(result.status) == EXIT_STOPPED
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, "handler removed"


# --------------------------------------------------------------------------------------------
# A3: stop.request from a second process
# --------------------------------------------------------------------------------------------


async def test_stop_request_file_stops_the_run(workfolder: Path) -> None:
    lines: list[str] = []
    launch = plan_launch(write_run(workfolder), cwd=workfolder)
    builders = fake_builders({"a": turns([say("x"), wait(30)])})
    running = asyncio.create_task(
        run_launch(
            launch,
            decider=None,
            report=lines.append,
            builders=builders,
            signals=False,
            interval_s=0.02,
            stop_poll_s=0.05,
        )
    )
    await eventually(lambda: any(line.startswith("▶ a: Turn 1") for line in lines))
    [run_dir] = [p for p in (workfolder / "runs").iterdir() if p.is_dir()]
    assert await asyncio.to_thread(request_stop, run_dir) is True
    assert await asyncio.to_thread(request_stop, run_dir) is False, "already requested"
    result = await asyncio.wait_for(running, 10)
    assert result.status is RunStatus.STOPPED
    assert not (run_dir / STOP_REQUEST).exists(), "consumed"
    assert "Stopp-Anforderung erhalten (arr runs stop): Der Run wird gestoppt …" in lines
    with pytest.raises(ArrError, match="läuft nicht"):
        request_stop(run_dir)


# --------------------------------------------------------------------------------------------
# Progress lines
# --------------------------------------------------------------------------------------------


def entry(kind: str, **data: Any) -> dict[str, Any]:
    return {"ts": time.time(), "kind": kind, "data": data}


def test_entries_become_german_lines() -> None:
    assert describe_entry(entry("status", status="running")) == "Status: läuft (running)"
    assert describe_entry(entry("checkpoint", stage="plan", stages=["a", "b"])) == "● Plan: a → b"
    assert (
        describe_entry(
            entry("checkpoint", stage="s", attempt=1, agent="x", decision="rework", score=4.5)
        )
        == "● Stufe „s“, Versuch 1 (x): Nacharbeit, Score 4,5"
    )
    assert (
        describe_entry(
            entry("checkpoint", stage="s", phase="stage_end", outcome="passed", accepted=3)
        )
        == "● Stufe „s“ abgeschlossen: bestanden, akzeptiert #3"
    )
    assert describe_entry(entry("turn", agent="a", turn=2, stage="s")) == "▶ a: Turn 2 · Stufe „s“"
    assert describe_entry(entry("turn_end", agent="a", turn=2, status="ok", cost_usd=0.5)) == (
        "■ a: Turn 2 beendet (ok, 0,5 $)"
    )
    assert describe_entry(entry("limit", limit="budget_usd")) == (
        "Limit erreicht: Das Budget des Runs ist erreicht."
    )
    assert describe_entry(entry("hint", agent="a")) == "📨 Hinweis an a mitten im Turn"
    assert describe_entry(entry("error", message="kaputt", where="finalizer")) == (
        "Fehler (finalizer): kaputt"
    )
    assert describe_entry(entry("secret_scan", clean=True)) is None
    assert describe_entry(entry("something_new", a=1)) == "something_new: a=1"
    request = {"agent_id": "a", "tool": "Bash", "command": "pip install x", "hosts": []}
    assert describe_approval({"event": "open", "item_id": "apr_1", "request": request}) == (
        "❓ Freigabe-Anfrage apr_1 von a · Bash: pip install x"
    )
    decided = {
        "event": "decided",
        "item_id": "apr_1",
        "decision": {"action": "allow", "scope": "run"},
    }
    assert describe_approval(decided) == "✔ Freigabe apr_1 erlaubt (run)"
    assert describe_approval({"event": "obsolete", "item_id": "apr_1", "note": "kein TTY"}) == (
        "✖ Freigabe apr_1 hinfällig: kein TTY"
    )
    assert describe_approval({"event": "decision"}) is None


def test_exit_codes() -> None:
    assert exit_code(RunStatus.DONE) == EXIT_OK == 0
    assert exit_code(RunStatus.STOPPED) == EXIT_STOPPED == 130
    assert exit_code(RunStatus.FAILED) == EXIT_FAILED == 1


# --------------------------------------------------------------------------------------------
# W2-05.1: run lock (M1), handlers until after the scan (G1), finalizing (G2), uv's repeat
# --------------------------------------------------------------------------------------------


def stale_run(workfolder: Path, name: str, status: str = "running") -> Path:
    folder = workfolder / "runs" / name
    folder.mkdir(parents=True)
    meta = {
        "run_id": name,
        "status": status,
        "starter": "cli",
        "project": "demo",
        "created": 1.0,
        "arr_version": "0",
    }
    (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder


def held(run_dir: Path) -> bool:
    """Does some process hold `run.lock`? (Own `flock` probe, independent of arr's code.)"""
    import fcntl

    with (run_dir / "run.lock").open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl.flock")
async def test_w2_05_1_a_run_whose_lock_another_process_holds_is_not_interrupted(
    workfolder: Path,
) -> None:
    import fcntl

    stale = stale_run(workfolder, "20261029-0900_demo_custom_dead")
    other = (stale / "run.lock").open("ab")  # a second handle behaves like another process
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert held(stale)
    lines: list[str] = []
    locked_while_running: list[bool] = []
    launch = plan_launch(write_run(workfolder), cwd=workfolder)

    async def look_while_running() -> None:
        await eventually(lambda: any(line.startswith("▶ a: Turn 1") for line in lines))
        [own] = [p for p in (workfolder / "runs").iterdir() if p != stale]
        locked_while_running.append(held(own))

    looker = asyncio.create_task(look_while_running())
    result = await run_launch(
        launch,
        decider=None,
        report=lines.append,
        builders=fake_builders({"a": turns([say("x"), wait(0.5)])}),
        signals=False,
        interval_s=0.02,
    )
    await looker
    assert load_run_meta(stale).status is RunStatus.RUNNING, "left to the other process"
    assert not any("unterbrochen" in line for line in lines)
    assert locked_while_running == [True], "our own run is locked while it runs"
    assert result.status is RunStatus.DONE
    assert (result.run_dir / "run.lock").is_file() and not held(result.run_dir)
    # the other process is gone: `arr runs stop` says so, the next start marks the run
    fcntl.flock(other, fcntl.LOCK_UN)
    other.close()
    with pytest.raises(ArrError, match="in keinem arr-run-Prozess mehr"):
        request_stop(stale)
    assert mark_interrupted_runs(workfolder) == [stale.name]
    assert load_run_meta(stale).status is RunStatus.INTERRUPTED


async def test_w2_05_1_a_run_that_cannot_be_prepared_leaves_no_folder(workfolder: Path) -> None:
    run_file = write_run(workfolder, SOLO.replace("auth: fake-auth", "auth: niemand"))
    launch = plan_launch(run_file, cwd=workfolder)  # the profiles are checked by prepare_run
    with pytest.raises(ArrError, match="niemand"):
        await run_launch(
            launch,
            decider=None,
            report=no_report,
            builders=fake_builders({"a": turns([say("x")])}),
            signals=False,
        )
    assert list((workfolder / "runs").iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_w2_05_1_ctrl_c_during_the_scan_says_finished_and_keeps_the_exit_code(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arr.runs.secret_scan import scan_run_dir

    lines: list[str] = []
    handler_during_scan: list[Any] = []

    def slow_scan(run_dir: Path, secrets: Any = None, **kwargs: Any) -> Any:
        handler_during_scan.append(signal.getsignal(signal.SIGINT))
        if handler_during_scan[0] is not signal.default_int_handler:  # arr still listens
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.3)
        return scan_run_dir(run_dir, secrets, **kwargs)

    monkeypatch.setattr(launch_module, "scan_run_dir", slow_scan)
    launch = plan_launch(write_run(workfolder), cwd=workfolder)
    result = await run_launch(
        launch,
        decider=None,
        report=lines.append,
        builders=fake_builders({"a": turns([say("x")])}),
        hard_exit=lambda code: None,
        interval_s=0.02,
    )
    assert handler_during_scan[0] is not signal.default_int_handler, "removed before the scan"
    assert "Der Run ist schon beendet. Noch einmal Strg-C beendet arr sofort." in lines
    assert result.status is RunStatus.DONE and exit_code(result.status) == EXIT_OK
    assert result.scan is not None and log_entries(result.run_dir, "secret_scan")
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler, "removed afterwards"


async def test_w2_05_1_first_ctrl_c_in_finalizing_lets_the_finalizer_finish(
    workfolder: Path,
) -> None:
    gate = asyncio.Event()
    seen: list[RunStatus] = []
    lines: list[str] = []
    exits: list[int] = []

    async def slow_finalizer(prepared: PreparedRun) -> None:
        seen.append(load_run_meta(prepared.run_dir).status)
        await gate.wait()

    launch = plan_launch(write_run(workfolder), cwd=workfolder)
    manager = build_manager(
        launch,
        decider=None,
        builders=fake_builders({"a": turns([say("x")])}),
        finalizer=slow_finalizer,
    )
    control = StopControl(manager, lines.append, hard_exit=exits.append)
    running = asyncio.create_task(manager.run())
    await eventually(lambda: seen == [RunStatus.FINALIZING])
    control.on_interrupt()
    assert lines[-1] == (
        "Strg-C: Der Run wird schon abgeschlossen, der Finalizer läuft zu Ende. "
        "Noch einmal Strg-C (nach einer Sekunde) bricht hart ab."
    )
    gate.set()
    assert await asyncio.wait_for(running, 10) is RunStatus.DONE
    assert exits == [] and log_entries(manager.run_dir, "stop") == []


async def test_w2_05_1_stop_request_in_finalizing_is_answered_not_executed(
    workfolder: Path,
) -> None:
    gate = asyncio.Event()
    seen: list[RunStatus] = []
    lines: list[str] = []

    async def slow_finalizer(prepared: PreparedRun) -> None:
        seen.append(load_run_meta(prepared.run_dir).status)
        await gate.wait()

    launch = plan_launch(write_run(workfolder), cwd=workfolder)
    running = asyncio.create_task(
        run_launch(
            launch,
            decider=None,
            report=lines.append,
            builders=fake_builders({"a": turns([say("x")])}),
            finalizer=slow_finalizer,
            signals=False,
            interval_s=0.02,
            stop_poll_s=0.02,
        )
    )
    await eventually(lambda: seen == [RunStatus.FINALIZING])
    [run_dir] = [p for p in (workfolder / "runs").iterdir() if p.is_dir()]
    with pytest.raises(ArrError, match="läuft nicht"):
        request_stop(run_dir)  # `arr runs stop` refuses in finalizing …
    request = run_dir / STOP_REQUEST
    request.write_text("{}", encoding="utf-8")  # … but a file that is there is answered
    await eventually(lambda: not request.exists())
    assert lines[-1] == (
        "Stopp-Anforderung erhalten (arr runs stop), aber der Run wird schon abgeschlossen; "
        "der Finalizer läuft zu Ende."
    )
    gate.set()
    result = await asyncio.wait_for(running, 10)
    assert result.status is RunStatus.DONE and log_entries(run_dir, "stop") == []


async def test_w2_05_1_a_signal_repeated_within_the_window_is_the_same_ctrl_c(
    workfolder: Path,
) -> None:
    """`uv run` forwards the terminal's SIGINT to arr ≈ 0.2 s after the kernel delivered it to
    the whole process group: the second one must not count as the hard abort."""
    clock = Clock()
    lines: list[str] = []
    exits: list[int] = []
    launch = plan_launch(write_run(workfolder), cwd=workfolder)
    manager = build_manager(
        launch, decider=None, builders=fake_builders({"a": turns([say("x"), wait(30)])})
    )
    control = StopControl(manager, lines.append, hard_exit=exits.append, clock=clock)
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: manager.orchestrator is not None and "a" in manager.orchestrator._active
    )
    control.on_interrupt()  # the terminal's signal
    clock.now = 0.2
    control.on_interrupt()  # the same one, forwarded by uv run
    assert exits == [] and sum(line.startswith("Strg-C") for line in lines) == 1
    assert "(nach einer Sekunde)" in lines[-1]
    assert await asyncio.wait_for(running, 10) is RunStatus.STOPPED
    assert (manager.run_dir / "output" / "summary.md").is_file(), "finalizer ran"
    clock.now = 1.5
    control.on_interrupt()  # a deliberate second Ctrl-C
    assert exits == [130]
