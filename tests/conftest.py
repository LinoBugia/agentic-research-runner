import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ["ARR_NOTIFY"] = "off"  # no test shows a real notification, child processes inherit it


@pytest.fixture
def arr_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ARR_HOME at a temporary directory for the duration of a test."""
    home = tmp_path / "arr-home"
    monkeypatch.setenv("ARR_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def no_real_arr_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test writes to the real `~/.arr`: `arr_home` or a test's own `ARR_HOME` wins.

    Building the options of an agent writes the `git` stub to `app_dir()` (W3-06.4).
    """
    monkeypatch.setenv("ARR_HOME", str(tmp_path / "arr-home-default"))


@pytest.fixture(autouse=True)
def venv_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ARR_VENV_ROOT at a temporary directory (not created) for every test.

    Run venvs and base venvs live below this root (`arr.venvs`); no test may touch
    `/Users/Shared` or `/var/tmp`.
    """
    root = tmp_path / "venv-root"
    monkeypatch.setenv("ARR_VENV_ROOT", str(root))
    return root


@pytest.fixture
def restore_root_logger() -> Iterator[logging.Logger]:
    """Give a test the root logger and restore its handlers and level afterwards.

    Use it in every test that calls `arr.log.setup_logging()`; otherwise the root logger
    keeps a handler on a closed capture stream and later log calls fail.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
