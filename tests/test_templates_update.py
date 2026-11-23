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
    StartSkillsUpdate,
    StartSkillUpdate,
    discover_skills,
    install_start_skills,
    snapshot_skills,
    start_skill_files,
    tree_digest,
    update_lines,
    update_start_skills,
)

OLD = Path(__file__).parent / "fixtures" / "start_skills_old"
# The lines of `arr skills update` are compared on stdout only: in the full suite a logging
# handler of an earlier test can write a „--- Logging error ---“ to stderr (full run of 611e104).

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
    results = update_start_skills(wf).results
    assert results == {
        n: SkillUpdate.UPDATED if n in GROUP_SKILLS else SkillUpdate.CURRENT for n in START_SKILLS
    }
    for name in START_SKILLS:
        assert tree(wf / "skills" / name) == start_skill_files(name), name
    for name, before in current.items():
        assert tree(wf / "skills" / name) == before
    assert hidden(wf) == [], "nothing half is left"
    assert not discover_skills(wf / "skills").problems
    again = update_start_skills(wf).results
    assert set(again.values()) == {SkillUpdate.CURRENT}


def test_a13_finder_and_python_leftovers_do_not_count(wf: Path) -> None:
    """`.DS_Store` and `__pycache__` (on every level) are neither compared nor kept."""
    (wf / "skills" / "proof" / ".DS_Store").write_bytes(b"finder")
    (wf / "skills" / "proof" / "roles" / "__pycache__").mkdir()
    (wf / "skills" / "proof" / "roles" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (wf / "skills" / "proof" / "roles" / ".DS_Store").write_bytes(b"finder")
    assert update_start_skills(wf, ["proof"]).results == {"proof": SkillUpdate.UPDATED}
    assert tree(wf / "skills" / "proof") == start_skill_files("proof")


OWN_FILES = {
    "notizen": ("proof", "_meine-notizen.md"),
    "git": ("proof", ".git/HEAD"),
    "entwurf": ("proof", "roles/_entwurf.md"),
    "hilfsmodul": ("sympy-check", "scripts/_hilfen.py"),
}


@pytest.mark.parametrize("what", sorted(OWN_FILES))
def test_h1_own_files_of_the_user_make_the_skill_changed(
    wf: Path, monkeypatch: pytest.MonkeyPatch, what: str
) -> None:
    """W4b-06a.1 H1: a name with `.` or `_` that arr does not ship is the user's own file:
    the skill counts as changed and nothing is deleted."""
    name, rel = OWN_FILES[what]
    if name == "sympy-check":  # the package's tree counts as an earlier version here
        digest = tree_digest(start_skill_files(name))
        monkeypatch.setitem(skills_module.EARLIER_DIGESTS, name, (("test", digest),))
        monkeypatch.setitem(skills_module.CURRENT_DIGESTS, name, ("test", "0" * 64))
        monkeypatch.setattr(
            skills_module,
            "start_skill_files",
            lambda n, real=start_skill_files: (
                {**real(n), "NEU.md": b"neu"} if n == name else real(n)
            ),
        )
        bare = update_start_skills(wf, [name])  # counter-check: without the own file
        assert bare.results == {name: SkillUpdate.UPDATED}
        (wf / "skills" / name / "NEU.md").unlink()
    own = wf / "skills" / name / rel
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text("meins", "utf-8")
    before = tree(wf / "skills" / name)
    update = update_start_skills(wf, [name])
    assert update.results == {name: SkillUpdate.CHANGED}
    assert update_lines(update) == [f"{name}: von dir geändert, bleibt unverändert"]
    assert tree(wf / "skills" / name) == before
    assert hidden(wf) == []


def test_h1_an_own_link_is_kept_and_not_followed(wf: Path, tmp_path: Path) -> None:
    outside = tmp_path / "draussen.md"
    outside.write_text("fremd", "utf-8")
    os.symlink(outside, wf / "skills" / "proof" / "_link-nach-draussen")
    before = tree(wf / "skills" / "proof")
    assert update_start_skills(wf, ["proof"]).results == {"proof": SkillUpdate.CHANGED}
    assert tree(wf / "skills" / "proof") == before
    assert outside.read_text("utf-8") == "fremd"


def test_a13_a_changed_skill_is_never_touched(wf: Path) -> None:
    role = wf / "skills" / "proof-review" / "roles" / "judge.md"
    role.write_text(role.read_text("utf-8") + "\nMein Zusatz.\n", "utf-8")
    extra = wf / "skills" / "roundtable" / "notizen.md"
    extra.write_text("meins", "utf-8")
    before = {n: tree(wf / "skills" / n) for n in ("proof-review", "roundtable")}
    results = update_start_skills(wf).results
    assert results["proof-review"] is SkillUpdate.CHANGED
    assert results["roundtable"] is SkillUpdate.CHANGED
    assert results["proof"] is SkillUpdate.UPDATED
    for name, old in before.items():
        assert tree(wf / "skills" / name) == old


def test_a13_a_missing_skill_is_not_created(wf: Path) -> None:
    shutil.rmtree(wf / "skills" / "exploration")
    (wf / "skills" / "arr" / "SKILL.md").unlink()
    (wf / "skills" / "arr").rmdir()
    results = update_start_skills(wf).results
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
    results = update_start_skills(wf).results
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
    update = update_start_skills(wf, ["proof"])
    assert update.failed and update.results == {"proof": SkillUpdate.FAILED}
    assert update_lines(update) == [
        "proof: ließ sich nicht aktualisieren (No space left on device). Die bisherige Fassung "
        "ist unverändert."
    ]
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
    update = update_start_skills(wf, ["proof"])
    assert update.results == {"proof": SkillUpdate.FAILED}
    assert "Die bisherige Fassung ist unverändert" in update.skills["proof"].note
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
    assert update_start_skills(wf, ["proof"]).results == {"proof": SkillUpdate.CHANGED}
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
    assert set(update_start_skills(wf).results.values()) >= {SkillUpdate.UPDATED}
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
    assert result.stdout.splitlines() == [
        "arr: schon aktuell",
        "exploration: aktualisiert",
        "latex-writeup: schon aktuell",
        "proof: aktualisiert",
        "proof-review: von dir geändert, bleibt unverändert",
        "roundtable: fehlt, wird nicht angelegt (das geht über „Start-Skills anlegen“)",
        "sympy-check: schon aktuell",
    ]
    again = CliRunner().invoke(app, ["skills", "update", "-w", str(wf)])
    assert again.exit_code == 0 and "aktualisiert\n" not in again.stdout


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
    assert "Link" in result.stderr


def test_a14_update_lines_are_german() -> None:
    update = StartSkillsUpdate(
        {
            "proof": StartSkillUpdate(SkillUpdate.UPDATED),
            "x": StartSkillUpdate(SkillUpdate.CHANGED),
            "y": StartSkillUpdate(SkillUpdate.UPDATED, ".y.arr-ab.old"),
        }
    )
    assert update_lines(update) == [
        "proof: aktualisiert",
        "x: von dir geändert, bleibt unverändert",
        "y: aktualisiert; die alte Fassung liegt noch unter skills/.y.arr-ab.old, lösche sie "
        "selbst.",
    ]


# --------------------------------------------------------------------------------------------
# W4b-06a.1 M3, N1, N2, N3
# --------------------------------------------------------------------------------------------


def test_m3_a_rest_after_the_swap_is_named(wf: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Removing the old version fails after the swap: the skill is updated (exit 0), the line
    names the rest with its path."""
    from arr.cli import app

    remove = skills_module._remove_tree

    def stuck(parent: int, name: str) -> None:
        if name.endswith(".old"):
            raise PermissionError(13, "Permission denied")
        remove(parent, name)

    monkeypatch.setattr(skills_module, "_remove_tree", stuck)
    result = CliRunner().invoke(app, ["skills", "update", "-w", str(wf)])
    assert result.exit_code == 0, result.output
    assert tree(wf / "skills" / "proof") == start_skill_files("proof")
    [line] = [x for x in result.stdout.splitlines() if x.startswith("proof:")]
    [rest] = [x for x in hidden(wf) if x.startswith(".proof.arr-")]
    assert line == (
        f"proof: aktualisiert; die alte Fassung liegt noch unter skills/{rest}, lösche sie selbst."
    )
    assert tree(wf / "skills" / rest) == files_of(OLD / "proof")


def test_m3_a_read_only_folder_in_an_old_skill(wf: Path) -> None:
    """The probe of the review: `roles/` of an old `proof` is read-only. Either the swap works
    and the rest is named, or (where the system refuses the move) nothing changed; never the
    message „unverändert“ after a swap."""
    roles = wf / "skills" / "proof" / "roles"
    roles.chmod(0o555)
    try:
        update = update_start_skills(wf, ["proof"])
    finally:
        for path in (wf / "skills").glob(".proof.arr-*/roles"):
            path.chmod(0o755)
        if roles.exists():
            roles.chmod(0o755)
    item = update.skills["proof"]
    if item.result is SkillUpdate.UPDATED:
        assert item.note.startswith(".proof.arr-") and (wf / "skills" / item.note).is_dir()
        assert tree(wf / "skills" / "proof") == start_skill_files("proof")
    else:
        assert item.result is SkillUpdate.FAILED
        assert files_of(wf / "skills" / "proof") == files_of(OLD / "proof")


def test_n1_a_failure_at_one_skill_does_not_stop_the_others(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arr.cli import app

    write = skills_module._write_tree

    def broken_for_proof(folder: int, files: dict[str, bytes]) -> None:
        if files == start_skill_files("proof"):
            raise OSError(28, "No space left on device")
        write(folder, files)

    monkeypatch.setattr(skills_module, "_write_tree", broken_for_proof)
    result = CliRunner().invoke(app, ["skills", "update", "-w", str(wf)])
    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert len(lines) == len(START_SKILLS), "one line per start skill"
    assert "exploration: aktualisiert" in lines
    assert "proof-review: aktualisiert" in lines and "roundtable: aktualisiert" in lines
    assert "proof: ließ sich nicht aktualisieren (No space left on device)." in result.stdout
    assert files_of(wf / "skills" / "proof") == files_of(OLD / "proof")


def test_n2_updating_creates_no_skills_folder(tmp_path: Path) -> None:
    root = tmp_path / "wf"
    root.mkdir()
    (root / "arr.yaml").write_text("version: 1\n", "utf-8")
    update = update_start_skills(root)
    assert set(update.results.values()) == {SkillUpdate.MISSING}
    assert not (root / "skills").exists()


@pytest.mark.parametrize("name", ["../nachbar", "proof/../../x", "unbekannt"])
def test_n3_only_start_skills_are_read(wf: Path, name: str) -> None:
    (wf / "nachbar").mkdir()
    with pytest.raises(SkillFileError, match="ist kein Start-Skill"):
        update_start_skills(wf, ["proof", name])
    assert files_of(wf / "skills" / "proof") == files_of(OLD / "proof"), "nothing done first"


def test_np4_an_interrupt_between_the_renames_puts_the_old_version_back(
    wf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W4b-06a.3 NP4: Ctrl-C while the new version goes into place: the old one comes back,
    nothing half is left, the interrupt goes on."""
    before = tree(wf / "skills" / "proof")
    rename = os.rename

    def interrupted(src: str, dst: str, **kwargs: object) -> None:
        if str(src).endswith(".tmp"):
            raise KeyboardInterrupt
        rename(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(skills_module.os, "rename", interrupted)
    with pytest.raises(KeyboardInterrupt):
        update_start_skills(wf, ["proof"])
    assert tree(wf / "skills" / "proof") == before
    assert hidden(wf) == []
