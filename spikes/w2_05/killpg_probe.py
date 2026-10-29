"""Offline probe for W2-05.1 point 7: a real Ctrl-C hits the whole process group.

    uv run python spikes/w2_05/killpg_probe.py [room|claude|codex|uv|all]

Which processes of an `arr run` sit in its process group, and what do they do when the group
gets SIGINT (`os.killpg`)? Each part starts real child processes the way the backends do, but
never calls a model: the room server (`python -m arr.room.server`), the Claude CLI through
`ClaudeSDKClient.connect()` (idle, with the room server as MCP server), the Codex app-server
through `AsyncCodex` (`thread/start`), and `uv run` with a Python child that handles SIGINT.
The probe moves itself into its own process group first, so the signal never reaches the
shell that started it. Throwaway code, never imported by arr.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

GOT_SIGINT: list[float] = []


def on_sigint(signum: int, frame: object) -> None:
    GOT_SIGINT.append(time.monotonic())


def group_members() -> list[str]:
    """`pid ppid comm` of every process in our process group (from `ps`)."""
    pgid = os.getpgrp()
    out = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,ppid=,comm="], capture_output=True, text=True, check=False
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[1] == str(pgid):
            rows.append(f"{parts[0]:>6} ppid {parts[2]:>6}  {Path(parts[3]).name}")
    return rows


def show(title: str) -> None:
    print(f"--- {title}: Prozessgruppe {os.getpgrp()}")
    for row in group_members():
        print("   ", row)


def fire() -> None:
    print(f"killpg(SIGINT) um {time.strftime('%H:%M:%S')}")
    os.killpg(os.getpgrp(), signal.SIGINT)


def make_room(tmp: Path) -> Path:
    """A run folder with a room and one channel for the room server."""
    from arr.models import GroupChatConfig
    from arr.room.store import RoomStore

    run_dir = tmp / "run"
    (run_dir / "agents" / "a" / "work").mkdir(parents=True)
    store = RoomStore.create(run_dir)
    store.setup_channels([GroupChatConfig(name="main", members=["a", "b"])])
    return run_dir


def room_env(run_dir: Path) -> dict[str, str]:
    from arr.room.server import AGENT_ID_ENV, RUN_DIR_ENV

    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG")}
    env[AGENT_ID_ENV] = "a"
    env[RUN_DIR_ENV] = str(run_dir)
    return env


def part_room(tmp: Path) -> None:
    """Room server as a direct child: does SIGINT end it?"""
    run_dir = make_room(tmp)
    proc = subprocess.Popen(
        [sys.executable, "-m", "arr.room.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=room_env(run_dir),
    )
    time.sleep(1.5)
    show("room: vor dem Signal")
    fire()
    time.sleep(1.0)
    print(f"room: returncode nach 1 s = {proc.poll()}")
    show("room: nach dem Signal")
    if proc.poll() is None:
        proc.terminate()
    _, err = proc.communicate(timeout=5)
    tail = err.decode("utf-8", "replace").strip().splitlines()[-3:]
    print("room: stderr-Ende:", *tail, sep="\n    ")


async def part_claude(tmp: Path) -> None:
    """Claude CLI (idle, connected, with the room MCP server) in the group."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    from arr.auth import clean_process_env

    clean_process_env()
    run_dir = make_room(tmp)
    cwd = run_dir / "agents" / "a" / "work"
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        mcp_servers={
            "arr-room": {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "arr.room.server"],
                "env": room_env(run_dir),
            }
        },
        stderr=lambda line: print("    claude stderr:", line[:160]),
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    await asyncio.sleep(4)  # let the CLI start its MCP servers
    show("claude: vor dem Signal")
    process = client._transport._process  # noqa: SLF001 - probe only
    print(f"claude: CLI-PID {process.pid}, pgid {os.getpgid(process.pid)}")
    fire()
    started = time.monotonic()
    try:  # at once, as the orchestrator's halt() does: does the CLI still answer?
        await asyncio.wait_for(client.interrupt(), 3)
        outcome = "ging durch"
    except BaseException as exc:  # noqa: BLE001 - we want the type
        outcome = f"→ {type(exc).__name__}"
    took = time.monotonic() - started
    print(f"claude: interrupt() sofort nach dem Signal {outcome} ({took:.2f} s)")
    print(f"claude: returncode der CLI jetzt = {process.returncode}")
    show("claude: nach dem Signal")
    try:
        await asyncio.wait_for(client.interrupt(), 5)
        print("claude: zweites interrupt() ging durch")
    except BaseException as exc:  # noqa: BLE001 - we want the type
        print(f"claude: zweites interrupt() → {type(exc).__name__}: {str(exc)[:200]}")
    try:
        await asyncio.wait_for(client.disconnect(), 10)
        print("claude: disconnect() ok")
    except BaseException as exc:  # noqa: BLE001
        print(f"claude: disconnect() → {type(exc).__name__}: {str(exc)[:200]}")
    await asyncio.sleep(1)
    show("claude: nach disconnect")


