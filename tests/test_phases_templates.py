"""W4b-02b A21: the test groups of `tests/fixtures/phases/` from start to end with FakeBackend
agents: `solo`, `waterfall` (rework, best attempt, `on_exhausted`), `exploration` in all three
variants of `exchange`, `review` with `discussion_rounds: 0` (`judge_memory` keep and reset).
Since W4b-03 (A20) `review` with its defaults, `roundtable` and `beispiel` run as well."""

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
    # what the packages hold (W4b-02b.1, H1): the ranker gets path and text of the three
    # explorations, the summarizer gets them as standard input, in all three variants
    [ranker] = log.of("ranker-1")
    rated = ranker.context["inputs"]
    assert len(rated) == 3 and all(i["path"] and "/output/" in i["path"] for i in rated)
    assert all(i["text"].startswith("Text von explorer-") for i in rated)
    assert sorted(ranker.expected["rate"]) == sorted(gate["ranking"])
    [summarizer] = log.of("summarizer")
    explorations = [i for i in summarizer.context["inputs"] if i["source"] == f"phase:{phase}"]
    assert [i["id"] for i in explorations] == gate["ranking"], "in the order of the ranking"
    assert all(i["path"] and i["summary"] for i in explorations)
    assert [i["source"] for i in summarizer.context["inputs"]][0] == "phase:ideen"


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

        fresh = Orchestrator.fresh_folder

        async def fresh_folder(self: Any, agent_id: str, *, stage: str, attempt: int) -> None:
            calls.append(("fresh", agent_id, stage, str(attempt)))
            await fresh(self, agent_id, stage=stage, attempt=attempt)  # the real one (W4b-05a)

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
    # the author sees the reviews of the isolated check (W4b-02b.1, H1): verdict, summary, path
    reviews = [i for i in revision.context["inputs"] if i["source"] == "phase:pruefung-allein"]
    # under their aliases only, as today's `review._alias` (W4b-02b.1 Teil 2, N-M1)
    assert sorted((i["from"], i["verdict"]) for i in reviews) == [
        ("Mitglied A", "minor"),
        ("Mitglied B", "major"),
    ]
    assert all(i["path"] and "/output/" in i["path"] and i["summary"] for i in reviews)
    assert "Mitglied A" in revision.prompt, "in the text the author reads"
    second = [v for v in log.of("judge-1") if v.attempt == 2][0]
    assert second.expected["known_objections"] == ["E1", "E2"]
    beweis = [i for i in second.context["inputs"] if i["source"] == "name:beweis"]
    assert beweis[0]["version"] == 2 and "author 1" in beweis[0]["text"]
    gates = [c for c in checkpoints(mgr, "gate") if c["stage"] == "pruefung-allein"]
    assert [g["value"] for g in gates] == ["major", "accept"]
    if memory == "reset":
        for judge_id in ("judge-1", "judge-2"):
            own = [c for c in calls if c[1] == judge_id]
            # the first check is the judge's first action: no transition (Entscheidung 58),
            # as today (`judge_memory: fresh` resets from round 2 on)
            assert own == [("reset", judge_id), ("fresh", judge_id, "pruefung-allein", "2")]
    else:
        assert calls == []


# --------------------------------------------------------------------------------------------
# W4b-03 A20: `review` with its defaults (discussion), `roundtable` and `beispiel` run now
# --------------------------------------------------------------------------------------------


