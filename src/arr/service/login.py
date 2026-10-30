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
`capture_token` stops it as well. On macOS the CLI stays in its exit until the terminal is
closed, so the terminal is read until the stop and the wait for the exit comes after the close
and never blocks the event loop.

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
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any

from arr.auth import clean_process_env, store_secret
from arr.backends.codex_login import codex_login
from arr.models import AuthKind, AuthProfile
from arr.service.api_models import (
    Action,
    ApiException,
    ErrorCode,
    LoginStart,
    LoginState,
    LoginStatus,
    ProfileInfo,
)

log = logging.getLogger("arr.service")

SETUP_TOKEN_NOTE = "arr führt dafür claude setup-token aus."
CODEX_NOTE = "arr meldet Codex im Login-Ordner des Profils an."
WAITING_TEXT = "Warte auf die Anmeldung im Browser …"
CLAUDE_LOGIN_TIMEOUT_S = 300.0
CODEX_LOGIN_TIMEOUT_S = 900.0
"""Codex lets a device code expire after 15 minutes."""
CODEX_START_TIMEOUT_S = 30.0

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


CLAUDE_FAILURES = {
    CaptureOutcome.NO_TOKEN: (
        "Die Claude-Anmeldung wurde abgebrochen. Versuche es erneut oder nimm einen Claude-API-Key."
    ),
    CaptureOutcome.TIMEOUT: (
        "Die Claude-Anmeldung ist abgelaufen. Versuche es erneut oder nimm einen Claude-API-Key."
    ),
    CaptureOutcome.START_FAILED: (
        "Die Claude-Anmeldung ließ sich nicht starten. Versuche es erneut oder nimm einen "
        "Claude-API-Key."
    ),
    CaptureOutcome.STORE_FAILED: (
        "Das Token ließ sich nicht im Schlüsselbund ablegen. Erlaube den Zugriff und versuche "
        "es erneut."
    ),
}
CODEX_FAILED = "Die ChatGPT-Anmeldung hat nicht geklappt. Versuche es erneut."
CODEX_EXPIRED = "Die ChatGPT-Anmeldung ist abgelaufen. Melde dich neu an."


def cancelled_text(kind: AuthKind) -> str:
    if kind is AuthKind.CLAUDE_OAUTH:
        return CLAUDE_FAILURES[CaptureOutcome.NO_TOKEN]
    return "Die ChatGPT-Anmeldung wurde abgebrochen. Versuche es erneut."


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


async def _ended(process: subprocess.Popen[bytes], wait_s: float) -> bool:
    """Wait without blocking the event loop; True once the process is gone."""
    for _ in range(int(wait_s / POLL_S)):
        if process.poll() is not None:
            return True
        await asyncio.sleep(POLL_S)
    return process.poll() is not None


async def stop_process(process: subprocess.Popen[bytes]) -> None:
    """End the child's process group: SIGTERM, after `STOP_WAIT_S` SIGKILL."""
    if process.poll() is None:
        _signal_group(process, signal.SIGTERM)
        await _ended(process, STOP_WAIT_S)
    _signal_group(process, signal.SIGKILL)


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

        def discard() -> None:
            """Read on and forget: the CLI writes while it stops and must not block on it."""
            try:
                if not os.read(master, 65536):
                    loop.remove_reader(master)
            except BlockingIOError:
                pass
            except OSError:
                loop.remove_reader(master)

        async def ended() -> None:
            while not closed.is_set() and process.poll() is None:  # noqa: ASYNC110 - a child
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
            loop.add_reader(master, discard)
            try:
                await stop_process(process)
            finally:
                _signal_group(process, signal.SIGKILL)
                loop.remove_reader(master)
                os.close(master)
            # The CLI stays in its exit until the terminal is gone (macOS), so only now.
            if not await _ended(process, STOP_WAIT_S):
                log.warning("Claude-Anmeldung: Der Prozess endet verzögert.")


# --------------------------------------------------------------------------------------------
# Logins of the service
# --------------------------------------------------------------------------------------------


@dataclass(eq=False)
class LoginJob:
    """One running or finished login; holds what the page polls, never a secret."""

    profile: AuthProfile
    expires_at: float
    state: LoginState = LoginState.WAITING
    message: str | None = WAITING_TEXT
    action: Action | None = None
    info: ProfileInfo | None = None
    task: asyncio.Task[None] | None = None

    def finish(
        self,
        state: LoginState,
        message: str,
        *,
        action: Action | None = None,
        info: ProfileInfo | None = None,
    ) -> None:
        self.state, self.message, self.action, self.info = state, message, action, info


AfterLogin = Callable[[AuthProfile], Awaitable[ProfileInfo]]
"""Called after a successful login: notes the secret, tests and returns the profile."""


