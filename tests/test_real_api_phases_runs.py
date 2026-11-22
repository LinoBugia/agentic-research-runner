"""W4b-07 with FakeBackend runs of `tests/fixtures/phases/` read by the service (A5, A6, A8–A10).

Progress bar over loops (`review` with a revision over `goto`, `waterfall` with `retry` and
scores, `beispiel` with an open gate `user`), the phase plan with passes, gate result, usage and
estimate, submissions in „Ergebnisse“, the private channel, and the gate `user` decided on the
card with a hint that reaches the next pass.
"""

import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from phases_kit import (
    TurnView,
    answer,
    errors,
    fixture_manager,
    hand_in,
    manager,
    of_kind,
    parallel,
    private,
    run,
    run_with,
    say,
)
from phases_ui_kit import real_api, write_fake_profiles
from run_kit import eventually, make_workfolder

from arr.errors import ArrError
from arr.models import RunStatus
from arr.permissions import policy
from arr.permissions.types import (
    ApprovalStatus,
    ApprovalType,
    DecidedBy,
    Decision,
    DecisionAction,
    EscalationOption,
)
from arr.room.store import USER
from arr.room.types import MessageKind, MessageVia
from arr.service.api_models import DecisionRequest, StageStatus
from arr.service.real_api.phase_run import phase_stage_infos
from arr.service.real_api.rundir import RunRecord, stage_infos

EXPLORERS = ["ideator", "explorer-1", "explorer-2", "strategist", "judge-1", "judge-2", "writer"]


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


@pytest.fixture(autouse=True)
def no_user_temp_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "user_temp_dirs", lambda: [])


