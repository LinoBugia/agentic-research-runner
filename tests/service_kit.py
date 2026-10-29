"""Helpers for the service tests (W3-01): a service in a real child process, an `arr run`
in a second process, hand-made run folders. Imported by `test_service*.py`,
`test_real_api*.py` and `test_gui_real.py`."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from run_kit import FAST, make_run_input, make_settings
from run_kit import make_workfolder as _make_workfolder

from arr.models import RunStatus
from arr.resolve import resolve_run
from arr.room.store import RoomStore
from arr.runs.inspect import RunLock
from arr.runs.manager import set_run_status
from arr.runs.prepare import prepare_run
from arr.service.guard import COOKIE
from arr.service.state import ServiceInfo

TESTS_DIR = Path(__file__).parent
CHILD = TESTS_DIR / "service_child.py"
RUN_CHILD = TESTS_DIR / "run_child.py"
START_TIMEOUT_S = 40.0
"""The child imports NiceGUI and starts uvicorn; CI machines are slow (W2-05.3)."""


def _clean_env() -> dict[str, str]:
    """The environment for a child without pytest's marks (NiceGUI would think it is a
    screen test and expect `NICEGUI_SCREEN_TEST_PORT`)."""
    return {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_workfolder(root: Path) -> Path:
    """`run_kit.make_workfolder` with the run venv off (FakeBackend runs)."""
    workfolder = _make_workfolder(root)
    (workfolder / "arr.yaml").write_text("version: 1\ntoolchain: {run_venv: off}\n", "utf-8")
    return workfolder


def write_json(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


class ServiceProcess:
    """`arr serve` as a child process on a free port, with FakeBackend builders.

    The service answers only under its own hostname `<random>.localhost` (W3-01.1 M1).
    Clients of the tests connect to `127.0.0.1` and send that name in `Host`, so no test
    depends on the resolver of the machine (`client`, `ws_connect`).
    """

    def __init__(self, process: subprocess.Popen[str], port: int, arr_home: Path, log: Path):
        self.process = process
        self.port = port
        self.arr_home = arr_home
        self.log = log
        self.info: ServiceInfo | None = None

    @property
    def hostname(self) -> str:
        assert self.info is not None
        return self.info.host

    @property
    def host(self) -> str:
        """`<hostname>:<port>`, the only `Host` the service answers."""
        return f"{self.hostname}:{self.port}"

    @property
    def url(self) -> str:
        """The service's own URL (resolves through `*.localhost`)."""
        return f"http://{self.host}"

    @property
    def loopback_url(self) -> str:
        """The address the tests connect to, with `Host` set separately."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def token(self) -> str:
        assert self.info is not None
        return self.info.token

    def client(self, *, cookie: bool = False, host: str | None = None, **kwargs: Any) -> Any:
        """An `httpx.Client` to the loopback address with the service's `Host` (or `host`)."""
        import httpx

        headers = {"Host": host or self.host, **kwargs.pop("headers", {})}
        cookies = {COOKIE: self.token} if cookie else None
        return httpx.Client(
            base_url=self.loopback_url,
            headers=headers,
            cookies=cookies,
            follow_redirects=False,
            timeout=10.0,
            **kwargs,
        )

    def ws_connect(self, headers: Mapping[str, str] | None = None, host: str | None = None) -> Any:
        """Open the Socket.IO websocket over a socket to the loopback address; the handshake
        carries the service's hostname (or `host`) in `Host`."""
        from websockets.sync.client import connect

        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        uri = f"ws://{host or self.host}/_nicegui_ws/socket.io/?EIO=4&transport=websocket"
        return connect(uri, sock=sock, additional_headers=dict(headers or {}), open_timeout=10)

    def output(self) -> str:
        try:
            return self.log.read_text("utf-8", errors="replace")
        except OSError:
            return ""

    def signal(self, sig: int = signal.SIGINT) -> None:
        """Send a signal to the whole process group, like a Ctrl-C in a terminal."""
        os.killpg(os.getpgid(self.process.pid), sig)

    def wait(self, timeout: float = 60.0) -> int:
        return self.process.wait(timeout=timeout)

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                self.signal(signal.SIGTERM)
                self.process.wait(timeout=30)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=10)


def start_service(
    tmp_path: Path,
    arr_home: Path,
    workfolder: Path,
    *,
    scripts: Mapping[str, Any] | None = None,
    autostart: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    port: int | None = None,
    wait_ready: bool = True,
) -> ServiceProcess:
    """Start the service child; `scripts` are FakeBackend scripts by agent ID, `autostart` a
    run input the child starts right after startup."""
    port = port or free_port()
    log = tmp_path / f"service-{port}.log"
    child_env = {
        **_clean_env(),
        "ARR_HOME": str(arr_home),
        "ARR_TEST_PORT": str(port),
        "ARR_TEST_WORKFOLDER": str(workfolder),
        "PYTHONUNBUFFERED": "1",
        **(env or {}),
    }
    if scripts is not None:
        scripts_file = write_json(tmp_path / f"scripts-{port}.json", scripts)
        child_env["ARR_TEST_SCRIPTS"] = str(scripts_file)
    if autostart is not None:
        start_file = write_json(tmp_path / f"start-{port}.json", autostart)
        child_env["ARR_TEST_AUTOSTART"] = str(start_file)
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(CHILD)],
        env=child_env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    handle.close()
    service = ServiceProcess(process, port, arr_home, log)
    if wait_ready:
        wait_for_service(service)
    return service


