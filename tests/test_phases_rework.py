"""W4b-02b.1: rework after the review. Submissions read again after the release of an isolated
phase (H1), isolation and the text the agent really reads (M1), cutting the package (M2),
`peers` (Entscheidung 58), no transition before the first action, the size of an assignment
after inserting, the start of a large file with `content: full` (G2, G7).

Every H1 case runs twice: isolated and, as the counter-check, the same group without
`isolated`; both must give the same contents.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from phases_kit import (
    TurnView,
    answer,
    errors,
    hand_in,
    judge_gate,
    manager,
    parallel,
    run,
    solo,
)
from run_kit import make_workfolder

from arr.models import RunStatus
from arr.room.server import build_server
from arr.room.store import RoomStore
from arr.room.types import Submission
from arr.runs.orchestrator import Orchestrator
from arr.runs.phases.gates import GateOutcome
from arr.runs.phases.inputs import build_feedback
from arr.runs.phases.state import PassRecord
from arr.runs.phases.text import MAX_FULL_CHARS, MAX_PACKAGE_CHARS, fit_package, package_size

AGENTS = {"agents": [{"id": a} for a in ("a", "b", "c", "r1")]}
both = pytest.mark.parametrize("isolated", [False, True], ids=["open", "isolated"])


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def first_inputs(seen: dict[str, Any]) -> Any:
    def watch(view: TurnView) -> list[dict[str, Any]]:
        seen.setdefault(view.agent, view.context.get("inputs", []))
        return hand_in(view)

    return watch


# --------------------------------------------------------------------------------------------
# 1. H1: after the release the runner reads the submissions again
# --------------------------------------------------------------------------------------------


@both
@pytest.mark.parametrize("fmt", ["text", "artifact"])
async def test_h1_the_rater_gets_path_and_text(workfolder: Path, isolated: bool, fmt: str) -> None:
    seen: dict[str, Any] = {}
    watch = first_inputs(seen)
    group = {
        **AGENTS,
        "phases": [
            {
                **parallel("x", ["a", "b"], fmt),
                "isolated": isolated,
                "gate": judge_gate("r1", "rank", routes={"ranked": "next"}),
            },
            solo("y", "c"),
        ],
    }
    mgr, _log = manager(workfolder, group, {k: watch for k in ("a", "b", "c", "r1")})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    rater = seen["r1"]
    assert [i["from"] for i in rater] == ["a", "b"]
    for item in rater:
        assert item["path"] and "/output/" in item["path"] and "note" not in item, item
        if fmt == "artifact":
            assert "[Datei „werk.md“]" in item["text"] and "kein lesbarer" not in item["text"]
        else:
            assert item["text"].startswith("Text von ")
    # the next phase gets both as its standard input
    # the next phase gets both as its standard input (in the order of the ranking)
    assert sorted((i["source"], i["from"]) for i in seen["c"]) == [
        ("phase:x", "a"),
        ("phase:x", "b"),
    ]
    assert all(i["path"] for i in seen["c"])
    # the checkpoint of the gate names the same submissions in both cases
    store = RoomStore(mgr.run_dir)
    assert not any(s.sealed for s in store.submissions())


@both
async def test_h1_the_feedback_of_a_retry_shows_everyone(workfolder: Path, isolated: bool) -> None:
    feedback: list[tuple[str, list[str], str]] = []

    def watch(view: TurnView) -> list[dict[str, Any]]:
        if view.attempt == 2:
            subs = view.context["feedback"]["submissions"]
            feedback.append((view.agent, [s["from"] for s in subs], view.prompt))
        return hand_in(view, status="done" if view.attempt == 2 else "not_solved")

    group = {
        **AGENTS,
        "phases": [
            {
                **parallel("x", ["a", "b"]),
                "isolated": isolated,
                "max_visits": 2,
                "on_exhausted": "stop",
                "gate": {
                    "type": "signal",
                    "field": "status",
                    "rule": "all",
                    "routes": {"done": "next", "not_solved": "retry"},
                },
            }
        ],
    }
    mgr, _log = manager(workfolder, group, {"a": watch, "b": watch})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert sorted((a, sources) for a, sources, _p in feedback) == [
        ("a", ["a", "b"]),
        ("b", ["a", "b"]),
    ]
    for _agent, _sources, prompt in feedback:
        assert '"feedback"' in prompt and "b in x, Durchlauf 1" in prompt, "in the prompt"


# --------------------------------------------------------------------------------------------
# 2. M1: isolation (I1, I10), peers with a slow transition (R13), prompt and get_assignment
#    (T6), a redirected retry (R7)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["from", "name"])
async def test_m1_an_isolated_phase_reading_itself_shows_nothing_of_the_others(
    workfolder: Path, source: str
) -> None:
    seen: list[tuple[str, int, list[Any], str]] = []

    def watch(view: TurnView) -> list[dict[str, Any]]:
        seen.append((view.agent, view.repetition, view.context.get("inputs", []), view.prompt))
        return hand_in(view, summary=f"GEHEIM-{view.agent}-{view.repetition}")

    spec = {"from": "x", "pick": "all"} if source == "from" else {"name": "beweis", "pick": "all"}
    group = {
        "agents": [{"id": "a"}, {"id": "b"}],
        "phases": [
            {
                **parallel("x", ["a", "b"], "artifact", name="beweis"),
                "isolated": True,
                "repeat": 2,
                "inputs": [spec],
            }
        ],
    }
    mgr, _log = manager(workfolder, group, {"a": watch, "b": watch})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    own = []
    for agent, rep, inputs, prompt in seen:
        other = "b" if agent == "a" else "a"
        assert f"GEHEIM-{other}" not in prompt, (agent, rep)
        own += [i for i in inputs if f"GEHEIM-{agent}" in i.get("summary", "")]
    assert own, "counter-check: the agent's own earlier submission is an input"
    assert all(i["path"] is None and "versiegelt" in i["note"] for i in own)


def sub(id_: int, agent: str, *, sealed: bool) -> Submission:
    return Submission(
        id=id_,
        ts=0,
        agent=agent,
        shown_as=agent,
        stage="x",
        format="text",
        status="done",
        summary=f"Kurzfassung {agent}",
        path=f"groupchats/sealed/submission-{id_}" if sealed else f"output/s/{id_}",
        sealed=sealed,
    )


class _Ctx:
    run_folder = Path("/run")

    def shown_name(self, viewer: str, agent: str) -> str:
        return agent


def test_m1_the_feedback_shows_no_sealed_submission_of_another() -> None:
    """A second wall behind H1 (I10): even a sealed submission in a record stays out of the
    feedback of everyone but its author."""
    record = PassRecord(
        phase="x",
        attempt=1,
        how="normal",
        latest=[sub(1, "a", sealed=True), sub(2, "b", sealed=False)],
        missing=[],
        gate="done",
        outcome=GateOutcome("done"),
    )
    for_b = build_feedback(_Ctx(), record, "b", None)  # type: ignore[arg-type]
    assert [s["from"] for s in for_b["submissions"]] == ["b"]
    for_a = build_feedback(_Ctx(), record, "a", None)  # type: ignore[arg-type]
    assert [s["from"] for s in for_a["submissions"]] == ["a", "b"], "counter-check: own"
    assert for_a["submissions"][0]["path"] is None


async def test_m1_peers_never_show_the_running_repetition(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R13: a slow `reset_session` of a lets b hand in during a's transition; a's peers must
    still show only what was there before the repetition began."""
    reset = Orchestrator.reset_session

    async def slow_reset(self: Any, agent_id: str) -> None:
        if agent_id == "a":
            await asyncio.sleep(0.8)
        await reset(self, agent_id)

    monkeypatch.setattr(Orchestrator, "reset_session", slow_reset)
    peers: list[tuple[int, list[Any]]] = []

    def watch(view: TurnView) -> list[dict[str, Any]]:
        if view.stage == "x" and view.agent == "a":
            peers.append((view.repetition, view.context.get("peers", [])))
        return hand_in(view)

    group = {
        "agents": [{"id": "a"}, {"id": "b"}],
        "phases": [
            parallel("w", ["a", "b"]),
            {
                **parallel("x", ["a", "b"]),
                "repeat": 2,
                "context": "reset",
                "folder": "keep",
                "inputs": [{"peers": "summary"}],
            },
        ],
    }
    mgr, _log = manager(workfolder, group, {"a": watch, "b": watch})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert peers[0] == (1, []), "b's submission of repetition 1 came during a's reset"
    assert [p["summary"] for p in peers[1][1]] == ["b in x, Durchlauf 1"]


