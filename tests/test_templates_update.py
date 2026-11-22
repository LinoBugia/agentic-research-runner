"""W4b-06a A13, A14: unchanged start skills come to the current version (`arr skills update`).

Real folders below `tmp_path`; the earlier versions are the trees of `81523c5` in
`tests/fixtures/start_skills_old/` (shipped at `f12039f` and `5676589`). Failures are played
in through hooks, never through timing.
"""

import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from arr import skills as skills_module
from arr.skills import (
    CURRENT_DIGESTS,
    EARLIER_DIGESTS,
    START_SKILLS,
    SkillFileError,
    SkillUpdate,
    discover_skills,
    install_start_skills,
    snapshot_skills,
    start_skill_files,
    tree_digest,
    update_lines,
    update_start_skills,
)

OLD = Path(__file__).parent / "fixtures" / "start_skills_old"
GROUP_SKILLS = ("exploration", "proof", "proof-review", "roundtable")


def tree(folder: Path) -> dict[str, bytes]:
    """Files and links below `folder` (links as their text)."""
    out: dict[str, bytes] = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        if path.is_symlink():
            out[rel] = b"->" + os.readlink(path).encode()
        elif path.is_file():
            out[rel] = path.read_bytes()
    return out


def files_of(folder: Path) -> dict[str, bytes]:
    return {k: v for k, v in tree(folder).items() if not k.startswith((".", "_"))}


@pytest.fixture
def wf(tmp_path: Path) -> Path:
    """A workfolder whose four group start skills are the old trees (`81523c5`), the others
    current."""
    root = tmp_path / "wf"
    (root / "skills").mkdir(parents=True)
    (root / "arr.yaml").write_text("version: 1\n", "utf-8")
    for name in GROUP_SKILLS:
        shutil.copytree(OLD / name, root / "skills" / name)
    install_start_skills(root)
    return root


def hidden(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "skills").iterdir() if p.name.startswith("."))


# --------------------------------------------------------------------------------------------
# The checksums arr knows
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", START_SKILLS)
def test_a13_the_current_checksum_is_held(name: str) -> None:
    """Care: a changed start skill turns this red until its old checksum moved."""
    found = tree_digest(start_skill_files(name))
    label, held = CURRENT_DIGESTS[name]
    assert found == held, (
        f"Der Start-Skill „{name}“ hat sich geändert. Verschiebe den alten Eintrag "
        f"({label}, {held}) in EARLIER_DIGESTS (mit dem Commit, der ihn ausgeliefert hat) und "
        f"trage {found} als aktuelle Fassung in CURRENT_DIGESTS ein (arr/skills.py)."
    )
    assert found not in {d for _c, d in EARLIER_DIGESTS[name]}


def test_a13_every_start_skill_has_its_history() -> None:
    assert set(CURRENT_DIGESTS) == set(EARLIER_DIGESTS) == set(START_SKILLS)
    for name, earlier in EARLIER_DIGESTS.items():
        digests = [d for _c, d in earlier]
        assert len(set(digests)) == len(digests), name
        assert all(len(d) == 64 for d in digests), name


@pytest.mark.parametrize("name", GROUP_SKILLS)
def test_a13_the_old_trees_are_shipped_versions(name: str) -> None:
    shipped = {d: c for c, d in EARLIER_DIGESTS[name]}
    assert tree_digest(files_of(OLD / name)) in shipped


def test_a13_the_checksum_takes_paths_and_contents() -> None:
    base = {"SKILL.md": b"a", "roles/x.md": b"b"}
    assert tree_digest(base) == tree_digest(dict(reversed(base.items())))
    assert tree_digest(base) != tree_digest({"SKILL.md": b"a", "roles/y.md": b"b"})
    assert tree_digest(base) != tree_digest({"SKILL.md": b"ab", "roles/x.md": b""})
    assert tree_digest({"a": b"bc"}) != tree_digest({"ab": b"c"})


# --------------------------------------------------------------------------------------------
# Updating
# --------------------------------------------------------------------------------------------


