"""Tests for arr.auth (W2-01): process environment, per-agent env from auth profiles."""

import logging

import pytest

from arr.auth import (
    CLAUDE_SWITCHES,
    START_PROFILE_VARIABLE,
    AuthError,
    claude_env,
    clean_process_env,
    is_billed,
    keychain_ref,
    read_secret,
)
from arr.models import AuthProfile, KeychainRef

SECRET = "sk-ant-oat01-TESTSECRET-abcdef"


def test_clean_process_env_removes_the_s1_list_and_is_idempotent() -> None:
    env = {
        "ANTHROPIC_API_KEY": "a",
        "ANTHROPIC_AUTH_TOKEN": "a",
        "ANTHROPIC_BASE_URL": "https://proxy.example",
        "CLAUDE_CODE_OAUTH_TOKEN": "t",
        "CLAUDE_CODE_SESSION_ID": "s",
        "CLAUDE_CODE_MESSAGING_TOKEN": "m",
        "CLAUDE_AGENT_SDK_VERSION": "1",
        "CLAUDECODE": "1",
        "CLAUDE_PID": "1",
        "CLAUDE_EFFORT": "high",
        "CLAUDE_CONFIG_DIR": "/x",
        "PATH": "/usr/bin",
        "HOME": "/Users/x",
        "CLAUDE_OTHER": "stays",
    }
    removed = clean_process_env(env)
    assert removed == sorted(removed) and len(removed) == 11
    assert env == {"PATH": "/usr/bin", "HOME": "/Users/x", "CLAUDE_OTHER": "stays"}
    assert clean_process_env(env) == []


def test_clean_process_env_on_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    assert "ANTHROPIC_API_KEY" in clean_process_env()
    import os

    assert "ANTHROPIC_API_KEY" not in os.environ


def test_oauth_with_token_in_keychain() -> None:
    calls: list[tuple[str, str]] = []

    def reader(service: str, account: str) -> str:
        calls.append((service, account))
        return SECRET

    env = claude_env(AuthProfile(name="max-privat", kind="claude_oauth"), reader)
    assert env == {**CLAUDE_SWITCHES, START_PROFILE_VARIABLE: "max-privat"}, "never the secret"
    assert calls == [("arr", "max-privat")]


def test_oauth_without_token_uses_the_mac_login(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    env = claude_env(AuthProfile(name="max", kind="claude_oauth"), lambda s, a: None)
    assert env == CLAUDE_SWITCHES
    assert "vorhandenen Claude-Login" in caplog.text and "max" in caplog.text


def test_api_key_profile() -> None:
    ref = KeychainRef(service="arr-keys", account="team")
    profile = AuthProfile(name="api", kind="claude_api_key", keychain=ref)
    seen: list[tuple[str, str]] = []

    def reader(service: str, account: str) -> str:
        seen.append((service, account))
        return SECRET

    env = claude_env(profile, reader)
    assert env[START_PROFILE_VARIABLE] == "api" and SECRET not in env.values()
    assert seen == [("arr-keys", "team")] and keychain_ref(profile) == ref
    with pytest.raises(AuthError, match="kein API-Key"):
        claude_env(profile, lambda s, a: None)
    assert is_billed(profile) and not is_billed(AuthProfile(name="m", kind="claude_oauth"))


def test_wrong_kind_and_keychain_errors_name_no_secret() -> None:
    with pytest.raises(AuthError, match="passt nicht"):
        claude_env(AuthProfile(name="gpt", kind="codex_chatgpt"), lambda s, a: SECRET)

    def broken(service: str, account: str) -> str:
        raise RuntimeError(f"locked {SECRET}")

    with pytest.raises(AuthError) as info:
        read_secret(AuthProfile(name="max", kind="claude_oauth"), broken)
    assert SECRET not in str(info.value) and "„max“" in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
