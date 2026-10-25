import logging
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def arr_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ARR_HOME at a temporary directory for the duration of a test."""
    home = tmp_path / "arr-home"
    monkeypatch.setenv("ARR_HOME", str(home))
    return home


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
