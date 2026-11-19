"""W4b-02b.1 Teil 2 C: the phase runner against the real `Orchestrator` with `fresh_folder`
(W4b-05a) and `compact_session` / auto-compact (W4b-05b). FakeBackend agents, no recording
context; every case with its counter-check.

C1 `review` (0 rounds) with `judge_memory: reset`; C2 `over` with `folder: fresh`; C3
`context: compact` with `note_next`; C4 auto-compact in the middle of an action; C5 an
`ArrError` of `fresh_folder` or `compact_session`, a stop during `fresh_folder`; C6 an
isolated phase with a fresh folder.

Checked are states and orders (log entries, folders, prompts), never durations.
"""

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from compact_kit import NOTE, fill, is_note_prompt, say
from phases_kit import (
    PolicyBackend,
    TurnView,
    answer,
    errors,
    fixture_manager,
    hand_in,
    manager,
    parallel,
    run,
    solo,
    submit,
)
from run_kit import events, eventually, log_entries, make_workfolder

from arr.errors import ArrError
from arr.models import RunStatus
from arr.room.store import NOTE_TURN_REFUSED
from arr.runs import folders
from arr.runs.compact import PACKAGE_HEADING
from arr.runs.finalize import finalize_run

JUDGES = ("judge-1", "judge-2")


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def agent_log(run_dir: Path, agent: str, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    """The entries of `kinds` about `agent`, in order: kind plus data."""
    return [
        {"kind": e["kind"], **e["data"]}
        for e in log_entries(run_dir)
        if e["kind"] in kinds and e["data"].get("agent") == agent
    ]


def retired(run_dir: Path, agent: str) -> list[str]:
    return [p.name for p in folders.retired_folders(run_dir, agent)]


def is_dir(path: str) -> bool:
    return Path(path).is_dir()


def work_dir(mgr: Any, agent: str) -> Path:
    assert mgr.prepared is not None
    return Path(mgr.prepared.agents[agent].work_dir)


# --------------------------------------------------------------------------------------------
# C1: `review` (0 rounds) with `judge_memory: reset`
# --------------------------------------------------------------------------------------------


def judge(seen: dict[str, list[Any]]) -> Any:
    def policy(view: TurnView) -> list[dict[str, Any]]:
        info = view.work_dir.stat()
        seen.setdefault(view.agent, []).append(
            (
                view.attempt,
                str(view.work_dir),
                info.st_ino,
                sorted(p.name for p in view.work_dir.iterdir()),
            )
        )
        if view.attempt == 1:
            (view.work_dir / f"notiz-{view.agent}.md").write_text("Lemma 2 offen.\n", "utf-8")
            return hand_in(view, summary="Gutachten", verdict="minor")
        return hand_in(view, summary="Gutachten", verdict="accept")

    return policy


@pytest.mark.parametrize("memory", ["reset", "keep"])
async def test_c1_review_with_judge_memory_reset(workfolder: Path, memory: str) -> None:
    seen: dict[str, list[Any]] = {}
    policies = {"author": answer(), **{j: judge(seen) for j in JUDGES}}
    mgr, _log = fixture_manager(
        workfolder, "review", policies, params={"discussion_rounds": 0, "judge_memory": memory}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    run_dir = mgr.run_dir
    for judge_id in JUDGES:
        [(a1, dir1, ino1, _), (a2, dir2, ino2, files2)] = seen[judge_id]
        assert (a1, a2) == (1, 2)
        assert dir1 == dir2 == str(work_dir(mgr, judge_id)) and ino1 == ino2, "the same folder"
        note = f"notiz-{judge_id}.md"
        moves = agent_log(run_dir, judge_id, ("session_reset", "folder_fresh"))
        if memory == "keep":  # counter-check
            assert moves == [] and retired(run_dir, judge_id) == []
            assert note in files2
            continue
        # first a new session, then a fresh folder, only before the second check
        assert [(m["kind"], m.get("attempt")) for m in moves] == [
            ("session_reset", None),
            ("folder_fresh", 2),
        ]
        assert moves[1]["stage"] == "pruefung-allein"
        assert note not in files2, "what the judge made in check 1 is gone"
        assert retired(run_dir, judge_id) == ["001-pruefung-allein"]
        filed = folders.retired_root(run_dir) / judge_id / "001-pruefung-allein"
        assert (filed / note).read_text("utf-8") == "Lemma 2 offen.\n"
        diff = run_dir / "agents" / judge_id / "retired-001-pruefung-allein.diff"
        assert note in diff.read_text("utf-8"), "the finalizer writes the diff"


# --------------------------------------------------------------------------------------------
# C2: `over` with `folder: fresh`
# --------------------------------------------------------------------------------------------


def per_entry(seen: dict[str, list[Any]]) -> Any:
    def policy(view: TurnView) -> list[dict[str, Any]]:
        seen.setdefault(view.agent, []).append(
            (view.entry, sorted(p.name for p in view.work_dir.glob("eintrag-*")))
        )
        (view.work_dir / f"eintrag-{view.entry}.md").write_text(f"{view.entry}\n", "utf-8")
        return hand_in(view)

    return policy


@pytest.mark.parametrize("mode", ["fresh", "keep"])
async def test_c2_over_with_a_fresh_folder_per_entry(workfolder: Path, mode: str) -> None:
    seen: dict[str, list[Any]] = {}
    group = {
        "agents": [{"id": a} for a in ("a", "b", "c")],
        "phases": [
            solo("ideen", "a", "list", name="ideen"),
            {**parallel("w", ["b", "c"]), "over": "ideen", "folder": mode},
        ],
    }
    policies = {"a": answer(items=5), "b": per_entry(seen), "c": per_entry(seen)}
    mgr, _log = manager(workfolder, group, policies, finalizer=finalize_run)
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    run_dir = mgr.run_dir
    # entries round robin: b gets 1, 3, 5, c gets 2, 4; the first action of each is no transition
    if mode == "keep":  # counter-check
        assert seen["b"] == [(1, []), (3, ["eintrag-1.md"]), (5, ["eintrag-1.md", "eintrag-3.md"])]
        assert retired(run_dir, "b") == retired(run_dir, "c") == []
        return
    assert seen["b"] == [(1, []), (3, []), (5, [])] and seen["c"] == [(2, []), (4, [])]
    assert retired(run_dir, "b") == ["001-w", "002-w"], "the filing counts up per agent"
    assert retired(run_dir, "c") == ["001-w"]
    root = folders.retired_root(run_dir)
    assert [p.name for p in (root / "b" / "001-w").glob("eintrag-*")] == ["eintrag-1.md"]
    assert [p.name for p in (root / "b" / "002-w").glob("eintrag-*")] == ["eintrag-3.md"]
    assert sorted(p.name for p in work_dir(mgr, "b").glob("eintrag-*")) == ["eintrag-5.md"]
    fresh = agent_log(run_dir, "b", ("folder_fresh",))
    assert [(f["stage"], f["attempt"]) for f in fresh] == [("w", 1), ("w", 1)]
    for name in ("retired-001-w.diff", "retired-002-w.diff"):
        assert (run_dir / "agents" / "b" / name).is_file()


# --------------------------------------------------------------------------------------------
# C3: `context: compact` with the agent's own `note_next`
# --------------------------------------------------------------------------------------------


def noting(view: TurnView) -> list[dict[str, Any]]:
    return hand_in(view, note_next=f"GEHEIM-NOTIZ-{view.agent}")


@pytest.mark.parametrize("mode", ["compact", "keep"])
async def test_c3_the_own_note_next_comes_back_only_to_its_author(
    workfolder: Path, mode: str
) -> None:
    group = {
        "agents": [{"id": a} for a in ("a", "b")],
        "channels": [{"name": "k", "members": ["a", "b"]}],
        "phases": [
            parallel("x", ["a", "b"]),
            {**parallel("y", ["a", "b"]), "context": {"a": mode}},
            parallel("z", ["a", "b"]),
        ],
    }
    mgr, log = manager(workfolder, group, {"a": noting, "b": noting})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    [ax, ay, az] = log.of("a")
    secret = "GEHEIM-NOTIZ-a"
    compacts = [e["data"] for e in log_entries(mgr.run_dir, "compact")]
    if mode == "keep":  # counter-check
        assert compacts == [] and not ay.prompt.startswith(PACKAGE_HEADING)
        assert secret not in ay.prompt
        return
    assert ay.prompt.startswith(PACKAGE_HEADING) and secret in ay.prompt
    assert "GEHEIM-NOTIZ-b" not in ay.prompt
    assert [(c["agent"], c["reason"]) for c in compacts] == [("a", "phase")]
    assert ay.context["phase"] == "y" and ay.context["attempt"] == 1
    # once, only for its author, in no context package and in no channel
    assert secret not in ax.prompt and secret not in az.prompt
    assert not az.prompt.startswith(PACKAGE_HEADING)
    for view in [*log.of("a"), *log.of("b")]:
        assert secret not in str(view.context)
        if view.agent == "b":
            assert secret not in view.prompt
    channels = [m.body for m in mgr.orchestrator.channel_messages("k")]
    assert not [b for b in channels if secret in b]


# --------------------------------------------------------------------------------------------
# C4: auto-compact between two turns of the same action
# --------------------------------------------------------------------------------------------


def compacting(turns: list[tuple[str, TurnView]]) -> Any:
    def policy(view: TurnView) -> list[dict[str, Any]]:
        if is_note_prompt(view.prompt):
            turns.append(("note", view))
            return [submit(status="done", summary="aus der Notiz"), say(NOTE), fill(100)]
        work = [v for kind, v in turns if kind == "work"]
        turns.append(("work", view))
        if not work:
            return [say("Ich rechne noch."), fill(600)]  # reaches the threshold of 500
        return hand_in(view, summary="Fassung")

    return policy


@pytest.mark.parametrize("at", [500, "100%"], ids=["compact", "off"])
async def test_c4_auto_compact_in_the_middle_of_an_action(workfolder: Path, at: Any) -> None:
    turns: list[tuple[str, TurnView]] = []
    group = {
        "agents": [{"id": "a", "compact": {"at": at}}],
        "phases": [solo("x", "a", turns=2)],
    }
    mgr, _log = manager(workfolder, group, {"a": compacting(turns)})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    kinds = [k for k, _ in turns]
    work = [v for k, v in turns if k == "work"]
    first, second = work
    subs = mgr.room.submissions(agent="a")
    assert [(s.summary, s.stage, s.attempt, s.action) for s in subs] == [
        ("Fassung", "x", 1, "work")
    ]
    assert first.context == second.context, "get_assignment gives the whole assignment"
    assert all(v.context["attempt"] == 1 for _, v in turns)
    # the second work turn is the next turn of the action, not the follow-up question
    assert "Turn 2 von 2" in second.prompt and not second.follow_up
    if at == "100%":  # counter-check
        assert kinds == ["work", "work"] and not second.prompt.startswith(PACKAGE_HEADING)
        return
    assert kinds == ["work", "note", "work"], "the note turn is no turn of the action"
    assert second.prompt.startswith(PACKAGE_HEADING)
    assert "Auftrag tasks/x.md." in second.prompt, "the running action is in the package"
    refused = [
        e["data"] for e in events(mgr.run_dir, "a") if e["turn"] == 2 and e["kind"] == "tool_result"
    ]
    assert [(r["is_error"], r["content"]) for r in refused] == [(True, NOTE_TURN_REFUSED)]
    assert mgr.orchestrator.agents["a"].turns_used == 3, "the note turn counts as a turn"


# --------------------------------------------------------------------------------------------
# C5: errors of `fresh_folder` and `compact_session`, a stop during `fresh_folder`
# --------------------------------------------------------------------------------------------

TWO_PHASES = {
    "agents": [{"id": "a"}],
    "phases": [solo("x", "a"), {**solo("y", "a"), "folder": "fresh"}],
}


@pytest.mark.parametrize("broken", [True, False], ids=["error", "counter-check"])
async def test_c5_an_error_of_fresh_folder_ends_the_run(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, broken: bool
) -> None:
    message = "Der Arbeitsordner von „a“ ist nach dem Neu-Füllen nicht frisch: „rest.md“."
    if broken:

        def not_fresh(*args: Any, **kwargs: Any) -> Any:
            raise ArrError(message)

        monkeypatch.setattr(folders, "fresh_folder", not_fresh)
    mgr, log = manager(workfolder, TWO_PHASES, {"a": answer()})
    status = await run(mgr)
    if not broken:
        assert status is RunStatus.DONE and [v.stage for v in log.of("a")] == ["x", "y"]
        return
    assert status is RunStatus.FAILED
    assert errors(mgr) == [message]
    assert [v.stage for v in log.of("a")] == ["x"], "no turn after the error"
    [failed] = agent_log(mgr.run_dir, "a", ("folder_fresh_failed",))
    assert failed["message"] == message and failed["stage"] == "y"


@pytest.mark.parametrize("broken", [True, False], ids=["error", "counter-check"])
async def test_c5_an_error_of_compact_session_ends_the_run(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, broken: bool
) -> None:
    message = "Die Sitzung von „a“ lässt sich nicht neu beginnen."
    if broken:

        async def reset(self: Any) -> None:
            raise ArrError(message)

        monkeypatch.setattr(PolicyBackend, "reset", reset)
    group = {
        "agents": [{"id": "a"}],
        "phases": [solo("x", "a"), {**solo("y", "a"), "context": "compact"}],
    }
    mgr, log = manager(workfolder, group, {"a": noting})
    status = await run(mgr)
    if not broken:
        assert status is RunStatus.DONE and [v.stage for v in log.of("a")] == ["x", "y"]
        return
    assert status is RunStatus.FAILED
    assert errors(mgr) == [message]
    assert [v.stage for v in log.of("a")] == ["x"]


def blocked_fresh_folder(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, ...]:
    """`folders.fresh_folder` that waits for `go` after `started` (the real one then)."""
    started, go = threading.Event(), threading.Event()
    original = folders.fresh_folder

    def slow(*args: Any, **kwargs: Any) -> Any:
        started.set()
        go.wait(60)  # a bound only: the test sets `go`
        return original(*args, **kwargs)

    monkeypatch.setattr(folders, "fresh_folder", slow)
    return started, go


@pytest.mark.parametrize("grace", ["runner ends", "cancelled"])
async def test_c5_a_stop_during_fresh_folder_ends_cleanly(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, grace: str
) -> None:
    """The runner ends by itself after `fresh_folder` returns, or (no grace) its task is
    cancelled while the files move: either way the folder is whole, no turn follows."""
    started, go = blocked_fresh_folder(monkeypatch)
    kwargs: dict[str, Any] = {"stop_grace_s": 0.0} if grace == "cancelled" else {}
    mgr, log = manager(workfolder, TWO_PHASES, {"a": answer()}, **kwargs)
    task = asyncio.create_task(run(mgr))
    await eventually(started.is_set, within=30)
    mgr.stop()
    if grace == "cancelled":
        await eventually(
            lambda: any(e["data"].get("where") == "topology" for e in log_entries(mgr.run_dir))
        )
    else:
        await eventually(lambda: mgr.orchestrator.halted.is_set())
    go.set()
    assert await task is RunStatus.STOPPED
    assert [v.stage for v in log.of("a")] == ["x"], "no turn after the stop"
    [fresh] = agent_log(mgr.run_dir, "a", ("folder_fresh", "folder_fresh_failed"))
    assert fresh["kind"] == "folder_fresh"
    assert bool(fresh.get("cancelled")) is (grace == "cancelled")
    assert retired(mgr.run_dir, "a") == ["001-y"]
    if grace == "runner ends":
        assert errors(mgr) == []


# --------------------------------------------------------------------------------------------
# C6: an isolated phase with a fresh folder
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("isolated", [True, False], ids=["isolated", "open"])
async def test_c6_sealing_release_and_filing_do_not_disturb_each_other(
    workfolder: Path, isolated: bool
) -> None:
    group = {
        "agents": [{"id": a} for a in ("a", "b", "c")],
        "phases": [
            solo("x", "a", "artifact", name="entwurf"),
            {**parallel("y", ["a", "b"], "artifact"), "isolated": isolated, "folder": "fresh"},
            {
                **parallel("z", ["a", "c"]),
                "folder": {"a": "fresh"},
                "inputs": [{"from": "y", "content": "full"}],
            },
        ],
    }
    mgr, log = manager(workfolder, group, {a: answer() for a in "abc"}, finalizer=finalize_run)
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    run_dir = mgr.run_dir
    root = folders.retired_root(run_dir)
    assert retired(run_dir, "a") == ["001-y", "002-z"]
    assert retired(run_dir, "b") == [], "b's first action: no transition"
    assert (root / "a" / "001-y" / "entwurf.md").read_text("utf-8") == "a 1\n"
    assert (root / "a" / "002-z" / "werk.md").read_text("utf-8") == "a 1\n"
    for agent in ("a", "c"):
        [z] = [v for v in log.of(agent) if v.stage == "z"]
        inputs = sorted(z.context["inputs"], key=lambda i: i["from"])
        assert [i["from"] for i in inputs] == ["a", "b"]
        for i, author in zip(inputs, "ab", strict=True):
            assert "/output/" in i["path"] and is_dir(i["path"])
            assert f"{author} 1" in i["text"]
    subs = mgr.room.submissions(stage="y")
    assert all(not s.sealed and s.path.startswith("output/") for s in subs)
    assert all((run_dir / s.path / "files" / "werk.md").is_file() for s in subs)
    sealed_dir = run_dir / "groupchats" / "sealed"
    assert not sealed_dir.exists() or not [p for p in sealed_dir.iterdir() if p.name != ".keep"]
    for name in ("retired-001-y.diff", "retired-002-z.diff"):
        assert (run_dir / "agents" / "a" / name).is_file()
    kinds = [
        (e["kind"], e["data"].get("stage"))
        for e in log_entries(run_dir)
        if e["kind"] in ("results_sealed", "results_released", "folder_fresh")
    ]
    if isolated:
        assert kinds[:3] == [
            ("results_sealed", "y"),
            ("folder_fresh", "y"),
            ("results_released", "y"),
        ]
    else:  # counter-check
        assert kinds[:1] == [("folder_fresh", "y")]