@pytest.fixture
def api(workfolder: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    write_fake_profiles(arr_home)
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return real_api(workfolder)


def record_of(mgr: Any) -> RunRecord:
    record = RunRecord(mgr.run_dir)
    record.refresh()
    return record


def judge(view: TurnView) -> list[dict[str, Any]]:
    verdict = "minor" if view.stage == "diskussion" and view.attempt == 1 else "accept"
    return hand_in(view, verdict=verdict)


async def review_run(workfolder: Path) -> Any:
    mgr, _ = fixture_manager(
        workfolder, "review", {"author": answer(), "judge-1": judge, "judge-2": judge}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    return mgr


SCORES = {("strategie", 1): 8, ("skizze", 1): 6, ("skizze", 2): 5, ("beweis", 1): 4}


async def waterfall_run(workfolder: Path) -> Any:
    def score(view: TurnView, ref: int) -> float:
        return SCORES.get((view.stage, view.attempt), 9)

    raters = {f"rater-{i}": answer(score=score) for i in (1, 2)}
    mgr, _ = fixture_manager(
        workfolder, "waterfall", {"strategist": answer(), "prover": answer(), **raters}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    return mgr


# --------------------------------------------------------------------------------------------
# A5: progress bar over loops
# --------------------------------------------------------------------------------------------


async def test_a5_a_phase_entered_again_has_two_passes(workfolder: Path) -> None:
    mgr = await review_run(workfolder)
    record = record_of(mgr)
    bar = {s.label: s for s in stage_infos(record, record.load_resolved())}
    assert [s.label for s in bar.values()] == ["fassung", "pruefung", "diskussion", "revision"]
    assert all(s.status is StageStatus.DONE for s in bar.values())
    assert [(p.n, p.value) for p in bar["diskussion"].passes] == [(1, "minor"), (2, "accept")]
    assert [p.n for p in bar["pruefung"].passes] == [1, 2]
    assert bar["pruefung"].agents == ["judge-1", "judge-2"], "participants of the phase"
    assert all(p.score is None for s in bar.values() for p in s.passes), "no mean: no score"


async def test_a5_the_phase_entered_again_runs_while_its_second_pass_runs(
    workfolder: Path,
) -> None:
    mgr = await review_run(workfolder)
    record = record_of(mgr)
    entries = record.log.entries
    second = next(
        i
        for i, e in enumerate(entries)
        if e.get("kind") == "checkpoint"
        and e["data"].get("phase") == "pass_start"
        and e["data"].get("stage") == "pruefung"
        and e["data"].get("attempt") == 2
    )
    plan = ["fassung", "pruefung", "diskussion", "revision"]
    bar = {
        s.label: s
        for s in phase_stage_infos(
            entries[: second + 1],
            record.load_resolved(),
            plan,
            active=True,
            failed_run=False,
            escalation_open=False,
        )
    }
    assert bar["pruefung"].status is StageStatus.RUNNING, "entered again: it runs"
    assert [(p.n, p.running) for p in bar["pruefung"].passes] == [(1, False), (2, True)]
    assert bar["revision"].status is StageStatus.DONE
    assert bar["diskussion"].status is StageStatus.DONE


async def test_a5_retry_with_scores_and_pass_marks(workfolder: Path) -> None:
    mgr = await waterfall_run(workfolder)
    record = record_of(mgr)
    bar = {s.label: s for s in stage_infos(record, record.load_resolved())}
    sketch = [(p.n, p.score, p.pass_score, p.passed) for p in bar["skizze"].passes]
    assert sketch == [(1, 6.0, 7.0, False), (2, 5.0, 7.0, False)]
    assert [(p.score, p.passed) for p in bar["beweis"].passes] == [(4.0, False), (9.0, True)]
    from arr.service.gui.components.stage_bar import stage_bar_html

    html = stage_bar_html(list(bar.values()), record.load_resolved().topology.kind)
    assert (
        "D1 6,0" in html and "D2 5,0" in html and 'aria-label="Ablauf in Phasen, 3 Phasen"' in html
    )


def test_a5_failed_after_stage_failed_and_odd_entries_never_break() -> None:
    entries: list[Any] = [
        {"kind": "checkpoint", "data": {"stage": "plan", "stages": ["a", "b"]}},
        {"kind": "checkpoint", "data": {"stage": "a", "phase": "pass_start", "attempt": 1}},
        {"kind": "checkpoint", "data": {"stage": "a", "phase": "gate", "attempt": "eins"}},
        {"kind": "checkpoint", "data": ["kein", "objekt"]},
        {"kind": "checkpoint", "data": {"stage": 7, "phase": "route"}},
        "keine zeile",
        {
            "kind": "checkpoint",
            "data": {"stage": "a", "phase": "stage_end", "outcome": "stage_failed"},
        },
    ]
    bar = phase_stage_infos(
        entries, None, ["a", "b"], active=False, failed_run=True, escalation_open=False
    )
    assert [s.status for s in bar] == [StageStatus.FAILED, StageStatus.TODO]


# --------------------------------------------------------------------------------------------
# A10, A5, A11-ish: gate `user` from the card to the context package
# --------------------------------------------------------------------------------------------


async def test_a10_gate_user_from_the_card_to_the_next_pass(
    workfolder: Path, api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arr.service.real_api import rundir
    from arr.service.real_api.approvals import _decision

    # the lock of `run.lock` is this process's own here; the service runs in another process
    monkeypatch.setattr(rundir, "run_lock_held", lambda run_dir: True)

    hint = "Mehr Ideen, bitte <b>keine</b> Wiederholung"
    seen: list[Any] = []
    bars: list[Any] = []

    class FromCard:
        async def decide(self, core: Any) -> Decision:
            item = await api.get_approval(core.id)
            seen.append(item)

            def bar() -> dict[str, Any]:
                record = record_of(mgr)
                return {s.label: s for s in stage_infos(record, record.load_resolved())}

            if len(seen) == 1:  # the run pauses for the escalation right after asking
                await eventually(lambda: bar()["ideen"].status is StageStatus.WAITING_USER, 10)
            bars.append(bar())
            if len(seen) == 1:
                request = DecisionRequest(
                    action=DecisionAction.OPTION, option_key=EscalationOption.RETRY, comment=hint
                )
            else:
                request = DecisionRequest(
                    action=DecisionAction.OPTION, option_key=EscalationOption.CONTINUE
                )
            return _decision(item, request)

    policies: dict[str, Any] = {a: answer() for a in EXPLORERS}
    policies["prover"] = answer()
    mgr, log = fixture_manager(workfolder, "beispiel", policies, decider=FromCard())
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    first, second = seen
    assert first.type is ApprovalType.ESCALATION and first.stage == "ideen"
    assert first.title == "Phase „ideen“ wartet auf deine Entscheidung"
    assert (first.attempt, second.attempt) == (1, 2)
    assert first.phase_reason == "Gate „user“: Du entscheidest, wie es weitergeht."
    assert first.summary_lines[0] == "Abgaben:" and "ideator" in first.summary_lines[1]
    assert first.comment_to is None and first.attempts == [], "no score chips without mean"
    helps = {o.key: (o.label, o.help) for o in first.options}
    assert helps[EscalationOption.CONTINUE] == (
        "Weiter",
        "Der Run geht mit der Phase „exploration“ weiter.",
    )
    assert helps[EscalationOption.RETRY][0] == "Noch ein Versuch"
    assert "Durchlauf 2" in helps[EscalationOption.RETRY][1]
    assert "max_visits" in helps[EscalationOption.RETRY][1]
    assert helps[EscalationOption.END] == (
        "Run beenden",
        "Keine weitere Phase; arr schließt den Run ab.",
    )
    assert bars[0]["ideen"].status is StageStatus.WAITING_USER, "„wartet auf dich“"
    assert bars[1]["ideen"].passes[0].value == "noch ein Versuch"
    # the hint is the decision's comment and reaches the next pass of the phase
    [again] = [v for v in log.of("ideator") if v.attempt == 2]
    assert again.context["feedback"]["user_hint"] == hint
    decided = of_kind(mgr, "escalation_decided")
    assert [(d["option"], d["hint"]) for d in decided] == [("retry", True), ("continue", False)]
    plan = (await api.get_run(mgr.run_dir.name)).phase_plan
    assert {r.name: r.gate_result for r in plan.rows}["ideen"] == "weiter", "the user's choice"
    history = await api.get_approval(first.id)
    assert history.status is ApprovalStatus.DECIDED and history.decision.comment == hint


def test_a10_old_escalations_keep_their_texts() -> None:
    from arr.service.real_api.approvals import _option_infos

    options = _option_infos(list(EscalationOption), "prover", "skizze")
    assert [(o.label, o.help) for o in options] == [
        ("Noch ein Versuch", "Die Nacharbeit wird um 1 erhöht. prover überarbeitet die Stufe."),
        ("Trotzdem weiter", "Der Run geht mit dem besten Versuch von „skizze“ weiter."),
        ("Run beenden", "Die Zusammenfassung wird erstellt, das Protokoll bleibt vollständig."),
    ]


# --------------------------------------------------------------------------------------------
# A6: the plan with passes, gate result, usage and estimate
# --------------------------------------------------------------------------------------------


async def test_a6_plan_of_a_finished_run(workfolder: Path, api: Any) -> None:
    mgr = await review_run(workfolder)
    detail = await api.get_run(mgr.run_dir.name)
    plan = detail.phase_plan
    assert plan is not None
    rows = {r.name: r for r in plan.rows}
    assert (rows["pruefung"].visits, rows["diskussion"].visits, rows["revision"].visits) == (
        2,
        2,
        1,
    )
    assert rows["revision"].max_visits == 1
    assert rows["diskussion"].gate_result == "accept"
    assert rows["pruefung"].usage is not None and rows["pruefung"].usage.turns == 4
    assert rows["fassung"].usage.turns == 1
    assert rows["diskussion"].estimate_text == "2–8 (2 Durchläufe)"
    assert plan.dropped == ["pruefung-allein"]
    usage = record_of(mgr).usage
    assert usage is not None and {k: v.turns for k, v in usage.by_phase.items()} == {
        r.name: r.usage.turns for r in plan.rows if r.usage
    }


async def test_a6_usage_per_phase_while_the_run_runs_comes_from_the_transcripts(
    workfolder: Path,
) -> None:
    from arr.service.real_api.phase_run import phase_usage

    mgr = await review_run(workfolder)
    record = record_of(mgr)
    final = {k: v.turns for k, v in record.usage.by_phase.items()}
    record.meta = record.meta.model_copy(update={"status": RunStatus.RUNNING})
    live = {k: v.turns for k, v in phase_usage(record, record.load_resolved()).items()}
    assert live == final
    agent = next(iter(record.agents.values()))
    first = agent.transcript.usage_records()
    assert agent.transcript.usage_records() is first, "cached while the file does not grow"


async def test_a6_estimate_is_open_with_a_gate_user(workfolder: Path, api: Any) -> None:
    policies: dict[str, Any] = {a: answer() for a in [*EXPLORERS, "prover"]}

    class Continue:
        async def decide(self, core: Any) -> Decision:
            return Decision(
                action=DecisionAction.OPTION,
                option=EscalationOption.CONTINUE,
                decided_by=DecidedBy.USER,
            )

    mgr, _ = fixture_manager(workfolder, "beispiel", policies, decider=Continue())
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    plan = (await api.get_run(mgr.run_dir.name)).phase_plan
    assert all("Obergrenze offen" in (r.estimate_text or "") for r in plan.rows)


# --------------------------------------------------------------------------------------------
# A8: submissions in „Ergebnisse“
# --------------------------------------------------------------------------------------------


async def test_a8_submissions_by_phase_and_pass_with_own_ids(workfolder: Path, api: Any) -> None:
    secret = "NOTE-NEXT-NIE-ZEIGEN"
    policies: dict[str, Any] = {a: answer(note_next=secret) for a in [*EXPLORERS, "prover"]}

    class Continue:
        async def decide(self, core: Any) -> Decision:
            return Decision(
                action=DecisionAction.OPTION,
                option=EscalationOption.CONTINUE,
                decided_by=DecidedBy.USER,
            )

    mgr, _ = fixture_manager(workfolder, "beispiel", policies, decider=Continue())
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    results = await api.get_results(mgr.run_dir.name)
    subs = results.submissions
    assert subs and results.empty_text is None
    formats = {s.format for s in subs}
    assert {"list", "text", "rating", "artifact", "review"} <= formats
    beweis = [s for s in subs if s.name == "beweis"]
    assert beweis and beweis[0].version == 1 and beweis[0].files
    ranked = [s for s in subs if s.stage == "exploration" and s.format == "text"]
    assert all(s.score is not None for s in ranked) and any(s.accepted for s in ranked)
    assert [s.entry for s in ranked if s.entry], "over: the entry of the list"
    review = next(s for s in subs if s.format == "review")
    assert "verdict" in review.fields
    assert secret not in results.model_dump_json(), "note_next is private"
    assert not any(s.sealed for s in subs), "released after the phase"
    ids = [s.id for s in subs]
    assert ids == sorted(set(ids))
    from arr.service.gui.pages.run_detail_results import results_html

    html = results_html(results, "h", "/r")
    assert "Phase exploration · Durchlauf 1" in html and 'id="a001"' in html
    assert secret not in html


def test_a8_a_sealed_submission_is_marked_and_an_alias_named() -> None:
    from arr.room.types import Submission
    from arr.service.gui.pages.run_detail_results import submissions_html
    from arr.service.real_api.phase_run import submission_infos

    sub = Submission(
        id=3,
        ts=1.0,
        agent="judge-1",
        shown_as="Judge A",
        stage="pruefung",
        attempt=1,
        action="work",
        format="review",
        status="done",
        summary="Gutachten",
        note_next="privat",
        fields={"verdict": "minor", "objections": [{"text": "Lücke"}]},
        path="groupchats/sealed/x",
        sealed=True,
    )
    record = SimpleNamespace(run_id="r", log=SimpleNamespace(entries=[]))
    store = SimpleNamespace(submissions=lambda: [sub])
    [info] = submission_infos(record, store)
    assert (info.alias, info.sealed, info.agent) == ("Judge A", True, "judge-1")
    html = submissions_html([info])
    assert (
        "versiegelt bis zum Ende der Phase" in html and "Judge A" in html and "privat" not in html
    )


async def test_a8_the_empty_state_of_a_phases_run_names_submit(workfolder: Path, api: Any) -> None:
    group = {"agents": [{"id": "a"}], "phases": [parallel("p", ["a"])]}
    mgr, _ = manager(workfolder, group, {"a": lambda v: [{"do": "text", "text": "nichts"}]})
    await run(mgr)
    results = await api.get_results(mgr.run_dir.name)
    assert results.submissions == [] and "submit" in (results.empty_text or "")
    assert "submit_result" not in (results.empty_text or "")


# --------------------------------------------------------------------------------------------
# A9: the private channel
# --------------------------------------------------------------------------------------------

ASK = "@user Darf ich annehmen, dass n prim ist?"


def asks_then_hands_in(view: TurnView) -> list[dict[str, Any]]:
    if "Der User hat deine Frage beantwortet" in view.prompt:
        return hand_in(view, summary="nach der Antwort")
    if view.follow_up:
        return [{"do": "text", "text": "…"}]
    return [say(private(view.agent), ASK, kind="question")]


def question_item(mgr: Any) -> Any:
    if getattr(mgr, "inbox", None) is None:
        return None
    found = [i for i in mgr.inbox.pending() if i.type is ApprovalType.QUESTION]
    return found[0] if found else None


async def test_a9_a_note_does_not_close_the_question_an_answer_does(
    workfolder: Path, api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arr.runs.phases import wake

    monkeypatch.setattr(wake, "user_question_hold_s", lambda: 600.0)
    found: dict[str, Any] = {}

    async def user(mgr: Any) -> None:
        await eventually(lambda: question_item(mgr) is not None, within=30)
        item = question_item(mgr)
        await eventually(lambda: bool(of_kind(mgr, "waiting", reason="user")), within=30)
        channels = {c.name: c for c in await api.list_channels(mgr.run_dir.name)}
        found["private_of"] = channels["user.a"].private_of
        mgr.room.post(
            "user.a",
            "Ich schaue gleich nach.",
            sender=USER,
            kind=MessageKind.NOTE,
            via=MessageVia.GUI,
        )
        await eventually(lambda: mgr.orchestrator._last_seen >= item.question.msg_id + 1, 30)
        found["after_note"] = mgr.orchestrator.user_question_open(item.question.msg_id)
        service = await api.get_approval(item.id)
        found["service"] = service
        from arr.service.real_api.approvals import _post_answer

        _post_answer(mgr.room, service, "Ja, n ist prim.")
        with contextlib.suppress(ArrError):
            mgr.inbox.resolve(
                item.id,
                Decision(action=DecisionAction.ANSWER, decided_by=DecidedBy.USER, comment="Ja."),
            )

    group = {"agents": [{"id": "a"}], "phases": [parallel("p", ["a"], turns=1)]}
    mgr, _ = manager(workfolder, group, {"a": asks_then_hands_in})
    assert await run_with(mgr, user) is RunStatus.DONE, errors(mgr)
    assert found["private_of"] == "a"
    assert found["after_note"], "a note leaves the question open (NR4)"
    assert found["service"].channel == "user.a"
    states = [d["state"] for d in of_kind(mgr, "user_question")]
    assert states == ["open", "answered"]


def test_a9_labels_never_show_a_group_chat_for_a_private_channel() -> None:
    from arr.service.gui.texts import channel_label

    assert channel_label("user.prover") == "privat · prover"
    assert channel_label("judges") == "#judges"
    assert channel_label("user.<b>") == "#user.<b>", "no slug: not a private channel"


async def test_a9_old_runs_have_no_private_channels(workfolder: Path, api: Any) -> None:
    from run_kit import make_manager, room, turns

    from arr.runs.topologies import solo as old_solo

    mgr = make_manager(
        workfolder,
        old_solo.run,
        {"a": turns([room("submit_result", kind="proof", title="T", body="B")])},
        run_extra={"topology": {"kind": "solo"}},
    )
    assert await mgr.run() is RunStatus.DONE, errors(mgr)
    assert await api.list_channels(mgr.run_dir.name) == []
    detail = await api.get_run(mgr.run_dir.name)
    assert detail.phase_plan is None and all(not s.passes for s in detail.stages)


async def test_the_runs_list_names_the_phase_entered_last(workfolder: Path, api: Any) -> None:
    await review_run(workfolder)
    [summary] = (await api.list_runs()).items
    assert summary.stage is not None and summary.stage.label == "diskussion", "after the goto"


def test_a_waiting_run_start_names_its_topology(tmp_path: Path) -> None:
    import json

    from arr.models import TopologyKind
    from arr.service.real_api.rundir import _start_topology

    run_dir = tmp_path / "r"
    run_dir.mkdir()
    line = {"event": "open", "item_id": "apr_1", "run_start": {"topology": "phases"}}
    (run_dir / "approvals.jsonl").write_text(json.dumps(line) + "\n", "utf-8")
    record = RunRecord(run_dir)
    record.approvals.refresh()
    assert _start_topology(record) is TopologyKind.PHASES
    (run_dir / "approvals.jsonl").write_text(json.dumps({**line, "run_start": "x"}) + "\n", "utf-8")
    other = RunRecord(run_dir)
    other.approvals.refresh()
    assert _start_topology(other) is None
