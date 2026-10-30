"""`arr serve` as a real process (W3-01 A1): service.json with mode 0600, a second start
finds the running service, Ctrl-C stops the service's runs with finalizer and secret scan, a
second Ctrl-C aborts hard, leftover runs become `interrupted` at start, German errors."""

import json
import signal
import socket
import stat
import time
from pathlib import Path

import pytest
from run_kit import say, turns, wait, write_profiles
from service_kit import (
    free_port,
    make_workfolder,
    read_meta,
    run_dirs,
    start_service,
    wait_for,
)

from arr.errors import ArrError
from arr.service import serve as serve_module
from arr.service.serve import resolve_workfolder, serve
from arr.service.state import read_service_info, recent_workfolders

RUN_INPUT = {
    "project": "demo",
    "task": "Teste arr.",
    "topology": {"kind": "solo"},
    "defaults": {"provider": "claude", "model": "fake-model", "auth": "fake-auth"},
    "agents": [{"id": "a"}],
    "groupchats": [{"name": "main", "members": ["a"]}],
}


@pytest.fixture
def home(arr_home: Path) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    return arr_home


def test_start_writes_service_json_0600_remembers_the_workfolder_and_a_second_start_finds_it(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workfolder = make_workfolder(tmp_path)
    service = start_service(tmp_path, home, workfolder)
    try:
        file = home / "service.json"
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
        info = json.loads(file.read_text())
        assert info["pid"] == service.process.pid and info["port"] == service.port
        assert info["workfolder"] == str(workfolder) and info["token"]
        assert recent_workfolders()[0] == workfolder
        # a second `arr serve` in this process: finds the service, opens nothing, ends
        lines: list[str] = []
        serve(workfolder=workfolder, open_browser=False, report=lines.append)
        assert any("läuft schon" in line for line in lines), lines
        assert any(service.token in line for line in lines)
        assert json.loads(file.read_text())["token"] == info["token"]
        # without --workfolder the last one is used
        assert resolve_workfolder(None) == workfolder
    finally:
        service.stop()
    assert not (home / "service.json").exists(), "service.json is removed at the end"


def test_ctrl_c_stops_the_services_runs_with_finalizer_and_scan(tmp_path: Path, home: Path) -> None:
    workfolder = make_workfolder(tmp_path)
    scripts = {"a": turns([say("los"), wait(120)])}
    service = start_service(tmp_path, home, workfolder, scripts=scripts, autostart=RUN_INPUT)
    try:
        wait_for(lambda: bool(run_dirs(workfolder)), what="run folder")
        [run_dir] = run_dirs(workfolder)
        wait_for(lambda: read_meta(run_dir)["status"] == "running", what="running")
        assert read_meta(run_dir)["starter"] == "gui"
        service.signal(signal.SIGINT)
        code = service.wait(timeout=90)
    finally:
        service.stop()
    assert code == 0, service.output()
    meta = read_meta(run_dir)
    assert meta["status"] == "stopped", meta
    assert (run_dir / "output" / "summary.md").is_file(), "finalizer ran"
    kinds = [
        json.loads(line)["kind"]
        for line in (run_dir / "orchestrator.jsonl").read_text().splitlines()
    ]
    assert "secret_scan" in kinds, kinds
    assert not (run_dir / "stop.request").exists()
    assert not (home / "service.json").exists()
    assert "Strg-C" in service.output()


def test_second_ctrl_c_aborts_hard_with_interrupted(tmp_path: Path, home: Path) -> None:
    workfolder = make_workfolder(tmp_path)
    scripts = {"a": turns([say("los"), {"do": "hang", "seconds": 120}])}
    service = start_service(tmp_path, home, workfolder, scripts=scripts, autostart=RUN_INPUT)
    try:
        wait_for(lambda: bool(run_dirs(workfolder)), what="run folder")
        [run_dir] = run_dirs(workfolder)
        wait_for(lambda: read_meta(run_dir)["status"] == "running", what="running")
        service.signal(signal.SIGINT)
        time.sleep(1.5)  # beyond the repeat window: the second signal counts
        service.signal(signal.SIGINT)
        code = service.wait(timeout=60)
    finally:
        service.stop()
    assert code == 130, service.output()
    assert read_meta(run_dir)["status"] == "interrupted"
    entries = [
        json.loads(line) for line in (run_dir / "orchestrator.jsonl").read_text().splitlines()
    ]
    assert any(e["kind"] == "status" and e["data"].get("previous") == "hard_abort" for e in entries)
    assert "harter Abbruch" in service.output()


def test_sigterm_ends_the_service_like_ctrl_c(tmp_path: Path, home: Path) -> None:
    workfolder = make_workfolder(tmp_path)
    service = start_service(tmp_path, home, workfolder)
    try:
        service.signal(signal.SIGTERM)
        assert service.wait(timeout=60) == 0, service.output()
    finally:
        service.stop()
    assert not (home / "service.json").exists()


def test_start_marks_leftover_runs_interrupted(tmp_path: Path, home: Path) -> None:
    workfolder = make_workfolder(tmp_path)
    stale = workfolder / "runs" / "20261029-1200_demo_custom_dead"
    stale.mkdir(parents=True)
    (stale / "meta.json").write_text(
        json.dumps(
            {
                "run_id": stale.name,
                "status": "running",
                "starter": "cli",
                "project": "demo",
                "created": time.time() - 600,
                "started": time.time() - 500,
                "arr_version": "0",
            }
        )
    )
    service = start_service(tmp_path, home, workfolder)
    try:
        wait_for(lambda: read_meta(stale)["status"] == "interrupted", what="interrupted")
        assert "unterbrochen" in service.output()
    finally:
        service.stop()


def test_serve_without_a_known_workfolder_says_the_way(home: Path) -> None:
    with pytest.raises(ArrError) as error:
        serve(open_browser=False, report=lambda line: None)
    assert "--workfolder" in str(error.value) and "arr init" in str(error.value)


def test_serve_refuses_a_port_another_program_uses(tmp_path: Path, home: Path) -> None:
    workfolder = make_workfolder(tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        with pytest.raises(ArrError) as error:
            serve(workfolder=workfolder, port=port, open_browser=False, report=lambda line: None)
    assert str(port) in str(error.value) and "--port" in str(error.value)


def test_stale_service_json_is_ignored(tmp_path: Path, home: Path) -> None:
    (home / "service.json").write_text(
        json.dumps(
            {
                "host": "abcdefghijklmnopqrst.localhost",
                "port": free_port(),
                "token": "x",
                "pid": 999999,
                "started": 0,
                "workfolder": "/",
            }
        )
    )
    assert serve_module.running_service() is None
    assert read_service_info() is not None  # the file is still there, just not alive


def test_foreign_server_on_the_old_port_is_not_taken_for_arr(tmp_path: Path, home: Path) -> None:
    """W3-01.1 G3: a stale `service.json` whose PID lives on (another program) and whose port
    answers 200 from some other server is no running arr; the token link never goes there."""
    import http.server
    import os
    import threading

    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            seen.append(self.path)
            body = b"not arr"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    foreign = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=foreign.serve_forever, daemon=True)
    thread.start()
    try:
        port = foreign.server_address[1]
        (home / "service.json").write_text(
            json.dumps(
                {
                    "host": "abcdefghijklmnopqrst.localhost",
                    "port": port,
                    "token": "old-token",
                    "pid": os.getpid(),
                    "started": 0,
                    "workfolder": "/",
                }
            )
        )
        assert not serve_module.answers_as_arr(port, "abcdefghijklmnopqrst.localhost")
        assert serve_module.running_service() is None
        workfolder = make_workfolder(tmp_path)
        lines: list[str] = []
        with pytest.raises(ArrError) as error:  # the old port is taken by that program
            serve(workfolder=workfolder, port=port, open_browser=False, report=lines.append)
        assert "nicht von arr" in str(error.value)
        assert not any("läuft schon" in line for line in lines)
        assert not any("old-token" in path for path in seen), seen
    finally:
        foreign.shutdown()
        foreign.server_close()
        thread.join(5)


def test_cli_serve_passes_the_options(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from arr.cli import app

    calls: list[dict[str, object]] = []
    monkeypatch.setattr("arr.service.serve.serve", lambda **kwargs: calls.append(kwargs) or None)
    result = CliRunner().invoke(app, ["serve", "--port", "9999", "--no-browser", "-w", "/tmp"])
    assert result.exit_code == 0, result.output
    assert calls == [{"workfolder": Path("/tmp"), "port": 9999, "open_browser": False}]
    monkeypatch.setattr(
        "arr.service.serve.serve",
        lambda **kwargs: (_ for _ in ()).throw(ArrError("Kein Workfolder")),
    )
    result = CliRunner().invoke(app, ["serve"])
    assert result.exit_code == 1 and "Kein Workfolder" in result.output
    assert "Traceback" not in result.output


def test_gui_without_mock_points_to_serve() -> None:
    from arr.service.gui import run

    with pytest.raises(ArrError) as error:
        run(mock=False)
    assert "arr serve" in str(error.value)


# --- W3-01.3 N3: a closed standard output never prevents the stop ------------------------------


def _log_kinds(run_dir: Path) -> list[str]:
    lines = (run_dir / "orchestrator.jsonl").read_text().splitlines()
    return [json.loads(line)["kind"] for line in lines]


def test_ctrl_c_stops_even_with_closed_standard_output(tmp_path: Path, home: Path) -> None:
    """`arr serve | tee log` whose tee is gone: the signal must still stop the run (N3)."""
    workfolder = make_workfolder(tmp_path)
    scripts = {"a": turns([say("los"), wait(120)])}
    service = start_service(
        tmp_path, home, workfolder, scripts=scripts, autostart=RUN_INPUT, pipe_output=True
    )
    try:
        wait_for(lambda: bool(run_dirs(workfolder)), what="run folder")
        [run_dir] = run_dirs(workfolder)
        wait_for(lambda: read_meta(run_dir)["status"] == "running", what="running")
        service.close_reader()
        service.signal(signal.SIGINT)
        code = service.wait(timeout=30)
    finally:
        service.stop()
    assert code == 0
    assert read_meta(run_dir)["status"] == "stopped"
    assert (run_dir / "output" / "summary.md").is_file(), "finalizer ran"
    assert "secret_scan" in _log_kinds(run_dir)
    assert not (run_dir / "stop.request").exists()
    assert not (home / "service.json").exists()


def test_second_ctrl_c_aborts_hard_even_with_closed_standard_output(
    tmp_path: Path, home: Path
) -> None:
    workfolder = make_workfolder(tmp_path)
    scripts = {"a": turns([say("los"), {"do": "hang", "seconds": 120}])}
    service = start_service(
        tmp_path, home, workfolder, scripts=scripts, autostart=RUN_INPUT, pipe_output=True
    )
    try:
        wait_for(lambda: bool(run_dirs(workfolder)), what="run folder")
        [run_dir] = run_dirs(workfolder)
        wait_for(lambda: read_meta(run_dir)["status"] == "running", what="running")
        service.close_reader()
        service.signal(signal.SIGINT)
        time.sleep(1.5)
        service.signal(signal.SIGINT)
        code = service.wait(timeout=30)
    finally:
        service.stop()
    assert code == 130
    assert read_meta(run_dir)["status"] == "interrupted"
    lines = (run_dir / "orchestrator.jsonl").read_text().splitlines()
    entries = [json.loads(line) for line in lines]
    assert any(e["kind"] == "status" and e["data"].get("previous") == "hard_abort" for e in entries)
    assert not (home / "service.json").exists()
