"""`arr run` and `arr runs …` through Typer (W2-05 A1, A3): the M1 scenario with FakeBackend
agents behind the launcher's backend builders, exit codes, German errors without traceback,
`runs list|status|stop|scan`. No model, no network."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from phases_kit import Log, TurnView, hand_in, policy_builders, write_project_tasks
from run_kit import (
    eventually,
    fake_builders,
    log_entries,
    make_workfolder,
    post,
    say,
    turns,
    wait,
    write_profiles,
)
from typer.testing import CliRunner

from arr.cli import app
from arr.models import RunStatus
from arr.runs import launch as launch_module
from arr.runs.launch import STOP_REQUEST, plan_launch, run_launch

runner = CliRunner()

M1_RUN_YAML = """\
version: 1
name: M1-Szenario
project: demo
task_file: task.md
topology: {kind: phases, params: {pass_score: 6, max_visits: 2}}
limits: {wall_time: 5m, turns_per_agent: 6, budget_usd: 0.5}
defaults: {provider: claude, model: fake-model, auth: fake-auth, effort: low}
agents:
  - {id: strategist}
  - {id: prover}
  - {id: rater, provider: codex, model: fake-gpt, auth: fake-codex}
groupchats:
  - {name: main, members: [strategist, prover, rater], mode: event}
  - {name: werkstatt, members: [strategist, prover], mode: rounds}
phases:
  - name: strategie
    topology: solo
    channels: [main, werkstatt]
    max_visits: "{{ params.max_visits }}"
    on_exhausted: stop
    actions:
      strategist: {task: tasks/strategie.md, submission: {format: artifact, name: strategie}}
    gate:
      type: judge
      raters: [prover, rater]
      action: {task: tasks/bewerten.md, submission: {format: rating}}
      rule: mean
      pass_score: "{{ params.pass_score }}"
      routes: {pass: next, fail: retry}
  - name: beweis
    topology: solo
    channels: [main, werkstatt]
    max_visits: "{{ params.max_visits }}"
    on_exhausted: stop
    actions:
      prover: {task: tasks/beweis.md, submission: {format: artifact, name: beweis}}
    gate:
      type: judge
      raters: [rater]
      action: {task: tasks/bewerten.md, submission: {format: rating}}
      rule: mean
      pass_score: "{{ params.pass_score }}"
      routes: {pass: next, fail: retry}
