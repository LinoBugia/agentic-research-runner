"""Finalizer and `changes.diff` (W2-04 A6-A8): the M1 scenario with FakeBackend through the real
RunManager, robustness after stop, limit, error and a half finished setup, idempotency."""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from run_kit import (
    agent,
    channel,
    eventually,
    log_entries,
    make_manager,
    make_run_input,
    make_settings,
    make_workfolder,
    post,
    room,
    say,
    turns,
    wait,
)

from arr.models import RunStatus
from arr.resolve import resolve_run
from arr.runs import finalize as finalize_module
from arr.runs.context import RunContext
from arr.runs.eventlog import read_events
from arr.runs.finalize import changes_diff, finalize_run
from arr.runs.manager import RunManager
from arr.runs.prepare import PreparedRun, prepare_run
from arr.runs.topologies import check_topology, topology_for

TASK = "Zeige, dass Lemma A für alle n ≡ 1 mod 4 gilt."


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def staged(**stages: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "turns": [],
        "by_stage": {name: {"turns": [{"steps": s} for s in ts]} for name, ts in stages.items()},
    }


def rate(score: float, comment: str = "passt") -> list[dict[str, Any]]:
    return [
        room("get_assignment", save_as="job"),
        room("submit_rating", result_id="${job.context.result_id}", score=score, comment=comment),
    ]


def submit(kind: str, title: str, **args: Any) -> dict[str, Any]:
    return room("submit_result", kind=kind, title=title, **args)


def tool(command: str, result: str) -> dict[str, Any]:
    return {"do": "tool", "name": "Bash", "input": {"command": command}, "result": result}


