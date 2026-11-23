"""Finalizer and `changes.diff` (W2-04 A6-A8): the M1 scenario with FakeBackend through the real
RunManager, robustness after stop, limit, error and a half finished setup, idempotency.

Since W4b-06b the runs are phases runs (the M1 scenario as two phases with a gate `judge`, one
rework); a phases run writes no `results.json`/`ratings.json` and no „## Ergebnisse“, its
submissions are in `submissions.json` and „## Abgaben“."""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from phases_kit import TurnView, hand_in, manager, say, solo
from run_kit import (
    agent,
    channel,
    eventually,
    log_entries,
    make_manager,
    make_run_input,
    make_settings,
    make_workfolder,
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
from arr.runs.topology import check_topology

TASK = "Zeige, dass Lemma A für alle n ≡ 1 mod 4 gilt."


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def tool(command: str, result: str) -> dict[str, Any]:
    return {"do": "tool", "name": "Bash", "input": {"command": command}, "result": result}


def text(body: str) -> dict[str, Any]:
    return {"do": "text", "text": body}


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


def judged(name: str, actor: str, raters: list[str]) -> dict[str, Any]:
    """A phase `solo` whose artifact `name` a gate `judge` rates (mean, 6), at most two
    passes, retried below the threshold."""
    return {
        **solo(name, actor),
        "channels": ["main", "werkstatt"],
        "max_visits": 2,
        "on_exhausted": "next",
        "actions": {
            actor: {"task": f"tasks/{name}.md", "submission": {"format": "artifact", "name": name}}
        },
        "gate": {
            "type": "judge",
            "raters": raters,
            "action": {"task": "tasks/bewerten.md", "submission": {"format": "rating"}},
            "rule": "mean",
            "pass_score": 6,
            "routes": {"pass": "next", "fail": "retry"},
        },
    }


M1_GROUP = {
    "agents": [
        {"id": "strategist"},
        {"id": "prover"},
        {"id": "rater", "provider": "codex", "model": "fake-gpt", "auth": "fake-codex"},
    ],
    "channels": [
        channel("main", ["strategist", "prover", "rater"]),
        channel("werkstatt", ["strategist", "prover"]),
    ],
    "phases": [
        judged("strategie", "strategist", ["prover", "rater"]),
        judged("beweis", "prover", ["rater"]),
    ],
}
SCORES = {("prover", "strategie", 1): 4, ("prover", "strategie", 2): 8}
SCORES |= {("rater", "strategie", 1): 5, ("rater", "strategie", 2): 7, ("rater", "beweis", 1): 8}


def m1_policy(view: TurnView) -> list[dict[str, Any]]:
    """The turns of the M1 scenario: tools, two channels, an attachment, files changed and
    removed, a rework of the strategy, ratings of both raters."""
    if view.expected.get("format") == "rating":
        score = SCORES[(view.agent, view.stage, view.attempt)]
        return hand_in(view, score=lambda _v, _r: score, comment="passt")
    work = view.work_dir
    if view.agent == "strategist":
        steps: list[dict[str, Any]] = []
        if view.attempt == 1:
            notes = work / "notes.md"
            notes.write_text(notes.read_text("utf-8") + "Lemma B folgt aus A.\n", "utf-8")
            steps = [
                text("Ich plane den Beweis."),
                tool("python check.py", "ok"),
                say("werkstatt", "Ansatz: Induktion über n."),
            ]
        return [*steps, *hand_in(view)]
    (work / "check.py").unlink(missing_ok=True)
    (work / "notiz.md").write_text("Beweis: n = 4k + 1 …\n", "utf-8")
    return [
        tool("cat notiz.md", "Beweis: n = 4k + 1 …"),
        say("main", "Der Beweis steht, siehe Anhang.", refs=["notiz.md"]),
        say("werkstatt", "Danke für die Strategie."),
        *hand_in(view),
    ]


def m1_manager(workfolder: Path) -> RunManager:
    mgr, _log = manager(
        workfolder,
        M1_GROUP,
        {a: m1_policy for a in ("strategist", "prover", "rater")},
        task=TASK,
        finalizer=finalize_run,
    )
    check_topology(mgr.resolved)
    return mgr


async def test_m1_scenario_run_folder_is_complete(workfolder: Path) -> None:
    manager = m1_manager(workfolder)
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    run_dir = manager.run_dir

    # M1 1: mixed agents, two channels with different members, at least one rework
    assert {a.provider.value for a in manager.resolved.agents} == {"claude", "codex"}
    decisions = [
        e["data"]["decision"] for e in log_entries(run_dir, "checkpoint") if "decision" in e["data"]
    ]
    assert decisions == ["failed", "passed", "passed"]

    # M1 2: the run folder of docs/02 §4
    for name in ("run.yaml", "run.input.yaml", "task.md", "meta.json", "orchestrator.jsonl"):
        assert (run_dir / name).is_file(), name
    for name in ("approvals.jsonl", "usage.json"):
        assert (run_dir / name).is_file(), name
    for name in ("base", "skills", "agents", "groupchats/attachments", "output/submissions"):
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
    assert "Der Beweis steht, siehe Anhang." in main and "- Anhang: [notiz.md](" in main
    assert "Ansatz: Induktion" not in main, "werkstatt is a separate channel"
    [attachment] = list((run_dir / "groupchats" / "attachments" / "main").glob("*/notiz.md"))
    assert attachment.read_text("utf-8").startswith("Beweis:")

    # changes.diff against base/, without the files written by arr
    diff = (run_dir / "agents" / "strategist" / "changes.diff").read_text("utf-8")
    assert "--- /dev/null\n+++ b/strategie.md\n" in diff
    assert "--- a/notes.md\n+++ b/notes.md\n" in diff and "+Lemma B folgt aus A.\n" in diff
    assert "AGENTS.md" not in diff and "CLAUDE.md" not in diff
    prover_diff = (run_dir / "agents" / "prover" / "changes.diff").read_text("utf-8")
    assert "--- a/check.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-print('ok')\n" in prover_diff
    assert (run_dir / "agents" / "rater" / "changes.diff").read_text("utf-8") == ""

    # submissions (W4b-06b: no results.json, no ratings.json)
    output = run_dir / "output"
    assert not (output / "results.json").exists() and not (output / "ratings.json").exists()
    subs = json.loads((output / "submissions.json").read_text("utf-8"))["submissions"]
    work = [(s["agent"], s["stage"], s["attempt"], s["version"]) for s in subs if s["name"]]
    assert work == [
        ("strategist", "strategie", 1, 1),
        ("strategist", "strategie", 2, 2),
        ("prover", "beweis", 1, 1),
    ]
    ratings = [s for s in subs if s["format"] == "rating"]
    assert len(ratings) == 5 and {s["agent"] for s in ratings if s["stage"] == "beweis"} == {
        "rater"
    }
    assert (output / "strategie" / "v2" / "diff.patch").is_file(), "the rework as a diff"

    # usage.json and summary.md
    usage = json.loads((run_dir / "usage.json").read_text("utf-8"))
    assert set(usage["by_agent"]) == {"strategist", "prover", "rater"}
    summary = (output / "summary.md").read_text("utf-8")
    assert summary.startswith(f"# Zusammenfassung – {manager.prepared.run_id}")
    assert "- **Status:** abgeschlossen (done)" in summary
    assert "- **Grund:** Die Topologie ist regulär durchgelaufen." in summary
    assert "| strategie | 1 |" in summary and "| 4,5 | nicht bestanden |" in summary
    assert "| strategie | 2 |" in summary and "| 7,5 | bestanden |" in summary
    assert "- **beweis:** bestanden, akzeptiert: #" in summary and "Score 8, 1 Versuch" in summary
    assert "## Abgaben" in summary and "strategie v2" in summary
    assert "## Ergebnisse" not in summary
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


def steps(*items: dict[str, Any]) -> Any:
    """A policy that plays the same steps in every turn."""
    return lambda _view: list(items)


def solo_manager(
    workfolder: Path, policy: Any, *, task: str = TASK, limits: Any = None
) -> RunManager:
    """A phases run of one agent `a` in one phase `arbeit` with the finalizer."""
    mgr, _log = manager(
        workfolder,
        {"agents": [{"id": "a"}], "phases": [solo("arbeit", "a")]},
        {"a": policy},
        task=task,
        limits=limits,
        finalizer=finalize_run,
    )
    return mgr


def summary(manager: RunManager) -> str:
    return (manager.run_dir / "output" / "summary.md").read_text("utf-8")


async def test_finalizer_after_stop_mid_turn(workfolder: Path) -> None:
    manager = solo_manager(workfolder, steps(text("Ich fange an."), wait(30)))
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: manager.orchestrator is not None and bool(manager.orchestrator._active)
    )
    manager.stop()
    assert await asyncio.wait_for(running, 10) is RunStatus.STOPPED
    text_ = summary(manager)
    assert "- **Status:** gestoppt (stopped)" in text_
    assert "- **Grund:** Der User hat den Run gestoppt." in text_
    assert "Keine Ergebnisse abgegeben." not in text_ and "## Abgaben" not in text_
    transcript = (manager.run_dir / "agents" / "a" / "transcript.md").read_text("utf-8")
    assert "Ich fange an." in transcript and "interrupted" in transcript
    assert json.loads((manager.run_dir / "output" / "submissions.json").read_text("utf-8")) == {
        "version": 1,
        "run": manager.run_dir.name,
        "submissions": [],
    }


