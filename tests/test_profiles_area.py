"""Auth profiles on the real API (W3-05 A2–A7): keychain and CLI are fakes, the rest is real."""

import asyncio
import json
import logging
import os
import stat
import tempfile
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, RateLimitEvent, RateLimitInfo, ResultMessage
from login_kit import FAIL, HANG, MARKER, PRINT_TOKEN, alive, facts_of, fake_cli, wait_until
from pydantic import SecretStr
from run_kit import write_profiles
from service_kit import make_workfolder
from test_gui import gui_user
from test_gui_real import see

from arr.backends.codex_config import codex_home_for
from arr.backends.codex_login import AccountState, CodexCheck, codex_check
from arr.models import AuthKind, AuthProfile, RunStatus
from arr.resolve import load_yaml_model
from arr.service.api_models import (
    ApiException,
    CreateProfileRequest,
    ErrorCode,
    LoginState,
)
from arr.service.profile_check import CheckFailure, CheckResult, check_profile, to_profile_test
from arr.service.real_api import RealApi
from arr.service.real_api.profiles import STATUS_FILE, login_folder_to_delete

BASE = "http://127.0.0.1:8765"
KEY = "sk-ant-api03-MARKERKEY-0123456789"


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    store: dict[tuple[str, str], str] = {}

    def delete(service: str, account: str) -> None:
        store.pop((service, account), None)

    monkeypatch.setattr("arr.auth._keyring_get", lambda s, a: store.get((s, a)))
    monkeypatch.setattr("arr.auth._keyring_set", lambda s, a, v: store.__setitem__((s, a), v))
    monkeypatch.setattr("arr.auth._keyring_delete", delete)
    return store


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The fake Claude CLI of „Testen“: records the options, answers with one good result."""
    seen: list[Any] = []

    async def fake_query(prompt: str, options: Any) -> AsyncIterator[Any]:
        seen.append(options)
        yield ok_result()

    monkeypatch.setattr("arr.service.profile_check._sdk_query", fake_query)
    return seen


def ok_result(*, is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s",
    )


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path) -> Path:
    write_profiles(
        arr_home,
        {"max": "claude_oauth", "api": "claude_api_key", "chat": "codex_chatgpt"},
    )
    return make_workfolder(tmp_path)


@pytest.fixture
def api(wf: Path, keychain: dict, calls: list) -> RealApi:
    return RealApi(wf, base_url=BASE, scan=False)


def request(name: str = "neu", key: str | None = KEY) -> CreateProfileRequest:
    kind = AuthKind.CLAUDE_API_KEY if key else AuthKind.CLAUDE_OAUTH
    return CreateProfileRequest(
        name=name, kind=kind, api_key=SecretStr(key) if key else None, concurrency=3
    )


def listing(root: Path) -> list[str]:
    return sorted(str(p) for p in root.rglob("*"))


def tree_text(*roots: Path) -> str:
    text = []
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file():
                text.append(path.read_bytes().decode("utf-8", "replace"))
    return "\n".join(text)


# -- A5: create -------------------------------------------------------------------------------


async def test_create_keeps_the_key_only_in_the_keychain(
    api: RealApi, keychain: dict, calls: list, arr_home: Path, wf: Path
) -> None:
    os.chmod(arr_home / "profiles.yaml", 0o640)
    info = await api.create_profile(request())
    assert keychain == {("arr", "neu"): KEY}
    assert info.secret_present and info.keychain_label == "arr/neu"
    assert KEY not in info.model_dump_json() and KEY not in repr(request())
    assert KEY not in tree_text(arr_home, wf)
    assert stat.S_IMODE(os.stat(arr_home / "profiles.yaml").st_mode) == 0o640
    assert [n for n in listing(arr_home) if "/." in n] == [], "a temp file stayed behind"
    assert calls == [], "creating must not call a model"
    names = [p.name for p in await api.list_profiles()]
    assert names == ["max", "api", "chat", "neu"]


async def test_create_refuses_an_existing_name(api: RealApi, keychain: dict) -> None:
    with pytest.raises(ApiException) as caught:
        await api.create_profile(request("max"))
    assert caught.value.error.code is ErrorCode.CONFLICT and caught.value.error.field == "name"
    assert keychain == {}


async def test_new_subscription_profile_has_no_secret_until_login(api: RealApi) -> None:
    info = await api.create_profile(request("abo", key=None))
    assert not info.secret_present and info.last_test is None


async def test_a_failed_write_leaves_no_secret(
    api: RealApi, keychain: dict, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_a: Any, **_k: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr("arr.service.real_api.profiles._write_atomic", fail)
    with pytest.raises(ApiException) as caught:
        await api.create_profile(request())
    assert caught.value.error.code is ErrorCode.UNAVAILABLE and keychain == {}


# -- A6: delete -------------------------------------------------------------------------------


async def test_delete_removes_entry_and_login_folder_and_nothing_else(
    api: RealApi, keychain: dict, arr_home: Path, tmp_path: Path
) -> None:
    await api.create_profile(request())
    other = await api.create_profile(request("bleibt"))
    home = codex_home_for(AuthProfile(name="chat", kind=AuthKind.CODEX_CHATGPT))
    home.mkdir(parents=True)
    (home / "auth.json").write_text("{}")
    neighbour = arr_home / "auth" / "nachbar" / "codex_home"
    neighbour.mkdir(parents=True)
    (neighbour / "auth.json").write_text("{}")
    await api.delete_profile("neu")
    await api.delete_profile("chat")
    assert list(keychain) == [("arr", "bleibt")] and other.secret_present
    assert not home.parent.exists() and (neighbour / "auth.json").exists()
    assert [p.name for p in await api.list_profiles()] == ["max", "api", "bleibt"]
    assert "neu" not in json.loads((arr_home / STATUS_FILE).read_text())


async def test_delete_is_refused_while_a_run_of_this_service_uses_it(
    api: RealApi, keychain: dict
) -> None:
    await api.create_profile(request())
    api.state.gates["neu"] = SimpleNamespace(active=1)  # type: ignore[assignment]
    with pytest.raises(ApiException) as caught:
        await api.delete_profile("neu")
    assert caught.value.error.code is ErrorCode.NOT_ALLOWED
    assert ("arr", "neu") in keychain


async def test_delete_is_refused_while_a_run_of_another_process_uses_it(
    api: RealApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = SimpleNamespace(
        run_id="r1",
        meta=SimpleNamespace(status=RunStatus.RUNNING),
        load_resolved=lambda: SimpleNamespace(agents=[SimpleNamespace(auth="max")]),
    )
    monkeypatch.setattr(api.state.store, "scan", lambda: [record])
    with pytest.raises(ApiException) as caught:
        await api.delete_profile("max")
    assert caught.value.error.code is ErrorCode.NOT_ALLOWED
    await api.delete_profile("api")  # another profile is free


@pytest.mark.parametrize("name", ["../../etc", "..", "a/b", "/etc/passwd", "MAX", ""])
async def test_delete_with_a_manipulated_name_touches_nothing(
    api: RealApi, name: str, arr_home: Path
) -> None:
    before = listing(arr_home)
    with pytest.raises(ApiException) as caught:
        await api.delete_profile(name)
    assert caught.value.error.code is ErrorCode.NOT_FOUND
    assert listing(arr_home) == before


def test_login_folder_outside_of_arr_is_never_deleted(
    arr_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "fremd"
    outside.mkdir()

    def chat(login_dir: str | None = None) -> AuthProfile:
        return AuthProfile(name="chat", kind=AuthKind.CODEX_CHATGPT, login_dir=login_dir)

    assert login_folder_to_delete(chat(str(outside))) is None
    assert login_folder_to_delete(chat(str(arr_home / "auth" / "anderes"))) is None
    monkeypatch.setenv("HOME", str(tmp_path))
    assert login_folder_to_delete(chat("~/.codex")) is None
    (arr_home / "auth").mkdir(parents=True)
    (arr_home / "auth" / "chat").symlink_to(outside)
    assert login_folder_to_delete(chat()) is None, "a symlink must not lead outside"
    own = AuthProfile(name="chat2", kind=AuthKind.CODEX_CHATGPT)
    assert login_folder_to_delete(own) == (arr_home / "auth" / "chat2").resolve()


async def test_delete_keeps_a_foreign_keychain_item(
    wf: Path, arr_home: Path, keychain: dict
) -> None:
    (arr_home / "profiles.yaml").write_text(
        "version: 1\nprofiles:\n  - name: fremd\n    kind: claude_oauth\n"
        "    keychain: {service: anderes-programm, account: konto}\n"
    )
    keychain[("anderes-programm", "konto")] = "fremd-und-unberuehrt"
    api = RealApi(wf, base_url=BASE, scan=False)
    with pytest.raises(ApiException) as caught:
        await api.start_login("fremd")
    assert caught.value.error.code is ErrorCode.NOT_ALLOWED
    await api.delete_profile("fremd")
    assert keychain == {("anderes-programm", "konto"): "fremd-und-unberuehrt"}


# -- A7: test ---------------------------------------------------------------------------------


async def test_test_shows_success_and_the_list_makes_no_call(
    api: RealApi, calls: list, keychain: dict, wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain[("arr", "api")] = KEY
    info = await api.test_profile("api")
    test = info.last_test
    assert test is not None and test.ok and test.models == ["claude-haiku-5-5"]
    assert info.secret_present and info.models == ["claude-haiku-5-5"]
    options = calls[0]
    assert options.model == "claude-haiku-5-5" and options.max_budget_usd <= 0.10
    assert options.max_turns == 1 and options.tools == [] and options.env["ANTHROPIC_API_KEY"]

    def no_keychain(*_a: Any) -> None:
        raise AssertionError("the list must not read the keychain")

    monkeypatch.setattr("arr.auth._keyring_get", no_keychain)
    fresh = RealApi(wf, base_url=BASE, scan=False)
    listed = {p.name: p for p in await fresh.list_profiles()}
    assert len(calls) == 1
    assert listed["api"].secret_present and listed["api"].last_test == test
    assert not listed["max"].secret_present and listed["max"].last_test is None


async def test_mac_login_works_without_a_token_in_the_keychain(api: RealApi, calls: list) -> None:
    info = await api.test_profile("max")
    assert info.last_test.ok and not info.secret_present
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in calls[0].env


async def test_service_environment_never_reaches_the_test(
    api: RealApi, calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "from-the-service")
    await api.test_profile("max")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ


@pytest.mark.parametrize(
    ("error", "profile", "text", "action"),
    [
        ("authentication_failed", "api", "Der API-Key von „api“ wurde abgelehnt (401).", "Key"),
        ("authentication_failed", "max", "Die Anmeldung von „max“ ist abgelaufen.", "Neu"),
        ("rate_limit", "max", "„max“ hat sein Nutzungslimit erreicht.", None),
        ("billing_error", "max", "Für „max“ fehlt Guthaben", None),
        ("invalid_request", "max", "„max“ kommt nicht an das Testmodell heran.", None),
        ("server_error", "max", "Der Test von „max“ hat nicht geklappt.", None),
    ],
)
async def test_failures_follow_the_fixed_texts(
    api: RealApi,
    keychain: dict,
    monkeypatch: pytest.MonkeyPatch,
    error: str,
    profile: str,
    text: str,
    action: str | None,
) -> None:
    keychain[("arr", profile)] = KEY

    async def fake_query(prompt: str, options: Any) -> AsyncIterator[Any]:
        yield AssistantMessage(content=[], model="m", error=error)  # type: ignore[arg-type]
        yield ok_result(is_error=True)

    monkeypatch.setattr("arr.service.profile_check._sdk_query", fake_query)
    test = (await api.test_profile(profile)).last_test
    assert test is not None and not test.ok and test.message.startswith(text)
    assert "Fehler:" not in test.message and "`" not in test.message
    assert (test.action.label.split()[0] if test.action else None) == action


async def test_cli_error_text_never_reaches_the_message(
    api: RealApi, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def fake_query(prompt: str, options: Any) -> AsyncIterator[Any]:
        raise RuntimeError(f"cli said {MARKER}")
        yield

    monkeypatch.setattr("arr.service.profile_check._sdk_query", fake_query)
    info = await api.test_profile("max")
    assert not info.last_test.ok and MARKER not in info.model_dump_json()
    assert MARKER not in caplog.text


async def test_deadline_of_a_test(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_query(prompt: str, options: Any) -> AsyncIterator[Any]:
        await asyncio.sleep(30)
        yield ok_result()

    monkeypatch.setattr("arr.service.profile_check._sdk_query", fake_query)
    profile = AuthProfile(name="max", kind=AuthKind.CLAUDE_OAUTH)
    result = await check_profile(profile, lambda s, a: None, timeout_s=0.05)
    assert result.failure is CheckFailure.TIMEOUT
    assert "zu lange" in to_profile_test(profile, result, 0).message


async def test_limit_window_and_rejection_of_a_subscription(
    api: RealApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    def limit(status: str, used: float) -> RateLimitEvent:
        info = RateLimitInfo(
            status=status,  # type: ignore[arg-type]
            resets_at=1_800_000_000,
            rate_limit_type="five_hour",
            utilization=used,
        )
        return RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s")

    async def fake_query(prompt: str, options: Any) -> AsyncIterator[Any]:
        yield limit("allowed_warning", 0.82)
        yield ok_result()

    monkeypatch.setattr("arr.service.profile_check._sdk_query", fake_query)
    info = await api.test_profile("max")
    assert info.last_test.ok
    assert info.usage_window.utilization == 0.82
    assert info.usage_window.label == "5-Stunden-Fenster"

    async def rejected(prompt: str, options: Any) -> AsyncIterator[Any]:
        yield limit("rejected", 1.0)
        yield ok_result(is_error=True)

    monkeypatch.setattr("arr.service.profile_check._sdk_query", rejected)
    info = await api.test_profile("max")
    assert not info.last_test.ok and "Nutzungslimit" in info.last_test.message


async def test_missing_api_key_and_openai_key(api: RealApi, keychain: dict) -> None:
    info = await api.test_profile("api")
    assert "kein API-Key" in info.last_test.message and not info.secret_present
    profile = AuthProfile(name="o", kind=AuthKind.OPENAI_API_KEY)
    assert (await check_profile(profile)).failure is CheckFailure.UNSUPPORTED


async def test_a_keychain_dialog_does_not_stop_the_service(
    api: RealApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dialog of the keychain blocks its caller for as long as the user waits."""
    release = threading.Event()

    def dialog(*_a: Any) -> str | None:
        assert release.wait(10)
        return None

    monkeypatch.setattr("arr.auth._keyring_get", dialog)
    monkeypatch.setattr("arr.auth._keyring_set", dialog)
    monkeypatch.setattr("arr.auth._keyring_delete", dialog)
    for call in (api.test_profile("max"), api.create_profile(request()), api.delete_profile("api")):
        task = asyncio.create_task(call)
        await asyncio.sleep(0.05)  # the loop runs on while the dialog is open
        assert not task.done()
        assert [p.name for p in await api.list_profiles()]
        release.set()
        await task
        release.clear()


