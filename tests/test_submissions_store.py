"""`submit` in the room store: filing, versions, sealing, files, reading (W4b-01 A2–A7, A13).

Every accepted submission lies exactly once below `output/` (named ones as `<name>/v<n>/`,
from v2 on with `diff.patch`), is never changed afterwards and never half written; in a sealed
stage it waits in `groupchats/sealed/` until `release`.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from arr.errors import ArrError
from arr.models import GroupChatConfig, Rubric
from arr.room import store as store_module
from arr.room.store import AgentRoom, RoomStore
from arr.room.types import RoomError, Submission, SubmitAPI

AGENTS = ["prover", "judge-1", "judge-2", "lead"]


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "runs" / "20261117-1200_demo_custom_b1b2"
    for agent in AGENTS:
        (run / "agents" / agent / "work").mkdir(parents=True)
    (run / "base").mkdir()
    (run / "output").mkdir()
    return run


@pytest.fixture
def store(run_dir: Path) -> RoomStore:
    store = RoomStore.create(run_dir)
    store.setup_channels(
        [
            GroupChatConfig(name="main", members=AGENTS),
            GroupChatConfig(name="judges", members=["judge-1", "judge-2"], anonymous=True),
        ]
    )
    return store


def work(store: RoomStore, agent: str) -> Path:
    return store.run_dir / "agents" / agent / "work"


def room(store: RoomStore, agent: str, **kwargs: Any) -> AgentRoom:
    return store.for_agent(agent, work(store, agent), **kwargs)


def expect(store: RoomStore, agent: str, stage: str | None = "beweis", **submission: Any) -> None:
    turn = len([a for a in [store.assignment(agent)] if a]) + 1
    context: dict[str, Any] = {"submission": submission} if submission else {}
    if "attempt" in submission:
        context["attempt"] = submission.pop("attempt")
    store.add_assignment(agent, turn, "Auftrag.", stage=stage, context=context)


def write(store: RoomStore, agent: str, rel: str, text: str = "Inhalt\n") -> Path:
    path = work(store, agent) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def leftovers(store: RoomStore) -> list[Path]:
    """Prepared submissions still in the staging folder (the lock files of names stay)."""
    staging = store.run_dir / store_module.STAGING_DIR
    return [p for p in staging.iterdir() if not p.name.startswith(".version-")]


def tree(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))


def digest(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


# --------------------------------------------------------------------------------------------
# A2, A3 through the room
# --------------------------------------------------------------------------------------------


def test_agent_room_offers_submit(store: RoomStore) -> None:
    assert isinstance(room(store, "prover"), SubmitAPI)


def test_every_builtin_format_is_accepted_and_filed(store: RoomStore) -> None:
    prover = room(store, "prover")
    write(store, "prover", "beweis.md", "Beweis.\n")
    cases: list[tuple[dict[str, Any], dict[str, Any]]] = [
        ({"format": "text"}, {"body": "Ein Text."}),
        ({"format": "artifact"}, {"files": ["beweis.md"]}),
        ({"format": "list"}, {"fields": {"items": [{"title": "A", "text": "a"}]}}),
        ({"format": "decision", "choices": ["ja", "nein"]}, {"fields": {"choice": "ja"}}),
        ({"format": "review"}, {"fields": {"verdict": "accept", "objections": []}}),
    ]
    for submission, args in cases:
        expect(store, "prover", **submission)
        sub = prover.submit("done", f"Abgabe {submission['format']}.", **args)
        assert sub.format == submission["format"] and not sub.sealed
        assert (store.run_dir / sub.path / "submission.json").is_file()
    expect(store, "judge-1", stage="bewertung", format="rating", rate=[1, 2])
    rated = room(store, "judge-1").submit(
        "done", "Bewertet.", fields={"ratings": [{"ref": 1, "score": 7}, {"ref": 2, "score": 3}]}
    )
    assert rated.fields["ratings"][1]["score"] == 3
    assert [s.format for s in store.submissions()] == [
        "text",
        "artifact",
        "list",
        "decision",
        "review",
        "rating",
    ]


def test_mistakes_come_back_as_room_error_and_nothing_is_filed(store: RoomStore) -> None:
    expect(store, "prover", format="review")
    with pytest.raises(RoomError) as info:
        room(store, "prover").submit("done", "", fields={"verdict": "gut"})
    text = str(info.value)
    assert text.startswith("Die Abgabe passt nicht:")
    assert "„summary“: darf nicht leer sein." in text and "„fields.objections“: fehlt." in text
    assert store.submissions() == [] and not (store.run_dir / "output" / "submissions").exists()
    # the same turn: the agent fixes it and hands in again
    fixed = room(store, "prover").submit(
        "done", "Gut.", fields={"verdict": "minor", "objections": [{"text": "Tippfehler."}]}
    )
    assert fixed.id == 1


def test_no_expected_submission_no_submit(store: RoomStore) -> None:
    prover = room(store, "prover")
    with pytest.raises(RoomError, match="erwartet keine Abgabe mit `submit`"):
        prover.submit("done", "x", body="y")
    expect(store, "prover")  # an assignment without `submission`
    with pytest.raises(RoomError, match="erwartet keine Abgabe mit `submit`"):
        prover.submit("done", "x", body="y")
    store.add_assignment("prover", 3, "x", stage="s", context={"submission": {"format": "nix"}})
    with pytest.raises(RoomError, match="Fehler in arr, nicht bei dir"):
        prover.submit("done", "x", body="y")
    assert store.submissions() == []


def test_own_format_from_spec(store: RoomStore) -> None:
    spec = {"fields": {"ansatz": {"type": "choice", "choices": ["induktion", "fall"]}}}
    expect(store, "prover", format="plan", spec=spec)
    prover = room(store, "prover")
    with pytest.raises(RoomError, match="„fields.ansatz“: Erwartet: einer von induktion, fall"):
        prover.submit("done", "Plan.", fields={"ansatz": "raten"})
    assert prover.submit("done", "Plan.", fields={"ansatz": "fall"}).fields == {"ansatz": "fall"}


def test_stage_attempt_action_come_from_the_assignment(store: RoomStore) -> None:
    expect(store, "prover", stage="revision", format="text", action="b", attempt=3)
    sub = room(store, "prover").submit("not_solved", "Lücke bleibt.")
    assert (sub.stage, sub.attempt, sub.action, sub.status) == ("revision", 3, "b", "not_solved")
    assert sub.path == "output/submissions/001-revision-prover"


def test_a_changed_assignment_is_noticed(store: RoomStore, monkeypatch: pytest.MonkeyPatch) -> None:
    expect(store, "prover", format="text")
    old = store.assignment("prover")
    expect(store, "prover", stage="neu", format="text")
    monkeypatch.setattr(RoomStore, "assignment", lambda self, agent: old)
    with pytest.raises(RoomError, match="hat sich während der Abgabe geändert"):
        room(store, "prover").submit("done", "x", body="y")
    assert store.submissions() == []


# --------------------------------------------------------------------------------------------
# A4: rating against the room
# --------------------------------------------------------------------------------------------


def test_rating_never_the_own_and_only_existing(store: RoomStore) -> None:
    expect(store, "prover", format="text")
    mine = room(store, "prover").submit("done", "Beweis.", body="…")
    expect(store, "judge-1", format="text")
    other = room(store, "judge-1").submit("done", "Gutachten.", body="…")
    expect(store, "judge-1", stage="rating", format="rating", rate=[mine.id, other.id])
    judge = room(store, "judge-1")
    fields = {"ratings": [{"ref": mine.id, "score": 5}, {"ref": other.id, "score": 9}]}
    with pytest.raises(RoomError, match=f"#{other.id} ist deine eigene Abgabe"):
        judge.submit("done", "Bewertet.", fields=fields)
    expect(store, "judge-1", stage="rating", format="rating", rate=[mine.id, 99])
    with pytest.raises(RoomError, match="Die Abgabe #99 gibt es nicht"):
        judge.submit(
            "done", "x", fields={"ratings": [{"ref": mine.id, "score": 5}, {"ref": 99, "score": 1}]}
        )
    expect(store, "judge-1", stage="rating", format="rating", rate=[mine.id])  # counter-check
    assert judge.submit("done", "ok", fields={"ratings": [{"ref": mine.id, "score": 5}]}).id
    assert len(store.submissions(format="rating")) == 1


def test_rating_scale_and_criteria_of_the_run(store: RoomStore) -> None:
    expect(store, "prover", format="text")
    target = room(store, "prover").submit("done", "Beweis.", body="…")
    rubric = Rubric(scale=(0, 5), criteria=["Klarheit"])
    expect(store, "judge-2", stage="rating", format="rating", rate=[target.id])
    judge = room(store, "judge-2", rubric=rubric)
    with pytest.raises(RoomError, match="Erwartet: eine Zahl von 0 bis 5"):
        judge.submit("done", "x", fields={"ratings": [{"ref": target.id, "score": 7}]})
    sub = judge.submit(
        "done",
        "x",
        fields={"ratings": [{"ref": target.id, "score": 4, "criteria": {"Klarheit": 5}}]},
    )
    assert sub.fields["ratings"][0]["criteria"] == {"Klarheit": 5.0}


# --------------------------------------------------------------------------------------------
# A5: filing
# --------------------------------------------------------------------------------------------


def test_folder_tree_versions_and_diff(store: RoomStore) -> None:
    prover = room(store, "prover")
    write(store, "prover", "notes/beweis.md", "Zeile 1\nZeile 2\n")
    expect(store, "prover", format="artifact", name="beweis")
    v1 = prover.submit("done", "Erste Fassung.", files=["notes/beweis.md"], note_next="Lemma 2")
    write(store, "prover", "notes/beweis.md", "Zeile 1\nZeile 2, korrigiert\n")
    expect(store, "prover", stage="revision", format="artifact", name="beweis", attempt=2)
    v2 = prover.submit("done", "Zweite Fassung.", files=["notes/beweis.md"], body="Was neu ist.")
    expect(store, "judge-1", stage="pruefung", format="review")
    judge = room(store, "judge-1")
    review = judge.submit("done", "Passt.", fields={"verdict": "accept", "objections": []})
    assert (v1.version, v2.version, review.version) == (1, 2, None)
    assert (v1.path, v2.path) == ("output/beweis/v1", "output/beweis/v2")
    # the anonymous channel hides the judge in the folder name, like output/results/
    assert review.path == "output/submissions/003-pruefung-mitglied-a"
    assert review.shown_as == "Mitglied A"
    assert tree(store.run_dir / "output") == [
        "beweis",
        "beweis/v1",
        "beweis/v1/files",
        "beweis/v1/files/beweis.md",
        "beweis/v1/submission.json",
        "beweis/v2",
        "beweis/v2/body.md",
        "beweis/v2/diff.patch",
        "beweis/v2/files",
        "beweis/v2/files/beweis.md",
        "beweis/v2/submission.json",
        "submissions",
        "submissions/003-pruefung-mitglied-a",
        "submissions/003-pruefung-mitglied-a/submission.json",
    ]
    diff = (store.run_dir / "output/beweis/v2/diff.patch").read_text()
    assert "--- v1/files/beweis.md\n+++ v2/files/beweis.md\n" in diff
    assert "-Zeile 2\n+Zeile 2, korrigiert\n" in diff
    assert "--- /dev/null\n+++ v2/body.md\n" in diff
    data = json.loads((store.run_dir / "output/beweis/v1/submission.json").read_text())
    assert data == {
        "id": 1,
        "ts": v1.ts,
        "agent": "prover",
        "stage": "beweis",
        "attempt": 1,
        "action": None,
        "format": "artifact",
        "name": "beweis",
        "version": 1,
        "status": "done",
        "summary": "Erste Fassung.",
        "files": ["beweis.md"],
        "note_next": "Lemma 2",
        "fields": {},
        "body": None,
    }
    shown = json.loads((store.run_dir / review.path / "submission.json").read_text())
    assert shown["agent"] == "Mitglied A" and "judge-1" not in json.dumps(shown)


def test_each_submission_exactly_once_and_never_changed(store: RoomStore) -> None:
    prover = room(store, "prover")
    expect(store, "prover", format="text", name="notiz")
    first = prover.submit("done", "Eins.", body="eins")
    before = digest(store.run_dir / first.path)
    for i in range(4):  # Entscheidung 16: hand in again, every one stays
        prover.submit("done", f"Noch einmal {i}.", body=f"text {i}")
    expect(store, "prover", format="text")
    for i in range(3):
        prover.submit("done", f"Unbenannt {i}.", body="x")
    assert digest(store.run_dir / first.path) == before, "v1 is never changed"
    subs = store.submissions()
    assert [s.version for s in subs if s.name] == [1, 2, 3, 4, 5]
    folders = [store.run_dir / s.path for s in subs]
    assert len(set(folders)) == len(subs) == 8 and all(f.is_dir() for f in folders)
    on_disk = sorted((store.run_dir / "output/notiz").iterdir())
    on_disk += sorted((store.run_dir / "output/submissions").iterdir())
    assert sorted(on_disk) == sorted(folders)
    # the newest per agent, stage, attempt and action: named or not, the same action
    assert [s.id for s in store.submissions(latest=True)] == [8]
    assert [s.id for s in store.submissions(name="notiz", latest=True)] == [5]
    assert leftovers(store) == []


def test_a_failure_while_filing_leaves_nothing(
    store: RoomStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(store, "prover", "beweis.md")
    expect(store, "prover", format="artifact", name="beweis")
    real_rename = os.rename

    def broken_after(src: Any, dst: Any) -> None:
        real_rename(src, dst)
        raise OSError("disk gone")

    for broken in (lambda src, dst: (_ for _ in ()).throw(OSError("full")), broken_after):
        monkeypatch.setattr(store_module.os, "rename", broken)
        with pytest.raises(OSError):
            room(store, "prover").submit("done", "x", files=["beweis.md"])
        monkeypatch.setattr(store_module.os, "rename", real_rename)
        assert store.submissions() == []
        assert not (store.run_dir / "output/beweis/v1").exists()
        assert leftovers(store) == []
    assert room(store, "prover").submit("done", "x", files=["beweis.md"]).version == 1


KILLED = """
import os, sys, time
from pathlib import Path
from arr.room import store as m
real = m.AgentRoom._copy_source
def slow(self, rel, target):
    name = real(self, rel, target)
    Path(sys.argv[2]).write_text("copied")
    time.sleep(60)
    return name