"""
"""The M1 scenario as a phases run (W4b-06b, was `waterfall`): two phases with a gate `judge`
(mean of the raters, at least `pass_score`), the strategy reworked once."""
TASKS = {
    "tasks/strategie.md": "Plane den Beweis.\n",
    "tasks/beweis.md": "Schreibe den Beweis.\n",
    "tasks/bewerten.md": "Bewerte die Abgabe.\n",
}
SCORES = {("prover", "strategie", 1): 4, ("prover", "strategie", 2): 8}
SCORES |= {("rater", "strategie", 1): 5, ("rater", "strategie", 2): 7, ("rater", "beweis", 1): 8}


def m1_policy(view: TurnView) -> list[dict[str, Any]]:
    """The turns of the M1 scenario: a tool, two channels, a rework of the strategy, ratings of
    both raters."""
    if view.expected.get("format") == "rating":
        score = SCORES[(view.agent, view.stage, view.attempt)]
        return hand_in(view, score=lambda _v, _r: score, comment="passt")
    if view.agent == "strategist":
        if view.attempt > 1:
            return hand_in(view)
        tool = {"do": "tool", "name": "Bash", "input": {"command": "python check.py"}}
        return [
            {"do": "text", "text": "Ich plane den Beweis."},
            {**tool, "result": "ok"},
            post("werkstatt", "Ansatz: Induktion."),
            *hand_in(view),
        ]
    return [post("main", "Der Beweis steht."), *hand_in(view)]


def m1_builders() -> dict[Any, Any]:
    return policy_builders({a: m1_policy for a in ("strategist", "prover", "rater")}, Log())


@pytest.fixture
def m1(
    tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch, restore_root_logger: Any
) -> Path:
    """Workfolder with the M1 run.yaml; the launcher builds FakeBackends.

    `arr run` calls `setup_logging()`, which binds a handler to the `CliRunner` stream of that
    call; `restore_root_logger` drops it afterwards, so later log calls never hit a closed
    stream (the „Logging error“ lines in CI, W2-05.3).
    """
    wf = make_workfolder(tmp_path)
    (wf / "arr.yaml").write_text("version: 1\ntoolchain: {run_venv: off}\n", encoding="utf-8")
    (wf / "run.yaml").write_text(M1_RUN_YAML, encoding="utf-8")
    (wf / "task.md").write_text("Zeige Lemma A für n ≡ 1 mod 4.\n", encoding="utf-8")
    write_project_tasks(wf, TASKS)
    write_profiles(arr_home, {"fake-auth": "claude_oauth", "fake-codex": "codex_chatgpt"})
    monkeypatch.setattr(launch_module, "BACKEND_BUILDERS", m1_builders())
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    monkeypatch.chdir(wf)
    return wf


def run_dirs(workfolder: Path) -> list[Path]:
    return sorted(p for p in (workfolder / "runs").iterdir() if p.is_dir())


# --------------------------------------------------------------------------------------------
# A1: arr run with the M1 scenario
# --------------------------------------------------------------------------------------------


def test_arr_run_plays_the_m1_scenario(m1: Path) -> None:
    result = runner.invoke(app, ["run", "run.yaml"])
    assert result.exit_code == 0, result.output
    [run_dir] = run_dirs(m1)
    out = result.output
    assert f"Workfolder: {m1}" in out
    assert f"Run {run_dir.name}" in out and f"Ordner: {run_dir}" in out
    assert "● Plan: strategie → beweis" in out
    assert "▶ strategist: Turn 1 · Phase „strategie“" in out
    assert "■ strategist: Turn 1 beendet (ok)" in out
    assert "● Phase „strategie“, Durchlauf 1: Gate „judge“ ergibt „fail“ – nicht bestanden, " in out
    assert "● Phase „strategie“, Durchlauf 1: Route „retry“ → Phase „strategie“" in out
    assert "● Phase „strategie“, Durchlauf 2: Gate „judge“ ergibt „pass“ – bestanden, " in out
    assert "● Phase „beweis“ abgeschlossen nach Durchlauf 1: bestanden, gilt: Abgabe #7" in out
    assert "Status: wird abgeschlossen (finalizing)" in out
    assert f"Run {run_dir.name} beendet: abgeschlossen (done)." in out
    assert "Grund: Die Topologie ist regulär durchgelaufen." in out
    assert f"Zusammenfassung: {run_dir / 'output' / 'summary.md'}" in out
    assert "Secret-Scan: sauber" in out
    summary = (run_dir / "output" / "summary.md").read_text(encoding="utf-8")
    assert "- **Status:** abgeschlossen (done)" in summary
    assert "| strategie | 1 | strategist | #1 | 4,5 | nicht bestanden |" in summary
    assert "| strategie | 2 | strategist | #4 | 7,5 | bestanden |" in summary
    assert "- `rater`: codex, fake-gpt" in summary
    decisions = [
        e["data"]["decision"] for e in log_entries(run_dir, "checkpoint") if "decision" in e["data"]
    ]
    assert decisions == ["failed", "passed", "passed"]
    starts = [
        (e["data"]["stage"], e["data"]["attempt"], e["data"]["how"])
        for e in log_entries(run_dir, "checkpoint")
        if e["data"].get("phase") == "pass_start"
    ]
    assert ("strategie", 2, "retry") in starts, f"at least one rework (M1 1): {starts}"
    for name in ("main", "werkstatt"):
        assert (run_dir / "groupchats" / f"{name}.md").is_file()
    [scan] = log_entries(run_dir, "secret_scan")
    assert scan["data"]["clean"] is True and scan["data"]["files"] > 10

    # arr runs list / status / scan / stop on the finished run
    listed = runner.invoke(app, ["runs", "list"])
    assert listed.exit_code == 0, listed.output
    assert run_dir.name in listed.output and "abgeschlossen (done)" in listed.output
    assert "Run-ID" in listed.output and "Dauer" in listed.output

    status = runner.invoke(app, ["runs", "status", run_dir.name])
    assert status.exit_code == 0, status.output
    assert f"Run:       {run_dir.name} (demo, M1-Szenario)" in status.output
    assert "Status:    abgeschlossen (done)" in status.output
    assert "Grund:     Die Topologie ist regulär durchgelaufen." in status.output
    assert (
        "Zuletzt:   Phase „beweis“ abgeschlossen nach Durchlauf 1: bestanden, gilt: Abgabe #7"
        in status.output
    )
    assert "  strategist: 2 Turns, zuletzt ok" in status.output
    assert "  prover: 3 Turns, zuletzt ok" in status.output
    assert "  rater: 3 Turns, zuletzt ok" in status.output
    assert "Usage:     " in status.output
    assert "Offene Freigaben: keine" in status.output
    assert "Secret-Scan: sauber" in status.output

    scanned = runner.invoke(app, ["runs", "scan", run_dir.name, "--workfolder", str(m1)])
    assert scanned.exit_code == 0, scanned.output
    assert scanned.output.startswith("Secret-Scan: sauber")

    stopped = runner.invoke(app, ["runs", "stop", run_dir.name])
    assert stopped.exit_code == 1
    assert f"Der Run {run_dir.name} läuft nicht (Status: abgeschlossen, done)" in stopped.output


def test_w3_00_m1_scenario_with_a_run_venv_outside_the_run_folder(
    m1: Path, venv_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The M1 scenario with `run_venv: overlay`: the venv lies at `<venv root>/runs/<id>/`,
    `meta.json` names it, and the secret scan covers it (`run-venv/…`)."""
    from arr.runs import manager as manager_module
    from arr.runs.prepare import load_run_meta

    real = manager_module.create_run_venv

    def without_pip(paths, base, *, with_pip=True):  # pip makes the test slow, not different
        return real(paths, base, with_pip=False)

    monkeypatch.setattr(manager_module, "create_run_venv", without_pip)
    (m1 / "arr.yaml").write_text("version: 1\ntoolchain: {run_venv: overlay}\n", "utf-8")
    result = runner.invoke(app, ["run", "run.yaml"])
    assert result.exit_code == 0, result.output
    [run_dir] = run_dirs(m1)
    venv = venv_root / "runs" / run_dir.name
    assert (venv / "bin" / "python").exists() and (venv / "pyvenv.cfg").is_file()
    assert not (run_dir / ".venv").exists()
    assert load_run_meta(run_dir).run_venv == str(venv)
    assert "Secret-Scan: sauber" in result.output
    [scan] = log_entries(run_dir, "secret_scan")
    in_run = sum(1 for p in run_dir.rglob("*") if p.is_file() and not p.is_symlink())
    assert scan["data"]["files"] > in_run  # the venv's files were read as well