async def test_finalizer_after_a_limit(workfolder: Path) -> None:
    manager = solo_manager(workfolder, steps(text("…")), limits={"turns_per_agent": 1})
    assert await asyncio.wait_for(manager.run(), 10) is RunStatus.DONE
    text_ = summary(manager)
    assert "- **Status:** abgeschlossen (done)" in text_
    assert "- **Grund:** Limit: Die Zahl der Turns pro Agent ist erreicht." in text_


async def test_status_after_a_limit_and_a_topology_that_does_not_stop(workfolder: Path) -> None:
    """W2-04.1 B3: the `error` with `where: topology` does not make summary.md say `failed`."""

    async def stubborn(ctx: RunContext) -> None:
        await ctx.turn("a", "Los.")
        await asyncio.sleep(30)  # ignores the stop

    manager = make_manager(
        workfolder,
        stubborn,
        {"a": turns([text("ok")])},
        run_extra={"task": TASK, "limits": {"wall_time": 0.3}},
        finalizer=finalize_run,
        stop_grace_s=0.2,
    )
    assert await asyncio.wait_for(manager.run(), 10) is RunStatus.DONE
    [error] = log_entries(manager.run_dir, "error")
    assert error["data"]["where"] == "topology"
    text_ = summary(manager)
    assert "- **Status:** abgeschlossen (done)" in text_
    assert "- **Grund:** Limit: Die Gesamtlaufzeit des Runs ist erreicht." in text_
    assert f"- {error['data']['message']}" in text_, "kept as a hint"