async def test_w4b03_review_with_its_defaults_discusses(workfolder: Path) -> None:
    """`fassung` → `pruefung` (isolated) → `diskussion` (`chat` in #judges, one round) →
    `revision` → `pruefung` → `diskussion` (accept) → stop."""

    def judging(view: TurnView) -> list[dict[str, Any]]:
        verdict = "minor" if view.attempt == 1 else "accept"
        return hand_in(view, verdict=verdict)

    mgr, log = fixture_manager(
        workfolder, "review", {"author": answer(), "judge-1": judging, "judge-2": judging}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert entered(mgr) == [
        "fassung",
        "pruefung",
        "diskussion",
        "revision",
        "pruefung",
        "diskussion",
    ]
    seq = turns(log)
    assert seq[0] == ("author", "fassung", 1, "work")
    by_phase: dict[tuple[str | None, int], list[str]] = {}
    for agent, stage, attempt, _action in seq:
        by_phase.setdefault((stage, attempt), []).append(agent)
    assert sorted(by_phase[("pruefung", 1)]) == ["judge-1", "judge-2"]
    assert sorted(by_phase[("diskussion", 1)]) == ["judge-1", "judge-2"]
    assert by_phase[("revision", 1)] == ["author"]
    assert sorted(by_phase[("diskussion", 2)]) == ["judge-1", "judge-2"]
    gates = [c["value"] for c in checkpoints(mgr, "gate") if c["stage"] == "diskussion"]
    assert gates == ["minor", "accept"]
    ends = [e["ended_by"] for e in of_kind_entries(mgr, "discussion_end")]
    assert ends == ["all_done", "all_done"]
    talk = [v for v in log.views if v.stage == "diskussion"]
    assert all(v.context["submission"]["format"] == "review" for v in talk)
    assert all("Mitglied" in v.prompt for v in talk), "the anonymous channel shows aliases"


async def test_w4b03_roundtable_discusses_and_summarizes(workfolder: Path) -> None:
    """`diskussion` (`chat` in #runde, `rounds`, 3 rounds; the summarizer is a member but not
    in the phase) → `zusammenfassung` (the channel as input)."""

    def talks(view: TurnView) -> list[dict[str, Any]]:
        if "Runde 1 von" in view.prompt.split("## Auftrag")[-1]:
            from run_kit import room

            return [room("post_message", channel="runde", body=f"Beitrag von {view.agent}.")]
        return hand_in(view)

    policies = {"diskutant-1": talks, "diskutant-2": talks, "summarizer": answer()}
    mgr, log = fixture_manager(workfolder, "roundtable", policies)
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert entered(mgr) == ["diskussion", "zusammenfassung"]
    assert turns(log) == [
        ("diskutant-1", "diskussion", 1, "work"),
        ("diskutant-2", "diskussion", 1, "work"),
        ("diskutant-1", "diskussion", 1, "work"),
        ("diskutant-2", "diskussion", 1, "work"),
        ("summarizer", "zusammenfassung", 1, "work"),
    ]
    [end] = of_kind_entries(mgr, "discussion_end")
    assert (end["ended_by"], end["rounds"]) == ("all_done", 2)
    summary = log.of("summarizer")[0]
    channel = [i for i in summary.context["inputs"] if i["source"] == "channel:runde"]
    assert channel and any("Beitrag von diskutant-1." in m["text"] for m in channel[0]["messages"])


async def test_w4b03_beispiel_with_a_sleeper_in_the_direct_channel(workfolder: Path) -> None:
    """The example of docs/10 §4.9: in `beweis` the prover asks the strategist (on standby)
    through #rueckfrage, the strategist is woken and answers, the prover hands in."""
    from run_kit import room

    def prover(view: TurnView) -> list[dict[str, Any]]:
        if view.stage == "beweis" and "# Auftrag –" in view.prompt:
            return [
                room(
                    "post_message",
                    channel="rueckfrage",
                    body="@strategist Welcher Ansatz?",
                    kind="question",
                ),
                room("wait_for_messages", seconds=40),
                *hand_in(view),
            ]
        return hand_in(view)

    def strategist(view: TurnView) -> list[dict[str, Any]]:
        return [
            room("inbox", save_as="box"),
            room(
                "post_message",
                channel="rueckfrage",
                body="Induktion über n.",
                kind="answer",
                reply_to="${box.open_questions.0.id}",
            ),
        ]

    def judge(view: TurnView) -> list[dict[str, Any]]:
        return hand_in(view, verdict="accept")

    policies: dict[str, Any] = {
        "ideator": answer(),
        "explorer-1": answer(),
        "explorer-2": answer(),
        "prover": prover,
        "strategist": strategist,
        "judge-1": judge,
        "judge-2": judge,
        "writer": answer(),
    }
    decider = Choose(EscalationOption.CONTINUE)
    mgr, log = fixture_manager(workfolder, "beispiel", policies, decider=decider)
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert entered(mgr) == [
        "ideen",
        "exploration",
        "beweis",
        "pruefung",
        "urteil",
        "zusammenfassung",
    ]
    [wake] = [v for v in log.of("strategist") if v.context.get("standby")]
    assert wake.stage == "beweis" and "#rueckfrage" in wake.prompt
    assert [v.stage for v in log.of("strategist")] == ["beweis"]
    [end] = of_kind_entries(mgr, "discussion_end")
    assert end["channel"] == "judges"
    assert [c["value"] for c in checkpoints(mgr, "gate") if c["stage"] == "urteil"] == ["accept"]


def of_kind_entries(mgr: Any, kind: str) -> list[dict[str, Any]]:
    from phases_kit import of_kind

    return of_kind(mgr, kind)