def files(folder: Path) -> dict[str, bytes]:
    return {
        p.relative_to(folder).as_posix(): p.read_bytes()
        for p in sorted(folder.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


FINALIZER_FILES = ("output", "groupchats", "usage.json")


def finalizer_output(run_dir: Path) -> dict[str, bytes]:
    out = {k: v for k, v in files(run_dir).items() if k.startswith(FINALIZER_FILES)}
    for agent_dir in (run_dir / "agents").iterdir():
        for name in ("transcript.md", "changes.diff"):
            out[f"agents/{agent_dir.name}/{name}"] = (agent_dir / name).read_bytes()
    return {k: v for k, v in out.items() if not k.endswith(("room.sqlite-wal", "room.sqlite-shm"))}


# --------------------------------------------------------------------------------------------
# A6 M1 scenario
# --------------------------------------------------------------------------------------------


def m1_manager(workfolder: Path) -> RunManager:
    holder: list[RunManager] = []

    async def topology(ctx: RunContext) -> None:
        prepared = holder[0].prepared
        assert prepared is not None
        strategist = prepared.agents["strategist"].work_dir
        (strategist / "strategie.md").write_text("Induktion über n.\n", "utf-8")
        notes = strategist / "notes.md"
        notes.write_text(notes.read_text("utf-8") + "Lemma B folgt aus A.\n", "utf-8")
        prover = prepared.agents["prover"].work_dir
        (prover / "beweis.md").write_text("Beweis: n = 4k + 1 …\n", "utf-8")
        (prover / "check.py").unlink()
        await topology_for("waterfall")(ctx)

    scripts = {
        "strategist": staged(
            strategie=[
                [
                    say("Ich plane den Beweis."),
                    tool("python check.py", "ok"),
                    post("werkstatt", "Ansatz: Induktion über n."),
                    submit("strategy", "Strategie", path="strategie.md"),
                ],
                [submit("strategy", "Strategie, überarbeitet", body="Induktion mit Basis n = 1.")],
            ]
        ),
        "prover": staged(
            strategie=[rate(4, "Basisfall fehlt"), rate(8)],
            beweis=[
                [
                    tool("cat beweis.md", "Beweis: n = 4k + 1 …"),
                    post("main", "Der Beweis steht, siehe Anhang.", refs=["beweis.md"]),
                    post("werkstatt", "Danke für die Strategie."),
                    submit("proof", "Beweis", path="beweis.md"),
                ]
            ],
        ),
        "rater": staged(strategie=[rate(5, "zu knapp"), rate(7)], beweis=[rate(8, "sauber")]),
    }
    manager = make_manager(
        workfolder,
        topology,
        scripts,
        agents=[
            agent("strategist"),
            agent("prover"),
            agent("rater", provider="codex", model="fake-gpt", auth="fake-codex"),
        ],
        groupchats=[
            channel("main", ["strategist", "prover", "rater"]),
            channel("werkstatt", ["strategist", "prover"]),
        ],
        run_extra={
            "task": TASK,
            "topology": {"kind": "waterfall", "params": {"pass_score": 6, "max_rework": 1}},
            "stages": [
                {"name": "strategie", "agent": "strategist", "produces": "strategie.md"},
                {"name": "beweis", "agent": "prover", "produces": "beweis.md"},
            ],
            "raters": ["prover", "rater"],
        },
        finalizer=finalize_run,
    )
    check_topology(manager.resolved)
    holder.append(manager)
    return manager


async def test_m1_scenario_run_folder_is_complete(workfolder: Path) -> None:
    manager = m1_manager(workfolder)
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    run_dir = manager.run_dir

    # M1 1: mixed agents, two channels with different members, at least one rework
    assert {a.provider.value for a in manager.resolved.agents} == {"claude", "codex"}
    decisions = [
        e["data"]["decision"] for e in log_entries(run_dir, "checkpoint") if "decision" in e["data"]
    ]
    assert decisions == ["rework", "passed", "passed"]

    # M1 2: the run folder of docs/02 §4
    for name in ("run.yaml", "run.input.yaml", "task.md", "meta.json", "orchestrator.jsonl"):
        assert (run_dir / name).is_file(), name
    for name in ("approvals.jsonl", "usage.json"):
        assert (run_dir / name).is_file(), name
    for name in ("base", "skills", "agents", "groupchats/attachments", "output/results"):
        assert (run_dir / name).is_dir(), name
    assert (run_dir / "task.md").read_text("utf-8") == TASK
    for agent_id in ("strategist", "prover", "rater"):
        folder = run_dir / "agents" / agent_id
        for name in (
            "agent.json",
            "role.md",
            "transcript.jsonl",
            "raw.jsonl",
            "transcript.md",
            "changes.diff",
            "session.json",
        ):
            assert (folder / name).is_file(), f"{agent_id}/{name}"
        assert (folder / "work").is_dir() and (folder / "blobs").is_dir()
        records = read_events(folder / "transcript.jsonl")
        calls = {r["data"]["id"] for r in records if r["kind"] == "tool_call"}
        answered = {r["data"]["id"] for r in records if r["kind"] == "tool_result"}
        assert calls and calls == answered, "every tool call has its result"
        transcript = (folder / "transcript.md").read_text("utf-8")
        assert transcript.startswith(f"# Transkript – {agent_id}")
        assert "nicht abgeschlossen" not in transcript

    # channel exports
    for name in ("main", "werkstatt"):
        markdown = (run_dir / "groupchats" / f"{name}.md").read_text("utf-8")
        lines = (run_dir / "groupchats" / f"{name}.jsonl").read_text("utf-8").splitlines()
        assert markdown.startswith(f"# #{name}") and lines
        assert all(json.loads(line)["channel"] == name for line in lines)
    main = (run_dir / "groupchats" / "main.md").read_text("utf-8")
    assert "Der Beweis steht, siehe Anhang." in main and "- Anhang: [beweis.md](" in main
    assert "Ansatz: Induktion" not in main, "werkstatt is a separate channel"
    [attachment] = list((run_dir / "groupchats" / "attachments" / "main").glob("*/beweis.md"))
    assert attachment.read_text("utf-8").startswith("Beweis:")

    # changes.diff against base/, without the files written by arr
    diff = (run_dir / "agents" / "strategist" / "changes.diff").read_text("utf-8")
    assert "--- /dev/null\n+++ b/strategie.md\n" in diff
    assert "--- a/notes.md\n+++ b/notes.md\n" in diff and "+Lemma B folgt aus A.\n" in diff
    assert "AGENTS.md" not in diff and "CLAUDE.md" not in diff
    prover_diff = (run_dir / "agents" / "prover" / "changes.diff").read_text("utf-8")
    assert "--- a/check.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-print('ok')\n" in prover_diff
    assert (run_dir / "agents" / "rater" / "changes.diff").read_text("utf-8") == ""

    # results, ratings
    results = json.loads((run_dir / "output" / "results.json").read_text("utf-8"))
    assert [(r["id"], r["stage"], r["attempt"], r["score"]) for r in results["results"]] == [
        (1, "strategie", 1, 4.5),
        (2, "strategie", 2, 7.5),
        (3, "beweis", 1, 8.0),
    ]
    assert [r["file"] for r in results["results"]] == [
        "output/results/001-strategist-strategy.md",
        "output/results/002-strategist-strategy.md",
        "output/results/003-prover-proof.md",
    ]
    assert (run_dir / "output/results/001-strategist-strategy.md").read_text("utf-8") == (
        "Induktion über n.\n"
    ), "the submitted file itself"
    second = (run_dir / "output/results/002-strategist-strategy.md").read_text("utf-8")
    assert second.startswith("# Strategie, überarbeitet\n\nErgebnis #2 · `strategist`")
    assert "Induktion mit Basis n = 1." in second and "- `prover`: 8" in second
    ratings = json.loads((run_dir / "output" / "ratings.json").read_text("utf-8"))
    assert len(ratings["ratings"]) == 5
    assert {r["rater"] for r in ratings["ratings"] if r["result_id"] == 3} == {"rater"}

    # usage.json and summary.md
    usage = json.loads((run_dir / "usage.json").read_text("utf-8"))
    assert set(usage["by_agent"]) == {"strategist", "prover", "rater"}
    summary = (run_dir / "output" / "summary.md").read_text("utf-8")
    assert summary.startswith(f"# Zusammenfassung – {manager.prepared.run_id}")
    assert "- **Status:** abgeschlossen (done)" in summary
    assert "- **Grund:** Die Topologie ist regulär durchgelaufen." in summary
    assert "| strategie | 1 | strategist | #1 | 4,5 | Nacharbeit |" in summary
    assert "| strategie | 2 | strategist | #2 | 7,5 | bestanden |" in summary
    assert "- **beweis:** bestanden, akzeptiert: #3, Score 8, 1 Versuch" in summary
    assert "- [#3 Beweis](results/003-prover-proof.md) · `prover` · proof" in summary
    assert "· Ø 8 (1 Rating) · ✓ akzeptiert" in summary
    assert "- `rater`: codex, fake-gpt" in summary
    assert "## Usage" in summary and "- Gesamt: " in summary
    assert "Schritt des Finalizers gescheitert" not in summary and "Symlink" not in summary


async def test_m1_scenario_finalizer_is_idempotent(workfolder: Path) -> None:
    manager = m1_manager(workfolder)
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    assert manager.prepared is not None
    before = finalizer_output(manager.run_dir)
    log_before = (manager.run_dir / "orchestrator.jsonl").read_text("utf-8")
    await finalize_run(manager.prepared)
    after = finalizer_output(manager.run_dir)
    assert after.keys() == before.keys()
    changed = [k for k in before if before[k] != after[k]]
    assert changed == [], changed
    assert (manager.run_dir / "orchestrator.jsonl").read_text("utf-8") == log_before


# --------------------------------------------------------------------------------------------
# A7 robust: stop, limit, error, half prepared, failing step
# --------------------------------------------------------------------------------------------


def solo_manager(workfolder: Path, script: dict[str, Any], **run_extra: Any) -> RunManager:
    kwargs = {k: run_extra.pop(k) for k in ("backend_factory",) if k in run_extra}
    manager = make_manager(
        workfolder,
        topology_for("solo"),
        {"a": script},
        run_extra={"topology": {"kind": "solo"}, "task": TASK, **run_extra},
        finalizer=finalize_run,
    )
    if "backend_factory" in kwargs:
        manager.backend_factory = kwargs["backend_factory"]
    return manager


def summary(manager: RunManager) -> str:
    return (manager.run_dir / "output" / "summary.md").read_text("utf-8")


async def test_finalizer_after_stop_mid_turn(workfolder: Path) -> None:
    manager = solo_manager(workfolder, turns([say("Ich fange an."), wait(30)]))
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: manager.orchestrator is not None and bool(manager.orchestrator._active)
    )
    manager.stop()
    assert await asyncio.wait_for(running, 10) is RunStatus.STOPPED
    text = summary(manager)
    assert "- **Status:** gestoppt (stopped)" in text
    assert "- **Grund:** Der User hat den Run gestoppt." in text
    assert "Keine Ergebnisse abgegeben." in text
    transcript = (manager.run_dir / "agents" / "a" / "transcript.md").read_text("utf-8")
    assert "Ich fange an." in transcript and "interrupted" in transcript
    assert json.loads((manager.run_dir / "output" / "results.json").read_text("utf-8")) == {
        "version": 1,
        "run": manager.run_dir.name,
        "results": [],
    }


async def test_finalizer_after_a_limit(workfolder: Path) -> None:
    manager = solo_manager(
        workfolder, turns([say("…")], repeat_last=True), limits={"turns_per_agent": 1}
    )
    assert await asyncio.wait_for(manager.run(), 10) is RunStatus.DONE
    text = summary(manager)
    assert "- **Status:** abgeschlossen (done)" in text
    assert "- **Grund:** Limit: Die Zahl der Turns pro Agent ist erreicht." in text


async def test_finalizer_after_an_error(workfolder: Path) -> None:
    manager = solo_manager(workfolder, turns(), task=" ")
    assert await asyncio.wait_for(manager.run(), 10) is RunStatus.FAILED
    text = summary(manager)
    assert "- **Status:** fehlgeschlagen (failed)" in text
    assert "- **Grund:** Fehler: Der Run hat keinen Auftrag: task.md ist leer." in text
    assert "- **Dauer:**" in text


async def test_finalizer_after_a_failed_setup(workfolder: Path) -> None:
    def broken_factory(prepared: Any) -> Any:
        raise RuntimeError("Backend nicht startbar")

    manager = solo_manager(workfolder, turns(), backend_factory=broken_factory)
    assert await asyncio.wait_for(manager.run(), 10) is RunStatus.FAILED
    text = summary(manager)
    assert "- **Grund:** Fehler: Backend nicht startbar" in text
    assert "- **Dauer:** nicht gestartet" in text
    assert (manager.run_dir / "agents" / "a" / "transcript.md").is_file()
    assert (manager.run_dir / "usage.json").is_file()


def prepared_only(workfolder: Path) -> PreparedRun:
    settings = make_settings()
    run_input = make_run_input(
        [agent("a"), agent("b", workspace={"mode": "empty"})],
        [channel("main", ["a", "b"])],
        task=TASK,
    )
    return prepare_run(workfolder, run_input, resolve_run(run_input, None, settings), settings)


async def test_finalizer_on_a_half_prepared_run(workfolder: Path) -> None:
    prepared = prepared_only(workfolder)  # no room, no transcripts, no status entries
    await finalize_run(prepared)
    run_dir = prepared.run_dir
    for name in ("output/summary.md", "output/results.json", "output/ratings.json", "usage.json"):
        assert (run_dir / name).is_file(), name
    for agent_id in ("a", "b"):
        assert (run_dir / "agents" / agent_id / "transcript.md").is_file()
        assert (run_dir / "agents" / agent_id / "changes.diff").read_text("utf-8") == ""
    text = (run_dir / "output" / "summary.md").read_text("utf-8")
    assert "- **Dauer:** nicht gestartet" in text
    assert "- Die Gruppenchat-Datenbank fehlt (der Run wurde nicht fertig vorbereitet)." in text
    assert "Schritt des Finalizers gescheitert" not in text
    assert not (run_dir / "groupchats" / "room.sqlite").exists(), "nothing is created"


async def test_a_failing_step_does_not_stop_the_others(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(agent_dir: Path) -> Path:
        raise RuntimeError("kaputt")

    monkeypatch.setattr(finalize_module, "write_transcript_md", broken)
    manager = solo_manager(workfolder, turns([submit("note", "Ergebnis", body="42")]))
    assert await asyncio.wait_for(manager.run(), 10) is RunStatus.DONE
    run_dir = manager.run_dir
    assert not (run_dir / "agents" / "a" / "transcript.md").exists()
    for name in ("groupchats", "output/results.json", "usage.json", "agents/a/changes.diff"):
        assert (run_dir / name).exists(), name
    text = summary(manager)
    assert "- Schritt des Finalizers gescheitert: Transkripte: RuntimeError: kaputt" in text
    assert "- [#1 Ergebnis](results/001-a-note.md)" in text
    [error] = [e for e in log_entries(run_dir, "error") if e["data"].get("where") == "finalizer"]
    assert error["data"]["step"] == "Transkripte" and error["data"]["message"] == "kaputt"


async def test_summary_lists_a_summarizer_and_a_failed_stage(workfolder: Path) -> None:
    scripts = {
        "a": {
            "turns": [{"steps": [post("main", "Mein Beitrag")]}],
            "by_stage": {
                "zusammenfassung": {
                    "turns": [{"steps": [submit("summary", "Fazit", body="Alles klar.")]}]
                }
            },
        },
        "b": turns([post("main", "Meiner auch")]),
    }
    manager = make_manager(
        workfolder,
        topology_for("roundtable"),
        scripts,
        groupchats=[channel("main", ["a", "b"], mode="rounds")],
        run_extra={
            "task": TASK,
            "topology": {"kind": "roundtable", "params": {"rounds": 1, "summarizer": "a"}},
        },
        finalizer=finalize_run,
    )
    assert await asyncio.wait_for(manager.run(), 10) is RunStatus.DONE
    text = summary(manager)
    assert "## Zusammenfassung von a\n\n**Fazit**\n\nAlles klar." in text


# --------------------------------------------------------------------------------------------
# A8 changes.diff
# --------------------------------------------------------------------------------------------


def write(folder: Path, rel: str, content: str | bytes) -> None:
    path = folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, "utf-8")


def test_changes_diff_cases(tmp_path: Path) -> None:
    base, work = tmp_path / "base", tmp_path / "work"
    secret = tmp_path / "geheim.txt"
    secret.write_text("GEHEIMNIS\n", "utf-8")
    for folder in (base, work):
        write(folder, "same.txt", "gleich\n")
        os.symlink("same.txt", folder / "link_same")
    write(base, "a.txt", "1\n2\n3\n")
    write(work, "a.txt", "1\nzwei\n3\n")
    write(base, "weg.txt", "alt\n")
    write(work, "neu/datei.md", "# neu\n")
    write(base, "bild.png", b"\x89PNG\0\0alt")
    write(work, "bild.png", b"\x89PNG\0\0neu")
    write(work, "neu.bin", b"\0\1\2")
    write(base, "alt.bin", b"\0\3")
    write(base, "ende.txt", "ohne Zeilenende")
    write(work, "ende.txt", "ohne Zeilenende\n")
    os.symlink("a.txt", base / "link_change")
    os.symlink("same.txt", work / "link_change")
    os.symlink(str(secret), work / "raus")
    write(base, "AGENTS.md", "Projekt\n")
    write(work, "AGENTS.md", "Projekt + Block arr\n")
    write(work, ".agents/skills/x/SKILL.md", "skill\n")
    os.symlink("data/big/fehlt.bin", base / "kaputt")  # target excluded by copy_exclude

    result = changes_diff(base, work, ["AGENTS.md", ".agents/skills"])
    text = result.text
    assert "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n 1\n-2\n+zwei\n 3\n" in text
    assert "--- a/weg.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-alt\n" in text
    assert "--- /dev/null\n+++ b/neu/datei.md\n@@ -0,0 +1 @@\n+# neu\n" in text
    assert "Binärdatei geändert: bild.png\n" in text
    assert "Binärdatei neu: neu.bin\n" in text
    assert "Binärdatei gelöscht: alt.bin\n" in text
    assert "--- a/ende.txt\n+++ b/ende.txt\nNur das Zeilenende am Dateiende" in text
    assert "Symlink geändert: link_change -> same.txt (vorher a.txt)\n" in text
    assert f"Symlink neu: raus -> {secret}\n" in text
    assert "GEHEIMNIS" not in text, "symlinks are never followed"
    assert "same.txt" not in text.replace("-> same.txt", "")
    assert "link_same" not in text
    assert "AGENTS.md" not in text and "SKILL.md" not in text
    assert "kaputt" not in text
    assert result.hints == [
        "Der Symlink „kaputt“ zeigt in base/ auf eine durch „copy_exclude“ ausgeschlossene oder "
        "fehlende Datei und fehlt deshalb im Arbeitsordner. Er steht nicht in changes.diff."
    ]
    assert changes_diff(base, base).text == "", "a folder has no changes against itself"


def test_changes_diff_large_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(finalize_module, "MAX_DIFF_BYTES", 10)
    base, work = tmp_path / "base", tmp_path / "work"
    write(base, "gross.txt", "a" * 50)
    write(work, "gross.txt", "a" * 49 + "b")
    write(base, "gleich.txt", "c" * 50)
    write(work, "gleich.txt", "c" * 50)
    assert changes_diff(base, work).text == (
        "Datei geändert (zu groß für einen Zeilen-Diff): gross.txt\n"
    )


async def test_changes_diff_for_shared_empty_and_copy_exclude_links(workfolder: Path) -> None:
    project = workfolder / "projects" / "demo"
    (project / "project.yaml").write_text('copy_exclude: ["data/big/**"]\n', "utf-8")
    write(project, "data/big/daten.bin", b"\0" * 10)
    os.symlink("data/big/daten.bin", project / "daten")
    settings = make_settings()
    run_input = make_run_input(
        [
            agent("a", workspace={"mode": "shared", "name": "team"}),
            agent("b", workspace={"mode": "shared", "name": "team"}),
            agent("c", workspace={"mode": "empty"}),
            agent("d"),
        ],
        task=TASK,
    )
    prepared = prepare_run(workfolder, run_input, resolve_run(run_input, None, settings), settings)
    run_dir = prepared.run_dir
    assert (run_dir / "base" / "daten").is_symlink() and not (run_dir / "base" / "daten").exists()
    write(run_dir / "shared" / "team", "beweis.md", "geteilt\n")
    write(run_dir / "agents" / "c" / "work", "notiz.md", "leer gestartet\n")
    await finalize_run(prepared)

    diff_a = (run_dir / "agents" / "a" / "changes.diff").read_text("utf-8")
    assert diff_a == (run_dir / "agents" / "b" / "changes.diff").read_text("utf-8")
    assert diff_a == "--- /dev/null\n+++ b/beweis.md\n@@ -0,0 +1 @@\n+geteilt\n"
    diff_c = (run_dir / "agents" / "c" / "changes.diff").read_text("utf-8")
    assert diff_c == "--- /dev/null\n+++ b/notiz.md\n@@ -0,0 +1 @@\n+leer gestartet\n"
    assert (run_dir / "agents" / "d" / "changes.diff").read_text("utf-8") == ""
    text = (run_dir / "output" / "summary.md").read_text("utf-8")
    hint = "- Der Symlink „daten“ zeigt in base/ auf eine durch „copy_exclude“ ausgeschlossene"
    assert text.count(hint) == 1, "one hint, even for three copies of base/"