def test_arr_run_reports_configuration_errors_in_german_without_traceback(m1: Path) -> None:
    text = M1_RUN_YAML.replace("params: {pass_score: 6, max_visits: 2}", "params: {}")
    (m1 / "kaputt.yaml").write_text(text, encoding="utf-8")
    result = runner.invoke(app, ["run", "kaputt.yaml"])
    assert result.exit_code == 1
    assert "params.pass_score" in result.output
    assert "verweist auf keinen Parameter" in result.output
    assert "Traceback" not in result.output
    assert run_dirs(m1) == [], "checked before prepare_run"

    (m1 / "tippfehler.yaml").write_text(M1_RUN_YAML.replace("agents:", "agnets:"), "utf-8")
    result = runner.invoke(app, ["run", "tippfehler.yaml"])
    assert result.exit_code == 1 and "tippfehler.yaml" in result.output
    assert "Traceback" not in result.output

    result = runner.invoke(app, ["run", "run.yaml", "--param", "pass_score"])
    assert result.exit_code == 1 and "kein Parameter" in result.output


def test_arr_run_without_profiles_file(m1: Path, arr_home: Path) -> None:
    (arr_home / "profiles.yaml").unlink()
    result = runner.invoke(app, ["run", "run.yaml"])
    assert result.exit_code == 1 and "profiles.yaml fehlt" in result.output


def test_arr_run_exit_code_1_for_a_failed_run(m1: Path) -> None:
    (m1 / "task.md").write_text("  \n", encoding="utf-8")
    result = runner.invoke(app, ["run", "run.yaml", "--workfolder", str(m1)])
    assert result.exit_code == 1, result.output
    assert "beendet: fehlgeschlagen (failed)" in result.output
    assert "Grund: Fehler: Der Run hat keinen Auftrag" in result.output


def test_arr_run_param_overrides_the_topology_parameter(m1: Path) -> None:
    result = runner.invoke(app, ["run", "run.yaml", "-p", "pass_score=9", "-p", "max_visits=1"])
    assert result.exit_code == 0, result.output
    [run_dir] = run_dirs(m1)
    run_yaml = (run_dir / "run.yaml").read_text(encoding="utf-8")
    assert "pass_score: 9" in run_yaml and "max_visits: 1" in run_yaml
    out = result.output
    assert "● Phase „strategie“, Durchlauf 1: Gate „judge“ ergibt „fail“ – nicht bestanden" in out
    assert "Route „retry“ → Ende des Runs (aufgebraucht: strategie)" in out


