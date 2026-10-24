from pathlib import Path

import pytest


@pytest.fixture
def arr_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ARR_HOME at a temporary directory for the duration of a test."""
    home = tmp_path / "arr-home"
    monkeypatch.setenv("ARR_HOME", str(home))
    return home
