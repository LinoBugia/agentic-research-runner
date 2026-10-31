"""Auth profiles on the real API (W3-05 A2–A7): keychain and CLI are fakes, the rest is real."""

import asyncio
import json
import logging
import os
import stat
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, RateLimitEvent, RateLimitInfo, ResultMessage
from login_kit import (
    FAIL,
    HANG,
    MARKER,
    PRINT_TOKEN,
    alive,
    facts_of,
    fake_cli,
    pids_of,
    wait_until,
)
from pydantic import SecretStr, ValidationError
from run_kit import write_profiles
from service_kit import make_workfolder
from test_gui import gui_user
from test_gui_real import page_html, see

from arr.backends.codex_config import codex_home_for
from arr.backends.codex_login import (
    AccountState,
    CodexCheck,
    ForeignLoginFolder,
    codex_check,
    own_login_folder,
    prepare_login_dir,
)
from arr.models import AuthKind, AuthProfile, ProfilesFile, RunStatus
from arr.resolve import load_yaml_model
from arr.service.api_models import (
    ApiException,
    CreateProfileRequest,
    ErrorCode,
    LoginState,
)
from arr.service.profile_check import CheckFailure, CheckResult, check_profile, to_profile_test
from arr.service.real_api import RealApi
from arr.service.real_api.profiles import STATUS_FILE
from arr.settings import app_dir

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


def missing(facts: Path) -> None:
    """Remove the fake CLI that wrote to `facts`."""
    (facts.parent / "claude").unlink()


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

    assert own_login_folder(chat(str(outside))) is None
    assert own_login_folder(chat(str(arr_home / "auth" / "anderes"))) is None
    monkeypatch.setenv("HOME", str(tmp_path))
    assert own_login_folder(chat("~/.codex")) is None
    (arr_home / "auth").mkdir(parents=True)
    (arr_home / "auth" / "chat").symlink_to(outside)
    assert own_login_folder(chat()) is None, "a symlink must not lead outside"
    own = AuthProfile(name="chat2", kind=AuthKind.CODEX_CHATGPT)
    assert own_login_folder(own) == (arr_home / "auth" / "chat2").resolve()


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
    monkeypatch.setattr("arr.service.login.CLAUDE_LOGIN_TIMEOUT_S", 3.0)
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
        html = await see(user, "Login dieses Macs", retries=200)
        assert "Nutzt den Claude-Login dieses Macs, kein eigenes Token." in html

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


# -- W3-05.1: „Key ersetzen“ -------------------------------------------------------------------


NEW_KEY = "sk-ant-api03-NEWMARKER-9876543210"


async def rejected_key(prompt: str, options: Any) -> AsyncIterator[Any]:
    yield AssistantMessage(content=[], model="m", error="authentication_failed")
    yield ok_result(is_error=True)


async def test_replacing_a_key_keeps_the_profile(
    api: RealApi, keychain: dict, arr_home: Path
) -> None:
    keychain[("arr", "api")] = KEY
    before = (arr_home / "profiles.yaml").read_text()
    info = await api.create_profile(
        CreateProfileRequest(
            name="api", kind=AuthKind.CLAUDE_API_KEY, api_key=SecretStr(NEW_KEY), replace=True
        )
    )
    assert keychain[("arr", "api")] == NEW_KEY and info.secret_present and info.last_test is None
    assert NEW_KEY not in info.model_dump_json()
    assert (arr_home / "profiles.yaml").read_text() == before