# --------------------------------------------------------------------------------------------
# A3: runs list / status / stop from a second process
# --------------------------------------------------------------------------------------------


def test_runs_list_and_status_on_an_empty_or_unknown_workfolder(m1: Path) -> None:
    result = runner.invoke(app, ["runs", "list"])
    assert result.exit_code == 0 and result.output.startswith("Keine Runs in")
    result = runner.invoke(app, ["runs", "status", "gibt-es-nicht"])
    assert result.exit_code == 1 and "gibt es nicht" in result.output
    result = runner.invoke(app, ["runs", "status", "../x"])
    assert result.exit_code == 1 and "keine Run-ID" in result.output
    result = runner.invoke(app, ["runs", "list", "--workfolder", str(m1.parent)])
    assert result.exit_code == 1 and "kein arr-Workfolder" in result.output


async def test_runs_stop_from_a_second_process_ends_a_running_run(m1: Path) -> None:
    builders = fake_builders({"a": turns([say("los"), wait(30)])})
    (m1 / "solo.yaml").write_text(
        "version: 1\nproject: demo\ntask: Los.\ntopology: {kind: phases}\n"
        "defaults: {provider: claude, model: fake-model, auth: fake-auth}\n"
        "agents: [{id: a}]\n"
        "phases: [{name: arbeit, topology: solo, "
        "actions: {a: {task: tasks/ohne-skill.md, submission: {format: text}}}}]\n",
        encoding="utf-8",
    )
    lines: list[str] = []
    launch = plan_launch(m1 / "solo.yaml", cwd=m1)
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
    [run_dir] = run_dirs(m1)

    status = await asyncio.to_thread(runner.invoke, app, ["runs", "status", run_dir.name])
    assert "Status:    läuft (running)" in status.output
    assert "Grund:     Der Run ist noch aktiv." in status.output
    assert "  a: " not in status.output and "Turns je Agent: noch keine" in status.output

    stop = await asyncio.to_thread(runner.invoke, app, ["runs", "stop", run_dir.name])
    assert stop.exit_code == 0, stop.output
    assert f"Stopp angefordert für {run_dir.name}" in stop.output
    again = await asyncio.to_thread(runner.invoke, app, ["runs", "stop", run_dir.name])
    # The running process may already have consumed the first request while the run is still
    # being stopped (seen in CI, W2-05.3): then the second call puts a new one, which the
    # process consumes as well; or the run is already in `finalizing` or over.
    assert (
        "schon eine Stopp-Anforderung" in again.output
        or f"Stopp angefordert für {run_dir.name}" in again.output
        or "läuft nicht" in again.output
    ), again.output

    result = await asyncio.wait_for(running, 10)
    assert result.status is RunStatus.STOPPED
    assert "Stopp-Anforderung erhalten (arr runs stop): Der Run wird gestoppt …" in lines
    status = await asyncio.to_thread(runner.invoke, app, ["runs", "status", run_dir.name])
    assert "Status:    gestoppt (stopped)" in status.output
    assert "Grund:     Der User hat den Run gestoppt." in status.output
    assert "  a: 1 Turn, zuletzt unterbrochen" in status.output
    assert "Stopp angefordert" not in status.output
    assert not (run_dir / STOP_REQUEST).exists(), "no request stays behind (W2-05.3)"
    assert json.loads((run_dir / "meta.json").read_text())["status"] == "stopped"


def test_w4_00_1_arr_run_records_its_workfolder(m1: Path, arr_home: Path) -> None:
    """Punkt 1: a run of `arr run` puts its workfolder into the list of known workfolders,
    before the first agent starts; the recent list of the GUI stays as it was."""
    from arr.service.state import known_workfolders_file, recent_workfolders

    result = runner.invoke(app, ["run", "run.yaml"])
    assert result.exit_code == 0, result.output
    known = json.loads(known_workfolders_file().read_text("utf-8"))["workfolders"]
    assert known == [str(m1)]
    assert recent_workfolders() == [], "„zuletzt geöffnet“ is the service's"
    [run_dir] = run_dirs(m1)
    started = min(e["ts"] for e in log_entries(run_dir, "turn"))
    assert known_workfolders_file().stat().st_mtime <= started
