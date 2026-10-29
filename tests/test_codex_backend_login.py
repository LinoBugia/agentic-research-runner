"""Codex login folder and device-code login with a fake app-server (no network, no model)."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arr.backends.codex_config import SECRET_ENV, codex_home_for
from arr.backends.codex_login import AccountState, codex_account, codex_login
from arr.models import AuthKind, AuthProfile

EMAIL = "someone@example.org"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"


def chatgpt(name: str = "chatgpt-test", login_dir: str | None = None) -> AuthProfile:
    return AuthProfile(name=name, kind=AuthKind.CODEX_CHATGPT, login_dir=login_dir)


def account_response(logged_in: bool) -> SimpleNamespace:
    if not logged_in:
        return SimpleNamespace(account=None, requires_openai_auth=True)
    root = SimpleNamespace(type="chatgpt", plan_type=SimpleNamespace(value="plus"), email=EMAIL)
    return SimpleNamespace(account=SimpleNamespace(root=root), requires_openai_auth=True)


class FakeHandle:
    def __init__(self, codex: "FakeCodex") -> None:
        self.codex = codex
        self.login_id = "login-secret-id"
        self.verification_url = "https://auth.example/codex/device"
        self.user_code = "ABCD-1234"
        self.cancelled = False

    async def wait(self) -> SimpleNamespace:
        if self.codex.hang:
            await asyncio.sleep(3600)
        self.codex.logged_in = self.codex.succeed
        error = None if self.codex.succeed else "device auth timed out after 15 minutes"
        return SimpleNamespace(success=self.codex.succeed, error=error, login_id=self.login_id)

    async def cancel(self) -> None:
        self.cancelled = True


class FakeCodex:
    instances: list["FakeCodex"] = []

    def __init__(self, config: Any, *, logged_in: bool = False, succeed: bool = True) -> None:
        self.config = config
        self.logged_in = logged_in
        self.succeed = succeed
        self.hang = False
        self.handle: FakeHandle | None = None
        self.closed = False
        FakeCodex.instances.append(self)

    async def __aenter__(self) -> "FakeCodex":
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def account(self, *, refresh_token: bool = False) -> SimpleNamespace:
        return account_response(self.logged_in)

    async def login_chatgpt_device_code(self) -> FakeHandle:
        self.handle = FakeHandle(self)
        return self.handle


def factory(**kwargs: Any):
    def make(config: Any) -> FakeCodex:
        return FakeCodex(config, **kwargs)

    return make


def test_login_dir_defaults_to_app_dir(arr_home: Path) -> None:
    assert codex_home_for(chatgpt("chatgpt-privat")) == arr_home / "auth/chatgpt-privat/codex_home"


def test_login_dir_from_profile(tmp_path: Path) -> None:
    assert codex_home_for(chatgpt(login_dir=str(tmp_path / "x"))) == tmp_path / "x"


def test_login_dir_never_user_codex_home(arr_home: Path) -> None:
    assert codex_home_for(chatgpt()) != Path.home() / ".codex"


def test_login_dir_only_for_chatgpt_profiles() -> None:
    with pytest.raises(ValueError, match="kein ChatGPT-Profil"):
        codex_home_for(AuthProfile(name="max", kind=AuthKind.CLAUDE_OAUTH))


async def test_login_shows_link_and_code_and_reports_plan(arr_home: Path) -> None:
    shown: list[tuple[str, str]] = []
    result = await codex_login(
        chatgpt(), show=lambda link, code: shown.append((link, code)), factory=factory()
    )
    assert shown == [("https://auth.example/codex/device", "ABCD-1234")]
    assert result.success and result.account == AccountState(kind="chatgpt", plan="plus")
    assert result.account.describe() == "ChatGPT (plus)"
    assert EMAIL not in repr(result)
    home = arr_home / "auth/chatgpt-test/codex_home"
    assert home.is_dir() and (home.stat().st_mode & 0o777) == 0o700
    config = FakeCodex.instances[-1].config
    assert config.env["CODEX_HOME"] == str(home)


async def test_login_failure_keeps_codex_message(arr_home: Path) -> None:
    result = await codex_login(chatgpt(), show=lambda *_: None, factory=factory(succeed=False))
    assert not result.success
    assert result.error == "device auth timed out after 15 minutes"
    assert not result.account.logged_in


async def test_cancelled_wait_cancels_login(arr_home: Path) -> None:
    fake = FakeCodex(None)
    fake.hang = True

    def make(config: Any) -> FakeCodex:
        fake.config = config
        return fake

    task = asyncio.create_task(codex_login(chatgpt(), show=lambda *_: None, factory=make))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.handle is not None and fake.handle.cancelled
    assert fake.closed


async def test_account_state_without_identity(arr_home: Path) -> None:
    state = await codex_account(chatgpt(), factory=factory(logged_in=True))
    assert state == AccountState(kind="chatgpt", plan="plus")
    assert EMAIL not in repr(state)


async def test_login_blanks_secret_env(arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key-0000000000")
    await codex_account(chatgpt(), factory=factory())
    env = FakeCodex.instances[-1].config.env
    assert env["OPENAI_API_KEY"] == ""
    assert set(env) <= {"CODEX_HOME", *SECRET_ENV}


def test_no_token_or_account_data_in_logs(arr_home: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    asyncio.run(codex_login(chatgpt(), show=lambda *_: None, factory=factory()))
    for text in (EMAIL, TOKEN, "login-secret-id", "ABCD-1234"):
        assert text not in caplog.text