async def test_damaged_status_file_is_an_empty_one(api: RealApi, arr_home: Path) -> None:
    (arr_home / STATUS_FILE).write_text("{kaputt")
    assert all(p.last_test is None for p in await api.list_profiles())


# -- A2, A3: Claude login ---------------------------------------------------------------------


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    def use(body: str) -> Path:
        path, facts = fake_cli(tmp_path / "cli", body)
        monkeypatch.setattr("arr.service.login.claude_cli", lambda: path)
        return facts

    return use


async def until_state(api: RealApi, name: str, state: LoginState) -> Any:
    for _ in range(400):
        status = await api.get_login_status(name)
        if status.state is state:
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(f"login is {status.state}: {status.message}")


async def test_login_stores_the_token_and_tests_the_profile(
    api: RealApi, cli: Any, keychain: dict, calls: list
) -> None:
    facts = cli(PRINT_TOKEN)
    start = await api.start_login("max")
    assert start.method == "browser" and start.browser_url is None
    assert start.note == "arr führt dafür claude setup-token aus."
    assert start.poll_url.endswith("/api/profiles/max/login")
    status = await until_state(api, "max", LoginState.DONE)
    assert keychain == {("arr", "max"): MARKER}
    assert status.profile_info.secret_present and status.profile_info.last_test.ok
    assert calls[0].env["CLAUDE_CODE_OAUTH_TOKEN"] == MARKER
    assert not alive(facts_of(facts)["pid"])


