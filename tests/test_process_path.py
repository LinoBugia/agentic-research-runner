"""`PATH` of the processes outside the sandbox (W3-06.3, B1): no entry of a write root."""

import os
from pathlib import Path

from arr.backends.process_path import is_unsafe, outside_path, within


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
