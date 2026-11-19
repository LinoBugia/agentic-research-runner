"""W4b-02b: isolation (A8), `over` (A9), `repeat` (A10), gates `done`/`signal` (A11) and `judge`
(A12), the user (A13), routes (A14), feedback and the valid submission (A15)."""

import asyncio
import random
import time
from pathlib import Path
from typing import Any

import pytest
from phases_kit import (
    Choose,
    Log,
    TurnView,
    answer,
    checkpoints,
    entered,
    errors,
    hand_in,
    judge_gate,
    manager,
    parallel,
    run,
    silent,
    solo,
)
from run_kit import eventually, make_settings, make_workfolder

from arr.errors import ArrError
from arr.models import GateRule, RunInput, RunStatus
from arr.permissions.inbox import ApprovalItem
from arr.permissions.types import Decision, EscalationOption
from arr.plan.check import check
from arr.plan.read import enter, follow
from arr.resolve import resolve_run, validate_model
from arr.room.store import RoomStore
from arr.runs.orchestrator import Orchestrator
from arr.runs.phases import work
from arr.runs.phases.gates import judge_mean, judge_rank, signal, verdicts
from arr.runs.phases.state import Pass, Slot

FAKE = {"provider": "claude", "model": "fake-model", "auth": "fake-auth"}
AGENTS = {"agents": [{"id": a} for a in ("a", "b", "c", "r1", "r2")]}

CONTINUE, RETRY, END = EscalationOption.CONTINUE, EscalationOption.RETRY, EscalationOption.END


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


# --------------------------------------------------------------------------------------------
# A8: isolation
# --------------------------------------------------------------------------------------------


async def test_a8_sealed_over_all_repetitions_released_before_judge(workfolder: Path) -> None:
    seen: list[dict[str, Any]] = []
    holder: dict[str, Any] = {}

    def watch(view: TurnView) -> list[dict[str, Any]]:
        mgr = holder["mgr"]
        run_dir = mgr.run_dir
        store = RoomStore(run_dir)
        others = [s for s in store.submissions() if s.agent != view.agent and s.agent in "ab"]
        engine = mgr.inbox.engine
        output = sorted(
            p.read_text("utf-8") for p in (run_dir / "output").rglob("*.md") if p.is_file()
        )
        seen.append(
            {
                "agent": view.agent,
                "action": view.action,
                "rep": view.repetition,
                "others_sealed": [s.sealed for s in others],
                "readable": [engine.profile(view.agent).can_read(run_dir / s.path) for s in others],
                "in_output": [
                    t for t in output if t.startswith("Text von a") or t.startswith("Text von b")
                ],
                "inputs": view.context.get("inputs", []),
                "channel": store.channel_state("gemeinsam").value,
            }
        )
        return hand_in(view)

    group = {
        **AGENTS,
        "channels": [
            {"name": "gemeinsam", "members": ["a", "b"]},
            {"name": "nur-a", "members": ["a"]},
        ],
        "phases": [
            {
                **parallel("x", ["a", "b"]),
                "isolated": True,
                "repeat": 2,
                "channels": ["nur-a"],
                "gate": judge_gate("r1", "rank", routes={"ranked": "next"}),
            },
            {**solo("y", "c"), "channels": ["gemeinsam"]},
        ],
    }
    mgr, log = manager(workfolder, group, {"a": watch, "b": watch, "r1": watch, "c": watch})
    holder["mgr"] = mgr
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    work_turns = [s for s in seen if s["agent"] in "ab"]
    second = [s for s in work_turns if s["rep"] == 2]
    assert second and all(s["others_sealed"] and all(s["others_sealed"]) for s in second)
    assert all(not any(s["readable"]) for s in work_turns), "sealed folders are not readable"
    assert all(s["in_output"] == [] for s in work_turns), "nothing of the pass in output/"
    assert all(s["inputs"] == [] for s in work_turns), "no input of the running pass"
    assert all(s["channel"] != "open" for s in work_turns), "the shared channel is closed"
    [rater] = [s for s in seen if s["agent"] == "r1"]
    assert rater["others_sealed"] and not any(rater["others_sealed"]), "released before judge"
    assert len(rater["inputs"]) == 2 and len(rater["in_output"]) == 4
    [after] = [s for s in seen if s["agent"] == "c"]
    assert after["channel"] == "open", "the next phase opens its channels"


