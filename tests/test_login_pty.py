"""`claude setup-token` in a pseudo-terminal (W3-05 A2): a real child process with a fake CLI."""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from login_kit import (
    FAIL,
    FLOOD,
    HANG,
    IGNORE_HUP,
    IGNORE_TERM,
    MARKER,
    PRINT_TOKEN,
    alive,
    facts_of,
    fake_cli,
    gone,
    wait_until,
)
from run_kit import write_profiles
from service_kit import _clean_env, free_port, make_workfolder

from arr.service.login import (
    OUTPUT_KEEP,
    CaptureOutcome,
    TokenScanner,
    capture_token,
    claude_cli,
    login_env,
)

START_S = 3.0
"""Deadline of the tests: long enough for a fake CLI to start on a busy machine."""


def test_scanner_finds_a_token_split_by_chunks_and_colour_codes() -> None:
    scanner = TokenScanner()
    assert scanner.feed(b"\x1b[33m" + MARKER[:25].encode()) is None
    assert scanner.feed(b"\x1b[0m\x1b[33m" + MARKER[25:].encode()) is None
    assert scanner.feed(b"\x1b[39m\r\n") == MARKER


def test_scanner_never_returns_a_cut_token_and_keeps_only_a_tail() -> None:
    scanner = TokenScanner()
    assert scanner.feed(b"sk-ant-oat01-short ") is None
    assert scanner.feed(MARKER.encode()) is None  # the end is not there yet
    assert scanner.feed(b"z" * (OUTPUT_KEEP + 1)) is None
    assert len(scanner._tail) <= OUTPUT_KEEP
    assert scanner.feed(b"\r\n") is None


def test_login_env_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "from-the-service")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-the-service")
    env = login_env("/tmp/own")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/own"


def test_bundled_cli_is_the_one_of_the_sdk() -> None:
    assert claude_cli().parts[-3:] == ("claude_agent_sdk", "_bundled", "claude")


async def test_token_goes_to_the_sink_and_the_process_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "from-the-service")
    cli, facts = fake_cli(tmp_path, PRINT_TOKEN)
    got: list[str] = []
    result = await capture_token(got.append, cli=cli, timeout_s=10)
    assert result.outcome is CaptureOutcome.TOKEN and result.token_length == len(MARKER)
    assert got == [MARKER] and MARKER not in repr(result)
    seen = facts_of(facts)
    assert seen["tty"] and seen["cols"] == 1000 and seen["args"] == ["setup-token"]
    assert not seen["token_in_env"] and seen["home_exists"]
    assert gone(seen["config_dir"])
    assert not alive(seen["pid"])


async def test_cli_without_token_reports_no_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    cli, _ = fake_cli(tmp_path, FAIL)
    got: list[str] = []
    result = await capture_token(got.append, cli=cli, timeout_s=10)
    assert result.outcome is CaptureOutcome.NO_TOKEN and result.exit_code == 1
    assert got == [] and "exchange" not in caplog.text


async def test_deadline_ends_the_process(tmp_path: Path) -> None:
    cli, facts = fake_cli(tmp_path, HANG)
    result = await capture_token(lambda t: None, cli=cli, timeout_s=START_S)
    assert result.outcome is CaptureOutcome.TIMEOUT
    assert not alive(facts_of(facts)["pid"])


async def test_cancel_ends_the_process(tmp_path: Path) -> None:
    cli, facts = fake_cli(tmp_path, HANG)
    got: list[str] = []
    task = asyncio.create_task(capture_token(got.append, cli=cli, timeout_s=30))
    assert await asyncio.to_thread(wait_until, facts.exists)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not alive(facts_of(facts)["pid"]) and got == []


async def test_a_child_that_ignores_sigterm_is_killed(tmp_path: Path) -> None:
    cli, facts = fake_cli(tmp_path, IGNORE_TERM)
    result = await capture_token(lambda t: None, cli=cli, timeout_s=START_S)
    assert result.outcome is CaptureOutcome.TIMEOUT
    assert not alive(facts_of(facts)["pid"])


async def test_a_failing_sink_is_reported_without_its_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    cli, _ = fake_cli(tmp_path, PRINT_TOKEN)

    def sink(token: str) -> None:
        raise RuntimeError(f"keychain says {token}")

    result = await capture_token(sink, cli=cli, timeout_s=10)
    assert result.outcome is CaptureOutcome.STORE_FAILED and MARKER not in caplog.text


async def test_cli_that_cannot_start(tmp_path: Path) -> None:
    result = await capture_token(lambda t: None, cli=tmp_path / "missing", timeout_s=2)
    assert result.outcome is CaptureOutcome.START_FAILED