m.AgentRoom._copy_source = slow
s = m.RoomStore(Path(sys.argv[1]))
s.for_agent("prover").submit("done", "x", files=["beweis.md"])
"""


def test_a_killed_process_leaves_no_half_folder_in_output(store: RoomStore, tmp_path: Path) -> None:
    """A5 with a real process: killed (by its PID) in the middle of copying."""
    write(store, "prover", "beweis.md", "x" * 100_000)
    expect(store, "prover", format="artifact", name="beweis")
    marker = tmp_path / "copied"
    process = subprocess.Popen(
        [sys.executable, "-c", KILLED, str(store.run_dir), str(marker)],
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "the child reached the copy"
    finally:
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
    assert store.submissions() == []
    assert tree(store.run_dir / "output") == [], "nothing below output/"
    [left] = leftovers(store)  # only below groupchats/staging/, readable for no agent
    assert left.name.startswith(".submission-")
    assert room(store, "prover").submit("done", "x", files=["beweis.md"]).path == "output/beweis/v1"


def test_diff_notes_for_binary_large_and_unchanged(store: RoomStore) -> None:
    prover = room(store, "prover")
    expect(store, "prover", format="artifact", name="daten")
    (work(store, "prover") / "bild.bin").write_bytes(b"\x00\x01")
    write(store, "prover", "gross.txt", "a\n" * (store_module.MAX_DIFF_LINES + 1))
    prover.submit("done", "v1", files=["bild.bin", "gross.txt"])
    (work(store, "prover") / "bild.bin").write_bytes(b"\x00\x02")
    write(store, "prover", "gross.txt", "b\n" * (store_module.MAX_DIFF_LINES + 1))
    v2 = prover.submit("done", "v2", files=["bild.bin", "gross.txt"])
    diff = (store.run_dir / v2.path / "diff.patch").read_text()
    assert "# files/bild.bin: geändert, kein Text-Diff" in diff
    assert "# files/gross.txt: geändert, kein Text-Diff" in diff
    v3 = prover.submit("done", "v3", files=["bild.bin", "gross.txt"])
    assert (
        store.run_dir / v3.path / "diff.patch"
    ).read_text() == "# Keine Änderung von v2 zu v3.\n"


def test_diff_runs_in_its_own_process_with_a_time_limit(
    store: RoomStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    prover = room(store, "prover")
    expect(store, "prover", format="text", name="text")
    prover.submit("done", "v1", body="a\n")
    monkeypatch.setattr(store_module, "DIFF_TIMEOUT_S", 0.000_1)
    v2 = prover.submit("done", "v2", body="b\n")
    assert "Er dauerte länger als" in (store.run_dir / v2.path / "diff.patch").read_text()


# --------------------------------------------------------------------------------------------
# A6: sealed stages
# --------------------------------------------------------------------------------------------


def test_sealed_stage_until_release(store: RoomStore) -> None:
    store.seal("pruefung", "judges")
    expect(store, "judge-1", stage="pruefung", format="review")
    expect(store, "judge-2", stage="pruefung", format="review", name="gutachten")
    one = room(store, "judge-1").submit(
        "done", "Major.", fields={"verdict": "major", "objections": [{"text": "Lücke."}]}
    )
    two = room(store, "judge-2").submit(
        "done", "Minor.", fields={"verdict": "minor", "objections": []}
    )
    assert (one.sealed, two.sealed) == (True, True)
    assert one.path == "groupchats/sealed/submission-001"
    assert two.path == "groupchats/sealed/submission-002"
    assert tree(store.run_dir / "output") == [], "nothing readable before the release"
    assert store.submission(one.id).sealed
    # an unsealed stage files at once (counter-check)
    expect(store, "prover", stage="beweis", format="text")
    assert not room(store, "prover").submit("done", "Beweis.", body="…").sealed
    store.release("pruefung")
    after = {s.id: s for s in store.submissions()}
    assert after[one.id].path == "output/submissions/001-pruefung-mitglied-a"
    assert after[two.id].path == "output/gutachten/v1"
    assert not after[one.id].sealed and not after[two.id].sealed
    assert (store.run_dir / "output/gutachten/v1/submission.json").is_file()
    assert list((store.run_dir / "groupchats/sealed").iterdir()) == []
    # later submissions of the stage are not sealed any more
    expect(store, "judge-1", stage="pruefung", format="text")
    assert not room(store, "judge-1").submit("done", "Nachtrag.", body="x").sealed


def test_no_diff_against_a_version_that_is_still_sealed(store: RoomStore) -> None:
    store.seal("pruefung")
    expect(store, "judge-1", stage="pruefung", format="text", name="gutachten")
    room(store, "judge-1").submit("done", "geheim", body="Geheimer Einwand\n")
    expect(store, "prover", stage="revision", format="text", name="gutachten")
    v2 = room(store, "prover").submit("done", "offen", body="Offener Text\n")
    diff = (store.run_dir / v2.path / "diff.patch").read_text()
    assert diff == "# Kein Diff: Die vorige Fassung v1 ist noch versiegelt.\n"
    assert "Geheim" not in diff


# --------------------------------------------------------------------------------------------
# A7: files and names
# --------------------------------------------------------------------------------------------


def refused(store: RoomStore, rel: str, match: str) -> None:
    expect(store, "prover", format="artifact")
    with pytest.raises(RoomError, match=match):
        room(store, "prover").submit("done", "x", files=[rel])
    assert store.submissions() == []
    assert not (store.run_dir / "output" / "submissions").exists()


def test_links_fifos_hardlinks_and_paths_outside_are_refused(
    store: RoomStore, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("geheim")
    folder = work(store, "prover")
    (folder / "link.md").symlink_to(secret)
    (folder / "dir-link").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(folder / "pipe")
    os.link(secret, folder / "hard.md")
    refused(store, "link.md", "Verweis \\(Symlink\\)")
    refused(store, "dir-link/secret.txt", "führt über den Verweis")
    refused(store, "pipe", "keine normale Datei")
    refused(store, "hard.md", "Hardlink")
    refused(store, "../../../../secret.txt", "liegt nicht in deinem Arbeitsordner")
    refused(store, str(secret), "liegt nicht in deinem Arbeitsordner")
    refused(store, "fehlt.md", "gibt es in deinem Arbeitsordner nicht")
    refused(store, "../prover/work/x.md", "liegt nicht in deinem Arbeitsordner")
    # counter-check: a plain file of the own folder is taken
    write(store, "prover", "ok.md", "ok")
    expect(store, "prover", format="artifact")
    sub = room(store, "prover").submit("done", "x", files=["ok.md"])
    assert (store.run_dir / sub.path / "files" / "ok.md").read_text() == "ok"


def test_too_large_files_are_refused(store: RoomStore, monkeypatch: pytest.MonkeyPatch) -> None:
    write(store, "prover", "a.md", "x" * 2000)
    write(store, "prover", "b.md", "x" * 2000)
    monkeypatch.setattr(store_module, "MAX_ATTACHMENT_BYTES", 1000)
    refused(store, "a.md", "ist zu groß")
    monkeypatch.setattr(store_module, "MAX_ATTACHMENT_BYTES", 10_000)
    monkeypatch.setattr(store_module, "MAX_SUBMISSION_BYTES", 3000)
    expect(store, "prover", format="artifact")
    with pytest.raises(RoomError, match="zusammen zu groß"):
        room(store, "prover").submit("done", "x", files=["a.md", "b.md"])
    assert store.submissions() == []
    assert room(store, "prover").submit("done", "x", files=["a.md"]).files == ["a.md"]


def test_names_that_lead_out_of_output_or_collide(store: RoomStore, tmp_path: Path) -> None:
    for name in ("../x", "a/b", ".hidden", "summary.md", "results", "submissions", "Beweis"):
        store.add_assignment(
            "prover", 1, "x", context={"submission": {"format": "text", "name": name}}
        )
        with pytest.raises(RoomError, match="Fehler in arr"):
            room(store, "prover").submit("done", "x", body="y")
    (store.run_dir / "output" / "datei").write_text("schon da")
    (store.run_dir / "output" / "link").symlink_to(tmp_path, target_is_directory=True)
    for name in ("datei", "link"):
        expect(store, "prover", format="text", name=name)
        with pytest.raises(RoomError, match=f"„output/{name}“ ist schon belegt"):
            room(store, "prover").submit("done", "x", body="y")
    assert not list(tmp_path.glob("v1"))
    assert store.submissions() == []
    expect(store, "prover", format="text", name="frei")  # counter-check
    assert room(store, "prover").submit("done", "x", body="y").path == "output/frei/v1"


# --------------------------------------------------------------------------------------------
# A5: reading for arr, a version made by arr
# --------------------------------------------------------------------------------------------


def test_read_functions(store: RoomStore) -> None:
    for agent, stage, attempt, action in (
        ("prover", "beweis", 1, None),
        ("prover", "beweis", 1, None),
        ("prover", "beweis", 2, None),
        ("judge-1", "pruefung", 1, "a"),
        ("judge-1", "pruefung", 1, "b"),
    ):
        expect(
            store,
            agent,
            stage=stage,
            format="text",
            name="t" if agent == "prover" else None,
            action=action,
            attempt=attempt,
        )
        room(store, agent).submit("done", f"{agent} {stage} {attempt} {action}", body="x")
    assert [s.id for s in store.submissions(agent="prover")] == [1, 2, 3]
    assert [s.id for s in store.submissions(stage="beweis", attempt=1)] == [1, 2]
    assert [s.id for s in store.submissions(action="b")] == [5]
    assert [s.id for s in store.submissions(latest=True)] == [2, 3, 4, 5]
    assert [s.id for s in store.submissions(stage="beweis", latest=True)] == [2, 3]
    assert [s.version for s in store.submissions(name="t")] == [1, 2, 3]
    latest = store.latest_version("t")
    assert latest is not None and latest.version == 3
    assert store.latest_version("gibt-es-nicht") is None
    assert store.submission(4).action == "a"
    with pytest.raises(ArrError, match="Die Abgabe #99 gibt es nicht"):
        store.submission(99)


def test_add_version_for_seed(store: RoomStore) -> None:
    (store.run_dir / "base" / "beweis.md").write_text("Aus dem Projekt.\n")
    seed = store.add_version("beweis", summary="Fassung aus dem Projekt.", files=["beweis.md"])
    assert (seed.agent, seed.shown_as, seed.version, seed.path) == (
        "orchestrator",
        "arr",
        1,
        "output/beweis/v1",
    )
    assert (store.run_dir / "output/beweis/v1/files/beweis.md").read_text() == "Aus dem Projekt.\n"
    ideas = store.add_version(
        "ideen",
        summary="Ideen aus dem Parameter.",
        format="list",
        fields={"items": [{"title": "A", "text": "a"}]},
    )
    assert ideas.fields["items"][0]["title"] == "A"
    write(store, "prover", "beweis.md", "Vom Beweiser.\n")
    expect(store, "prover", format="artifact", name="beweis")
    v2 = room(store, "prover").submit("done", "v2", files=["beweis.md"])
    assert (
        "-Aus dem Projekt.\n+Vom Beweiser.\n"
        in (store.run_dir / v2.path / "diff.patch").read_text()
    )
    (store.run_dir / "base" / "link.md").symlink_to(store.run_dir / "base" / "beweis.md")
    with pytest.raises(ArrError, match="Symlink"):
        store.add_version("x", summary="s", files=["link.md"])
    with pytest.raises(ArrError, match="mindestens eine Datei"):
        store.add_version("y", summary="s")
    with pytest.raises(ArrError, match="schon belegt"):
        store.add_version("results", summary="s", files=["beweis.md"])


def test_room_before_w4b01_has_no_submissions(tmp_path: Path) -> None:
    import sqlite3

    run = tmp_path / "run"
    store = RoomStore.create(run)
    con = sqlite3.connect(run / "groupchats" / "room.sqlite")
    con.executescript("DROP TABLE submission; DROP TABLE sealed_submission;")
    con.close()
    assert store.submissions() == [] and store.latest_version("x") is None
    store.seal("s")
    assert store.release("s") == []


# --------------------------------------------------------------------------------------------
# A13: two processes at the same time
# --------------------------------------------------------------------------------------------


WRITER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from arr.room.store import RoomStore
    run, agent, count, start = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
    store = RoomStore(run)
    room = store.for_agent(agent)
    while time.time() < start:
        time.sleep(0.001)
    for i in range(count):
        name = "gemeinsam" if i % 2 == 0 else None
        context = {"submission": {"format": "text", "name": name} if name else {"format": "text"}}
        store.add_assignment(agent, i + 1, "x", stage="parallel", context=context)
        room.submit("done", f"{agent} {i}", body=f"{agent} {i}\\n")
    """
)