class LoginService:
    """Runs one login per profile as a task with a deadline (docs/08 §5.8, E18).

    A new start replaces a running login of the profile. Every message is a fixed text: no
    output of the CLI or Codex reaches the page.
    """

    def __init__(
        self,
        after: AfterLogin,
        *,
        poll_url: Callable[[str], str],
        codex_factory: Any = None,
    ) -> None:
        self.after = after
        self.poll_url = poll_url
        self.codex_factory = codex_factory
        self.jobs: dict[str, LoginJob] = {}

    async def start(self, profile: AuthProfile) -> LoginStart:
        await self.cancel(profile.name)
        if profile.kind is AuthKind.CODEX_CHATGPT:
            return await self._start_codex(profile)
        now = time.time()
        job = LoginJob(profile, expires_at=now + CLAUDE_LOGIN_TIMEOUT_S)
        self.jobs[profile.name] = job
        job.task = asyncio.create_task(self._claude(job), name=f"arr-login-{profile.name}")
        return LoginStart(
            profile=profile.name,
            method="browser",
            poll_url=self.poll_url(profile.name),
            expires_at=job.expires_at,
            note=SETUP_TOKEN_NOTE,
        )

    async def _after(self, job: LoginJob) -> None:
        try:
            info = await self.after(job.profile)
        except Exception as exc:  # noqa: BLE001 - the login itself worked
            log.warning("Test nach der Anmeldung: %s", type(exc).__name__)
            job.finish(LoginState.DONE, "Angemeldet. Teste das Profil auf seiner Karte.")
            return
        job.finish(LoginState.DONE, "Angemeldet und getestet.", info=info)

    def _retry(self, job: LoginJob, service: str) -> Action:
        return Action(label=f"Erneut mit {service} anmelden", url=f"/profile/{job.profile.name}")

    async def _claude(self, job: LoginJob) -> None:
        store = partial(store_secret, job.profile)
        try:
            result = await capture_token(store, timeout_s=CLAUDE_LOGIN_TIMEOUT_S)
        except asyncio.CancelledError:
            job.finish(LoginState.CANCELLED, cancelled_text(AuthKind.CLAUDE_OAUTH))
            raise
        log.info("Claude-Anmeldung %s: %s", job.profile.name, result.outcome.value)
        if result.outcome is CaptureOutcome.TOKEN:
            await self._after(job)
            return
        job.finish(
            LoginState.FAILED,
            CLAUDE_FAILURES[result.outcome],
            action=self._retry(job, "Claude"),
        )

    async def _start_codex(self, profile: AuthProfile) -> LoginStart:
        now = time.time()
        job = LoginJob(profile, expires_at=now + CODEX_LOGIN_TIMEOUT_S)
        self.jobs[profile.name] = job
        ready: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()

        def show(link: str, code: str) -> None:
            if not ready.done():
                ready.set_result((link, code))

        job.task = asyncio.create_task(self._codex(job, show), name=f"arr-login-{profile.name}")
        job.task.add_done_callback(lambda _task: ready.cancel())
        try:
            link, code = await asyncio.wait_for(asyncio.shield(ready), CODEX_START_TIMEOUT_S)
        except (TimeoutError, asyncio.CancelledError) as exc:
            if not isinstance(exc, TimeoutError) and job.state is LoginState.WAITING:
                raise  # the caller was cancelled, not the login
            await self.cancel(profile.name)
            raise ApiException.of(
                ErrorCode.UNAVAILABLE,
                "Die ChatGPT-Anmeldung ließ sich nicht starten. Prüfe deine Internetverbindung "
                "und versuche es erneut.",
            ) from None
        return LoginStart(
            profile=profile.name,
            method="device_code",
            browser_url=link,
            device_code=code,
            poll_url=self.poll_url(profile.name),
            expires_at=job.expires_at,
            note=CODEX_NOTE,
        )

    async def _codex(self, job: LoginJob, show: Callable[[str, str], None]) -> None:
        try:
            async with asyncio.timeout(CODEX_LOGIN_TIMEOUT_S):
                result = await codex_login(job.profile, show=show, factory=self.codex_factory)
        except asyncio.CancelledError:
            job.finish(LoginState.CANCELLED, cancelled_text(AuthKind.CODEX_CHATGPT))
            raise
        except TimeoutError:
            job.finish(LoginState.FAILED, CODEX_EXPIRED, action=self._retry(job, "ChatGPT"))
            return
        except Exception as exc:  # noqa: BLE001 - the type only, a message may quote Codex
            log.warning("ChatGPT-Anmeldung %s: %s", job.profile.name, type(exc).__name__)
            job.finish(LoginState.FAILED, CODEX_FAILED, action=self._retry(job, "ChatGPT"))
            return
        if result.success:
            await self._after(job)
        else:
            job.finish(LoginState.FAILED, CODEX_FAILED, action=self._retry(job, "ChatGPT"))

    def status(self, name: str) -> LoginStatus:
        job = self.jobs.get(name)
        if job is None:
            return LoginStatus(
                profile=name, state=LoginState.CANCELLED, message="Keine Anmeldung läuft."
            )
        return LoginStatus(
            profile=name,
            state=job.state,
            message=job.message,
            action=job.action,
            profile_info=job.info,
        )

    async def cancel(self, name: str) -> LoginStatus:
        """Stop a running login and wait until its process is gone."""
        job = self.jobs.get(name)
        if job is not None and job.task is not None and not job.task.done():
            job.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job.task
            if job.state is LoginState.WAITING:  # cancelled before the task started
                job.finish(LoginState.CANCELLED, cancelled_text(job.profile.kind))
        return self.status(name)

    async def close(self) -> None:
        for name in list(self.jobs):
            await self.cancel(name)