async def test_finalizer_after_an_error(workfolder: Path) -> None:
    manager = solo_manager(workfolder, steps(), task=" ")
    assert await asyncio.wait_for(manager.run(), 10) is RunStatus.FAILED
    text_ = summary(manager)
    assert "- **Status:** fehlgeschlagen (failed)" in text_
    assert "- **Grund:** Fehler: Der Run hat keinen Auftrag: task.md ist leer." in text_
    assert "- **Dauer:**" in text_


async def test_finalizer_after_a_failed_setup(workfolder: Path) -> None:
    def broken_factory(prepared: Any) -> Any:
        raise RuntimeError("Backend nicht startbar")

    manager = solo_manager(workfolder, steps())
    manager.backend_factory = broken_factory
    assert await asyncio.wait_for(manager.run(), 10) is RunStatus.FAILED
    text_ = summary(manager)
    assert "- **Grund:** Fehler: Backend nicht startbar" in text_
    assert "- **Dauer:** nicht gestartet" in text_
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
    for name in ("output/summary.md", "output/submissions.json", "usage.json"):
        assert (run_dir / name).is_file(), name
    assert not (run_dir / "output" / "results.json").exists()
    for agent_id in ("a", "b"):
        assert (run_dir / "agents" / agent_id / "transcript.md").is_file()
        assert (run_dir / "agents" / agent_id / "changes.diff").read_text("utf-8") == ""
    text_ = (run_dir / "output" / "summary.md").read_text("utf-8")
    assert "- **Dauer:** nicht gestartet" in text_
    assert "- Die Gruppenchat-Datenbank fehlt (der Run wurde nicht fertig vorbereitet)." in text_
    assert "Schritt des Finalizers gescheitert" not in text_
    assert not (run_dir / "groupchats" / "room.sqlite").exists(), "nothing is created"


