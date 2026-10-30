"""Claude login without a terminal (E18): `claude setup-token` in a pseudo-terminal.

`claude setup-token` draws a terminal UI, opens the browser and, after the sign-in, prints a
long-lived token (it stores nothing itself, W3-05 §7). `capture_token` runs the CLI of the
`claude-agent-sdk` in a pty, scans the output for the token and hands it to a `sink` (the
keychain). The rules of docs/01 §8 hold for every byte of that output:

- The output is kept only in a short rolling buffer and never written or logged. A failure
  is reported as a fixed `CaptureOutcome`, never with text of the CLI.
- The token leaves this module only through `sink`; `CaptureResult` holds its length.
- The child gets a clean environment (`clean_process_env`) and its own empty
  `CLAUDE_CONFIG_DIR`, so `~/.claude` of the user is not touched.

The child can never outlive its limit: it is the session leader with the pty as controlling
terminal, so the end of the service, however hard, closes the master and the kernel sends
SIGHUP; `alarm` (kept across `exec`) ends it at the deadline even then. A cancelled
`capture_token` stops it, and `asyncio.run` cancels the task when the service ends.

See docs/04-service-gui-und-schnittstellen.md §5, docs/08-ux-konzept.md G4.
"""

import asyncio
import contextlib
import fcntl
import logging
import os
import re
import signal
import struct
import subprocess
import sys
import tempfile
import termios
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from arr.auth import clean_process_env

log = logging.getLogger("arr.service")

SETUP_TOKEN_NOTE = "arr führt dafür claude setup-token aus."
CLAUDE_LOGIN_TIMEOUT_S = 300.0

PTY_COLUMNS = 1000
"""Wide enough that the terminal UI never breaks the token over two lines."""
PTY_ROWS = 50
OUTPUT_KEEP = 32 * 1024
"""Bytes of output kept for the scan; older output is dropped at once."""
ALARM_GRACE_S = 5
STOP_WAIT_S = 2.0
DRAIN_S = 0.5
POLL_S = 0.05

_TOKEN = re.compile(rb"sk-ant-oat[0-9]{2}-[A-Za-z0-9_-]{20,}(?=[^A-Za-z0-9_-])")
_ANSI = re.compile(
    rb"\x1b\[[0-9;:<=>?]*[ -/]*[@-~]"  # CSI
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    rb"|\x1b[@-Z\\^_]"  # two-byte escapes
)

_CHILD = """\
import fcntl, os, signal, sys, termios
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
signal.alarm(int(sys.argv[1]))
os.execv(sys.argv[2], sys.argv[2:])
"""
"""Runs between `setsid` and the CLI: the pty becomes the controlling terminal, the deadline
is armed. A separate interpreter, because forking code must not run in a threaded service."""


class CaptureOutcome(StrEnum):
    """How a capture ended; the fixed vocabulary for messages and logs."""

    TOKEN = "token"
    NO_TOKEN = "no_token"
    TIMEOUT = "timeout"
    START_FAILED = "start_failed"
    STORE_FAILED = "store_failed"


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """Result without the token: outcome, its length and the exit code of the CLI."""

    outcome: CaptureOutcome
    token_length: int = 0
    exit_code: int | None = None


def claude_cli() -> Path:
    """The CLI bundled with `claude-agent-sdk`."""
    import claude_agent_sdk

    return Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"


def login_env(config_dir: str) -> dict[str, str]:
    """Environment of the login process: clean, with a config folder of its own."""
    env = dict(os.environ)
    clean_process_env(env)
    env |= {
        "CLAUDE_CONFIG_DIR": config_dir,
        "TERM": "xterm-256color",
        "COLUMNS": str(PTY_COLUMNS),
        "LINES": str(PTY_ROWS),
    }
    return env