def test_a13_earlier_versions_are_replaced_current_ones_stay(wf: Path) -> None:
    current = {n: tree(wf / "skills" / n) for n in START_SKILLS if n not in GROUP_SKILLS}
    results = update_start_skills(wf)
    assert results == {
        n: SkillUpdate.UPDATED if n in GROUP_SKILLS else SkillUpdate.CURRENT for n in START_SKILLS
    }
    for name in START_SKILLS:
        assert tree(wf / "skills" / name) == start_skill_files(name), name
    for name, before in current.items():
        assert tree(wf / "skills" / name) == before
    assert hidden(wf) == [], "nothing half is left"
    assert not discover_skills(wf / "skills").problems
    again = update_start_skills(wf)
    assert set(again.values()) == {SkillUpdate.CURRENT}


def test_a13_hidden_names_do_not_count(wf: Path) -> None:
    (wf / "skills" / "proof" / ".DS_Store").write_bytes(b"finder")
    (wf / "skills" / "proof" / "roles" / "__pycache__").mkdir()
    assert update_start_skills(wf, ["proof"]) == {"proof": SkillUpdate.UPDATED}
    assert tree(wf / "skills" / "proof") == start_skill_files("proof")


def test_a13_a_changed_skill_is_never_touched(wf: Path) -> None:
    role = wf / "skills" / "proof-review" / "roles" / "judge.md"
    role.write_text(role.read_text("utf-8") + "\nMein Zusatz.\n", "utf-8")
    extra = wf / "skills" / "roundtable" / "notizen.md"
    extra.write_text("meins", "utf-8")
    before = {n: tree(wf / "skills" / n) for n in ("proof-review", "roundtable")}
    results = update_start_skills(wf)
    assert results["proof-review"] is SkillUpdate.CHANGED
    assert results["roundtable"] is SkillUpdate.CHANGED
    assert results["proof"] is SkillUpdate.UPDATED
    for name, old in before.items():
        assert tree(wf / "skills" / name) == old


def test_a13_a_missing_skill_is_not_created(wf: Path) -> None:
    shutil.rmtree(wf / "skills" / "exploration")
    (wf / "skills" / "arr" / "SKILL.md").unlink()
    (wf / "skills" / "arr").rmdir()
    results = update_start_skills(wf)
    assert results["exploration"] is SkillUpdate.MISSING
    assert results["arr"] is SkillUpdate.MISSING
    assert not (wf / "skills" / "exploration").exists()
    assert not (wf / "skills" / "arr").exists()