async def test_a8_an_error_of_release_ends_the_run(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def release_results(self: Any, stage: str) -> Any:
        raise ArrError("Die versiegelte Abgabe #1 fehlt. Das ist ein Fehler in arr.")

    monkeypatch.setattr(Orchestrator, "release_results", release_results)
    group = {**AGENTS, "phases": [{**parallel("x", ["a", "b"]), "isolated": True}, solo("y", "c")]}
    mgr, log = manager(workfolder, group, {"a": answer(), "b": answer(), "c": answer()})
    started = time.monotonic()
    assert await run(mgr) is RunStatus.FAILED
    assert errors(mgr) == ["Die versiegelte Abgabe #1 fehlt. Das ist ein Fehler in arr."]
    assert log.of("c") == [] and time.monotonic() - started < 20


async def test_a8_a_stop_in_an_isolated_phase_releases_it(workfolder: Path) -> None:
    group = {**AGENTS, "phases": [{**parallel("x", ["a", "b"]), "isolated": True}]}
    holder: dict[str, Any] = {}

    def slow(view: TurnView) -> list[dict[str, Any]]:
        return [{"do": "delay", "seconds": 30}]

    def quick(view: TurnView) -> list[dict[str, Any]]:
        holder["mgr"].stop()
        return hand_in(view)

    mgr, _log = manager(workfolder, group, {"a": quick, "b": slow})
    holder["mgr"] = mgr
    assert await run(mgr) is RunStatus.STOPPED
    store = RoomStore(mgr.run_dir)
    assert store.sealed_stages() == {}, "nothing stays sealed after the stop"
    assert all(not s.sealed for s in store.submissions())


# --------------------------------------------------------------------------------------------
# A9: over
# --------------------------------------------------------------------------------------------


async def test_a9_over_a_list_round_robin_in_order(workfolder: Path) -> None:
    group = {
        **AGENTS,
        "phases": [
            solo("ideen", "c", "list", name="ideen"),
            {**parallel("x", ["a", "b"]), "over": "ideen"},
        ],
    }
    mgr, log = manager(workfolder, group, {"a": answer(), "b": answer(), "c": answer(items=5)})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    work_turns = [(t[0], t[3]) for t in log.turns if t[1] == "x"]
    assert sorted(work_turns) == [
        ("a", "work-1"),
        ("a", "work-3"),
        ("a", "work-5"),
        ("b", "work-2"),
        ("b", "work-4"),
    ]
    assert [t[3] for t in log.turns if t[0] == "a" and t[1] == "x"] == [
        "work-1",
        "work-3",
        "work-5",
    ]
    entry = log.of("b")[0].context["entry"]
    assert entry == {"number": 2, "title": "Idee 2", "text": "Ansatz 2"}
    [gate] = checkpoints(mgr, "gate")[1:]
    assert len(gate["results"]) == 5


async def test_a9_a_missing_list_asks_the_user(workfolder: Path) -> None:
    group = {
        **AGENTS,
        "phases": [
            {
                **solo("ideen", "c", "list", name="ideen"),
                "gate": {
                    "type": "signal",
                    "field": "status",
                    "routes": {"done": "next", "not_solved": "next"},
                },
            },
            {**parallel("x", ["a", "b"]), "over": "ideen"},
        ],
    }
    decider = Choose(END)
    mgr, log = manager(
        workfolder, group, {"a": answer(), "b": answer(), "c": silent}, decider=decider
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    [item] = decider.asked
    assert "Die Liste „ideen“ für „over“ gibt es noch nicht." in str(item.summary)
    assert [t[0] for t in log.turns if t[1] == "x"] == []


# --------------------------------------------------------------------------------------------
# A10: repeat
# --------------------------------------------------------------------------------------------


async def test_a10_repeat_in_lockstep(workfolder: Path) -> None:
    group = {**AGENTS, "phases": [{**parallel("x", ["a", "b"]), "repeat": 3}]}

    def slow_a(view: TurnView) -> list[dict[str, Any]]:
        return [{"do": "delay", "seconds": 0.2}, *hand_in(view)]

    mgr, log = manager(workfolder, group, {"a": slow_a, "b": answer()})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    reps = [t[4] for t in log.turns]
    assert reps == sorted(reps) == [1, 1, 2, 2, 3, 3], "b waits for a before the next one"
    [gate] = checkpoints(mgr, "gate")
    store = RoomStore(mgr.run_dir)
    newest = {s.agent: s.id for s in store.submissions()}
    assert sorted(gate["results"]) == sorted(newest.values()), "the last state counts"


async def test_a10_repeat_with_over_in_blocks(workfolder: Path) -> None:
    group = {
        **AGENTS,
        "seed": {"liste": {"param": "ideas"}},
        "phases": [{**parallel("x", ["a", "b"]), "over": "liste", "repeat": 2}],
    }
    mgr, log = manager(
        workfolder, group, {"a": answer(), "b": answer()}, params={"ideas": ["1", "2", "3"]}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    order = [(t[5], t[4]) for t in log.turns]
    blocks = [order[:2], order[2:4], order[4:]]
    assert sorted(blocks[0]) == [(1, 1), (2, 1)]
    assert sorted(blocks[1]) == [(1, 2), (2, 2)]
    assert blocks[2] == [(3, 1), (3, 2)]


# --------------------------------------------------------------------------------------------
# A11: done and signal
# --------------------------------------------------------------------------------------------

STRICT = list(reversed(["accept", "minor", "major", "reject"]))


@pytest.mark.parametrize(
    ("field", "rule", "values", "result", "tie"),
    [
        ("status", GateRule.ALL, ["done", "done"], "done", False),
        ("status", GateRule.ALL, ["done", "not_solved"], "not_solved", False),
        ("status", GateRule.ANY, ["not_solved", "done"], "done", False),
        ("status", GateRule.ANY, ["not_solved", "not_solved"], "not_solved", False),
        ("status", GateRule.MAJORITY, ["done", "done", "not_solved"], "done", False),
        ("status", GateRule.MAJORITY, ["done", "not_solved"], None, True),
        ("status", None, ["done"], "done", False),
        ("verdict", GateRule.MAJORITY, ["accept", "major"], "major", False),
        ("verdict", GateRule.MAJORITY, ["minor", "minor", "reject"], "minor", False),
        ("verdict", GateRule.MAJORITY, [None, "accept"], "accept", False),
        ("verdict", GateRule.UNANIMOUS, ["accept", "accept"], "accept", False),
        ("verdict", GateRule.UNANIMOUS, ["accept", None], "minor", False),
        ("verdict", GateRule.UNANIMOUS, ["minor", "major"], "major", False),
        ("verdict", None, [None], None, False),
        ("choice", GateRule.MAJORITY, ["ja", "ja", "nein"], "ja", False),
        ("choice", GateRule.MAJORITY, ["ja", "nein"], None, True),
        ("choice", GateRule.UNANIMOUS, ["ja", "ja"], "ja", False),
        ("choice", GateRule.UNANIMOUS, ["ja", "nein"], None, True),
        ("choice", GateRule.UNANIMOUS, ["ja", None], None, True),
        ("choice", GateRule.MAJORITY, [None, None], None, False),
        ("choice", None, ["nein"], "nein", False),
    ],
)
def test_a11_signal_rows(
    field: str, rule: GateRule | None, values: list[Any], result: str | None, tie: bool
) -> None:
    outcome = signal(field, rule, values)
    assert (outcome.result, outcome.tie) == (result, tie)
    if result is None and not tie:
        assert outcome.reason


def test_a11_verdicts_like_review_aggregate() -> None:
    from arr.runs.topologies.review import aggregate

    for a in [None, *STRICT]:
        for b in [None, *STRICT]:
            for rule in ("majority", "unanimous"):
                assert verdicts([a, b], rule) == aggregate([a, b], rule), (a, b, rule)


DECIDE = {"format": "decision", "choices": ["ja", "nein"]}


def decision_phase(name: str, agents: list[str], **gate: Any) -> dict[str, Any]:
    phase = parallel(name, agents, "decision", choices=["ja", "nein"])
    phase["gate"] = {"type": "signal", "field": "choice", **gate}
    return phase


async def test_a11_tie_takes_on_tie_and_missing_gives_no_value(workfolder: Path) -> None:
    group = {
        **AGENTS,
        "phases": [
            decision_phase(
                "x",
                ["a", "b"],
                rule="majority",
                routes={"ja": "stop", "nein": "stop"},
                on_tie="next",
            ),
            decision_phase(
                "y",
                ["a", "b"],
                rule="majority",
                routes={"ja": "next", "nein": "next"},
                on_tie="next",
            ),
            solo("z", "c"),
        ],
    }
    decider = Choose(CONTINUE)

    def a_policy(view: TurnView) -> list[dict[str, Any]]:
        return silent(view) if view.stage == "y" else hand_in(view, choice="ja")

    mgr, log = manager(
        workfolder,
        group,
        {"a": a_policy, "b": answer(choice="nein", skip=lambda v: v.stage == "y"), "c": answer()},
        decider=decider,
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    gates = checkpoints(mgr, "gate")
    assert gates[0]["tie"] is True and checkpoints(mgr, "route")[0]["route"] == "next"
    assert gates[1]["value"] is None and gates[1]["reason"] == "no_result"
    assert "Keine Abgabe trägt einen Wert für „choice“." in str(decider.asked[0].summary)
    assert entered(mgr) == ["x", "y", "z"]


async def test_a11_done_waits_for_every_action(workfolder: Path) -> None:
    group = {**AGENTS, "phases": [parallel("x", ["a", "b"], turns=1), solo("y", "c")]}
    mgr, log = manager(workfolder, group, {"a": answer(), "b": silent, "c": answer()})
    assert await run(mgr) is RunStatus.DONE
    [gate, _] = checkpoints(mgr, "gate")
    assert gate["value"] == "done" and gate["missing"] == ["b/work"]
    assert len(log.of("b")) == 2 and log.views.index(log.of("c")[0]) == len(log.views) - 1


# --------------------------------------------------------------------------------------------
# A12: judge
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("scores", "result"), [((6, 8), "pass"), ((6, 7.9), "fail")])
async def test_a12_mean_against_pass_score(workfolder: Path, scores: tuple, result: str) -> None:
    group = {
        **AGENTS,
        "phases": [
            {
                **solo("x", "a"),
                "gate": judge_gate(
                    ["r1", "r2"],
                    "mean",
                    pass_score=7,
                    criteria=["streng"],
                    routes={"pass": "next", "fail": "stop"},
                ),
            },
            solo("y", "c"),
        ],
    }
    mgr, log = manager(
        workfolder,
        group,
        {
            "a": answer(),
            "c": answer(),
            "r1": answer(score=scores[0]),
            "r2": answer(score=scores[1]),
        },
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    [r1] = log.of("r1")
    assert r1.expected["action"] == "gate" and r1.expected["rate"] == [1]
    assert r1.expected["criteria"] == ["streng"], "the criteria of the gate"
    assert r1.context["inputs"][0]["id"] == 1 and "text" in r1.context["inputs"][0]
    gate = checkpoints(mgr, "gate")[0]
    assert gate["value"] == result and gate["score"] == pytest.approx(sum(scores) / 2)
    assert gate["decision"] == ("passed" if result == "pass" else "failed")
    assert bool(log.of("c")) is (result == "pass")


async def test_a12_rank_orders_the_submissions(workfolder: Path) -> None:
    points = {"a": 5, "b": 9, "c": 7}

    def by_author(view: TurnView, ref: int) -> float:
        store = RoomStore(Path(view.work_dir).parents[2])
        return points[store.submission(ref).agent]

    group = {
        **AGENTS,
        "phases": [
            {
                **parallel("x", ["a", "b", "c"]),
                "gate": judge_gate(["r1"], "rank", routes={"ranked": "next"}),
            }
        ],
    }
    mgr, log = manager(
        workfolder,
        group,
        {"a": answer(), "b": answer(), "c": answer(), "r1": answer(score=by_author)},
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    gate = checkpoints(mgr, "gate")[0]
    store = RoomStore(mgr.run_dir)
    ranked = [store.submission(i).agent for i in gate["ranking"]]
    assert ranked == ["b", "c", "a"] and gate["value"] == "ranked"
    assert checkpoints(mgr, "stage_end")[0]["valid"] == gate["ranking"]
    assert len(log.of("r1")) == 1, "one rating action for all submissions"


@pytest.mark.parametrize(
    ("rule", "given", "result"),
    [
        ("majority", ("minor", "major"), "major"),
        ("unanimous", ("accept", "accept"), "accept"),
        ("unanimous", ("accept", "minor"), "minor"),
    ],
)
async def test_a12_majority_and_unanimous_over_reviews(
    workfolder: Path, rule: str, given: tuple, result: str
) -> None:
    group = {
        **AGENTS,
        "phases": [
            {
                **solo("x", "a"),
                "gate": judge_gate(
                    ["r1", "r2"],
                    rule,
                    fmt="review",
                    routes={v: "next" for v in ("accept", "minor", "major", "reject")},
                ),
            }
        ],
    }
    mgr, log = manager(
        workfolder,
        group,
        {"a": answer(), "r1": answer(verdict=given[0]), "r2": answer(verdict=given[1])},
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert checkpoints(mgr, "gate")[0]["value"] == result
    assert "rate" not in log.of("r1")[0].expected


async def test_a12_no_rating_asks_the_user(workfolder: Path) -> None:
    group = {
        **AGENTS,
        "phases": [
            {
                **solo("x", "a"),
                "gate": judge_gate(
                    "r1", "mean", pass_score=7, routes={"pass": "next", "fail": "stop"}
                ),
            }
        ],
    }
    decider = Choose(END)
    mgr, log = manager(workfolder, group, {"a": answer(), "r1": silent}, decider=decider)
    assert await run(mgr) is RunStatus.DONE
    assert len(log.of("r1")) == 2, "one turn and one follow-up question"
    gate = checkpoints(mgr, "gate")[0]
    assert gate["reason"] == "no_rating" and gate["decision"] == "escalated"
    assert "Kein Rater hat bewertet." in str(decider.asked[0].summary)


async def test_a12_too_many_submissions_is_a_clear_error() -> None:
    from arr.room.types import Submission

    subs = [
        Submission(
            id=i,
            ts=0,
            agent=f"x{i}",
            shown_as=f"x{i}",
            format="text",
            status="done",
            summary="s",
            path="p",
        )
        for i in range(1, 102)
    ]
    pass_ = Pass(
        "p",
        1,
        "normal",
        slots=[
            Slot(s.agent, "work", None, "", found=s)
            for s in subs  # type: ignore[arg-type]
        ],
    )

    class Ctx:
        stop_requested = False

    class Runner:
        ctx = Ctx()

    data = {
        "project": "demo",
        "task": "x",
        "topology": {"kind": "phases"},
        "defaults": FAKE,
        **AGENTS,
        "phases": [{**solo("p", "a"), "gate": judge_gate("r1", "rank", routes={"ranked": "next"})}],
    }
    phase = resolve_run(validate_model(RunInput, data), None, make_settings()).phases[0]
    with pytest.raises(ArrError, match="soll 101 Abgaben bewerten; eine Bewertungs-Action"):
        await work.rate(Runner(), phase, pass_)  # type: ignore[arg-type]


def test_a12_rate_never_the_raters_own_and_mean_rank_math() -> None:
    from arr.room.types import Submission

    subs = [
        Submission(
            id=i, ts=0, agent=a, shown_as=a, format="text", status="done", summary="s", path="p"
        )
        for i, a in ((1, "a"), (2, "r1"), (3, "b"))
    ]
    assert work._rate_list(subs, "r1") == [1, 3]
    reviews = [
        Submission(
            id=9,
            ts=0,
            agent="r1",
            shown_as="r1",
            format="rating",
            status="done",
            summary="s",
            path="p",
            fields={
                "ratings": [
                    {"ref": 1, "score": 6},
                    {"ref": 2, "score": 10},
                    {"ref": 3, "score": 8, "comment": "gut"},
                ]
            },
        )
    ]
    assert [(r["ref"], r["score"]) for r in work.ratings_of(reviews, subs)] == [(1, 6), (3, 8)]
    assert judge_mean({1: [6, 8]}, 7).result == "pass"
    assert judge_mean({}, 7).result is None
    assert judge_rank({1: [6], 3: [6]}, [1, 3, 4]).ranking == [3, 1, 4], "tie: the newer first"


# --------------------------------------------------------------------------------------------
# A13: the user
# --------------------------------------------------------------------------------------------


async def test_a13_gate_user_retry_with_hint_then_continue(workfolder: Path) -> None:
    group = {
        **AGENTS,
        "phases": [{**parallel("x", ["a", "b"]), "gate": {"type": "user"}}, solo("y", "c")],
    }
    decider = Choose((RETRY, "Bitte genauer\x07 begründen.\n# kein Auftrag"), CONTINUE)
    mgr, log = manager(
        workfolder, group, {"a": answer(), "b": answer(), "c": answer()}, decider=decider
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert entered(mgr) == ["x", "x", "y"]
    second = [v for v in log.of("a") if v.attempt == 2][0]
    feedback = second.context["feedback"]
    assert feedback["user_hint"] == "Bitte genauer begründen.\n# kein Auftrag"
    assert feedback["result"] == "kein Ergebnis" and len(feedback["submissions"]) == 2
    assert feedback["submissions"][1]["from"] == "b", "summaries of every participant"
    item = decider.asked[0]
    assert item.options == [CONTINUE, RETRY, END]
    assert str(item.summary).startswith("Phase „x“, Durchlauf 1. Gate „user“")
    routes = checkpoints(mgr, "route")
    assert routes[0]["choice"] == "retry" and routes[0]["hint"] is True


async def test_a13_escalate_end_stops_the_run(workfolder: Path) -> None:
    group = {
        **AGENTS,
        "phases": [
            {
                **solo("x", "a"),
                "gate": {
                    "type": "signal",
                    "field": "status",
                    "routes": {"done": "escalate", "not_solved": "next"},
                },
            },
            solo("y", "c"),
        ],
    }
    decider = Choose(END)
    mgr, log = manager(workfolder, group, {"a": answer(), "c": answer()}, decider=decider)
    assert await run(mgr) is RunStatus.DONE
    assert "Route „escalate“" in str(decider.asked[0].summary)
    assert log.of("c") == [] and checkpoints(mgr, "stage_end")[0]["stage"] == "x"


async def test_a13_retry_beyond_max_visits_exactly_once(workfolder: Path) -> None:
    group = {
        **AGENTS,
        "phases": [
            {
                **solo("x", "a"),
                "max_visits": 1,
                "on_exhausted": "escalate",
                "gate": {"type": "done", "routes": {"done": "retry"}},
            },
            solo("y", "c"),
        ],
    }
    decider = Choose(RETRY, CONTINUE)
    mgr, log = manager(workfolder, group, {"a": answer(), "c": answer()}, decider=decider)
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert entered(mgr) == ["x", "x", "y"], "one more pass beyond max_visits, then on"
    assert [c["how"] for c in checkpoints(mgr, "pass_start")] == ["normal", "user_retry", "normal"]
    assert all("aufgebraucht" in str(i.summary) for i in decider.asked)
    assert len(decider.asked) == 2


class Never:
    """A decider that never answers (the user is away)."""

    def __init__(self) -> None:
        self.asked: list[ApprovalItem] = []

    async def decide(self, item: ApprovalItem) -> Decision:
        self.asked.append(item)
        await asyncio.Event().wait()
        raise AssertionError


async def test_a13_a_stop_during_the_question_ends_the_runner(workfolder: Path) -> None:
    group = {**AGENTS, "phases": [{**solo("x", "a"), "gate": {"type": "user"}}, solo("y", "c")]}
    decider = Never()
    mgr, log = manager(workfolder, group, {"a": answer(), "c": answer()}, decider=decider)
    task = asyncio.create_task(run(mgr))
    await eventually(lambda: bool(decider.asked), within=20)
    started = time.monotonic()
    mgr.stop()
    assert await task is RunStatus.STOPPED
    assert time.monotonic() - started < 5 and log.of("c") == []


# --------------------------------------------------------------------------------------------
# A14: routes only through read.follow and read.enter
# --------------------------------------------------------------------------------------------

VALUES = ["ja", "nein", "vielleicht"]


def random_group(rng: random.Random, n: int) -> tuple[dict[str, Any], dict[str, list[str]]]:
    names = [f"p{i}" for i in range(n)]
    phases = []
    plans = {}
    for name in names:
        choices = rng.sample(VALUES, 2)
        routes: dict[str, Any] = {}
        for value in choices:
            kind = rng.choice(["next", "retry", "stop", "goto", "goto"])
            routes[value] = {"goto": rng.choice(names)} if kind == "goto" else kind
        phase = solo(name, "a", "decision", choices=choices)
        phase["gate"] = {"type": "signal", "field": "choice", "routes": routes}
        if rng.random() < 0.8:
            phase["max_visits"] = rng.choice([0, 1, 1, 2, 2, 3])
            kind = rng.choice(["next", "stop", "goto"])
            phase["on_exhausted"] = {"goto": rng.choice(names)} if kind == "goto" else kind
        phases.append(phase)
        plans[name] = [rng.choice(choices) for _ in range(4)]
    return {"agents": [{"id": "a"}], "phases": phases}, plans


def expected_entries(resolved: Any, plans: dict[str, list[str]]) -> list[str]:
    visits: dict[str, int] = {}
    out: list[str] = []
    entry = enter(resolved, resolved.phases[0].name, visits)
    while entry.phase is not None:
        name = entry.phase
        visits[name] = visits.get(name, 0) + 1
        out.append(name)
        value = plans[name][(visits[name] - 1) % len(plans[name])]
        route = resolved.phase(name).gate.routes[value]
        entry = enter(resolved, follow(resolved, name, route), visits)
        assert len(out) < 100
    return out


async def test_a14_generated_groups_follow_read_enter(tmp_path: Path) -> None:
    rng = random.Random(4711)
    tried = done = 0
    kinds: set[str] = set()
    while done < 30:
        tried += 1
        assert tried < 2000
        group, plans = random_group(rng, rng.randint(2, 4))
        data = {
            "project": "demo",
            "task": "x",
            "topology": {"kind": "phases"},
            "defaults": FAKE,
            **group,
        }
        try:
            resolved = resolve_run(validate_model(RunInput, data), None, make_settings())
        except ArrError:
            continue
        if check(resolved).errors:
            continue
        expected = expected_entries(resolved, plans)
        workfolder = make_workfolder(tmp_path / f"g{tried}")

        def policy(view: TurnView, plans: dict[str, list[str]] = plans) -> list[dict[str, Any]]:
            seq = plans[view.stage or ""]
            return hand_in(view, choice=seq[(view.attempt - 1) % len(seq)])

        mgr, _log = manager(workfolder, group, {"a": policy}, log=Log())
        assert await run(mgr) is RunStatus.DONE, errors(mgr)
        assert entered(mgr) == expected, (group, plans)
        for route in checkpoints(mgr, "route"):
            if route["exhausted"]:
                kinds.add("exhausted")
            kinds.add(str(route["route"]).split(":")[0])
        if any(p.max_visits == 0 for p in resolved.phases):
            kinds.add("max_visits_0")
        done += 1
    assert {"exhausted", "goto", "retry", "stop", "next", "max_visits_0"} <= kinds, kinds


async def test_a14_single_cases(workfolder: Path) -> None:
    group = {
        **AGENTS,
        "phases": [
            {**solo("nie", "a"), "max_visits": 0, "on_exhausted": "next"},
            {
                **solo("x", "a"),
                "max_visits": 1,
                "on_exhausted": {"goto": "z"},
                "gate": {"type": "done", "routes": {"done": {"goto": "y"}}},
            },
            {
                **solo("y", "b"),
                "max_visits": 2,
                "on_exhausted": "next",
                "gate": {"type": "done", "routes": {"done": {"goto": "x"}}},
            },
            solo("z", "c"),
        ],
    }
    mgr, _log = manager(workfolder, group, {a: answer() for a in "abc"})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    # nie: never entered; x → y → x exhausted → its on_exhausted goto z
    assert entered(mgr) == ["x", "y", "z"]
    routes = checkpoints(mgr, "route")
    assert routes[1]["exhausted"] == ["x"] and routes[1]["entered"] == "z"
    assert routes[-1]["entered"] is None, "the end after the last phase"


# --------------------------------------------------------------------------------------------
# A15: feedback and the valid submission
# --------------------------------------------------------------------------------------------


async def test_a15_feedback_of_a_judge_retry_and_the_best_attempt(workfolder: Path) -> None:
    scores = iter([8, 6, 5, 5])
    group = {
        **AGENTS,
        "phases": [
            {
                **solo("x", "a", "artifact", name="beweis"),
                "max_visits": 2,
                "on_exhausted": "next",
                "gate": judge_gate(
                    "r1", "mean", pass_score=9, routes={"pass": "next", "fail": "retry"}
                ),
            },
            {**solo("y", "c")},
        ],
    }
    mgr, log = manager(
        workfolder,
        group,
        {
            "a": answer(summary=lambda v: f"Fassung {v.attempt}"),
            "c": answer(),
            "r1": answer(score=lambda v, ref: next(scores), comment="zu knapp"),
        },
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    first, second = log.of("a")
    assert "feedback" not in first.context
    feedback = second.context["feedback"]
    assert feedback["result"] == "fail" and feedback["score"] == 8
    assert feedback["submissions"][0]["summary"] == "Fassung 1"
    assert feedback["ratings"] == [{"rater": "r1", "ref": 1, "score": 8.0, "comment": "zu knapp"}]
    # best attempt: 8 points (#1) beat 6 (#3)
    [end] = [c for c in checkpoints(mgr, "stage_end") if c["stage"] == "x"]
    assert end["accepted"] == 1 and end["outcome"] == "best_accepted" and end["score"] == 8
    [item] = log.of("c")[0].context["inputs"]
    assert item["id"] == 1 and item["score"] == 8


async def test_a15_on_a_tie_the_newer_and_without_scores_the_newest(workfolder: Path) -> None:
    group = {
        **AGENTS,
        "phases": [
            {
                **solo("x", "a"),
                "max_visits": 2,
                "on_exhausted": "next",
                "gate": judge_gate(
                    "r1", "mean", pass_score=9, routes={"pass": "next", "fail": "retry"}
                ),
            },
            {
                **solo("y", "b"),
                "max_visits": 2,
                "on_exhausted": "next",
                "gate": {"type": "done", "routes": {"done": "retry"}},
            },
            solo("z", "c"),
        ],
    }
    mgr, log = manager(
        workfolder, group, {"a": answer(), "b": answer(), "c": answer(), "r1": answer(score=6)}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    ends = {c["stage"]: c for c in checkpoints(mgr, "stage_end")}
    assert ends["x"]["accepted"] == 3, "6 and 6: the newer"
    second_y = [v for v in log.of("b") if v.attempt == 2][0]
    assert second_y.context["feedback"]["result"] == "done"
    store = RoomStore(mgr.run_dir)
    assert ends["y"]["accepted"] == store.submissions(agent="b")[-1].id, "the newest"
    inputs = log.of("c")[0].context["inputs"]
    assert [i["id"] for i in inputs] == [3, store.submissions(agent="b")[-1].id]


async def test_a15_review_objections_in_the_feedback(workfolder: Path) -> None:
    group = {
        **AGENTS,
        "phases": [
            {
                **parallel("x", ["a", "b"], "review"),
                "max_visits": 2,
                "on_exhausted": "next",
                "gate": {
                    "type": "signal",
                    "field": "verdict",
                    "rule": "majority",
                    "routes": {
                        "accept": "next",
                        "minor": "retry",
                        "major": "retry",
                        "reject": "stop",
                    },
                },
            }
        ],
    }
    mgr, log = manager(
        workfolder,
        group,
        {
            "a": answer(verdict="minor", objections=[{"text": "Lücke in (3)"}]),
            "b": answer(verdict="accept"),
        },
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    second = [v for v in log.of("b") if v.attempt == 2][0]
    reviews = second.context["feedback"]["reviews"]
    assert reviews[0]["verdict"] == "minor" and reviews[0]["objections"] == [
        {"text": "Lücke in (3)"}
    ]
    assert second.context["objections"] == [{"id": "E1", "status": "offen", "text": "Lücke in (3)"}]