async def test_a_failed_replacement_keeps_the_old_key(
    api: RealApi, keychain: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain[("arr", "api")] = KEY

    def refuse(*_a: Any) -> None:
        raise RuntimeError(f"keychain says no to {NEW_KEY}")

    monkeypatch.setattr("arr.auth._keyring_set", refuse)
    request = CreateProfileRequest(
        name="api", kind=AuthKind.CLAUDE_API_KEY, api_key=SecretStr(NEW_KEY), replace=True
    )
    with pytest.raises(ApiException) as caught:
        await api.create_profile(request)
    assert caught.value.error.code is ErrorCode.UNAVAILABLE
    assert NEW_KEY not in caught.value.error.message
    assert keychain[("arr", "api")] == KEY
    assert "api" in [p.name for p in await api.list_profiles()]


async def test_replacing_needs_a_matching_profile(api: RealApi, keychain: dict) -> None:
    for name, kind, code in [
        ("gibt-es-nicht", AuthKind.CLAUDE_API_KEY, ErrorCode.NOT_FOUND),
        ("chat", AuthKind.CLAUDE_API_KEY, ErrorCode.INVALID),
    ]:
        request = CreateProfileRequest(
            name=name, kind=kind, api_key=SecretStr(NEW_KEY), replace=True
        )
        with pytest.raises(ApiException) as caught:
            await api.create_profile(request)
        assert caught.value.error.code is code
    assert keychain == {}
    with pytest.raises(ApiException, match="Ersetzen"):
        CreateProfileRequest(name="x", kind=AuthKind.CLAUDE_OAUTH, replace=True)


async def test_key_replaced_on_the_page_for_a_profile_without_a_limit(
    api: RealApi, keychain: dict, monkeypatch: pytest.MonkeyPatch, arr_home: Path
) -> None:
    """The profile has no entry in `arr.yaml`, so its limit is 0: the review's H1."""
    keychain[("arr", "api")] = KEY
    monkeypatch.setattr("arr.service.profile_check._sdk_query", rejected_key)
    await api.test_profile("api")
    assert (await api.list_profiles())[1].concurrency.limit == 0

    async def good(prompt: str, options: Any) -> AsyncIterator[Any]:
        yield ok_result()

    monkeypatch.setattr("arr.service.profile_check._sdk_query", good)
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile")
        await see(user, "wurde abgelehnt (401)")
        user.find(marker="primary-api").click()
        await see(user, "Neuer API-Key")
        user.find(marker="field-replace-key").trigger("input", NEW_KEY)
        user.find(marker="form-confirm").click()
        html = await see(user, "✓ Key von api ersetzt und getestet.", retries=200)
        assert "wurde abgelehnt" not in html
    assert keychain[("arr", "api")] == NEW_KEY
    assert "api" in (arr_home / "profiles.yaml").read_text()


# -- W3-05.1: login flows (M2, M3, M4, G7) -------------------------------------------------------


class SlowKeychain:
    """The write hangs like a system dialog until `release`; the entry appears only then."""

    def __init__(self, store: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        self.store = store
        self.entered = threading.Event()
        self.release = threading.Event()
        monkeypatch.setattr("arr.auth._keyring_set", self.write)

    def write(self, service: str, account: str, value: str) -> None:
        self.entered.set()
        assert self.release.wait(10)
        self.store[(service, account)] = value


async def test_cancel_during_the_keychain_write_leaves_no_token(
    api: RealApi, cli: Any, keychain: dict, monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    slow = SlowKeychain(keychain, monkeypatch)
    cli(PRINT_TOKEN)
    await api.start_login("max")
    assert await asyncio.to_thread(slow.entered.wait, 10)
    cancelling = asyncio.create_task(api.cancel_login("max"))
    await asyncio.sleep(0.05)
    assert not cancelling.done(), "cancel must wait for the write"
    slow.release.set()
    status = await cancelling
    assert status.state is LoginState.CANCELLED and keychain == {}
    assert calls == [], "no test after a cancelled login"
    listed = next(p for p in await api.list_profiles() if p.name == "max")
    assert not listed.secret_present and listed.last_test is None


async def test_delete_during_the_keychain_write_leaves_no_token(
    api: RealApi, cli: Any, keychain: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = SlowKeychain(keychain, monkeypatch)
    cli(PRINT_TOKEN)
    await api.start_login("max")
    assert await asyncio.to_thread(slow.entered.wait, 10)
    deleting = asyncio.create_task(api.delete_profile("max"))
    await asyncio.sleep(0.05)
    slow.release.set()
    await deleting
    await asyncio.sleep(0.1)  # nothing may come in after the delete
    assert keychain == {}
    assert "max" not in [p.name for p in await api.list_profiles()]


async def test_cancel_during_the_test_after_the_login_takes_the_token_back(
    api: RealApi, cli: Any, keychain: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    async def slow_test(prompt: str, options: Any) -> AsyncIterator[Any]:
        started.set()
        await asyncio.sleep(30)
        yield ok_result()

    monkeypatch.setattr("arr.service.profile_check._sdk_query", slow_test)
    cli(PRINT_TOKEN)
    await api.start_login("max")
    await asyncio.wait_for(started.wait(), 10)
    assert (await api.get_login_status("max")).message == "Testen …"
    status = await api.cancel_login("max")
    assert status.state is LoginState.CANCELLED and keychain == {}
    listed = next(p for p in await api.list_profiles() if p.name == "max")
    assert not listed.secret_present and listed.last_test is None


async def test_a_token_that_cannot_be_taken_back_is_named(
    api: RealApi, cli: Any, keychain: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = SlowKeychain(keychain, monkeypatch)

    def refuse(*_a: Any) -> None:
        raise RuntimeError("no")

    monkeypatch.setattr("arr.auth._keyring_delete", refuse)
    cli(PRINT_TOKEN)
    await api.start_login("max")
    assert await asyncio.to_thread(slow.entered.wait, 10)
    cancelling = asyncio.create_task(api.cancel_login("max"))
    slow.release.set()
    status = await cancelling
    assert "Das Token liegt noch im Schlüsselbund." in status.message
    assert next(p for p in await api.list_profiles() if p.name == "max").secret_present


async def test_any_error_of_the_login_ends_the_job(
    api: RealApi, cli: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Not only `OSError` of the start: nothing may stay on „Warte auf die Anmeldung“."""
    caplog.set_level(logging.DEBUG)
    cli(PRINT_TOKEN)

    def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError(f"temp is broken: {MARKER}")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", broken)
    await api.start_login("max")
    status = await until_state(api, "max", LoginState.FAILED)
    assert status.message.startswith("Die Claude-Anmeldung ließ sich nicht starten.")
    assert "RuntimeError" in caplog.text and MARKER not in caplog.text


async def test_missing_cli_says_it_could_not_start(api: RealApi, cli: Any, tmp_path: Path) -> None:
    missing(cli(HANG))
    await api.start_login("max")
    status = await until_state(api, "max", LoginState.FAILED)
    assert status.message.startswith("Die Claude-Anmeldung ließ sich nicht starten.")


async def test_parallel_starts_leave_one_login_that_cancel_reaches(api: RealApi, cli: Any) -> None:
    facts = cli(HANG)
    await asyncio.gather(*(api.start_login("max") for _ in range(3)))
    assert await asyncio.to_thread(wait_until, facts.exists)
    await api.cancel_login("max")
    assert await asyncio.to_thread(wait_until, lambda: all(not alive(p) for p in pids_of(facts)))
    assert len(api.profiles.logins.jobs) == 1


async def test_close_of_the_api_ends_logins_and_refuses_new_ones(api: RealApi, cli: Any) -> None:
    facts = cli(HANG)
    await asyncio.gather(*(api.start_login("max") for _ in range(2)))
    assert await asyncio.to_thread(wait_until, facts.exists)
    await api.close()
    assert await asyncio.to_thread(wait_until, lambda: all(not alive(p) for p in pids_of(facts)))
    with pytest.raises(ApiException) as caught:
        await api.start_login("max")
    assert caught.value.error.code is ErrorCode.UNAVAILABLE


# -- W3-05.1: M1, parallel writes ----------------------------------------------------------------


async def test_parallel_creates_keep_every_profile(
    api: RealApi, keychain: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(service: str, account: str, value: str) -> None:
        time.sleep(0.02)
        keychain[(service, account)] = value

    monkeypatch.setattr("arr.auth._keyring_set", slow)
    await asyncio.gather(*(api.create_profile(request(f"neu-{i}")) for i in range(4)))
    names = [p.name for p in await api.list_profiles()]
    assert {f"neu-{i}" for i in range(4)} <= set(names)
    fresh = load_yaml_model(app_dir() / "profiles.yaml", ProfilesFile)
    assert {p.name for p in fresh.profiles} >= {f"neu-{i}" for i in range(4)}
    assert len(keychain) == 4


async def test_parallel_creates_of_one_name_leave_no_orphan(
    api: RealApi, keychain: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(service: str, account: str, value: str) -> None:
        time.sleep(0.02)
        keychain[(service, account)] = value

    monkeypatch.setattr("arr.auth._keyring_set", slow)
    results = await asyncio.gather(
        *(api.create_profile(request("neu", key=f"{KEY}-{i}")) for i in range(3)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ApiException) for r in results) == 2
    assert len(keychain) == 1


async def test_the_file_is_read_again_after_the_keychain_dialog(
    api: RealApi, keychain: dict, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def edited_by_hand_meanwhile(service: str, account: str, value: str) -> None:
        keychain[(service, account)] = value
        path = arr_home / "profiles.yaml"
        path.write_text(path.read_text() + "  - {name: von-hand, kind: claude_oauth}\n")

    monkeypatch.setattr("arr.auth._keyring_set", edited_by_hand_meanwhile)
    await api.create_profile(request())
    assert {"neu", "von-hand"} <= {p.name for p in await api.list_profiles()}


# -- W3-05.1: M7, foreign login folders ----------------------------------------------------------


@pytest.fixture
def foreign(wf: Path, arr_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A profile with `login_dir: ~/.codex` of a test HOME; returns that folder (mode 0755)."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    os.chmod(home / ".codex", 0o755)
    monkeypatch.setenv("HOME", str(home))
    (arr_home / "profiles.yaml").write_text(
        "version: 1\nprofiles:\n  - {name: fremd, kind: codex_chatgpt, login_dir: ~/.codex}\n"
    )
    return home / ".codex"


async def test_no_login_and_no_chmod_in_a_foreign_folder(
    foreign: Path, wf: Path, keychain: dict
) -> None:
    api = RealApi(wf, base_url=BASE, scan=False)
    with pytest.raises(ApiException) as caught:
        await api.start_login("fremd")
    assert caught.value.error.code is ErrorCode.NOT_ALLOWED
    assert "außerhalb von arr" in caught.value.error.message
    info = await api.test_profile("fremd")
    assert not info.last_test.ok and "außerhalb von arr" in info.last_test.message
    assert stat.S_IMODE(os.stat(foreign).st_mode) == 0o755
    assert api.profiles.logins.jobs == {}


def test_prepare_login_dir_only_in_arrs_folders(tmp_path: Path, arr_home: Path) -> None:
    outside = tmp_path / "anderswo"
    profile = AuthProfile(name="c", kind=AuthKind.CODEX_CHATGPT, login_dir=str(outside))
    with pytest.raises(ForeignLoginFolder, match="außerhalb von arr"):
        prepare_login_dir(profile)
    assert not outside.exists()
    own = AuthProfile(name="c", kind=AuthKind.CODEX_CHATGPT)
    assert prepare_login_dir(own) == arr_home / "auth" / "c" / "codex_home"


# -- W3-05.1: Mac login (point 6), G8 ------------------------------------------------------------


async def test_the_mac_login_is_named_and_is_no_login_of_the_profile(
    api: RealApi, keychain: dict, calls: list
) -> None:
    test = (await api.test_profile("max")).last_test
    assert test.ok and test.message == "Nutzt den Claude-Login dieses Macs, kein eigenes Token."
    keychain[("arr", "max")] = MARKER
    assert (await api.test_profile("max")).last_test.message is None


async def test_a_cancelled_login_does_not_appear_as_logged_in(
    api: RealApi, cli: Any, calls: list
) -> None:
    facts = cli(HANG)
    await api.create_profile(request("abo", key=None))
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await api.start_login("abo")
        assert await asyncio.to_thread(wait_until, facts.exists)
        await api.cancel_login("abo")
        await user.open("/profile/abo")
        html = await see(user, "nicht verbunden")
        assert "noch keins – melde dich an" in html
        user.find(marker="test-abo").click()
        html = await see(user, "Login dieses Macs", retries=200)
        assert "kein eigenes Token" in html and "funktioniert" not in html
        assert "Mit Claude anmelden" in html


async def test_a_test_with_a_token_stays_out_of_the_claude_folder(
    api: RealApi, keychain: dict, calls: list
) -> None:
    keychain[("arr", "api")] = KEY
    keychain[("arr", "max")] = MARKER
    await api.test_profile("api")
    await api.test_profile("max")
    assert all("CLAUDE_CONFIG_DIR" in options.env for options in calls)
    assert all("no-session-persistence" in options.extra_args for options in calls)
    keychain.clear()
    await api.test_profile("max")
    assert "CLAUDE_CONFIG_DIR" not in calls[-1].env, "the Mac login needs its real folder"
    assert "no-session-persistence" in calls[-1].extra_args


# -- W3-05.1: page (M5, M6, G1, G5, G6, G7) ------------------------------------------------------


def confirm_of(user: Any) -> Any:
    return next(iter(user.find(marker="form-confirm").elements))


async def test_every_dialog_has_its_confirm_button_after_a_login(
    api: RealApi, cli: Any, keychain: dict
) -> None:
    cli(FAIL)
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile")
        await see(user, "Mit Claude anmelden")
        user.find(marker="primary-max").click()
        await see(user, "max neu anmelden")
        user.find(marker="form-confirm").click()
        await see(user, "Erneut versuchen", retries=200)
        assert "display" in confirm_of(user)._style
        user.find(marker="form-dialog").trigger("close")
        await see(user, "Bearbeiten", retries=200)  # the list is loaded again
        for marker, title in [
            ("edit-max", "max bearbeiten"),
            ("primary-max", "max neu anmelden"),
            ("primary-api", "Key ersetzen"),
        ]:
            user.find(marker=marker).click()
            await see(user, title)
            assert "display" not in confirm_of(user)._style, title


async def test_closing_the_dialog_shows_the_profile_of_the_cancelled_login(
    api: RealApi, cli: Any
) -> None:
    facts = cli(HANG)
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile")
        await see(user, "Neues Profil")
        user.find(marker="new-profile").click()
        await see(user, "Neues Auth-Profil")
        user.find(marker="field-new-profile-name").trigger("input", "abo-neu")
        user.find(marker="form-confirm").click()
        await see(user, "arr führt dafür claude setup-token aus.", retries=200)
        assert await asyncio.to_thread(wait_until, facts.exists)
        assert "profil-abo-neu" not in page_html(user)
        user.find(marker="form-dialog").trigger("close")
        await see(user, "abo-neu", retries=200)
    assert (await api.get_login_status("abo-neu")).state is LoginState.CANCELLED


async def test_confirm_is_locked_while_the_action_runs(
    api: RealApi, cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli(HANG)
    created: list[str] = []
    release = asyncio.Event()
    original = api.profiles.create_profile

    async def slow(create: CreateProfileRequest) -> Any:
        created.append(create.name)
        await release.wait()
        return await original(create)

    monkeypatch.setattr(api.profiles, "create_profile", slow)
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile?neu=1")
        await see(user, "Neues Auth-Profil")
        user.find(marker="field-new-profile-name").trigger("input", "abo-neu")
        user.find(marker="form-confirm").click()
        for _ in range(100):
            if created:
                break
            await asyncio.sleep(0.01)
        assert "disabled" in confirm_of(user)._props
        user.find(marker="form-confirm").click()
        await asyncio.sleep(0.05)
        release.set()
        html = await see(user, "arr führt dafür claude setup-token aus.", retries=200)
        assert "gibt es schon" not in html
        await api.cancel_login("abo-neu")
    assert created == ["abo-neu"]


async def test_the_key_leaves_the_page_with_the_dialog(
    api: RealApi, keychain: dict, calls: list
) -> None:
    def everything(user: Any) -> str:
        with user.client:
            return " ".join(f"{e._props} {e._text}" for e in user.client.elements.values())

    keychain[("arr", "api")] = KEY
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile?neu=1")
        await see(user, "Neues Auth-Profil")
        user.find(marker="radio-kind-claude_api_key").trigger("change")
        await see(user, "Speichern und testen")
        user.find(marker="field-new-profile-name").trigger("input", "api-neu")
        user.find(marker="field-new-key").trigger("input", NEW_KEY)
        user.find(marker="form-confirm").click()
        await see(user, "api-neu", retries=200)
        for _ in range(100):
            if NEW_KEY not in everything(user):
                break
            await asyncio.sleep(0.01)
        assert NEW_KEY not in everything(user)
        user.find(marker="test-api").click()
        await see(user, "funktioniert", retries=200)
        user.find(marker="new-profile").click()
        await see(user, "Neues Auth-Profil")
        user.find(marker="radio-kind-claude_api_key").trigger("change")
        await see(user, "Speichern und testen")
        user.find(marker="field-new-key").trigger("input", "typed-and-then-left-alone")
        user.find(marker="form-dialog").trigger("close")
        for _ in range(100):
            if "typed-and-then-left-alone" not in everything(user):
                break
            await asyncio.sleep(0.01)
        assert "typed-and-then-left-alone" not in everything(user)


async def test_help_text_fits_the_kind(api: RealApi) -> None:
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile?neu=1")
        await see(user, "übernimmt das Token und legt es im Schlüsselbund ab")
        user.find(marker="radio-kind-codex_chatgpt").trigger("change")
        html = await see(user, "zeigt dir einen Link und einen Code")
        assert "im Ordner des Profils ab" in html
        assert "Anmeldung mit Link und Code." in html and "oder per Code" not in html


async def test_api_key_button_after_a_failed_login_logs_no_error(
    api: RealApi, cli: Any, caplog: pytest.LogCaptureFixture
) -> None:
    cli(FAIL)
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile")
        await see(user, "Mit Claude anmelden")
        user.find(marker="primary-max").click()
        await see(user, "max neu anmelden")
        user.find(marker="form-confirm").click()
        await see(user, "Claude-API-Key verwenden", retries=200)
        caplog.clear()
        user.find(marker="use-api-key").click()
        await see(user, "Neues Auth-Profil", retries=200)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], caplog.text


async def test_page_says_testing_after_the_token_and_reports_a_failed_test(
    api: RealApi, cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()

    async def slow_test(prompt: str, options: Any) -> AsyncIterator[Any]:
        await release.wait()
        yield AssistantMessage(content=[], model="m", error="authentication_failed")
        yield ok_result(is_error=True)

    monkeypatch.setattr("arr.service.profile_check._sdk_query", slow_test)
    monkeypatch.setattr("arr.service.gui.pages.profile.LOGIN_POLL_S", 0.05)
    cli(PRINT_TOKEN)
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile")
        await see(user, "Mit Claude anmelden")
        user.find(marker="primary-max").click()
        await see(user, "max neu anmelden")
        user.find(marker="form-confirm").click()
        html = await see(user, "<b>Testen …</b>", retries=200)
        assert "Warte auf die Anmeldung" not in html
        release.set()
        html = await see(user, "Die Anmeldung von „max“ ist abgelaufen", retries=200)
        assert "angemeldet und getestet" not in html


async def test_a_login_whose_test_breaks_does_not_claim_a_test(
    api: RealApi, cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(profile: AuthProfile, reader: Any = None) -> CheckResult:
        raise RuntimeError("no test")

    monkeypatch.setattr("arr.service.real_api.profiles.check_profile", broken)
    monkeypatch.setattr("arr.service.gui.pages.profile.LOGIN_POLL_S", 0.05)
    cli(PRINT_TOKEN)
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await user.open("/profile")
        await see(user, "Mit Claude anmelden")
        user.find(marker="primary-max").click()
        await see(user, "max neu anmelden")
        user.find(marker="form-confirm").click()
        html = await see(user, "Angemeldet. Teste das Profil auf seiner Karte.", retries=200)
        assert "angemeldet und getestet" not in html


# -- W3-05.1: small points (G2, G3, G4) ----------------------------------------------------------


def test_a_wrong_combination_never_returns_the_key() -> None:
    raw_request = {"name": "x", "kind": "claude_oauth", "api_key": KEY}
    with pytest.raises(ApiException) as caught:
        CreateProfileRequest.model_validate(raw_request)
    assert KEY not in caught.value.error.message and KEY not in str(caught.value)


def test_validation_errors_of_a_request_hide_the_key() -> None:
    raw_request = {"name": "UNGÜLTIG", "kind": "claude_api_key", "api_key": KEY}
    with pytest.raises(ValidationError) as caught:
        CreateProfileRequest.model_validate(raw_request)
    assert KEY not in str(caught.value) and KEY not in repr(caught.value)
    assert KEY not in caught.value.json() and KEY not in repr(caught.value.errors())


def test_str_of_a_validation_error_hides_a_key_in_a_wrong_field() -> None:
    raw_request = {"name": "x", "kind": "claude_oauth", "token": KEY}
    with pytest.raises(ValidationError) as caught:
        CreateProfileRequest.model_validate(raw_request)
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("content", ["[]", "null", "1", '"text"', '{"max": 1}', '{"max": []}'])
async def test_damaged_status_files_are_empty(api: RealApi, arr_home: Path, content: str) -> None:
    (arr_home / STATUS_FILE).write_text(content)
    assert all(p.last_test is None for p in await api.list_profiles())
    assert (await api.test_profile("max")).last_test.ok


async def test_profiles_file_with_broken_bytes_is_a_message(
    wf: Path, arr_home: Path, keychain: dict
) -> None:
    (arr_home / "profiles.yaml").write_bytes(b"version: 1\nprofiles: \xff\xfe\n")
    api = RealApi(wf, base_url=BASE, scan=False)
    for call in (api.list_profiles(), api.create_profile(request()), api.delete_profile("max")):
        with pytest.raises(ApiException) as caught:
            await call
        assert "profiles.yaml ließ sich nicht lesen" in caught.value.error.message
    assert keychain == {}


@pytest.fixture
def read_only(arr_home: Path) -> Iterator[Any]:
    locked: list[Path] = []

    def lock(path: Path) -> None:
        path.chmod(0o500)
        locked.append(path)

    yield lock
    for path in locked:
        path.chmod(0o700)


async def test_delete_in_a_read_only_folder_keeps_everything(
    api: RealApi, keychain: dict, arr_home: Path, read_only: Any
) -> None:
    keychain[("arr", "api")] = KEY
    read_only(arr_home)
    with pytest.raises(ApiException) as caught:
        await api.delete_profile("api")
    assert "ließ sich nicht speichern" in caught.value.error.message
    assert keychain == {("arr", "api"): KEY}
    assert "api" in (arr_home / "profiles.yaml").read_text()


async def test_a_login_folder_that_stays_is_named_and_the_profile_stays(
    api: RealApi, keychain: dict, arr_home: Path, read_only: Any
) -> None:
    home = codex_home_for(AuthProfile(name="chat", kind=AuthKind.CODEX_CHATGPT))
    home.mkdir(parents=True)
    (home / "auth.json").write_text("{}")
    read_only(home)
    with pytest.raises(ApiException) as caught:
        await api.delete_profile("chat")
    assert "auth.json" in caught.value.error.message
    assert "chat" in [p.name for p in await api.list_profiles()]
    assert "chat" in (arr_home / "profiles.yaml").read_text()


async def test_a_keychain_item_that_stays_keeps_the_profile(
    api: RealApi, keychain: dict, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keychain[("arr", "api")] = KEY

    def refuse(*_a: Any) -> None:
        raise RuntimeError("no")

    monkeypatch.setattr("arr.auth._keyring_delete", refuse)
    with pytest.raises(ApiException) as caught:
        await api.delete_profile("api")
    assert "Schlüsselbund" in caught.value.error.message
    assert "api" in (arr_home / "profiles.yaml").read_text()


async def test_a_status_that_cannot_be_written_is_a_message(
    api: RealApi, calls: list, arr_home: Path, read_only: Any
) -> None:
    read_only(arr_home)
    with pytest.raises(ApiException) as caught:
        await api.test_profile("max")
    assert "ließ sich nicht speichern" in caught.value.error.message
