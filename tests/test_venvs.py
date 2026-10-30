"""Root of the base venvs and run venvs outside `~` (W3-00, `arr.venvs`)."""

import os
import stat
import sys
from pathlib import Path

import pytest

from arr import venvs
from arr.errors import ArrError
from arr.venvs import (
    VENV_ROOT_ENV,
    base_venv_dir,
    default_venv_root,
    ensure_run_venvs_dir,
    ensure_venv_root,
    run_venv_dir,
    venv_base_prefix,
    venv_root,
)


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_default_root_is_outside_home_and_named_after_the_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(VENV_ROOT_ENV)
    monkeypatch.setattr(venvs, "_user_name", lambda: "alice")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert default_venv_root() == venv_root() == Path("/Users/Shared/arr-alice")
    monkeypatch.setattr(sys, "platform", "linux")
    assert venv_root() == Path("/var/tmp/arr-alice")


def test_user_name_comes_from_the_user_database(monkeypatch: pytest.MonkeyPatch) -> None:
    import pwd

    monkeypatch.setenv("USER", "somebody-else")
    assert venvs._user_name() == pwd.getpwuid(os.getuid()).pw_name


def test_env_overrides_the_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(VENV_ROOT_ENV, str(tmp_path / "v"))
    assert venv_root() == tmp_path / "v"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(VENV_ROOT_ENV, "rel")
    assert venv_root() == tmp_path / "rel"  # made absolute


def test_paths_below_the_root(venv_root: Path) -> None:
    assert run_venv_dir("20261029-1200_demo_custom_ab12") == (
        venv_root / "runs" / "20261029-1200_demo_custom_ab12"
    )
    assert base_venv_dir("mathe") == venv_root / "venvs" / "mathe"
    assert not venv_root.exists()  # paths only, nothing created
    for bad in ("", ".", "..", "a/b", "x\0y"):
        with pytest.raises(ValueError):
            run_venv_dir(bad)
        with pytest.raises(ValueError):
            base_venv_dir(bad)


def test_root_and_runs_are_created_with_0700(venv_root: Path) -> None:
    old = os.umask(0)
    try:
        runs = ensure_run_venvs_dir()
    finally:
        os.umask(old)
    assert runs == venv_root / "runs"
    assert mode(venv_root) == 0o700 and mode(runs) == 0o700
    assert ensure_run_venvs_dir() == runs  # an existing, correct folder is fine


def test_symlink_is_refused(venv_root: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    venv_root.symlink_to(target)
    with pytest.raises(ArrError, match="symbolischer Link") as info:
        ensure_venv_root()
    assert VENV_ROOT_ENV in info.value.message and str(venv_root) in info.value.message


def test_runs_symlink_is_refused(venv_root: Path, tmp_path: Path) -> None:
    ensure_venv_root()
    (tmp_path / "other").mkdir(mode=0o700)
    (venv_root / "runs").symlink_to(tmp_path / "other")
    with pytest.raises(ArrError, match="symbolischer Link"):
        ensure_run_venvs_dir()


@pytest.mark.parametrize("bits", [0o770, 0o707, 0o777, 0o720])
def test_writable_for_others_is_refused(venv_root: Path, bits: int) -> None:
    venv_root.mkdir()
    venv_root.chmod(bits)
    with pytest.raises(ArrError, match="Gruppe oder andere") as info:
        ensure_venv_root()
    assert f"chmod 700 {venv_root}" in info.value.message


def test_writable_runs_is_refused(venv_root: Path) -> None:
    ensure_venv_root()
    (venv_root / "runs").mkdir()
    (venv_root / "runs").chmod(0o777)
    with pytest.raises(ArrError, match="Gruppe oder andere"):
        ensure_run_venvs_dir()


def test_readable_for_others_is_fine(venv_root: Path) -> None:
    venv_root.mkdir()
    venv_root.chmod(0o755)
    assert ensure_venv_root() == venv_root


def test_foreign_owner_is_refused(venv_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv_root.mkdir(mode=0o700)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    with pytest.raises(ArrError, match="anderen Benutzer"):
        ensure_venv_root()


def test_a_file_is_refused(venv_root: Path) -> None:
    venv_root.write_text("x")
    with pytest.raises(ArrError, match="kein Ordner"):
        ensure_venv_root()


def test_missing_parent_gives_a_german_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(VENV_ROOT_ENV, str(tmp_path / "missing" / "root"))
    with pytest.raises(ArrError, match="fehlt"):
        ensure_venv_root()


def test_venv_base_prefix(tmp_path: Path) -> None:
    (tmp_path / "pyvenv.cfg").write_text("home = /Library/Frameworks/P/bin\nversion = 3.12\n")
    assert venv_base_prefix(tmp_path) == Path("/Library/Frameworks/P")
    assert venv_base_prefix(tmp_path / "none") is None


def test_w3_00_2_glob_safe_name() -> None:
    from arr.venvs import glob_safe_name

    for name in ("mathe", "py3.12_x-1", "20261029-1912_demo_custom_b32e"):
        assert glob_safe_name(name), name
    for name in ("mäthe", "w3rev-é1", "é", "мат", "ｍａｔｈｅ", "a[b]", "a,b", "a\\b", "a!b"):
        assert not glob_safe_name(name), name
