"""W4-01 A8 (Sperr-Probe): from the sandbox of both backends neither the port of the arr service
nor `~/.arr/service.json` can be reached.

Like `test_login_guard.py`: a test `HOME` (`login_guard_kit.open_lab`), the real Codex
sandbox (`codex sandbox -P arr` with the agent's real permission map) and the real Claude CLI
with the options of `build_options` against `FakeMessagesApi`. A real listener stands for the
service (on `127.0.0.1` and `[::1]`), and a `service.json` with a random marker as token lies
where `arr mcp` reads it. Only `reached` flags leave the probe.

Counter-tests: the same probe outside a sandbox reaches the file and the port; with the read
block of the home folder removed, the file is reached inside the sandbox too.
"""

import base64
import json
import secrets
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt exists on macOS only"),
]

PROBE = Path(__file__).with_name("service_lockout_probe.py")
SERVICE_ROWS = {
    "service.json: open",
    "service.json: cat",
    "service.json: Symlink",
    "service.json: Kopie",
}
PORT_ROWS = {"Port IPv4", "Port IPv6", "HTTP /mcp/"}


class Listener:
    """Something that answers like a service on `127.0.0.1` and `[::1]` of one port."""

    def __init__(self) -> None:
        self.v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.v4.bind(("127.0.0.1", 0))
        self.port = self.v4.getsockname()[1]
        self.v6: socket.socket | None = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            self.v6.bind(("::1", self.port))
        except OSError:
            self.v6.close()
            self.v6 = None
        self.stop = threading.Event()
        self.threads = [
            threading.Thread(target=self._serve, args=(s,), daemon=True) for s in self.sockets
        ]

    @property
    def sockets(self) -> list[socket.socket]:
        return [s for s in (self.v4, self.v6) if s is not None]

    def _serve(self, sock: socket.socket) -> None:
        sock.listen(8)
        sock.settimeout(0.2)
        while not self.stop.is_set():
            try:
                con, _ = sock.accept()
            except OSError:
                continue
            with con:
                con.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")

    def __enter__(self) -> "Listener":
        for thread in self.threads:
            thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=5)
        for sock in self.sockets:
            sock.close()


@contextmanager
def service_lab(provider: str) -> Iterator[tuple[Any, dict[str, Any]]]:
    import login_guard_kit as kit

    with kit.open_lab(provider) as lab, Listener() as listener:
        marker = f"ARR-SERVICE-TOKEN-{secrets.token_hex(12)}"
        service_json = lab.home / ".arr" / "service.json"
        service_json.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "host": "probe0123456789abcde.localhost",
            "port": listener.port,
            "token": marker,
            "pid": 1,
            "workfolder": str(lab.workfolder),
        }
        service_json.write_text(json.dumps(data))
        service_json.chmod(0o600)
        control = lab.agent.work_dir / "control.json"
        control.write_text(json.dumps({"token": marker}))
        plan = {
            "service_json": str(service_json),
            "control": str(control),
            "marker": marker,
            "port": listener.port,
            "host": f"{data['host']}:{listener.port}",
            "scratch": str(lab.agent.work_dir / "scratch"),
        }
        yield lab, plan


def setup_command(plan: dict[str, Any]) -> str:
    source = base64.b64encode(PROBE.read_bytes()).decode()
    data = base64.b64encode(json.dumps(plan).encode()).decode()
    probe = f"printf %s {source} | base64 -d > lockout.py"
    return f"{probe} && printf %s {data} | base64 -d > lockout.json"


def in_codex(lab: Any, plan: dict[str, Any], entries: dict[str, str] | None = None) -> dict:
    import login_guard_kit as kit

    real, env, cwd = kit.codex_entries(lab)
    entries = real if entries is None else entries
    kit.in_codex_sandbox(lab, entries, env, cwd, setup_command(plan))
    done = kit.in_codex_sandbox(lab, entries, env, cwd, "python3 lockout.py lockout.json")
    assert done.returncode == 0, done.stderr[-300:]
    return {row["way"]: row["reached"] for row in json.loads(done.stdout)}


def in_claude(lab: Any, plan: dict[str, Any], allow_home: bool = False) -> dict:
    import asyncio

    import login_guard_kit as kit
    from fake_messages_api import FakeMessagesApi

    script = [
        kit.bash(setup_command(plan), "Probe anlegen"),
        kit.bash("python3 lockout.py lockout.json > lockout-answer.json"),
    ]
    original = kit.build_options

    def options(*args: Any, **kwargs: Any) -> Any:
        built = original(*args, **kwargs)
        return kit.opened(built, {"path": str(lab.home / ".arr")}) if allow_home else built

    kit.build_options = options
    try:
        with FakeMessagesApi(script, credential=lab.token) as api:
            asyncio.run(asyncio.wait_for(kit._claude_turn(lab, api), 240))
    finally:
        kit.build_options = original
    answer = lab.agent.work_dir / "lockout-answer.json"
    return {row["way"]: row["reached"] for row in json.loads(answer.read_text())}


def outside(plan: dict[str, Any], folder: Path) -> dict:
    """The counter-test: the same probe without a sandbox."""
    (folder / "lockout.json").write_text(json.dumps(plan))
    done = subprocess.run(
        [sys.executable, str(PROBE), str(folder / "lockout.json")],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return {row["way"]: row["reached"] for row in json.loads(done.stdout)}


def reached(rows: dict[str, bool], names: set[str]) -> list[str]:
    return sorted(name for name in names if rows.get(name))


def test_the_probe_reaches_everything_outside_a_sandbox() -> None:
    with service_lab("codex") as (lab, plan):
        rows = outside(plan, lab.base)
    assert reached(rows, SERVICE_ROWS) == sorted(SERVICE_ROWS)
    assert rows["Port IPv4"] and rows["HTTP /mcp/"]


def test_codex_reaches_neither_port_nor_service_json() -> None:
    with service_lab("codex") as (lab, plan):
        rows = in_codex(lab, plan)
    assert rows["Kontrolle: open"], "the probe ran and can read its own folder"
    assert reached(rows, SERVICE_ROWS | PORT_ROWS) == []


def test_claude_reaches_neither_port_nor_service_json() -> None:
    with service_lab("claude") as (lab, plan):
        rows = in_claude(lab, plan)
    assert rows["Kontrolle: open"], "the probe ran and can read its own folder"
    assert reached(rows, SERVICE_ROWS | PORT_ROWS) == []


def test_a_missing_home_block_is_found_in_codex() -> None:
    import login_guard_kit as kit

    with service_lab("codex") as (lab, plan):
        entries = kit.codex_entries(lab)[0]
        entries[str(lab.home / ".arr")] = "read"
        rows = in_codex(lab, plan, entries)
    assert rows["service.json: open"]


def test_a_missing_home_block_is_found_in_claude() -> None:
    with service_lab("claude") as (lab, plan):
        rows = in_claude(lab, plan, allow_home=True)
    assert rows["service.json: open"]
