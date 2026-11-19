"""W4b-02b A21: the test groups of `tests/fixtures/phases/` from start to end with FakeBackend
agents: `solo`, `waterfall` (rework, best attempt, `on_exhausted`), `exploration` in all three
variants of `exchange`, `review` with `discussion_rounds: 0` (`judge_memory` keep and reset).
`review` with its defaults, `roundtable` and `beispiel` are refused before the start."""

from pathlib import Path
from typing import Any

import pytest
from phases_kit import (
    Choose,
    TurnView,
    answer,
    checkpoints,
    entered,
    errors,
    fixture_manager,
    hand_in,
    run,
)
from run_kit import make_workfolder

from arr.errors import ConfigError
from arr.models import RunStatus
from arr.permissions.types import EscalationOption
from arr.room.store import RoomStore
from arr.runs.orchestrator import Orchestrator


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def turns(log: Any) -> list[tuple[str, str | None, int, str | None]]:
    return [(t[0], t[1], t[2], t[3]) for t in log.turns]


async def test_solo(workfolder: Path) -> None:
    mgr, log = fixture_manager(workfolder, "solo", {"worker": answer()})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert turns(log) == [("worker", "arbeit", 1, "work")]
    assert (mgr.run_dir / "output" / "submissions.json").is_file()