async def test_m1_feedback_is_in_the_prompt_and_in_get_assignment(workfolder: Path) -> None:
    prompts: list[str] = []

    def policy(view: TurnView) -> list[dict[str, Any]]:
        if view.attempt == 2:
            prompts.append(view.prompt)
        return hand_in(view, status="not_solved" if view.attempt == 1 else "done")

    group = {
        "agents": [{"id": "a"}],
        "phases": [
            {
                **solo("x", "a"),
                "max_visits": 2,
                "on_exhausted": "stop",
                "gate": {
                    "type": "signal",
                    "field": "status",
                    "routes": {"done": "next", "not_solved": "retry"},
                },
            }
        ],
    }
    mgr, _log = manager(workfolder, group, {"a": policy})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert '"feedback"' in prompts[0] and '"result": "not_solved"' in prompts[0]
    store = RoomStore(mgr.run_dir)
    async with Client(build_server(store.for_agent("a", mgr.prepared.agents["a"].work_dir))) as cl:
        text = (await cl.call_tool("get_assignment", {})).content[0].text
    assert '"feedback"' in text and '"result": "not_solved"' in text and "Durchlauf 2" in text


async def test_m1_a_redirected_retry_is_a_normal_entry(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7: `retry` of x, but x is exhausted and `on_exhausted: next` leads to y: y is entered
    like by `next` (context applies, no feedback)."""
    resets: list[str] = []
    reset = Orchestrator.reset_session

    async def record(self: Any, agent_id: str) -> None:
        resets.append(agent_id)
        await reset(self, agent_id)

    monkeypatch.setattr(Orchestrator, "reset_session", record)
    group = {
        "agents": [{"id": "a"}],
        "phases": [
            {
                **solo("x", "a"),
                "max_visits": 1,
                "on_exhausted": "next",
                "gate": {"type": "done", "routes": {"done": "retry"}},
            },
            {**solo("y", "a"), "context": "reset", "folder": "keep"},
        ],
    }
    mgr, log = manager(workfolder, group, {"a": answer()})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert resets == ["a"], "the context of y applies"
    second = log.of("a")[1]
    assert second.stage == "y" and "feedback" not in second.context
    assert '"feedback"' not in second.prompt


# --------------------------------------------------------------------------------------------
# 3. M2: the feedback is cut inside first
# --------------------------------------------------------------------------------------------


def test_m2_a_big_feedback_keeps_inputs_and_objections() -> None:
    package = {
        "phase": "x",
        "attempt": 2,
        "task": "T",
        "research_question": "Q",
        "submission": {"format": "text"},
        "inputs": [{"source": "phase:a", "id": 1, "summary": "wichtige Eingabe"}],
        "objections": [{"id": "E1", "status": "offen", "text": "Lücke in Lemma 2"}],
        "feedback": {
            "attempt": 1,
            "gate": "judge",
            "result": "ranked",
            "submissions": [{"id": k, "summary": "s" * 200} for k in range(50)],
            "ratings": [
                {"rater": "r", "ref": k, "score": 5, "comment": "c" * 1000} for k in range(150)
            ],
        },
    }
    out = fit_package(package)
    assert package_size(out) <= MAX_PACKAGE_CHARS
    assert out["inputs"] == [{"source": "phase:a", "id": 1, "summary": "wichtige Eingabe"}]
    assert out["objections"] == [{"id": "E1", "status": "offen", "text": "Lücke in Lemma 2"}]
    feedback = out["feedback"]
    assert feedback["result"] == "ranked" and out["feedback_cut"] is True
    assert len(feedback["ratings"]) == 150, "only comments went"
    assert sum("comment" in r for r in feedback["ratings"]) < 150


def test_m2_the_order_of_cutting() -> None:
    def package(n_inputs: int, n_objections: int, comment: int) -> dict[str, Any]:
        return {
            "task": "T",
            "submission": {"format": "text"},
            "inputs": [{"id": i, "summary": "i" * 790} for i in range(n_inputs)],
            "objections": [{"id": f"E{i}", "text": "o" * 300} for i in range(n_objections)],
            "feedback": {
                "result": "fail",
                "ratings": [{"ref": 1, "comment": "c" * comment}],
                "submissions": [{"summary": "s" * 790}] * 30,
            },
        }

    out = fit_package(package(200, 100, 50_000))
    assert package_size(out) <= MAX_PACKAGE_CHARS
    fb = out["feedback"]
    assert not fb.get("ratings") and not fb.get("submissions"), "the feedback is cut inside"
    assert fb["result"] == "fail", "the result of the gate stays"
    assert out["inputs_cut"] and len(out["objections"]) == 100, "objections fall last"


# --------------------------------------------------------------------------------------------
# 4. peers: per other agent its newest summary from before the repetition
# --------------------------------------------------------------------------------------------


async def test_peers_with_over_in_blocks(workfolder: Path) -> None:
    peers: dict[tuple[str, int | None, int], list[Any]] = {}

    def watch(view: TurnView) -> list[dict[str, Any]]:
        peers[(view.agent, view.entry, view.repetition)] = view.context.get("peers", [])
        return hand_in(view, summary=f"{view.agent} E{view.entry} W{view.repetition}")

    group = {
        "agents": [{"id": "a"}, {"id": "b"}],
        "seed": {"liste": {"param": "ideas"}},
        "phases": [
            {
                **parallel("x", ["a", "b"]),
                "over": "liste",
                "repeat": 2,
                "isolated": True,
                "inputs": [{"peers": "summary"}],
            }
        ],
    }
    mgr, _log = manager(
        workfolder, group, {"a": watch, "b": watch}, params={"ideas": ["1", "2", "3"]}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert peers[("a", 1, 1)] == [] and peers[("b", 2, 1)] == []
    assert [p["summary"] for p in peers[("a", 1, 2)]] == ["b E2 W1"]
    assert [p["summary"] for p in peers[("b", 2, 2)]] == ["a E1 W1"]
    # block 2: a works entry 3; only b, with its newest, never a's own earlier entries
    assert peers[("a", 3, 1)] == [{"from": "b", "status": "done", "summary": "b E2 W2", "entry": 2}]
    assert [p["summary"] for p in peers[("a", 3, 2)]] == ["b E2 W2"]


# --------------------------------------------------------------------------------------------
# 5. no transition before the very first action of an agent
# --------------------------------------------------------------------------------------------


async def test_no_transition_before_the_first_action(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    reset = Orchestrator.reset_session

    async def reset_session(self: Any, agent_id: str) -> None:
        calls.append(("reset", agent_id))
        await reset(self, agent_id)

    async def fresh_folder(self: Any, agent_id: str, *, stage: str, attempt: int) -> None:
        calls.append(("fresh", agent_id, stage))

    async def compact_session(self: Any, agent_id: str, *, note: str | None = None) -> None:
        calls.append(("compact", agent_id))

    monkeypatch.setattr(Orchestrator, "reset_session", reset_session)
    monkeypatch.setattr(Orchestrator, "fresh_folder", fresh_folder)
    monkeypatch.setattr(Orchestrator, "compact_session", compact_session)
    group = {
        "agents": [{"id": "a"}, {"id": "b"}, {"id": "r1"}],
        "phases": [
            {
                **solo("x", "a"),
                "context": {"a": "reset", "r1": "compact"},
                "folder": {"a": "fresh"},
                "gate": judge_gate("r1", "rank", routes={"ranked": "next"}),
            },
            {**parallel("y", ["a", "b"]), "context": "reset"},
        ],
    }
    mgr, _log = manager(workfolder, group, {"a": answer(), "b": answer(), "r1": answer()})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert sorted(calls) == [("fresh", "a", "y"), ("reset", "a")], (
        "x is the first action of a and r1, y the first of b: only a gets a transition"
    )


# --------------------------------------------------------------------------------------------
# 6. G2: the limit of an assignment after inserting, 7. G7: the start of a large file
# --------------------------------------------------------------------------------------------


async def test_g2_the_limit_holds_after_inserting(workfolder: Path) -> None:
    group = {"agents": [{"id": "a"}], "phases": [solo("x", "a")]}
    mgr, log = manager(
        workfolder,
        group,
        {"a": answer()},
        tasks={"tasks/x.md": "Lies: {{ params.long }}"},
        params={"long": "y" * 70_000},
    )
    assert await run(mgr) is RunStatus.FAILED
    [message] = errors(mgr)
    assert "phases[0].actions.a.task" in message and "Phase „x“" in message
    assert "„tasks/x.md“ ist nach dem Einsetzen der Platzhalter größer als 64 KB" in message
    assert log.turns == []


async def test_g7_a_large_file_gives_its_start(workfolder: Path) -> None:
    def big(view: TurnView) -> list[dict[str, Any]]:
        (view.work_dir / "beweis.md").write_text("Anfang\n" + "ä" * 200_000, "utf-8")
        (view.work_dir / "bild.bin").write_bytes(b"\x89PNG\x00\xff" * 10)
        return [
            {
                "do": "room",
                "action": "submit",
                "args": {"status": "done", "summary": "groß", "files": ["beweis.md", "bild.bin"]},
            }
        ]

    group = {
        "agents": [{"id": "a"}, {"id": "b"}],
        "phases": [
            solo("x", "a", "artifact", name="beweis"),
            {**solo("y", "b"), "inputs": [{"name": "beweis", "content": "full"}]},
        ],
    }
    mgr, log = manager(workfolder, group, {"a": big, "b": answer()})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    [item] = log.of("b")[0].context["inputs"]
    text = item["text"]
    assert "[Datei „beweis.md“, nur der Anfang; die ganze Datei liegt unter „path“]" in text
    assert "Anfang\näää" in text and len(text) <= MAX_FULL_CHARS
    assert item["path"]


async def test_g7_binary_stays_no_readable_text(workfolder: Path) -> None:
    def binary(view: TurnView) -> list[dict[str, Any]]:
        (view.work_dir / "bild.bin").write_bytes(b"\x89PNG\x00\xff" * 10)
        return [
            {
                "do": "room",
                "action": "submit",
                "args": {"status": "done", "summary": "Bild", "files": ["bild.bin"]},
            }
        ]

    group = {
        "agents": [{"id": "a"}, {"id": "b"}],
        "phases": [
            solo("x", "a", "artifact", name="bild"),
            {**solo("y", "b"), "inputs": [{"name": "bild", "content": "full"}]},
        ],
    }
    mgr, log = manager(workfolder, group, {"a": binary, "b": answer()})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    [item] = log.of("b")[0].context["inputs"]
    assert item["text"] == "[Datei „bild.bin“: kein lesbarer Text]"