async def test_a_failing_step_does_not_stop_the_others(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(agent_dir: Path) -> Path:
        raise RuntimeError("kaputt")

    monkeypatch.setattr(finalize_module, "write_transcript_md", broken)
    manager = solo_manager(workfolder, lambda view: hand_in(view, summary="Ergebnis", body="42"))
    assert await asyncio.wait_for(manager.run(), 10) is RunStatus.DONE
    run_dir = manager.run_dir
    assert not (run_dir / "agents" / "a" / "transcript.md").exists()
    for name in ("groupchats", "output/submissions.json", "usage.json", "agents/a/changes.diff"):
        assert (run_dir / name).exists(), name
    text_ = summary(manager)
    assert "- Schritt des Finalizers gescheitert: Transkripte: RuntimeError: kaputt" in text_
    assert "- **#1** · `a` · text · done · Phase arbeit, Durchlauf 1" in text_
    assert "- Gesamt: 1 Turn, 0 Tokens ein, 0 aus, 1 Turn ohne Kosten" in text_  # W2-04.1 P. 7
    [error] = [e for e in log_entries(run_dir, "error") if e["data"].get("where") == "finalizer"]
    assert error["data"]["step"] == "Transkripte" and error["data"]["message"] == "kaputt"


async def test_summary_lists_a_failed_stage(workfolder: Path) -> None:
    """W2-04.1 B6 on a phases run: a phase whose gate `judge` fails ends the run there
    (`on_exhausted: stop`); the summary names the phase, its outcome and no later phase."""
    phase = {
        **solo("skizze", "a"),
        "max_visits": 1,
        "on_exhausted": "stop",
        "gate": {
            "type": "judge",
            "raters": ["b"],
            "action": {"task": "tasks/bewerten.md", "submission": {"format": "rating"}},
            "rule": "mean",
            "pass_score": 6,
            "routes": {"pass": "next", "fail": "retry"},
        },
    }

    def policy(view: TurnView) -> list[dict[str, Any]]:
        return hand_in(view, score=3, comment="Lücke")

    failed, _log = manager(
        workfolder,
        {"agents": [{"id": "a"}, {"id": "b"}], "phases": [phase, solo("beweis", "b")]},
        {"a": policy, "b": policy},
        finalizer=finalize_run,
    )
    assert await asyncio.wait_for(failed.run(), 10) is RunStatus.DONE
    text_ = summary(failed)
    assert "- **Status:** abgeschlossen (done)" in text_
    assert "- **Grund:** Die Phase „skizze“ wurde nicht bestanden (stage_failed)." in text_
    assert "| skizze | 1 |" in text_ and "| 3 | nicht bestanden |" in text_
    assert "- **skizze:** nicht bestanden, Run beendet, akzeptiert: keins, Score 3" in text_
    assert "beweis" not in text_.split("## Stufen")[1].split("## Abgaben")[0]


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


def test_changes_diff_large_file(tmp_path: Path) -> None:
    """W2-04.1 B2: a line diff only up to 256 KB and 10 000 lines per side."""
    base, work = tmp_path / "base", tmp_path / "work"
    write(base, "gross.txt", "a" * (300 * 1024))
    write(work, "gross.txt", "a" * (300 * 1024 - 1) + "b")
    write(base, "gleich.txt", "c" * (300 * 1024))
    write(work, "gleich.txt", "c" * (300 * 1024))
    write(base, "zeilen.txt", "x\n" * 10_001)
    write(work, "zeilen.txt", "x\n" * 10_000 + "y\n")
    write(base, "grenze.txt", "x\n" * 10_000)
    write(work, "grenze.txt", "x\n" * 9_999 + "y\n")
    text = changes_diff(base, work).text
    assert "Datei geändert (zu groß für einen Zeilen-Diff): gross.txt\n" in text
    assert "Datei geändert (zu groß für einen Zeilen-Diff): zeilen.txt\n" in text
    assert "--- a/grenze.txt\n+++ b/grenze.txt\n" in text and "+y\n" in text
    assert "gleich.txt" not in text


def test_changes_diff_of_a_large_scattered_change_is_fast(tmp_path: Path) -> None:
    """W2-04.1 B2: ~2 MB of text, every 7th line changed: under 1 s (before: minutes)."""
    base, work = tmp_path / "base", tmp_path / "work"
    lines = [f"{n},{n * n},{n % 97}\n" for n in range(102_000)]  # 1.98 MB
    write(base, "daten.csv", "".join(lines))
    write(work, "daten.csv", "".join(x if n % 7 else "geändert\n" for n, x in enumerate(lines)))
    assert 1_900_000 < (base / "daten.csv").stat().st_size < 2 * 1024 * 1024  # old limit
    start = time.monotonic()
    text = changes_diff(base, work).text
    assert time.monotonic() - start < 1.0
    assert text == "Datei geändert (zu groß für einen Zeilen-Diff): daten.csv\n"


skip_as_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read files with mode 000"
)


