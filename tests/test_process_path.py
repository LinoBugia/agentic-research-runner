"""`PATH` of the processes outside the sandbox (W3-06.3, B1): no entry of a write root, and
a `git` stub first (W3-06.4)."""

import os
from pathlib import Path

import pytest

from arr.backends.process_path import (
    NO_GIT_TEXT,
    is_unsafe,
    no_git_folder,
    outside_path,
    within,
)
from arr.errors import ArrError


def test_within_ignores_case_and_follows_links(tmp_path: Path) -> None:
    root = tmp_path / "Root"
    (root / "bin").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(root / "bin")
    assert within(root / "bin", root) and within(str(root).lower() + "/bin", root)
    assert within(link, root) and within(root, root)
    assert not within(tmp_path / "Root2", root)


def test_an_entry_in_above_or_relative_to_a_root_is_unsafe(tmp_path: Path) -> None:
    root = tmp_path / "run" / "work"
    root.mkdir(parents=True)
    assert is_unsafe(str(root / "bin"), [root])  # a program the agent can write
    assert is_unsafe(str(root), [root])
    assert is_unsafe(str(tmp_path / "run"), [root])  # above a root
    assert is_unsafe("bin", [root]) and is_unsafe(".", [root])  # relative: the cwd
    assert not is_unsafe("/usr/bin", [root])
    assert not is_unsafe(str(tmp_path / "other"), [root])


def test_outside_path_keeps_the_order_of_the_rest(tmp_path: Path) -> None:
    root = tmp_path / "venv"
    entries = [str(root / "bin"), "/opt/base/bin", "", "bin", "/usr/bin", "/opt/base/bin"]
    assert outside_path(entries, [root]) == os.pathsep.join(["/opt/base/bin", "/usr/bin"])
    assert outside_path([], [root]) == ""


def test_a_link_in_a_root_is_unsafe_even_when_it_points_outside(tmp_path: Path) -> None:
    """N2: the agent can turn the link later, so it counts as the root."""
    root = tmp_path / "run" / "work"
    root.mkdir(parents=True)
    (root / "lnk").symlink_to("/usr/bin")
    (tmp_path / "alias").symlink_to(root)
    assert is_unsafe(str(root / "lnk"), [root])
    assert is_unsafe(str(tmp_path / "alias" / "lnk"), [root])  # through another spelling
    assert is_unsafe(str(root / "lnk" / "sub"), [root])
    assert outside_path([str(root / "lnk"), "/usr/bin"], [root]) == "/usr/bin"


def test_the_git_stub_does_nothing_and_fails(arr_home: Path, tmp_path: Path) -> None:
    import subprocess

    folder = no_git_folder([tmp_path / "work"])
    assert folder == arr_home / "bin" / "no-git"
    stub = folder / "git"
    assert stub.read_text() == NO_GIT_TEXT
    assert oct(stub.stat().st_mode & 0o777) == "0o700"
    done = subprocess.run([str(stub), "status"], capture_output=True, text=True, cwd=tmp_path)
    assert (done.returncode, done.stdout, done.stderr) == (1, "", "")


def test_the_git_stub_is_written_again_when_changed(arr_home: Path, tmp_path: Path) -> None:
    stub = no_git_folder([]) / "git"
    stub.write_text('#!/bin/sh\nexec /usr/bin/git "$@"\n')
    assert (no_git_folder([]) / "git").read_text() == NO_GIT_TEXT
    stub.unlink()
    stub.symlink_to("/usr/bin/git")
    no_git_folder([])
    assert not stub.is_symlink()  # replaced, never written through
    assert stub.read_text() == NO_GIT_TEXT


def test_no_git_stub_in_a_write_root(arr_home: Path) -> None:
    with pytest.raises(ArrError, match="arr-Ordner"):
        no_git_folder([arr_home])
    with pytest.raises(ArrError, match="arr-Ordner"):
        no_git_folder([arr_home.parent])