WRITERS = ("prover", "lead", "judge-2")


def test_processes_submit_at_the_same_time(store: RoomStore) -> None:
    """A13 with three real processes, each handing in named and unnamed submissions."""
    count = 12
    start = time.time() + 1.0
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", WRITER, str(store.run_dir), agent, str(count), str(start)],
            stderr=subprocess.PIPE,
            text=True,
        )
        for agent in WRITERS
    ]
    for process in processes:
        _, err = process.communicate(timeout=120)
        assert process.returncode == 0, err
    subs = store.submissions()
    total = len(WRITERS) * count
    assert len(subs) == total, "no submission is lost"
    ids = [s.id for s in subs]
    assert ids == sorted(set(ids)) == list(range(1, total + 1)), "no number twice"
    named = [s for s in subs if s.name == "gemeinsam"]
    versions = sorted(s.version or 0 for s in named)
    assert versions == list(range(1, total // 2 + 1)), "no version twice, none missing"
    assert {s.agent for s in named} == set(WRITERS)
    folders = {store.run_dir / s.path for s in subs}
    assert len(folders) == len(subs) and all((f / "submission.json").is_file() for f in folders)
    for sub in named:
        data = json.loads((store.run_dir / sub.path / "submission.json").read_text())
        assert (data["id"], data["version"]) == (sub.id, sub.version)
        assert (store.run_dir / sub.path / "body.md").read_text() == sub.body
        if (sub.version or 0) > 1:
            assert (store.run_dir / sub.path / "diff.patch").read_text().startswith("--- v")
    assert leftovers(store) == []


def test_submission_model_has_no_room_types_inside() -> None:
    assert set(Submission.model_fields) >= {"id", "agent", "shown_as", "format", "path", "sealed"}