async def test_cancel_waits_for_a_running_sink(tmp_path: Path) -> None:
    """A thread cannot be cancelled: the caller must not go on while it still writes."""
    cli, _ = fake_cli(tmp_path, PRINT_TOKEN)
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()

    def sink(token: str) -> None:
        entered.set()
        release.wait(10)
        ended.set()

    task = asyncio.create_task(capture_token(sink, cli=cli, timeout_s=10))
    assert await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    await asyncio.wait({task}, timeout=1)
    assert not task.done(), "the cancellation overtook the sink"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ended.is_set()


def _parent(cli: Path, limit: int, grace: int) -> subprocess.Popen[bytes]:
    child = Path(__file__).with_name("login_child.py")
    env = {**os.environ, "TMPDIR": str(cli.parent)}  # a killed parent leaves its folder there
    return subprocess.Popen([sys.executable, str(child), str(cli), str(limit), str(grace)], env=env)


def test_killing_the_parent_ends_the_child_by_sighup(tmp_path: Path) -> None:
    cli, facts = fake_cli(tmp_path, HANG)
    parent = _parent(cli, 60, 5)
    try:
        assert wait_until(facts.exists)
        pid = facts_of(facts)["pid"]
        assert alive(pid)
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait()
        assert wait_until(lambda: not alive(pid), 5), "child survived the end of its parent"
    finally:
        parent.kill()
        parent.wait()


def test_the_alarm_ends_a_child_that_ignores_sighup_at_the_deadline(tmp_path: Path) -> None:
    cli, facts = fake_cli(tmp_path, IGNORE_HUP)
    parent = _parent(cli, int(START_S), 1)
    try:
        assert wait_until(facts.exists)
        pid = facts_of(facts)["pid"]
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait()
        assert alive(pid)
        assert wait_until(lambda: not alive(pid), START_S + 6), "child outlived its deadline"
    finally:
        parent.kill()
        parent.wait()


def test_unread_output_after_the_token_does_not_hang_the_capture(tmp_path: Path) -> None:
    cli, facts = fake_cli(tmp_path, FLOOD)
    parent = _parent(cli, 30, 5)
    try:
        assert parent.wait(15) == 0, "the capture did not come back"
        assert wait_until(lambda: not alive(facts_of(facts)["pid"]), 5)
    finally:
        parent.kill()
        parent.wait()


@pytest.mark.macos
def test_the_real_cli_ends_without_blocking_the_capture(tmp_path: Path) -> None:
    """The real CLI stays in its exit until the terminal is closed: waiting for it first hung
    the event loop for good, seen only with the real CLI (gate of W3-05). The plain start
    shows a welcome screen, so no login, no browser and no model request."""
    child = Path(__file__).with_name("login_child.py")
    parent = subprocess.Popen(
        [sys.executable, str(child), str(claude_cli()), "3", "5", "interactive"],
        env={**os.environ, "TMPDIR": str(tmp_path)},
    )
    try:
        assert parent.wait(15) == 0, "the capture did not come back"
    finally:
        parent.kill()
        parent.wait()


# -- the end of the service -------------------------------------------------------------------


def _service(tmp_path: Path, arr_home: Path, cli: Path) -> subprocess.Popen[bytes]:
    write_profiles(arr_home, {"max": "claude_oauth"})
    workfolder = make_workfolder(tmp_path)
    child = Path(__file__).with_name("login_service_child.py")
    env = {
        **_clean_env(),
        "TMPDIR": str(tmp_path),
        "ARR_HOME": str(arr_home),
        "ARR_TEST_CLI": str(cli),
        "ARR_TEST_PROFILE": "max",
        "ARR_TEST_PORT": str(free_port()),
        "ARR_TEST_WORKFOLDER": str(workfolder),
    }
    log = (tmp_path / "service.log").open("wb")
    return subprocess.Popen([sys.executable, str(child)], env=env, stdout=log, stderr=log)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGKILL])
def test_end_of_the_service_ends_the_login(
    tmp_path: Path, arr_home: Path, signum: signal.Signals
) -> None:
    cli, facts = fake_cli(tmp_path / "cli", HANG)
    service = _service(tmp_path, arr_home, cli)
    try:
        assert wait_until(facts.exists, 30), (tmp_path / "service.log").read_text()[-2000:]
        pid = facts_of(facts)["pid"]
        assert alive(pid)
        os.kill(service.pid, signum)
        service.wait(20)
        assert wait_until(lambda: not alive(pid), 5), "the login outlived the service"
    finally:
        service.kill()
        service.wait()