def test_a13_no_link_and_no_file_is_followed(wf: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "anderswo"
    shutil.copytree(OLD / "proof", elsewhere / "proof")
    shutil.rmtree(wf / "skills" / "proof")
    os.symlink(elsewhere / "proof", wf / "skills" / "proof")  # the skill is a link
    shutil.rmtree(wf / "skills" / "roundtable")
    (wf / "skills" / "roundtable").write_text("eine Datei", "utf-8")  # a file
    # a link inside an otherwise old tree, to a file with the old content
    role = wf / "skills" / "exploration" / "roles" / "ranker.md"
    copy = elsewhere / "ranker.md"
    shutil.copyfile(role, copy)
    role.unlink()
    os.symlink(copy, role)
    before = tree(elsewhere)
    results = update_start_skills(wf)
    assert results["proof"] is SkillUpdate.CHANGED
    assert results["roundtable"] is SkillUpdate.CHANGED
    assert results["exploration"] is SkillUpdate.CHANGED
    assert results["proof-review"] is SkillUpdate.UPDATED
    assert tree(elsewhere) == before, "nothing written through a link"
    assert (wf / "skills" / "proof").is_symlink()
    assert (wf / "skills" / "roundtable").read_text("utf-8") == "eine Datei"
    assert role.is_symlink()


def test_a13_a_linked_skills_folder_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "anderswo"
    shutil.copytree(OLD / "proof", target / "proof")
    root = tmp_path / "wf"
    root.mkdir()
    os.symlink(target, root / "skills")
    before = tree(target)
    with pytest.raises(SkillFileError, match="Link"):
        update_start_skills(root)
    assert tree(target) == before


def test_a13_a_failure_while_building_leaves_the_old_version(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = tree(wf / "skills" / "proof")

    def broken(folder: int, files: dict[str, bytes]) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(skills_module, "_write_tree", broken)
    with pytest.raises(SkillFileError, match="Die bisherige Fassung ist unverändert"):
        update_start_skills(wf, ["proof"])
    assert tree(wf / "skills" / "proof") == before
    assert hidden(wf) == []


def test_a13_a_failure_in_the_swap_leaves_the_old_version(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second step of the swap (new folder into place) fails: the old one comes back."""
    before = tree(wf / "skills" / "proof")
    rename = os.rename
    calls: list[tuple[str, str]] = []

    def failing(src: str, dst: str, **kwargs: object) -> None:
        calls.append((str(src), str(dst)))
        if str(src).endswith(".tmp"):
            raise OSError(5, "Input/output error")
        rename(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(skills_module.os, "rename", failing)
    with pytest.raises(SkillFileError, match="Die bisherige Fassung ist unverändert"):
        update_start_skills(wf, ["proof"])
    assert [c[0] for c in calls][:1] == ["proof"], "the old one was moved away first"
    assert tree(wf / "skills" / "proof") == before
    assert hidden(wf) == []


def test_a13_a_change_right_before_the_swap_wins(wf: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The checksum is read again right before the swap: a change made while the new version
    is built is never replaced."""
    write = skills_module._write_tree
    skill = wf / "skills" / "proof"

    def and_change(folder: int, files: dict[str, bytes]) -> None:
        write(folder, files)
        (skill / "SKILL.md").write_text("geändert, während arr baut", "utf-8")

    monkeypatch.setattr(skills_module, "_write_tree", and_change)
    assert update_start_skills(wf, ["proof"]) == {"proof": SkillUpdate.CHANGED}
    assert (skill / "SKILL.md").read_text("utf-8") == "geändert, während arr baut"
    assert hidden(wf) == []


def test_a13_a_running_run_keeps_its_copy(wf: Path, tmp_path: Path) -> None:
    """Runs work with their snapshot of the skills (`snapshot_skills`); an update changes
    nothing there."""
    catalog = discover_skills(wf / "skills")
    snapshot = tmp_path / "run" / "skills"
    snapshot.mkdir(parents=True)
    snapshot_skills([catalog.get(n) for n in GROUP_SKILLS], snapshot)
    before = tree(snapshot)
    assert set(update_start_skills(wf).values()) >= {SkillUpdate.UPDATED}
    assert tree(snapshot) == before
    assert tree(snapshot / "proof") == files_of(OLD / "proof")


# --------------------------------------------------------------------------------------------
# A14: `arr skills update`
# --------------------------------------------------------------------------------------------


def test_a14_the_cli_names_each_start_skill(wf: Path) -> None:
    from arr.cli import app

    role = wf / "skills" / "proof-review" / "roles" / "judge.md"
    role.write_text("meine Rolle", "utf-8")
    shutil.rmtree(wf / "skills" / "roundtable")
    result = CliRunner().invoke(app, ["skills", "update", "-w", str(wf)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "arr: schon aktuell",
        "exploration: aktualisiert",
        "latex-writeup: schon aktuell",
        "proof: aktualisiert",
        "proof-review: von dir geändert, bleibt unverändert",
        "roundtable: fehlt, wird nicht angelegt (das geht über „Start-Skills anlegen“)",
        "sympy-check: schon aktuell",
    ]
    again = CliRunner().invoke(app, ["skills", "update", "-w", str(wf)])
    assert again.exit_code == 0 and "aktualisiert\n" not in again.output


def test_a14_the_cli_fails_with_a_message(tmp_path: Path) -> None:
    from arr.cli import app

    root = tmp_path / "wf"
    root.mkdir()
    (root / "arr.yaml").write_text("version: 1\n", "utf-8")
    target = tmp_path / "anderswo"
    target.mkdir()
    os.symlink(target, root / "skills")
    result = CliRunner().invoke(app, ["skills", "update", "-w", str(root)])
    assert result.exit_code == 1
    assert "Link" in result.output


def test_a14_update_lines_are_german() -> None:
    lines = update_lines({"proof": SkillUpdate.UPDATED, "x": SkillUpdate.CHANGED})
    assert lines == ["proof: aktualisiert", "x: von dir geändert, bleibt unverändert"]