async def test_waterfall_rework_and_best_attempt(workfolder: Path) -> None:
    """strategie passes at once; skizze fails twice (6, then 5): the best attempt (6) counts,
    `on_exhausted: next`; beweis passes in its second attempt."""
    plan = {
        ("strategie", 1): 8,
        ("skizze", 1): 6,
        ("skizze", 2): 5,
        ("beweis", 1): 4,
        ("beweis", 2): 9,
    }

    def score(view: TurnView, ref: int) -> float:
        return plan[(view.stage, view.attempt)]

    raters = {f"rater-{i}": answer(score=score) for i in (1, 2)}
    mgr, log = fixture_manager(
        workfolder, "waterfall", {"strategist": answer(), "prover": answer(), **raters}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert entered(mgr) == ["strategie", "skizze", "skizze", "beweis", "beweis"]
    assert turns(log) == [
        ("strategist", "strategie", 1, "work"),
        ("rater-1", "strategie", 1, "gate"),
        ("rater-2", "strategie", 1, "gate"),
        ("prover", "skizze", 1, "work"),
        ("rater-1", "skizze", 1, "gate"),
        ("rater-2", "skizze", 1, "gate"),
        ("prover", "skizze", 2, "work"),
        ("rater-1", "skizze", 2, "gate"),
        ("rater-2", "skizze", 2, "gate"),
        ("prover", "beweis", 1, "work"),
        ("rater-1", "beweis", 1, "gate"),
        ("rater-2", "beweis", 1, "gate"),
        ("prover", "beweis", 2, "work"),
        ("rater-1", "beweis", 2, "gate"),
        ("rater-2", "beweis", 2, "gate"),
    ]
    ends = {c["stage"]: c for c in checkpoints(mgr, "stage_end")}
    store = RoomStore(mgr.run_dir)
    first_sketch = store.submissions(stage="skizze", action="work")[0]
    assert ends["skizze"]["outcome"] == "best_accepted"
    assert ends["skizze"]["accepted"] == first_sketch.id and ends["skizze"]["score"] == 6
    assert ends["beweis"]["outcome"] == "passed"
    second_sketch = [v for v in log.of("prover") if v.stage == "skizze"][1]
    assert second_sketch.context["feedback"]["result"] == "fail"
    beweis = [v for v in log.of("prover") if v.stage == "beweis"][0]
    sources = {i["source"]: i["id"] for i in beweis.context["inputs"]}
    assert sources["phase:skizze"] == first_sketch.id, "the best attempt is the input"
    summary = (mgr.run_dir / "output" / "summary.md").read_text("utf-8")
    assert "skizze" in summary


async def test_waterfall_escalates_when_asked(workfolder: Path) -> None:
    decider = Choose(EscalationOption.END)
    raters = {f"rater-{i}": answer(score=3) for i in (1, 2)}
    mgr, log = fixture_manager(
        workfolder,
        "waterfall",
        {"strategist": answer(), "prover": answer(), **raters},
        params={"on_fail": "escalate", "max_attempts": 1},
        decider=decider,
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert entered(mgr) == ["strategie"]
    assert "aufgebraucht" in str(decider.asked[0].summary)


EXPLORERS = {
    "ideator": answer(items=3),
    "explorer-1": answer(),
    "explorer-2": answer(),
    "ranker-1": answer(),
    "summarizer": answer(),
}


@pytest.mark.parametrize(
    ("exchange", "phase"),
    [("none", "exploration"), ("digest", "exploration-digest"), ("channel", "exploration-kanal")],
)
async def test_exploration_three_variants(workfolder: Path, exchange: str, phase: str) -> None:
    mgr, log = fixture_manager(workfolder, "exploration", EXPLORERS, params={"exchange": exchange})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert entered(mgr) == ["ideen", phase, "zusammenfassung"]
    order = [(t[0], t[3], t[4]) for t in log.turns if t[1] == phase]
    assert sorted(order[:4]) == [
        ("explorer-1", "work-1", 1),
        ("explorer-1", "work-1", 2),
        ("explorer-2", "work-2", 1),
        ("explorer-2", "work-2", 2),
    ]
    assert [o[2] for o in order[:4]] == [1, 1, 2, 2], "lockstep"
    assert order[4:] == [
        ("explorer-1", "work-3", 1),
        ("explorer-1", "work-3", 2),
        ("ranker-1", "gate", 1),
    ]
    [gate] = [c for c in checkpoints(mgr, "gate") if c["stage"] == phase]
    assert gate["value"] == "ranked" and len(gate["ranking"]) == 3
    peers = [
        v.context.get("peers")
        for v in log.views
        if v.stage == phase and v.agent.startswith("explorer") and v.repetition == 2
    ]
    if exchange == "digest":
        assert all(p for p in peers), "digest: summaries of the others"
    else:
        assert peers == [None, None, None]
    first = [v for v in log.views if v.stage == phase][0]
    assert first.context["entry"]["title"] == "Idee 1"


async def test_exploration_with_given_ideas(workfolder: Path) -> None:
    mgr, log = fixture_manager(
        workfolder, "exploration", EXPLORERS, params={"ideas": ["Modulo 4", "Gaußsche Zahlen"]}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert entered(mgr) == ["exploration", "zusammenfassung"], "seed instead of the ideator"
    assert [v.context["entry"]["title"] for v in log.views[:2]] == ["Modulo 4", "Gaußsche Zahlen"]


def judge(first: str, later: str = "accept") -> Any:
    def policy(view: TurnView) -> list[dict[str, Any]]:
        if view.attempt == 1:
            return hand_in(view, verdict=first, objections=[{"text": f"{view.agent}: Lücke"}])
        return hand_in(
            view, verdict=later, objections=[{"ref": "E1", "status": "behoben", "text": "ok"}]
        )

    return policy


@pytest.mark.parametrize("memory", ["keep", "reset"])
async def test_review_without_discussion(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, memory: str
) -> None:
    calls: list[tuple[str, ...]] = []
    if memory == "reset":
        reset = Orchestrator.reset_session

        async def reset_session(self: Any, agent_id: str) -> None:
            calls.append(("reset", agent_id))
            await reset(self, agent_id)

        async def fresh_folder(self: Any, agent_id: str, *, stage: str, attempt: int) -> None:
            calls.append(("fresh", agent_id, stage, str(attempt)))

        monkeypatch.setattr(Orchestrator, "reset_session", reset_session)
        monkeypatch.setattr(Orchestrator, "fresh_folder", fresh_folder)
    mgr, log = fixture_manager(
        workfolder,
        "review",
        {"author": answer(), "judge-1": judge("minor"), "judge-2": judge("major")},
        params={"discussion_rounds": 0, "judge_memory": memory},
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert entered(mgr) == ["fassung", "pruefung-allein", "revision", "pruefung-allein"]
    seq = turns(log)
    assert seq[0] == ("author", "fassung", 1, "work") and seq[3] == (
        "author",
        "revision",
        1,
        "work",
    )
    assert sorted(seq[1:3]) == [
        ("judge-1", "pruefung-allein", 1, "work"),
        ("judge-2", "pruefung-allein", 1, "work"),
    ]
    assert sorted(seq[4:]) == [
        ("judge-1", "pruefung-allein", 2, "work"),
        ("judge-2", "pruefung-allein", 2, "work"),
    ]
    revision = [v for v in log.of("author") if v.stage == "revision"][0]
    assert [o["id"] for o in revision.context["objections"]] == ["E1", "E2"]
    second = [v for v in log.of("judge-1") if v.attempt == 2][0]
    assert second.expected["known_objections"] == ["E1", "E2"]
    beweis = [i for i in second.context["inputs"] if i["source"] == "name:beweis"]
    assert beweis[0]["version"] == 2 and "author 1" in beweis[0]["text"]
    gates = [c for c in checkpoints(mgr, "gate") if c["stage"] == "pruefung-allein"]
    assert [g["value"] for g in gates] == ["major", "accept"]
    if memory == "reset":
        for judge_id in ("judge-1", "judge-2"):
            own = [c for c in calls if c[1] == judge_id]
            assert own == [
                ("reset", judge_id),
                ("fresh", judge_id, "pruefung-allein", "1"),
                ("reset", judge_id),
                ("fresh", judge_id, "pruefung-allein", "2"),
            ]
        assert sorted(calls) == sorted(
            [("reset", j) for j in ("judge-1", "judge-2") for _ in (1, 2)]
            + [
                ("fresh", j, "pruefung-allein", n)
                for j in ("judge-1", "judge-2")
                for n in ("1", "2")
            ]
        )
    else:
        assert calls == []


@pytest.mark.parametrize(
    ("name", "params", "path", "sentence"),
    [
        (
            "review",
            {},
            "phases[3].topology",
            "Die Topologie „chat“ (Phase „diskussion“) ist in dieser Version noch nicht verfügbar.",
        ),
        (
            "roundtable",
            {},
            "phases[0].topology",
            "Die Topologie „chat“ (Phase „diskussion“) ist in dieser Version noch nicht verfügbar.",
        ),
        (
            "beispiel",
            {},
            "phases[2].standby",
            "Agenten auf Abruf („standby“, Phase „beweis“) sind in dieser Version noch nicht "
            "verfügbar.",
        ),
        (
            "review",
            {"discussion_rounds": 0},
            "phases[2].folder",
            "Ein frischer Arbeitsordner („folder: fresh“, Phase „pruefung-allein“) ist in dieser "
            "Version noch nicht verfügbar.",
        ),
    ],
)
def test_refused_before_the_start(
    workfolder: Path, name: str, params: dict[str, Any], path: str, sentence: str
) -> None:
    from arr.runs.topologies import check_topology

    mgr, _log = fixture_manager(workfolder, name, {}, params=params)
    with pytest.raises(ConfigError) as info:
        check_topology(mgr.resolved)
    found = {i.path: i.message for i in info.value.issues}
    assert found.get(path, "").startswith(sentence), info.value
    assert not (workfolder / "runs").exists() or list((workfolder / "runs").iterdir()) == []
