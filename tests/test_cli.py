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


def test_init_placeholder() -> None:
    result = runner.invoke(app, ["init", "irgendwo"])
    assert result.exit_code == 1
    assert "„arr init“ kommt mit Auftrag W1-02." in result.output


def test_gui_mock_placeholder() -> None:
    result = runner.invoke(app, ["gui", "--mock"])
    assert result.exit_code == 1
    assert "kommt mit Auftrag W1-06" in result.output


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