async def test_marker_token_is_in_no_file_log_answer_or_event(
    api: RealApi,
    cli: Any,
    keychain: dict,
    arr_home: Path,
    wf: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    cli(PRINT_TOKEN)
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    events: list[str] = []
    publish = api.state.hub.publish
    monkeypatch.setattr(
        api.state.hub, "publish", lambda *a, **k: (events.append(repr((a, k))), publish(*a, **k))[1]
    )
    start = await api.start_login("max")
    status = await until_state(api, "max", LoginState.DONE)
    listed = await api.list_profiles()
    assert keychain[("arr", "max")] == MARKER
    answers = [start.model_dump_json(), status.model_dump_json()]
    answers += [p.model_dump_json() for p in listed]
    assert not any(MARKER in text for text in answers)
    assert MARKER not in caplog.text
    captured = capfd.readouterr()
    assert MARKER not in captured.out + captured.err
    assert MARKER not in tree_text(arr_home, wf)
    assert not any(MARKER in event for event in events)
    assert list(temp.iterdir()) == [], "the login folder of the CLI stays behind"
    assert "Your OAuth token" not in caplog.text, "no output of the CLI in the log"


async def test_login_failure_uses_a_fixed_text(
    api: RealApi, cli: Any, keychain: dict, calls: list
) -> None:
    cli(FAIL)
    await api.start_login("max")
    status = await until_state(api, "max", LoginState.FAILED)
    assert status.message == (
        "Die Claude-Anmeldung wurde abgebrochen. Versuche es erneut oder nimm einen Claude-API-Key."
    )
    assert status.action.label == "Erneut mit Claude anmelden"
    assert keychain == {} and calls == []


async def test_login_deadline_ends_the_process(
    api: RealApi, cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts = cli(HANG)
    monkeypatch.setattr("arr.service.login.CLAUDE_LOGIN_TIMEOUT_S", 0.5)
    await api.start_login("max")
    status = await until_state(api, "max", LoginState.FAILED)
    assert "abgelaufen" in status.message
    assert not alive(facts_of(facts)["pid"])


async def test_cancel_ends_the_process_and_says_so(api: RealApi, cli: Any, keychain: dict) -> None:
    facts = cli(HANG)
    await api.start_login("max")
    assert await asyncio.to_thread(wait_until, facts.exists)
    status = await api.cancel_login("max")
    assert status.state is LoginState.CANCELLED
    assert status.message.startswith("Die Claude-Anmeldung wurde abgebrochen.")
    assert not alive(facts_of(facts)["pid"]) and keychain == {}


async def test_a_new_start_replaces_a_running_login(api: RealApi, cli: Any) -> None:
    facts = cli(HANG)
    await api.start_login("max")
    assert await asyncio.to_thread(wait_until, facts.exists)
    first = facts_of(facts)["pid"]
    facts.unlink()
    await api.start_login("max")
    assert await asyncio.to_thread(wait_until, facts.exists)
    second = facts_of(facts)["pid"]
    assert first != second and not alive(first) and alive(second)
    await api.profiles.close()
    assert not alive(second)


async def test_deleting_a_profile_stops_its_login(api: RealApi, cli: Any) -> None:
    facts = cli(HANG)
    await api.start_login("max")
    assert await asyncio.to_thread(wait_until, facts.exists)
    await api.delete_profile("max")
    assert not alive(facts_of(facts)["pid"])


async def test_login_is_only_for_subscriptions(api: RealApi) -> None:
    with pytest.raises(ApiException) as caught:
        await api.start_login("api")
    assert caught.value.error.code is ErrorCode.INVALID
    with pytest.raises(ApiException) as caught:
        await api.start_login("gibt-es-nicht")
    assert caught.value.error.code is ErrorCode.NOT_FOUND
    status = await api.get_login_status("max")
    assert status.state is LoginState.CANCELLED


# -- A4: Codex login --------------------------------------------------------------------------


class FakeHandle:
    verification_url = "https://auth.example/codex/device"
    user_code = "ABCD-1234"

    def __init__(self, codex: "FakeCodex") -> None:
        self.codex = codex
        self.cancelled = False

    async def wait(self) -> SimpleNamespace:
        if self.codex.hang:
            await asyncio.sleep(60)
        self.codex.logged_in = self.codex.succeed
        return SimpleNamespace(success=self.codex.succeed, error="raw text of codex")

    async def cancel(self) -> None:
        self.cancelled = True


class FakeThread:
    def __init__(self, codex: "FakeCodex") -> None:
        self.codex = codex

    async def run(self, prompt: str, *, effort: Any) -> SimpleNamespace:
        self.codex.runs.append((prompt, effort.value))
        if self.codex.turn_error:
            return SimpleNamespace(
                status=SimpleNamespace(value="failed"),
                error=SimpleNamespace(message=self.codex.turn_error, codex_error_info=None),
            )
        return SimpleNamespace(status=SimpleNamespace(value="completed"), error=None)


class FakeCodex:
    def __init__(self, config: Any, **options: Any) -> None:
        self.config = config
        self.logged_in = options.get("logged_in", False)
        self.succeed = options.get("succeed", True)
        self.hang = options.get("hang", False)
        self.turn_error = options.get("turn_error")
        self.handle = FakeHandle(self)
        self.closed = False
        self.runs: list[tuple[str, str]] = []
        self.thread_options: dict[str, Any] = {}

    async def __aenter__(self) -> "FakeCodex":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.closed = True

    async def login_chatgpt_device_code(self) -> FakeHandle:
        return self.handle

    async def account(self) -> SimpleNamespace:
        if not self.logged_in:
            return SimpleNamespace(account=None)
        root = SimpleNamespace(type="chatgpt", plan_type=SimpleNamespace(value="plus"))
        return SimpleNamespace(account=SimpleNamespace(root=root))

    async def thread_start(self, **options: Any) -> FakeThread:
        self.thread_options = options
        return FakeThread(self)


@pytest.fixture
def codexes(api: RealApi, monkeypatch: pytest.MonkeyPatch) -> Any:
    built: list[FakeCodex] = []

    def use(**options: Any) -> list[FakeCodex]:
        def factory(config: Any) -> FakeCodex:
            built.append(FakeCodex(config, **options))
            return built[-1]

        api.profiles.logins.codex_factory = factory
        return built

    async def fake_check(profile: AuthProfile, reader: Any = None) -> CheckResult:
        return CheckResult(models=("gpt-6-luna",))

    monkeypatch.setattr("arr.service.real_api.profiles.check_profile", fake_check)
    return use


async def test_codex_login_goes_to_the_folder_of_the_profile(
    api: RealApi, codexes: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arr_home: Path
) -> None:
    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_text("unberührt")
    monkeypatch.setenv("HOME", str(fake_home))
    built = codexes()
    start = await api.start_login("chat")
    assert start.method == "device_code" and start.device_code == "ABCD-1234"
    assert start.browser_url == "https://auth.example/codex/device"
    status = await until_state(api, "chat", LoginState.DONE)
    assert status.profile_info.last_test.ok
    home = built[0].config.env["CODEX_HOME"]
    assert Path(home) == arr_home / "auth" / "chat" / "codex_home"
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    assert (fake_home / ".codex" / "auth.json").read_text() == "unberührt"
    assert [p.name for p in (fake_home / ".codex").iterdir()] == ["auth.json"]


async def test_codex_login_failure_and_cancel_use_fixed_texts(api: RealApi, codexes: Any) -> None:
    codexes(succeed=False)
    await api.start_login("chat")
    status = await until_state(api, "chat", LoginState.FAILED)
    assert status.message == "Die ChatGPT-Anmeldung hat nicht geklappt. Versuche es erneut."
    built = codexes(hang=True)
    await api.start_login("chat")
    status = await api.cancel_login("chat")
    assert status.state is LoginState.CANCELLED and built[-1].handle.cancelled


async def test_codex_login_that_cannot_start(api: RealApi, codexes: Any) -> None:
    def broken(config: Any) -> Any:
        raise RuntimeError(f"cannot start {MARKER}")

    api.profiles.logins.codex_factory = broken
    with pytest.raises(ApiException) as caught:
        await api.start_login("chat")
    assert caught.value.error.code is ErrorCode.UNAVAILABLE
    assert MARKER not in caught.value.error.message


async def test_codex_login_deadline(
    api: RealApi, codexes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = codexes(hang=True)
    monkeypatch.setattr("arr.service.login.CODEX_LOGIN_TIMEOUT_S", 0.2)
    await api.start_login("chat")
    status = await until_state(api, "chat", LoginState.FAILED)
    assert status.message == "Die ChatGPT-Anmeldung ist abgelaufen. Melde dich neu an."
    assert built[-1].handle.cancelled and built[-1].closed


async def test_codex_check_turn(tmp_path: Path, arr_home: Path) -> None:
    auth = AuthProfile(name="chat", kind=AuthKind.CODEX_CHATGPT)
    built: list[FakeCodex] = []

    def factory(**options: Any) -> Any:
        def build(config: Any) -> FakeCodex:
            built.append(FakeCodex(config, **options))
            return built[-1]

        return build

    good = await codex_check(auth, tmp_path, factory=factory(logged_in=True))
    assert good == CodexCheck(AccountState("chatgpt", "plus"))
    assert built[0].runs == [("Antworte nur mit: ok", "low")]
    assert built[0].thread_options["ephemeral"] is True
    assert built[0].thread_options["model"] == "gpt-6-luna"
    assert (await codex_check(auth, tmp_path, factory=factory())).problem == "login"
    expired = factory(logged_in=True, turn_error="401 Unauthorized")
    assert (await codex_check(auth, tmp_path, factory=expired)).problem == "login"
    limited = factory(logged_in=True, turn_error="You've hit your usage limit")
    assert (await codex_check(auth, tmp_path, factory=limited)).problem == "limit"
    other = factory(logged_in=True, turn_error="boom")
    assert (await codex_check(auth, tmp_path, factory=other)).problem == "other"


async def test_codex_test_reports_its_limit_window(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_check(profile: AuthProfile, cwd: Path) -> CodexCheck:
        limit = {"status": "allowed_warning", "utilization": 0.9, "resets_at": 1_800_000_000.0}
        return CodexCheck(AccountState("chatgpt", "plus"), limit=limit)  # type: ignore[arg-type]

    monkeypatch.setattr("arr.service.profile_check.codex_check", fake_check)
    result = await check_profile(AuthProfile(name="c", kind=AuthKind.CODEX_CHATGPT))
    assert result.failure is None and result.models == ("gpt-6-luna",)
    assert result.window.utilization == 0.9 and result.window.resets_at == 1_800_000_000.0


async def test_yaml_stays_loadable_after_writes(api: RealApi, arr_home: Path) -> None:
    from arr.models import ProfilesFile

    await api.create_profile(request())
    text = (arr_home / "profiles.yaml").read_text()
    assert text.startswith("# Auth-Profile von arr")
    file = load_yaml_model(arr_home / "profiles.yaml", ProfilesFile)
    assert [p.name for p in file.profiles] == ["max", "api", "chat", "neu"]


# -- A8: the page on real data ----------------------------------------------------------------


async def test_e2_claude_login_from_the_page(
    api: RealApi, cli: Any, keychain: dict, arr_home: Path
) -> None:
    """„Richte deinen Claude-Zugang ein“: 2 clicks and the name, the rest is the browser."""
    cli(PRINT_TOKEN)
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile")
        await see(user, "Neues Profil")
        user.find(marker="new-profile").click()
        await see(user, "Neues Auth-Profil")
        user.find(marker="field-new-profile-name").trigger("input", "max-privat")
        user.find(marker="form-confirm").click()
        await see(user, "✓ max-privat angemeldet und getestet.", retries=200)
        html = await see(user, "im Schlüsselbund ✓")
        assert "funktioniert" in html and "claude-haiku-5-5" in html
        assert MARKER not in html
    assert keychain[("arr", "max-privat")] == MARKER
    assert MARKER not in (arr_home / "profiles.yaml").read_text()


async def test_failed_login_offers_retry_and_api_key(api: RealApi, cli: Any) -> None:
    cli(FAIL)
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile")
        await see(user, "Mit Claude anmelden")
        user.find(marker="primary-max").click()
        await see(user, "max neu anmelden")
        user.find(marker="form-confirm").click()
        html = await see(user, "Die Claude-Anmeldung wurde abgebrochen", retries=200)
        assert "Erneut versuchen" in html and "Claude-API-Key verwenden" in html


async def test_cancel_on_the_page_ends_the_login(api: RealApi, cli: Any) -> None:
    facts = cli(HANG)
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile")
        await see(user, "Mit Claude anmelden")
        user.find(marker="primary-max").click()
        await see(user, "max neu anmelden")
        user.find(marker="form-confirm").click()
        await see(user, "arr führt dafür claude setup-token aus.", retries=200)
        assert await asyncio.to_thread(wait_until, facts.exists)
        user.find(marker="login-cancel").click()
        await see(user, "Erneut versuchen", retries=200)
    assert not alive(facts_of(facts)["pid"])


async def test_closing_the_dialog_ends_the_login(api: RealApi, cli: Any) -> None:
    facts = cli(HANG)
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile")
        await see(user, "Mit Claude anmelden")
        user.find(marker="primary-max").click()
        await see(user, "max neu anmelden")
        user.find(marker="form-confirm").click()
        await see(user, "arr führt dafür claude setup-token aus.", retries=200)
        assert await asyncio.to_thread(wait_until, facts.exists)
        pid = facts_of(facts)["pid"]
        user.find(marker="form-dialog").trigger("close")
        assert await asyncio.to_thread(wait_until, lambda: not alive(pid))
    assert (await api.get_login_status("max")).state is LoginState.CANCELLED


async def test_mac_login_card_and_failed_test_on_the_page(
    api: RealApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile")
        await see(user, "nicht verbunden")
        user.find(marker="test-max").click()
        html = await see(user, "Claude-Login dieses Macs", retries=200)
        assert "funktioniert" in html

        async def rejected(prompt: str, options: Any) -> AsyncIterator[Any]:
            yield AssistantMessage(content=[], model="m", error="authentication_failed")
            yield ok_result(is_error=True)

        monkeypatch.setattr("arr.service.profile_check._sdk_query", rejected)
        user.find(marker="test-max").click()
        html = await see(user, "Test fehlgeschlagen", retries=200)
        assert "Die Anmeldung von „max“ ist abgelaufen" in html and "Neu anmelden" in html


async def test_api_key_profile_from_the_page(api: RealApi, keychain: dict, calls: list) -> None:
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile?neu=1")
        await see(user, "Neues Auth-Profil")
        user.find(marker="radio-kind-claude_api_key").trigger("change")
        await see(user, "Speichern und testen")
        user.find(marker="field-new-profile-name").trigger("input", "api-neu")
        user.find(marker="field-new-key").trigger("input", KEY)
        user.find(marker="form-confirm").click()
        html = await see(user, "kommt mit Auftrag W3-04", retries=200)
        assert KEY not in html
    assert keychain[("arr", "api-neu")] == KEY and calls


async def test_default_button_shows_the_message_until_w3_04(api: RealApi) -> None:
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile")
        await see(user, "Standard für Claude")
        user.find(marker="default-max").click()
        await see(user, "kommt mit Auftrag W3-04", retries=200)