async def part_codex(tmp: Path) -> None:
    """Codex app-server (thread started, idle) in the group."""
    from openai_codex import AsyncCodex, CodexConfig

    from arr.backends.codex_config import codex_home_for
    from arr.backends.codex_sdk import start_thread
    from arr.models import AuthKind, AuthProfile

    name = os.environ.get("ARR_CODEX_PROFILE", "chatgpt-w2-02a")
    profile = AuthProfile(name=name, kind=AuthKind.CODEX_CHATGPT)
    home = codex_home_for(profile)
    if not (home / "auth.json").is_file():
        print("codex: Profil nicht angemeldet, Teil übersprungen")
        return
    cwd = tmp / "codex-work"
    cwd.mkdir()
    codex = AsyncCodex(CodexConfig(env={"CODEX_HOME": str(home)}))
    thread, started = await start_thread(
        codex,
        approvals=False,
        cwd=str(cwd),
        model="gpt-6-luna",
        developer_instructions="Sonde.",
        config={},
    )
    print(f"codex: Thread {started.thread.id[:8]}… gestartet (kein Modellaufruf)")
    await asyncio.sleep(2)
    show("codex: vor dem Signal")
    proc = codex._client._sync._proc  # noqa: SLF001 - probe only
    print(f"codex: app-server PID {proc.pid}, pgid {os.getpgid(proc.pid)}")
    fire()
    await asyncio.sleep(2)
    print(f"codex: returncode des app-servers nach 2 s = {proc.poll()}")
    show("codex: nach dem Signal")
    try:
        await asyncio.wait_for(thread.interrupt(), 5)
        print("codex: thread.interrupt() ging durch")
    except BaseException as exc:  # noqa: BLE001
        print(f"codex: thread.interrupt() → {type(exc).__name__}: {str(exc)[:200]}")
    try:
        await asyncio.wait_for(codex._client.close(), 10)  # noqa: SLF001
        print("codex: close() ok")
    except BaseException as exc:  # noqa: BLE001
        print(f"codex: close() → {type(exc).__name__}: {str(exc)[:200]}")


CHILD = """
import os, signal, sys, time
t0 = time.monotonic()
def on_int(*a):
    print(f"child: SIGINT nach {time.monotonic() - t0:.3f} s, arbeite weiter", flush=True)
signal.signal(signal.SIGINT, on_int)
print(f"child: läuft, PID {os.getpid()}, ppid {os.getppid()}, pgid {os.getpgrp()}", flush=True)
time.sleep(4)
print("child: fertig", flush=True)
sys.exit(7)
"""


def part_uv() -> None:
    """`uv run python …` in its own session: does uv survive SIGINT and wait for the child?"""
    proc = subprocess.Popen(
        ["uv", "run", "python", "-c", CHILD],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    time.sleep(2.5)
    print(f"uv: killpg({proc.pid}, SIGINT) um {time.strftime('%H:%M:%S')}")
    os.killpg(proc.pid, signal.SIGINT)
    out, _ = proc.communicate(timeout=20)
    print(f"uv: returncode {proc.returncode} (7 = Kind lief zu Ende und uv gab es weiter)")
    print("uv: Ausgabe:", *out.strip().splitlines(), sep="\n    ")


async def main(parts: list[str]) -> None:
    os.setpgid(0, 0)
    signal.signal(signal.SIGINT, on_sigint)
    print(f"Sonde PID {os.getpid()}, eigene Prozessgruppe {os.getpgrp()}")
    with tempfile.TemporaryDirectory(prefix="arr-killpg-") as tmp_name:
        tmp = Path(tmp_name)
        for part in parts:
            print(f"\n===== Teil {part} =====")
            GOT_SIGINT.clear()
            try:
                if part == "room":
                    part_room(tmp / "room")
                elif part == "claude":
                    (tmp / "claude").mkdir()
                    await part_claude(tmp / "claude")
                elif part == "codex":
                    (tmp / "codex").mkdir()
                    await part_codex(tmp / "codex")
                elif part == "uv":
                    part_uv()
            except Exception as exc:  # noqa: BLE001 - report and go on
                print(f"{part}: Fehler {type(exc).__name__}: {str(exc)[:300]}")
            print(f"{part}: die Sonde selbst bekam SIGINT {len(GOT_SIGINT)}×")


if __name__ == "__main__":
    wanted = sys.argv[1:] or ["all"]
    if wanted == ["all"]:
        wanted = ["room", "claude", "codex", "uv"]
    asyncio.run(main(wanted))