@skip_as_root
def test_changes_diff_unreadable_file_and_folder(tmp_path: Path) -> None:
    """W2-04.1 B1: one line per unreadable file or folder instead of an error."""
    base, work = tmp_path / "base", tmp_path / "work"
    write(base, "geheim/alt.txt", "alt\n")
    write(work, "geheim/alt.txt", "alt\n")
    write(work, "geheim/neu.txt", "neu\n")
    write(base, "zu.txt", "vorher\n")
    write(work, "zu.txt", "nachher\n")
    write(work, "ok.txt", "lesbar\n")
    (work / "zu.txt").chmod(0)
    (work / "geheim").chmod(0)
    try:
        text = changes_diff(base, work).text
    finally:
        (work / "geheim").chmod(0o755)
        (work / "zu.txt").chmod(0o644)
    assert "Datei nicht lesbar: zu.txt\n" in text
    assert "Ordner nicht lesbar: geheim\n" in text
    assert "alt.txt" not in text, "nothing inside an unreadable folder counts as deleted"
    assert "--- /dev/null\n+++ b/ok.txt\n" in text
    assert str(tmp_path) not in text


@skip_as_root
async def test_unreadable_files_cost_no_other_diff(workfolder: Path) -> None:
    """W2-04.1 B1: an unreadable file of agent a leaves the diffs of a and b intact."""
    prepared = prepared_only(workfolder)
    work_a = prepared.agents["a"].work_dir
    write(work_a, "zu.txt", "x\n")
    (work_a / "zu.txt").chmod(0)
    write(prepared.agents["b"].work_dir, "notiz.md", "b\n")
    try:
        await finalize_run(prepared)
    finally:
        (work_a / "zu.txt").chmod(0o644)
    run_dir = prepared.run_dir
    assert (run_dir / "agents/a/changes.diff").read_text("utf-8") == "Datei nicht lesbar: zu.txt\n"
    assert "+++ b/notiz.md" in (run_dir / "agents/b/changes.diff").read_text("utf-8")
    assert "Schritt des Finalizers gescheitert" not in (run_dir / "output/summary.md").read_text(
        "utf-8"
    )


async def test_a_failing_working_folder_does_not_stop_the_others(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W2-04.1 B1: one `try` per working folder in the step `changes.diff`."""
    prepared = prepared_only(workfolder)
    write(prepared.agents["b"].work_dir, "notiz.md", "b\n")
    real = finalize_module.changes_diff

    def flaky(base: Path | None, work: Path, exclude: Any = ()) -> Any:
        if work == prepared.agents["a"].work_dir:
            raise RuntimeError(f"kaputt in {work}")
        return real(base, work, exclude)

    monkeypatch.setattr(finalize_module, "changes_diff", flaky)
    await finalize_run(prepared)
    run_dir = prepared.run_dir
    assert (run_dir / "agents/a/changes.diff").read_text("utf-8") == (
        "Änderungen nicht ermittelbar (RuntimeError).\n"
    )
    assert "+++ b/notiz.md" in (run_dir / "agents/b/changes.diff").read_text("utf-8")
    summary_text = (run_dir / "output/summary.md").read_text("utf-8")
    assert "- Schritt des Finalizers gescheitert: changes.diff für agents/a/work: RuntimeError" in (
        summary_text
    )
    assert str(run_dir) not in summary_text


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


async def test_w2_05_summary_names_turns_that_ended_with_an_error(workfolder: Path) -> None:
    """A failed Codex login (`login_hint`) and every other turn error reach summary.md."""
    hint = "Profil „chatgpt-x“ neu anmelden: uv run arr auth codex-login chatgpt-x (401)"

    def policy(view: TurnView) -> list[dict[str, Any]]:
        if view.turn == 1:
            return [text("Ich versuche es."), {"do": "error", "message": hint}]
        return hand_in(view, summary="Zweiter Versuch", body="Jetzt klappt es.")

    manager = solo_manager(workfolder, policy)
    assert await asyncio.wait_for(manager.run(), 10) is RunStatus.DONE
    found = summary(manager)
    assert "## Hinweise" in found
    assert f"- Turn 1 von `a` endete mit Fehler: {hint}" in found
    assert "Turn 2 von `a`" not in found