def wait_for_service(service: ServiceProcess, timeout: float = START_TIMEOUT_S) -> ServiceInfo:
    """Wait until `service.json` exists and the port answers."""
    end = time.monotonic() + timeout
    file = service.arr_home / "service.json"
    while time.monotonic() < end:
        if service.process.poll() is not None:
            raise AssertionError(f"service child ended early:\n{service.output()}")
        if file.is_file():
            try:
                info = ServiceInfo.model_validate_json(file.read_text("utf-8"))
            except ValueError:
                info = None
            if info is not None and port_answers(info.port):
                service.info = info
                return info
        time.sleep(0.1)
    raise AssertionError(f"service not ready within {timeout}s:\n{service.output()}")


def port_answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for(check: Any, timeout: float = 20.0, interval: float = 0.05, what: str = "") -> None:
    """Poll `check()` until true; fail after `timeout` seconds (blocking, for process tests)."""
    end = time.monotonic() + timeout
    while not check():
        if time.monotonic() > end:
            raise AssertionError(f"Bedingung nicht rechtzeitig erfüllt: {what}")
        time.sleep(interval)


def read_meta(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "meta.json").read_text("utf-8"))


def run_dirs(workfolder: Path) -> list[Path]:
    return sorted(p for p in (workfolder / "runs").iterdir() if (p / "meta.json").is_file())


def start_run_process(
    tmp_path: Path, arr_home: Path, run_yaml: Path, scripts: Mapping[str, Any]
) -> subprocess.Popen[str]:
    """`arr run <run.yaml>` with FakeBackends in its own process group (signals like a TTY)."""
    log = tmp_path / f"run-{run_yaml.stem}.log"
    scripts_file = write_json(tmp_path / f"run-scripts-{run_yaml.stem}.json", scripts)
    env = {
        **_clean_env(),
        "ARR_HOME": str(arr_home),
        "ARR_TEST_SCRIPTS": str(scripts_file),
        "PYTHONUNBUFFERED": "1",
    }
    handle = log.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(RUN_CHILD), str(run_yaml)],
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
        cwd=str(run_yaml.parent),
    )
    handle.close()
    return process


# --------------------------------------------------------------------------------------------
# Hand-made run folders (no process): prepared like a real run, status as wanted
# --------------------------------------------------------------------------------------------


def make_run_folder(
    workfolder: Path,
    *,
    agents: list[str] | None = None,
    status: RunStatus = RunStatus.RUNNING,
    run_id: str | None = None,
    channels: list[dict[str, Any]] | None = None,
    name: str | None = None,
) -> Path:
    """A real run folder (`prepare_run` + room with channels) in the given status."""
    agents = agents or ["a"]
    channels = channels if channels is not None else [{"name": "main", "members": list(agents)}]
    extra: dict[str, Any] = {}
    if name:
        extra["name"] = name
    run_input = make_run_input([{"id": a} for a in agents], channels, **extra)
    settings = make_settings()
    resolved = resolve_run(run_input, None, settings)
    prepared = prepare_run(workfolder, run_input, resolved, settings, run_id=run_id)
    room = RoomStore.create(prepared.run_dir)
    room.setup_channels(resolved.groupchats, {})
    set_run_status(prepared.run_dir, status)
    return prepared.run_dir


def hold_lock(run_dir: Path) -> RunLock:
    """Hold `run.lock` like the process that executes the run; without it the service marks
    an active folder `interrupted` at start (W2-05.1)."""
    lock = RunLock(run_dir)
    lock.acquire()
    return lock


def transcript_line(
    seq: int,
    agent: str,
    run_id: str,
    kind: str,
    data: dict[str, Any],
    *,
    turn: int = 1,
    ts: float | None = None,
) -> str:
    record = {
        "seq": seq,
        "ts": time.time() if ts is None else ts,
        "run": run_id,
        "agent": agent,
        "turn": turn,
        "kind": kind,
        "data": data,
    }
    return json.dumps(record, ensure_ascii=False)


def append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()


__all__ = [
    "FAST",
    "ServiceProcess",
    "append",
    "free_port",
    "hold_lock",
    "make_run_folder",
    "make_workfolder",
    "read_meta",
    "run_dirs",
    "start_run_process",
    "start_service",
    "transcript_line",
    "wait_for",
    "wait_for_service",
]