class TokenScanner:
    """Finds the token in the raw output of a terminal UI, keeping only a short tail."""

    def __init__(self) -> None:
        self._tail = bytearray()

    def feed(self, chunk: bytes) -> str | None:
        """Add output; returns the token as soon as it is complete in the output."""
        self._tail += chunk
        del self._tail[:-OUTPUT_KEEP]
        found = _TOKEN.search(_ANSI.sub(b"", bytes(self._tail)))
        if found is None:
            return None
        self.clear()
        return found.group().decode("ascii")

    def clear(self) -> None:
        self._tail[:] = bytes(len(self._tail))
        self._tail.clear()


def _spawn(
    cli: Path, args: tuple[str, ...], cwd: str, env: dict[str, str], limit_s: float
) -> tuple[subprocess.Popen[bytes], int]:
    master, slave = os.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLUMNS, 0, 0))
        os.set_blocking(master, False)
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                _CHILD,
                str(int(limit_s) + ALARM_GRACE_S),
                str(cli),
                *args,
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    return process, master


def _signal_group(process: subprocess.Popen[bytes], signum: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signum)


async def stop_process(process: subprocess.Popen[bytes]) -> None:
    """End the child's process group: SIGTERM, after `STOP_WAIT_S` SIGKILL. Always reaps it."""
    try:
        if process.poll() is None:
            _signal_group(process, signal.SIGTERM)
            for _ in range(int(STOP_WAIT_S / POLL_S)):
                if process.poll() is not None:
                    break
                await asyncio.sleep(POLL_S)
    finally:
        _signal_group(process, signal.SIGKILL)
        process.wait()


async def capture_token(
    sink: Callable[[str], None],
    *,
    timeout_s: float = CLAUDE_LOGIN_TIMEOUT_S,
    cli: Path | None = None,
    args: tuple[str, ...] = ("setup-token",),
) -> CaptureResult:
    """Run `claude setup-token` in a pty and pass the token to `sink` in a worker thread.

    A keychain dialog in `sink` therefore never blocks the event loop. Cancelling the call
    stops the child; `sink` is not called after that.
    """
    loop = asyncio.get_running_loop()
    scanner = TokenScanner()
    closed = asyncio.Event()
    found: list[str] = []
    with tempfile.TemporaryDirectory(prefix="arr-login-") as workdir:
        try:
            process, master = _spawn(
                cli or claude_cli(), args, workdir, login_env(workdir), timeout_s
            )
        except OSError as exc:
            log.warning("Claude-Anmeldung: Start fehlgeschlagen (%s)", type(exc).__name__)
            return CaptureResult(CaptureOutcome.START_FAILED)

        def readable() -> None:
            try:
                chunk = os.read(master, 65536)
            except BlockingIOError:
                return
            except OSError:  # EIO: the last writer closed the terminal
                chunk = b""
            if not chunk:
                loop.remove_reader(master)
                closed.set()
                return
            token = scanner.feed(chunk)
            if token is not None:
                found.append(token)
                loop.remove_reader(master)
                closed.set()

        async def ended() -> None:
            while not closed.is_set() and process.poll() is None:
                await asyncio.sleep(POLL_S)
            # A helper of the CLI may keep the terminal open: read what is left, then go on.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(closed.wait(), DRAIN_S)

        loop.add_reader(master, readable)
        try:
            try:
                await asyncio.wait_for(ended(), timeout_s)
            except TimeoutError:
                return CaptureResult(CaptureOutcome.TIMEOUT)
            if not found:
                return CaptureResult(CaptureOutcome.NO_TOKEN, exit_code=process.poll())
            token = found.pop()
            try:
                await asyncio.to_thread(sink, token)
            except Exception as exc:  # noqa: BLE001 - the type only, a message could quote it
                log.warning("Claude-Anmeldung: Ablage fehlgeschlagen (%s)", type(exc).__name__)
                return CaptureResult(CaptureOutcome.STORE_FAILED)
            return CaptureResult(CaptureOutcome.TOKEN, token_length=len(token))
        finally:
            found.clear()
            scanner.clear()
            loop.remove_reader(master)
            try:
                await stop_process(process)
            finally:
                os.close(master)
