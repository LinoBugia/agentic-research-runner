import logging
import platform
from pathlib import Path

import pytest
from typer.testing import CliRunner

from arr import __version__
from arr.cli import app
from arr.log import setup_logging
from arr.settings import app_dir

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


def test_setup_logging_writes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("debug")
    logging.getLogger("arr.test").debug("hello")
    err = capsys.readouterr().err
    assert "DEBUG" in err
    assert "arr.test: hello" in err
