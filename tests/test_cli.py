import logging
import platform
from pathlib import Path

import pytest
from typer.testing import CliRunner

from arr import __version__
from arr.cli import app
from arr.log import setup_logging
from arr.settings import app_dir

pytest_plugins = ["pytester"]

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"arr {__version__}"


def test_info_uses_arr_home(arr_home: Path) -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert __version__ in result.output
    assert platform.python_version() in result.output
    assert str(arr_home) in result.output
    assert not arr_home.exists(), "info must not create the app directory"


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "info" in result.output


def test_app_dir_defaults_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARR_HOME", raising=False)
    assert app_dir() == Path.home() / ".arr"


def test_app_dir_respects_arr_home(arr_home: Path) -> None:
    assert app_dir() == arr_home


def test_app_dir_makes_relative_arr_home_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARR_HOME", "rel/home")
    home = app_dir()
    assert home.is_absolute()
    assert home == tmp_path / "rel" / "home"
    monkeypatch.chdir(tmp_path.parent)
    assert home == tmp_path / "rel" / "home", "result must not depend on later chdir"


def test_app_dir_expands_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARR_HOME", "~/arr-x")
    assert app_dir() == Path.home() / "arr-x"


def test_gui_without_mock_points_to_serve() -> None:
    # `arr gui --mock` starts the prototype (W1-06, tests/test_gui*.py); real data: `arr serve`.
    result = runner.invoke(app, ["gui"])
    assert result.exit_code == 1
    assert "arr serve" in result.output


def test_init_and_gui_call_their_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[object] = []
    monkeypatch.setattr("arr.workfolder.init_workfolder", lambda p: calls.append(p) or tmp_path)
    monkeypatch.setattr("arr.service.gui.run", lambda **kw: calls.append(kw))

    result = runner.invoke(app, ["init", "wf"])
    assert result.exit_code == 0
    assert f"Workfolder bereit: {tmp_path}" in result.output
    result = runner.invoke(app, ["gui", "--mock", "--port", "8123", "--no-browser"])
    assert result.exit_code == 0
    assert calls == [Path("wf"), {"mock": True, "port": 8123, "open_browser": False}]


def test_setup_logging_writes_to_stderr(
    capsys: pytest.CaptureFixture[str], restore_root_logger: logging.Logger
) -> None:
    setup_logging("debug")
    logging.getLogger("arr.test").debug("hello")
    err = capsys.readouterr().err
    assert "DEBUG" in err
    assert "arr.test: hello" in err


LOGGING_TESTS = """
import logging

from arr.log import LOG_FORMAT, setup_logging

ORIGINAL_LEVEL = logging.getLogger().level


def test_a_changes_logging({fixture}):
    setup_logging("debug")
    logging.getLogger("arr.test").debug("hello")


def test_b_root_logger_is_clean():
    root = logging.getLogger()
    assert root.level == ORIGINAL_LEVEL
    assert not [h for h in root.handlers if getattr(h.formatter, "_fmt", None) == LOG_FORMAT]
"""


@pytest.mark.parametrize(("fixture", "passed"), [("restore_root_logger", 2), ("", 1)])
def test_restore_root_logger_fixture(pytester: pytest.Pytester, fixture: str, passed: int) -> None:
    pytester.makeconftest((Path(__file__).parent / "conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(test_logging_state=LOGGING_TESTS.format(fixture=fixture))
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=passed, failed=2 - passed)


# --- arr auth codex-login (W2-02) --------------------------------------------------------


class _LoginFake:
    """Stands in for the app-server of `arr.backends.codex_login` (no network)."""

    def __init__(self, *, logged_in: bool = False, succeed: bool = True) -> None:
        self.logged_in = logged_in
        self.succeed = succeed
        self.homes: list[str] = []

    def __call__(self, config: object) -> "_LoginFake":
        self.homes.append(config.env["CODEX_HOME"])  # type: ignore[attr-defined]
        return self

    async def __aenter__(self) -> "_LoginFake":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def account(self, *, refresh_token: bool = False) -> object:
        from types import SimpleNamespace

        if not self.logged_in:
            return SimpleNamespace(account=None)
        root = SimpleNamespace(type="chatgpt", plan_type="plus", email="someone@example.org")
        return SimpleNamespace(account=SimpleNamespace(root=root))

    async def login_chatgpt_device_code(self) -> object:
        from types import SimpleNamespace

        fake = self

        class Handle(SimpleNamespace):
            async def wait(self) -> object:
                fake.logged_in = fake.succeed
                return SimpleNamespace(success=fake.succeed, error=None if fake.succeed else "nope")

            async def cancel(self) -> None:
                return None

        return Handle(verification_url="https://auth.example/device", user_code="WXYZ-9876")


@pytest.fixture
def login_fake(monkeypatch: pytest.MonkeyPatch) -> _LoginFake:
    from arr.backends import codex_login

    fake = _LoginFake()
    monkeypatch.setattr(codex_login, "_default_factory", fake)
    return fake


def test_codex_login_shows_link_code_and_result(arr_home: Path, login_fake: _LoginFake) -> None:
    result = runner.invoke(app, ["auth", "codex-login", "chatgpt-privat"])
    assert result.exit_code == 0, result.output
    assert "https://auth.example/device" in result.output
    assert "WXYZ-9876" in result.output
    assert "Angemeldet: ChatGPT (plus)" in result.output
    assert "someone@example.org" not in result.output
    assert login_fake.homes[-1] == str(arr_home / "auth/chatgpt-privat/codex_home")


def test_codex_login_already_logged_in(arr_home: Path, login_fake: _LoginFake) -> None:
    login_fake.logged_in = True
    result = runner.invoke(app, ["auth", "codex-login", "chatgpt-privat"])
    assert result.exit_code == 0
    assert "bereits angemeldet: ChatGPT (plus)" in result.output
    assert "Code:" not in result.output


def test_codex_login_failure(arr_home: Path, login_fake: _LoginFake) -> None:
    login_fake.succeed = False
    result = runner.invoke(app, ["auth", "codex-login", "chatgpt-privat"])
    assert result.exit_code == 1
    assert "Der Login ist fehlgeschlagen: nope" in result.output


def test_codex_login_uses_login_dir_from_profiles(
    arr_home: Path, login_fake: _LoginFake, tmp_path: Path
) -> None:
    arr_home.mkdir(parents=True)
    (arr_home / "profiles.yaml").write_text(
        "version: 1\nprofiles:\n"
        f"  - {{name: chatgpt-x, kind: codex_chatgpt, login_dir: {tmp_path / 'login'}}}\n"
        "  - {name: max-privat, kind: claude_oauth}\n"
    )
    result = runner.invoke(app, ["auth", "codex-login", "chatgpt-x"])
    assert result.exit_code == 0, result.output
    assert login_fake.homes[-1] == str(tmp_path / "login")
    other = runner.invoke(app, ["auth", "codex-login", "max-privat"])
    assert other.exit_code == 1
    assert "kein ChatGPT-Profil" in other.output


def test_codex_login_rejects_bad_profile_name(arr_home: Path, login_fake: _LoginFake) -> None:
    result = runner.invoke(app, ["auth", "codex-login", "Not a slug"])
    assert result.exit_code == 1
    assert "kein gültiger Profilname" in result.output
