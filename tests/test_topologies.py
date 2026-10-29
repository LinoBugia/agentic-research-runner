"""Topologies `solo`, `roundtable`, `waterfall` and `check_topology` (W2-04 A1-A5).

FakeBackend agents with the real room, run through the real RunManager; no model, no network.
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from run_kit import (
    agent,
    channel,
    done,
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

from arr.backends.base import TurnOutcome
from arr.errors import ArrError, ConfigError
from arr.models import ResolvedRun, RunStatus
from arr.permissions.inbox import ApprovalItem
from arr.permissions.types import DecidedBy, Decision, DecisionAction, EscalationOption
from arr.resolve import resolve_run
from arr.runs.context import RunContext, TurnJob
from arr.runs.manager import RunManager
from arr.runs.topologies import check_topology, topology_for

TASK = "Zeige, dass Lemma A für alle n ≡ 1 mod 4 gilt."


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


@dataclass
class Call:
    agent: str
    prompt: str
    stage: str | None
    context: dict[str, Any]


@dataclass
class Recorder:
    """Records every `turn` (also those of `turns` and `discuss`) and every `turns` batch."""

    calls: list[Call] = field(default_factory=list)
    batches: list[tuple[str | None, list[str]]] = field(default_factory=list)

    def wrap(self, kind: str) -> Any:
        topology = topology_for(kind)

        async def run(ctx: RunContext) -> None:
            turn, many = ctx.turn, ctx.turns

            async def record_turn(
                agent_id: str, prompt: str, *, stage: str | None = None, context: Any = None
            ) -> TurnOutcome:
                self.calls.append(Call(agent_id, prompt, stage, dict(context or {})))
                return await turn(agent_id, prompt, stage=stage, context=context)

            async def record_turns(jobs: list[TurnJob], *, stage: str | None = None) -> Any:
                self.batches.append((stage, [j.agent_id for j in jobs]))
                return await many(jobs, stage=stage)

            ctx.turn = record_turn  # type: ignore[method-assign]
            ctx.turns = record_turns  # type: ignore[method-assign]
            await topology(ctx)

        return run

    def of(self, agent_id: str, stage: str | None = None) -> list[Call]:
        return [
            c for c in self.calls if c.agent == agent_id and (stage is None or c.stage == stage)
        ]


class Choose:
    """Decider that answers escalations with the given options, in order."""

    def __init__(self, *options: EscalationOption) -> None:
        self.options = list(options)
        self.asked: list[ApprovalItem] = []

    async def decide(self, item: ApprovalItem) -> Decision:
        self.asked.append(item)
        return Decision(
            action=DecisionAction.OPTION, option=self.options.pop(0), decided_by=DecidedBy.USER
        )


def setup(
    workfolder: Path,
    kind: str,
    scripts: dict[str, Any],
    *,
    params: dict[str, Any] | None = None,
    recorder: Recorder | None = None,
    agents: list[dict[str, Any]] | None = None,
    groupchats: list[dict[str, Any]] | None = None,
    task: str = TASK,
    **run_extra: Any,
) -> RunManager:
    """A RunManager running the topology `kind` (checked with `check_topology` first)."""
    recorder = recorder or Recorder()
    decider = run_extra.pop("decider", None)
    manager = make_manager(
        workfolder,
        recorder.wrap(kind),
        scripts,
        agents=agents,
        groupchats=groupchats,
        run_extra={"topology": {"kind": kind, "params": params or {}}, "task": task, **run_extra},
        decider=decider,
    )
    check_topology(manager.resolved)
    return manager


async def run(manager: RunManager) -> RunStatus:
    return await asyncio.wait_for(manager.run(), 30)


def stage(*turn_steps: list[dict[str, Any]], repeat_last: bool = False) -> dict[str, Any]:
    """Turns of one stage for `by_stage`."""
    return {"turns": [{"steps": steps} for steps in turn_steps], "repeat_last": repeat_last}


def staged(**stages: dict[str, Any]) -> dict[str, Any]:
    return {"turns": [], "by_stage": stages}


def submit(title: str = "Ergebnis", kind: str = "note", body: str = "Inhalt") -> dict[str, Any]:
    return room("submit_result", kind=kind, title=title, body=body)


def rate(score: float, comment: str = "passt", **criteria: float) -> list[dict[str, Any]]:
    """Rate the result named in the assignment (`context.result_id`)."""
    return [
        room("get_assignment", save_as="job"),
        room(
            "submit_rating",
            result_id="${job.context.result_id}",
            score=score,
            comment=comment,
            criteria=criteria,
        ),
    ]


def checkpoints(manager: RunManager, name: str | None = None) -> list[dict[str, Any]]:
    entries = [e["data"] for e in log_entries(manager.run_dir, "checkpoint")]
    return [e for e in entries if name is None or e["stage"] == name]


def attempts(manager: RunManager, name: str) -> list[dict[str, Any]]:
    return [c for c in checkpoints(manager, name) if "decision" in c]


def stage_end(manager: RunManager, name: str) -> dict[str, Any]:
    [end] = [c for c in checkpoints(manager, name) if c.get("phase") == "stage_end"]
    return end


def resolved_run(kind: str, agents: list[dict[str, Any]], **extra: Any) -> ResolvedRun:
    params = extra.pop("params", {})
    run_input = make_run_input(agents, extra.pop("groupchats", None), **extra)
    run_input.topology = run_input.topology.model_copy(update={"kind": kind, "params": params})  # type: ignore[union-attr]
    return resolve_run(run_input, None, make_settings())


# --------------------------------------------------------------------------------------------
# Register, task
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["exploration", "review"])
def test_exploration_and_review_are_not_available_yet(kind: str) -> None:
    with pytest.raises(ArrError, match=f"Die Topologie „{kind}“ ist noch nicht verfügbar"):
        topology_for(kind)
    with pytest.raises(ArrError, match="noch nicht verfügbar. Verfügbar sind: solo, roundtable"):
        check_topology(resolved_run(kind, [agent("a")]))


def test_unknown_topology_kind() -> None:
    with pytest.raises(ArrError, match="Die Topologie „chaos“ gibt es nicht"):
        topology_for("chaos")


async def test_empty_task_fails_before_the_first_turn(workfolder: Path) -> None:
    recorder = Recorder()
    manager = setup(workfolder, "solo", {"a": turns()}, recorder=recorder, task="   \n")
    assert await run(manager) is RunStatus.FAILED
    assert recorder.calls == []
    [error] = log_entries(manager.run_dir, "error")
    assert error["data"]["message"].startswith("Der Run hat keinen Auftrag: task.md ist leer.")


# --------------------------------------------------------------------------------------------
# A1 solo
# --------------------------------------------------------------------------------------------


async def test_solo_asks_again_until_a_result_comes(workfolder: Path) -> None:
    recorder = Recorder()
    script = turns([say("Ich denke nach.")], [say("Fast fertig.")], [submit("Beweis", "proof")])
    manager = setup(workfolder, "solo", {"a": script}, recorder=recorder)
    assert await run(manager) is RunStatus.DONE
    first, second, third = recorder.calls
    assert first.prompt.startswith(f"## Aufgabe\n\n{TASK}\n\nGib dein Ergebnis")
    assert first.context == {"task": TASK}
    assert "noch kein Ergebnis" in second.prompt and "(Nachfrage 1 von 2)" in second.prompt
    assert "(Nachfrage 2 von 2)" in third.prompt
    [result] = manager.room.results()
    assert (result.agent, result.kind, result.title) == ("a", "proof", "Beweis")
    assert checkpoints(manager, "arbeit") == [
        {"stage": "arbeit", "agent": "a", "turns": 3, "results": [1], "decision": "passed"}
    ]
    assert manager.room.assignment("a").context["task"] == TASK


async def test_solo_gives_up_after_max_turns(workfolder: Path) -> None:
    recorder = Recorder()
    script = turns([say("…")], repeat_last=True)
    manager = setup(workfolder, "solo", {"a": script}, recorder=recorder, params={"max_turns": 2})
    assert await run(manager) is RunStatus.DONE
    assert len(recorder.calls) == 2
    [end] = checkpoints(manager, "arbeit")
    assert (end["decision"], end["reason"], end["turns"]) == ("failed", "no_result", 2)


async def test_solo_error_turn_counts_as_no_result(workfolder: Path) -> None:
    recorder = Recorder()
    failing = [submit("halb"), {"do": "end", "status": "error", "message": "kaputt"}]
    manager = setup(workfolder, "solo", {"a": turns(failing, [submit("ganz")])}, recorder=recorder)
    assert await run(manager) is RunStatus.DONE
    assert len(recorder.calls) == 2
    assert checkpoints(manager, "arbeit")[0]["results"] == [2]


async def test_solo_without_require_result_has_one_turn(workfolder: Path) -> None:
    recorder = Recorder()
    manager = setup(
        workfolder,
        "solo",
        {"a": turns([say("nur eine Antwort")])},
        recorder=recorder,
        params={"require_result": False},
    )
    assert await run(manager) is RunStatus.DONE
    assert len(recorder.calls) == 1
    assert checkpoints(manager, "arbeit")[0]["decision"] == "passed"


async def test_solo_names_its_agent_and_returns_on_stop(workfolder: Path) -> None:
    recorder = Recorder()
    scripts = {"a": turns(), "b": turns([wait(30)], repeat_last=True)}
    manager = setup(workfolder, "solo", scripts, recorder=recorder, params={"agent": "b"})
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: manager.orchestrator is not None and bool(manager.orchestrator._active)
    )
    manager.stop()
    assert await asyncio.wait_for(running, 10) is RunStatus.STOPPED
    assert [c.agent for c in recorder.calls] == ["b"]
    assert checkpoints(manager, "arbeit") == [], "no follow-up after the stop"


# --------------------------------------------------------------------------------------------
# A2 roundtable
# --------------------------------------------------------------------------------------------


async def test_roundtable_rounds_then_summarizer(workfolder: Path) -> None:
    recorder = Recorder()
    summary = submit("Fazit", "summary", "Lemma A gilt, Randfall n = 1 offen.")
    scripts = {
        "a": {
            **turns([post("main", "a1")], [post("main", "a2")]),
            "by_stage": {"zusammenfassung": stage([summary])},
        },
        "b": turns([post("main", "b1")], [post("main", "b2")]),
    }
    manager = setup(
        workfolder,
        "roundtable",
        scripts,
        recorder=recorder,
        params={"rounds": 2, "summarizer": "a"},
        groupchats=[channel("main", ["a", "b"], mode="rounds")],
    )
    assert await run(manager) is RunStatus.DONE
    discussion = [c for c in recorder.calls if c.stage == "diskussion"]
    assert len(discussion) == 4
    assert discussion[0].prompt.startswith("Runde 1 von 2 in #main.\n\n## Aufgabe\n\n" + TASK)
    assert discussion[0].context == {"task": TASK, "channel": "main"}
    [end] = log_entries(manager.run_dir, "discussion_end")
    assert (end["data"]["ended_by"], end["data"]["rounds"]) == ("rounds", 2)
    [last] = recorder.of("a", "zusammenfassung")
    assert 'submit_result(kind="summary")' in last.prompt
    [result] = manager.room.results()
    assert (result.kind, result.stage, result.agent) == ("summary", "zusammenfassung", "a")


async def test_roundtable_ends_when_all_are_done(workfolder: Path) -> None:
    scripts = {
        "a": turns([post("main", "fertig"), done()]),
        "b": turns([done()]),
    }
    manager = setup(
        workfolder,
        "roundtable",
        scripts,
        params={"rounds": 5},
        groupchats=[channel("main", ["a", "b"], mode="rounds")],
    )
    assert await run(manager) is RunStatus.DONE
    [end] = log_entries(manager.run_dir, "discussion_end")
    assert (end["data"]["ended_by"], end["data"]["rounds"]) == ("all_done", 1)


async def test_roundtable_in_event_mode(workfolder: Path) -> None:
    recorder = Recorder()
    scripts = {
        "a": turns(
            [wait(0.1), post("main", "Gilt das für n = 9?", kind="question", to=["b"]), done()]
        ),
        "b": turns([say("bereit")], [post("main", "Ja."), done()]),
    }
    manager = setup(
        workfolder,
        "roundtable",
        scripts,
        recorder=recorder,
        groupchats=[channel("main", ["a", "b"], mode="event", debounce_s=0.05, quiet_after_s=1)],
    )
    assert await run(manager) is RunStatus.DONE
    kickoff = recorder.of("a")[0]
    assert kickoff.prompt.startswith("## Aufgabe\n\n" + TASK) and kickoff.stage == "diskussion"
    assert len(recorder.of("b")) == 2, "b is woken by the question"
    [end] = log_entries(manager.run_dir, "discussion_end")
    assert end["data"]["ended_by"] == "all_done"


async def test_roundtable_stop_skips_the_summarizer(workfolder: Path) -> None:
    recorder = Recorder()
    scripts = {"a": turns([wait(30)]), "b": turns([wait(30)])}
    manager = setup(
        workfolder,
        "roundtable",
        scripts,
        recorder=recorder,
        params={"summarizer": "a"},
        groupchats=[channel("main", ["a", "b"], mode="rounds")],
    )
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: manager.orchestrator is not None and len(manager.orchestrator._active) == 2
    )
    manager.stop()
    assert await asyncio.wait_for(running, 10) is RunStatus.STOPPED
    assert recorder.of("a", "zusammenfassung") == []
    [end] = log_entries(manager.run_dir, "discussion_end")
    assert end["data"]["ended_by"] == "stopped"


# --------------------------------------------------------------------------------------------
# A3 waterfall: context package, ratings, rework, follow-ups
# --------------------------------------------------------------------------------------------

WATERFALL = {
    "agents": [agent("p"), agent("q"), agent("r")],
    "stages": [{"name": "skizze", "agent": "p"}, {"name": "beweis", "agent": "q"}],
    "raters": ["q", "r"],
    "rubric": {"scale": [1, 10], "criteria": ["korrektheit", "klarheit"]},
}


def waterfall(workfolder: Path, scripts: dict[str, Any], **extra: Any) -> RunManager:
    params = {"pass_score": 6, **extra.pop("params", {})}
    config = {**WATERFALL, **extra}
    return setup(workfolder, "waterfall", scripts, params=params, **config)


async def test_waterfall_context_package_ratings_and_rework(workfolder: Path) -> None:
    recorder = Recorder()
    scripts = {
        "p": staged(skizze=stage([submit("Skizze 1")], [submit("Skizze 2")])),
        "q": staged(
            skizze=stage(rate(4, "Lücke bei n = 1", korrektheit=3), rate(8, "gut", korrektheit=8)),
            beweis=stage([submit("Beweis", "proof")]),
        ),
        "r": staged(
            skizze=stage(rate(5, "unklar", klarheit=4), rate(7)),
            beweis=stage(rate(9)),
        ),
    }
    manager = waterfall(workfolder, scripts, recorder=recorder)
    assert await run(manager) is RunStatus.DONE

    [plan] = checkpoints(manager, "plan")  # W2-04.1 B4: only the RunManager writes it
    assert plan["stages"] == ["skizze", "beweis"]
    first, second = recorder.of("p", "skizze")
    assert first.context == {
        "task": TASK,
        "stage": "skizze",
        "attempt": 1,
        "produces": None,
        "accepted": [],
    }
    assert "get_assignment()" in first.prompt and "Versuch 1" in first.prompt
    assert second.context["attempt"] == 2 and "Nacharbeit" in second.prompt
    rework = second.context["rework"]
    assert (rework["result"], rework["score"], rework["pass_score"]) == (1, 4.5, 6.0)
    assert [(r["rater"], r["score"], r["comment"], r["criteria"]) for r in rework["ratings"]] == [
        ("q", 4.0, "Lücke bei n = 1", {"korrektheit": 3.0}),
        ("r", 5.0, "unklar", {"klarheit": 4.0}),
    ]
    results = manager.room.results()
    assert [(x.agent, x.stage, x.attempt) for x in results] == [
        ("p", "skizze", 1),
        ("p", "skizze", 2),
        ("q", "beweis", 1),
    ]
    [proof] = recorder.of("q", "beweis")[:1]
    assert proof.context["accepted"] == [
        {"id": 2, "stage": "skizze", "title": "Skizze 2", "path": None, "body": "Inhalt"}
    ]
    # ratings in parallel, never by the author
    assert manager.room.ratings(result_id=3)[0].rater == "r"
    assert ("skizze", ["q", "r"]) in recorder.batches
    assert ("beweis", ["r"]) in recorder.batches
    rater_job = recorder.of("r", "beweis")[0]
    assert rater_job.context["result_id"] == 3 and rater_job.context["task"] == TASK
    assert rater_job.context["criteria"] == ["korrektheit", "klarheit"]
    assert [(a["attempt"], a["score"], a["decision"]) for a in attempts(manager, "skizze")] == [
        (1, 4.5, "rework"),
        (2, 7.5, "passed"),
    ]
    assert stage_end(manager, "skizze")["accepted"] == 2
    assert stage_end(manager, "beweis") == {
        "stage": "beweis",
        "phase": "stage_end",
        "outcome": "passed",
        "accepted": 3,
        "attempt": 1,
        "score": 9.0,
        "attempts": 1,
    }


async def test_waterfall_asks_again_for_missing_result_and_rating(workfolder: Path) -> None:
    recorder = Recorder()
    scripts = {
        "p": staged(skizze=stage([say("…")], [say("noch nicht")], [say("…")], [submit("S")])),
        "q": staged(skizze=stage([say("später")], rate(7))),
        "r": staged(skizze=stage(rate(9))),
    }
    manager = waterfall(workfolder, scripts, recorder=recorder, stages=WATERFALL["stages"][:1])
    assert await run(manager) is RunStatus.DONE
    first, follow_up, *_ = recorder.of("p", "skizze")
    assert "noch kein Ergebnis abgegeben" in follow_up.prompt
    assert follow_up.context == first.context
    no_result, passed = attempts(manager, "skizze")
    assert (no_result["decision"], no_result["reason"], no_result["result"]) == (
        "rework",
        "no_result",
        None,
    )
    assert passed["score"] == 8.0 and passed["ratings"] == 2
    q_jobs = recorder.of("q", "skizze")
    assert len(q_jobs) == 2 and "fehlt noch" in q_jobs[1].prompt
    assert len(recorder.of("r", "skizze")) == 1, "only missing ratings are asked again"
    # the attempt without a result had no rating turns
    assert recorder.batches[0] == ("skizze", ["q", "r"])
    assert recorder.of("p", "skizze")[2].context["rework"] == {
        "result": None,
        "score": None,
        "pass_score": 6.0,
        "ratings": [],
    }


async def test_waterfall_without_any_rating_fails_the_attempt(workfolder: Path) -> None:
    scripts = {
        "p": staged(skizze=stage([submit("S")], repeat_last=True)),
        "q": staged(skizze=stage([say("keine Zeit")], repeat_last=True)),
        "r": staged(skizze=stage([say("keine Zeit")], repeat_last=True)),
    }
    manager = waterfall(
        workfolder, scripts, stages=WATERFALL["stages"][:1], params={"max_rework": 0}
    )
    assert await run(manager) is RunStatus.DONE
    [failed] = attempts(manager, "skizze")
    assert (failed["decision"], failed["reason"], failed["score"]) == ("failed", "no_rating", None)
    end = stage_end(manager, "skizze")
    assert (end["outcome"], end["accepted"]) == ("best_accepted", None)


# --------------------------------------------------------------------------------------------
# A4 on_fail
# --------------------------------------------------------------------------------------------


def failing_stage(*scores: float) -> dict[str, Any]:
    """Scripts where `p` submits every time and `q` rates with `scores` (r is not a rater)."""
    return {
        "p": staged(skizze=stage([submit("S")], repeat_last=True)),
        "q": staged(skizze=stage(*(rate(s) for s in scores), repeat_last=True)),
        "r": staged(beweis=stage([submit("B")])),
    }


ONE_RATER = {
    "raters": ["q"],
    "stages": [{"name": "skizze", "agent": "p"}, {"name": "beweis", "agent": "r"}],
}


async def test_on_fail_continue_accepts_the_best_attempt(workfolder: Path) -> None:
    scripts = failing_stage(3, 5, 4)
    scripts["q"]["by_stage"]["beweis"] = stage(rate(9))
    manager = waterfall(workfolder, scripts, params={"max_rework": 2}, **ONE_RATER)
    assert await run(manager) is RunStatus.DONE
    assert [a["decision"] for a in attempts(manager, "skizze")] == ["rework", "rework", "failed"]
    end = stage_end(manager, "skizze")
    assert (end["outcome"], end["accepted"], end["attempt"], end["score"]) == (
        "best_accepted",
        2,
        2,
        5.0,
    )
    assert stage_end(manager, "beweis")["outcome"] == "passed", "the next stage runs"


async def test_on_fail_stop_ends_the_run_regularly(workfolder: Path) -> None:
    recorder = Recorder()
    manager = waterfall(
        workfolder,
        failing_stage(3),
        recorder=recorder,
        params={"max_rework": 0, "on_fail": "stop"},
        **ONE_RATER,
    )
    assert await run(manager) is RunStatus.DONE
    end = stage_end(manager, "skizze")
    assert (end["outcome"], end["reason"]) == ("stage_failed", "stage_failed")
    assert recorder.of("r") == [], "the next stage does not run"


async def test_on_fail_escalate_retry_twice_then_continue(workfolder: Path) -> None:
    scripts = failing_stage(2, 3, 5, 4)
    scripts["q"]["by_stage"]["beweis"] = stage(rate(9))
    decider = Choose(EscalationOption.RETRY, EscalationOption.RETRY, EscalationOption.CONTINUE)
    manager = waterfall(
        workfolder,
        scripts,
        params={"max_rework": 1, "on_fail": "escalate"},
        decider=decider,
        **ONE_RATER,
    )
    assert await run(manager) is RunStatus.DONE
    assert [a["decision"] for a in attempts(manager, "skizze")] == [
        "rework",
        "escalated",
        "escalated",
        "escalated",
    ]
    assert len(decider.asked) == 3
    item = decider.asked[0]
    assert item.stage == "skizze" and item.options == [
        EscalationOption.RETRY,
        EscalationOption.CONTINUE,
        EscalationOption.END,
    ]
    assert "2 Versuche nicht bestanden" in str(item.summary)
    assert [e["data"]["option"] for e in log_entries(manager.run_dir, "escalation_decided")] == [
        "retry",
        "retry",
        "continue",
    ]
    end = stage_end(manager, "skizze")
    assert (end["outcome"], end["accepted"], end["score"], end["attempts"]) == (
        "best_accepted",
        3,
        5.0,
        4,
    )
    assert stage_end(manager, "beweis")["outcome"] == "passed"


async def test_on_fail_escalate_end_is_like_stop(workfolder: Path) -> None:
    recorder = Recorder()
    manager = waterfall(
        workfolder,
        failing_stage(3),
        recorder=recorder,
        params={"max_rework": 0, "on_fail": "escalate"},
        decider=Choose(EscalationOption.END),
        **ONE_RATER,
    )
    assert await run(manager) is RunStatus.DONE
    assert stage_end(manager, "skizze")["outcome"] == "stage_failed"
    assert recorder.of("r") == []


async def test_waterfall_returns_on_stop_during_ratings(workfolder: Path) -> None:
    recorder = Recorder()
    scripts = {
        "p": staged(skizze=stage([submit("S")])),
        "q": staged(skizze=stage([wait(30)])),
        "r": staged(skizze=stage([wait(30)])),
    }
    manager = waterfall(workfolder, scripts, recorder=recorder)
    running = asyncio.create_task(manager.run())
    await eventually(
        lambda: manager.orchestrator is not None and len(manager.orchestrator._active) == 2
    )
    manager.stop()
    assert await asyncio.wait_for(running, 10) is RunStatus.STOPPED
    assert attempts(manager, "skizze") == [], "no decision after the stop"
    assert len(recorder.calls) == 3


# --------------------------------------------------------------------------------------------
# A5 check_topology
# --------------------------------------------------------------------------------------------


def issues(resolved: ResolvedRun) -> dict[str, str]:
    with pytest.raises(ConfigError) as info:
        check_topology(resolved)
    assert isinstance(info.value, ArrError)
    return {issue.path: issue.message for issue in info.value.issues}


def test_check_solo() -> None:
    found = issues(resolved_run("solo", [agent("a"), agent("b")], params={"max_turns": 2.5}))
    assert found["params.agent"].startswith("„solo“ braucht genau einen Agenten, der Run hat 2.")
    assert found["params.max_turns"] == (
        "Parameter „max_turns“ muss eine ganze Zahl sein, gefunden 2.5."
    )
    found = issues(resolved_run("solo", [agent("a")], params={"agent": "x", "max_turns": 0}))
    assert (
        "„agent“ nennt „x“, das ist kein Agent dieses Runs. Vorhanden: a." in found["params.agent"]
    )
    assert found["params.max_turns"] == "Parameter „max_turns“ muss mindestens 1 sein, gefunden 0."
    found = issues(resolved_run("solo", [agent("a")], params={"require_result": "ja"}))
    assert found == {
        "params.require_result": "Parameter „require_result“ muss true oder false sein, "
        "gefunden „ja“."
    }
    check_topology(resolved_run("solo", [agent("a")], params={"max_turns": 2.0}))


def test_check_roundtable() -> None:
    two = [agent("a"), agent("b")]
    found = issues(resolved_run("roundtable", two))
    assert found == {
        "params.channel": "„roundtable“ braucht einen Gruppenchat. Lege unter „groupchats“ "
        "einen an."
    }
    chats = [channel("main", ["a", "b"]), channel("side", ["b"])]
    found = issues(resolved_run("roundtable", two, groupchats=chats, params={"rounds": "drei"}))
    assert (
        found["params.channel"] == "Nenne den Kanal der Diskussion im Parameter „channel“ "
        "(main, side)."
    )
    assert (
        found["params.rounds"] == "Parameter „rounds“ muss eine ganze Zahl sein, gefunden „drei“."
    )
    found = issues(resolved_run("roundtable", two, groupchats=chats, params={"channel": "x"}))
    assert "„x“, den Kanal gibt es nicht. Vorhanden: main, side." in found["params.channel"]
    found = issues(
        resolved_run(
            "roundtable", two, groupchats=chats, params={"channel": "side", "summarizer": "a"}
        )
    )
    assert found["params.summarizer"].startswith("Der Summarizer „a“ ist nicht Mitglied")
    late = [channel("main", ["a", "b"], open_from="später")]
    found = issues(resolved_run("roundtable", two, groupchats=late))
    assert "öffnet erst ab Stufe „später“" in found["params.channel"]
    check_topology(
        resolved_run("roundtable", two, groupchats=chats[:1], params={"summarizer": "b"})
    )


def test_check_waterfall() -> None:
    base = {
        "stages": [{"name": "s1", "agent": "p"}, {"name": "s2", "agent": "q"}],
        "raters": ["q"],
    }
    three = [agent("p"), agent("q"), agent("r")]
    found = issues(resolved_run("waterfall", three, **base))
    assert found == {
        "params.pass_score": "„waterfall“ braucht eine Bestehensgrenze. Setze den Parameter "
        "„pass_score“ oder „rubric.pass_score“.",
        "stages[1].agent": "Für die Stufe „s2“ gibt es keinen Rater außer dem Autor „q“. "
        "Ergänze unter „raters“ einen anderen Agenten.",
    }
    good = {**base, "raters": ["q", "r"]}
    check_topology(resolved_run("waterfall", three, **good, rubric={"pass_score": 7}))
    found = issues(
        resolved_run(
            "waterfall",
            three,
            **good,
            rubric={"scale": [0, 5]},
            params={"pass_score": 7, "max_rework": 1.5, "on_fail": "egal"},
        )
    )
    assert found == {
        "params.max_rework": "Parameter „max_rework“ muss eine ganze Zahl sein, gefunden 1.5.",
        "params.on_fail": "Parameter „on_fail“ ist „egal“. Erlaubt sind: continue, stop, escalate.",
        "params.pass_score": "Parameter „pass_score“ = 7 liegt außerhalb der Skala [0, 5] der "
        "Rubrik.",
    }
    found = issues(resolved_run("waterfall", three, params={"pass_score": True, "max_rework": -1}))
    assert (
        found["params.pass_score"] == "Parameter „pass_score“ muss eine Zahl sein, gefunden true."
    )
    assert (
        found["params.max_rework"] == "Parameter „max_rework“ muss mindestens 0 sein, gefunden -1."
    )
    assert found["stages"].startswith("„waterfall“ braucht Stufen.")
    found = issues(
        resolved_run(
            "waterfall",
            three,
            stages=[{"name": "plan", "agent": "p"}, {"name": "plan", "agent": "p"}],
            raters=["q"],
            params={"pass_score": 5},
        )
    )
    assert found["stages[0].name"].startswith("Der Stufenname „plan“ ist für arr reserviert.")
    assert (
        found["stages[1].name"] == "Die Stufe „plan“ gibt es mehrfach. Benenne eine der Stufen um."
    )
    found = issues(
        resolved_run("waterfall", three, stages=base["stages"][:1], params={"pass_score": 5})
    )
    assert "raters" in found, "no raters at all"


async def test_topology_checks_itself_when_check_topology_was_skipped(workfolder: Path) -> None:
    recorder = Recorder()
    manager = make_manager(
        workfolder,
        recorder.wrap("waterfall"),
        {"a": turns()},
        run_extra={"topology": {"kind": "waterfall", "params": {"max_rework": None}}},
    )
    assert await run(manager) is RunStatus.FAILED
    assert recorder.calls == []
    [error] = log_entries(manager.run_dir, "error")
    assert error["data"]["type"] == "ConfigError"
    assert "params.pass_score" in error["data"]["message"] and "stages" in error["data"]["message"]
