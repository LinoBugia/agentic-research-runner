"""W4b-06a A5, A6: the start skills as phases against the target flows of the old topologies.

Every case of `templates_kit.CASES` runs the real start skill from the package (`solo` as
`tests/fixtures/phases/solo.yaml`) with FakeBackend agents and compares its flow with
`tests/fixtures/template_flows/<template>-<case>.json`, up to the deviations
`templates_kit.DEVIATIONS` names one by one (table of §6.3 of the order). Nothing here needs
the old topologies; the file runs unchanged after W4b-06b.
"""

from pathlib import Path
from typing import Any

import pytest
from phases_kit import errors, run
from run_kit import make_workfolder
from templates_kit import (
    CASES,
    DEVIATIONS,
    FIXTURE_OF,
    FLOWS,
    Case,
    compare,
    deviations_for,
    new_manager,
    new_policies,
    run_new,
    structure_issues,
)

from arr.skills import start_skill_files


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def test_a5_every_case_has_its_target_flow() -> None:
    names = sorted(p.name for p in FLOWS.glob("*.json"))
    assert names == sorted(f"{c.id}.json" for c in CASES)


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_a6_the_start_skill_meets_its_target_flow(workfolder: Path, case: Case) -> None:
    mgr, flow = await run_new(workfolder, case)
    assert errors(mgr) == []
    applied = compare(case, flow)
    assert applied == [d.key for d in deviations_for(case)]


def test_a6_every_deviation_is_used_by_a_case() -> None:
    used = {d.key for c in CASES for d in deviations_for(c)}
    assert used == {d.key for d in DEVIATIONS}


def _falsified(name: str, old: str, new: str) -> dict[str, bytes]:
    files = start_skill_files(name)
    text = files["group.yaml"].decode()
    assert old in text, old
    files["group.yaml"] = text.replace(old, new).encode()
    return files


FALSIFIED = {
    "revision-ohne-goto": (
        "proof-review-revision",
        "proof-review",
        "gate: { type: done, routes: { done: { goto: pruefung } } }",
        "gate: { type: done, routes: { done: next } }",
    ),
    "fail-next-statt-retry": (
        "proof-nacharbeit",
        "proof",
        "routes: { pass: next, fail: retry }",
        "routes: { pass: next, fail: next }",
    ),
    # W4b-06a.2: without the proof as an input of `diskussion` and `revision`, the proof from
    # the project (seed) does not reach them
    "diskussion-ohne-beweis": (
        "proof-review-artefakt-aus-projekt",
        "proof-review",
        'rounds: "{{ params.discussion_rounds }}"\n    inputs:\n'
        "      - { name: beweis, content: full }\n      - { from: pruefung }\n",
        'rounds: "{{ params.discussion_rounds }}"\n',
    ),
    "revision-ohne-beweis": (
        "proof-review-artefakt-aus-projekt",
        "proof-review",
        "on_exhausted: stop\n    inputs:\n      - { name: beweis, content: full }\n",
        "on_exhausted: stop\n    inputs:\n",
    ),
    # without the list as an input of `zusammenfassung`, given ideas (seed) do not reach it
    "zusammenfassung-ohne-ideen": (
        "exploration-vorgegeben",
        "exploration",
        "    inputs:\n      - { name: ideen, content: full }\n      - { from: exploration }\n"
        "      - { from: exploration-digest }\n      - { from: exploration-kanal }\n",
        "",
    ),
}


@pytest.mark.parametrize("what", sorted(FALSIFIED))
async def test_a6_a_falsified_template_fails_the_comparison(workfolder: Path, what: str) -> None:
    """Counter-check: a template with a wrong route does not meet the target flow."""
    case_id, name, old, new = FALSIFIED[what]
    [case] = [c for c in CASES if c.id == case_id]
    mgr, flow = await run_new(workfolder, case, files=_falsified(name, old, new))
    with pytest.raises(AssertionError, match="Abweichung ohne Ausnahme"):
        compare(case, flow)


async def test_a6_note_next_stays_private(workfolder: Path) -> None:
    """Deviation `notiz-bleibt-privat` (Entscheidung 28): the note of one judge reaches no
    other judge, also not after a revision."""
    [case] = [c for c in CASES if c.id == "proof-review-revision"]
    policies = new_policies(case)
    judge = policies["judge-1"]

    def noting(view: Any) -> list[dict[str, Any]]:
        steps = judge(view)
        steps[-1]["args"]["note_next"] = f"geheime Notiz von {view.agent}"
        return steps

    policies["judge-1"] = noting
    mgr, log = new_manager(workfolder, "proof-review", policies)
    assert await run(mgr) == "done", errors(mgr)
    others = [v for v in log.views if v.agent != "judge-1"]
    assert others and not any("geheime Notiz" in v.prompt for v in others)
    assert not any("geheime Notiz" in str(v.context) for v in others)


@pytest.mark.parametrize("name", sorted(FIXTURE_OF))
def test_a6_the_templates_keep_the_agreed_structure(name: str) -> None:
    """M2 c: phases as in the test data of W4b-02a, `turns: 1` everywhere, isolated checks,
    every task file there."""
    assert structure_issues(name) == []


STRUCTURE_CHANGES = {
    "turns-3": ("proof-review", "group.yaml", "turns: 1 }", "turns: 3 }", "turns 3 statt 1"),
    "nicht-isoliert": ("proof-review", "group.yaml", "    isolated: true\n", "", "nicht isoliert"),
    "auftrag-vertauscht": (
        "proof",
        "group.yaml",
        "tasks/skizze.md",
        "tasks/beweis.md",
        "phases weichen",
    ),
    "ohne-peers": ("exploration", "group.yaml", "      - { peers: summary }\n", "", "phases"),
    # W4b-06a.3 NP2: the review's mutations outside `phases`
    "X2-summarizer-nicht-in-runde": (
        "roundtable",
        "group.yaml",
        "members: [diskutant-*, summarizer]",
        "members: [diskutant-*]",
        "channels weichen",
    ),
    "X3-werkstatt-event": (
        "exploration",
        "group.yaml",
        "    mode: rounds\n",
        "    mode: event\n",
        "channels weichen",
    ),
    "X4-autor-sonnet": (
        "proof-review",
        "group.yaml",
        "    role: roles/author.md\n    model: claude-opus-5-5\n",
        "    role: roles/author.md\n    model: claude-sonnet-5-5\n",
        "agents weichen",
    ),
    "X10-budget-30": ("roundtable", "group.yaml", "budget_usd: 3", "budget_usd: 30", "limits"),
    "X11-judge-mit-autorenrolle": (
        "proof-review",
        "group.yaml",
        "    role: roles/judge.md\n",
        "    role: roles/author.md\n",
        "agents weichen",
    ),
    "X15-wall-time-10h": (
        "proof-review",
        "group.yaml",
        "wall_time: 1h",
        "wall_time: 10h",
        "limits weichen",
    ),
    "X16-ideen-ohne-platzhalter": (
        "exploration",
        "tasks/ideen.md",
        "{{ params.n_ideas }}",
        "3",
        "„{{ params.n_ideas }}“ fehlt",
    ),
    "X17-pruefen-ohne-unabhaengig": (
        "proof-review",
        "tasks/pruefen.md",
        "unabhängig von den anderen Judges; ihre Gutachten siehst du in dieser Phase nicht",
        "zusammen mit den anderen Judges",
        "„unabhängig“ fehlt",
    ),
}


@pytest.mark.parametrize("key", sorted(STRUCTURE_CHANGES))
def test_a6_the_structure_check_finds_changes(key: str) -> None:
    name, path, old, new, what = STRUCTURE_CHANGES[key]
    files = start_skill_files(name)
    text = files[path].decode()
    assert old in text
    files[path] = text.replace(old, new, 1).encode()
    assert any(what in issue for issue in structure_issues(name, files))


# --------------------------------------------------------------------------------------------
# W4b-06a.1 point 7: the end of a phase that did not pass (H3, M1, Entscheidung 81)
# --------------------------------------------------------------------------------------------


def _case(case_id: str) -> Case:
    [case] = [c for c in CASES if c.id == case_id]
    return case


async def test_p7_the_old_line_in_stage_end_turns_the_comparison_red(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counter-check: with the old rule („stage_failed“ whenever no phase follows) the last
    stage of `proof` with `next` differs from the old topology."""
    from arr.runs.phases.runner import PhaseRunner

    monkeypatch.setattr(PhaseRunner, "_left_by_next", lambda self, phase, route, info: False)
    case = _case("proof-hohe-schwelle")
    _mgr, flow = await run_new(workfolder, case)
    with pytest.raises(AssertionError, match="Abweichung ohne Ausnahme"):
        compare(case, flow)


@pytest.mark.parametrize(
    ("case_id", "reason", "line"),
    [
        (
            "proof-aufgebraucht-stopp",
            "Die Phase „skizze“ wurde nicht bestanden (stage_failed).",
            "- **skizze:** nicht bestanden, Run beendet, akzeptiert: keins",
        ),
        (
            "proof-hohe-schwelle",
            "Die Topologie ist regulär durchgelaufen.",
            "- **beweis:** bester Versuch übernommen, akzeptiert: #",
        ),
    ],
)
async def test_p7_summary_and_run_detail_name_the_end(
    workfolder: Path, case_id: str, reason: str, line: str
) -> None:
    from arr.runs.inspect import run_reason
    from arr.runs.orchestrator import OrchestratorLog
    from arr.runs.prepare import load_run_meta
    from arr.service.api_models import StageStatus
    from arr.service.real_api import RealApi

    mgr, _flow = await run_new(workfolder, _case(case_id))
    summary = (mgr.run_dir / "output" / "summary.md").read_text("utf-8")
    assert f"- **Grund:** {reason}" in summary
    assert line in summary
    entries = list(OrchestratorLog(mgr.run_dir).entries())
    assert run_reason(load_run_meta(mgr.run_dir), entries) == reason
    detail = await RealApi(workfolder, base_url="http://test").get_run(mgr.run_dir.name)
    stages = {s.key: s.status for s in detail.stages}
    if case_id == "proof-aufgebraucht-stopp":
        assert stages["skizze"] is StageStatus.FAILED
    else:
        assert stages["beweis"] is StageStatus.DONE, "best attempt, not „nicht bestanden“"


# --------------------------------------------------------------------------------------------
# W4b-06a.3 NP1: the list `ideen` reaches summarizer and rankers with its entries
# --------------------------------------------------------------------------------------------

LIST_CASES = ["exploration-vorgegeben", "exploration-ideator", "exploration-digest"]


@pytest.mark.parametrize("case_id", LIST_CASES)
async def test_np1_the_old_full_text_turns_the_comparison_red(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, case_id: str
) -> None:
    """Counter-check: `_full_text` as before W4b-06a.3 (a `list` brings no text) → the
    summarizer sees the list only by its name."""
    from arr.runs.phases import inputs

    full = inputs._full_text

    def old_full_text(ctx: Any, s: Any) -> str:
        return full(ctx, s.model_copy(update={"fields": {}}) if s.format == "list" else s)

    monkeypatch.setattr(inputs, "_full_text", old_full_text)
    case = _case(case_id)
    _mgr, flow = await run_new(workfolder, case)
    with pytest.raises(AssertionError, match="Abweichung ohne Ausnahme"):
        compare(case, flow)


@pytest.mark.parametrize("case_id", LIST_CASES)
async def test_np1_without_the_entry_title_the_rankers_differ(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, case_id: str
) -> None:
    """Counter-check: without `entry_title` the rankers no longer know which idea they rate."""
    from arr.runs.phases import inputs

    monkeypatch.setattr(inputs, "_entry_title", lambda ctx, s, number: None)
    case = _case(case_id)
    _mgr, flow = await run_new(workfolder, case)
    with pytest.raises(AssertionError, match="Abweichung ohne Ausnahme"):
        compare(case, flow)


def test_np1_a_long_list_is_cut_with_a_hint_and_cleaned() -> None:
    from arr.room.types import Submission
    from arr.runs.phases import inputs
    from arr.runs.phases.text import MAX_FULL_CHARS

    items = [{"title": f"Idee‮ {k}\x00", "text": "x" * 400} for k in range(60)]
    sub = Submission(
        id=1,
        ts=0.0,
        agent="ideator",
        shown_as="ideator",
        stage="ideen",
        format="list",
        name="ideen",
        version=1,
        status="done",
        summary="s",
        fields={"items": items},
        path="output/ideen/v1",
    )
    text = inputs._list_text(sub)
    assert len(text) <= MAX_FULL_CHARS
    assert text.startswith("1. Idee") and "‮" not in text and "\x00" not in text
    assert text.endswith("[Liste gekürzt; alle Einträge stehen in „submission.json“ unter „path“]")
    short = inputs._list_text(sub.model_copy(update={"fields": {"items": items[:2]}}))
    assert "gekürzt" not in short and short.count("\n") == 1


async def test_np6_the_author_gets_one_review_per_judge(workfolder: Path) -> None:
    """W4b-06a.3 NP6: `revision` names the reviews of the discussion only (with a discussion),
    one per judge as in the old topology."""
    case = _case("proof-review-revision")
    mgr, log = new_manager(workfolder, case.template, new_policies(case), params={})
    await run(mgr)
    [revision] = [v for v in log.views if v.agent == "author" and v.stage == "revision"]
    reviews = [i for i in revision.context["inputs"] if i.get("format") == "review"]
    assert sorted(i["source"] for i in reviews) == ["phase:diskussion", "phase:diskussion"]
    assert len({i["from"] for i in reviews}) == 2


# --------------------------------------------------------------------------------------------
# W4b-06a.3 NP3: the end of a phase that was left over `next` into an exhausted one
# --------------------------------------------------------------------------------------------

PHASE_END = {
    # G11: `a` is left over `fail: next`; the run ends at `c` (`max_visits: 0`, stop)
    "G11": ("stop", None),
    # G12: the same with an escalation at `c`, the user ends it
    "G12": ("escalate", "end"),
}


@pytest.mark.parametrize("key", sorted(PHASE_END))
async def test_np3_a_phase_left_over_next_keeps_its_best_attempt(
    workfolder: Path, key: str
) -> None:
    from phases_kit import Choose, answer, judge_gate, manager, solo

    from arr.permissions.types import EscalationOption
    from arr.runs.finalize import finalize_run

    exhausted, option = PHASE_END[key]
    gate = judge_gate("r", pass_score=9, routes={"pass": "next", "fail": "next"})
    phases = [
        solo("a", "w", gate=gate),
        solo("c", "w", max_visits=0, on_exhausted=exhausted),
    ]
    decider = Choose(EscalationOption(option)) if option else None
    mgr, _log = manager(
        workfolder,
        {"agents": [{"id": "w"}, {"id": "r"}], "phases": phases},
        {"w": answer(), "r": answer(score=8)},
        decider=decider,
        finalizer=finalize_run,
    )
    await run(mgr)
    from phases_kit import checkpoints

    [end] = [c for c in checkpoints(mgr, "stage_end") if c["stage"] == "a"]
    assert end["outcome"] == "best_accepted" and end["accepted"] is not None
    summary = (mgr.run_dir / "output" / "summary.md").read_text("utf-8")
    assert "- **Grund:** Die Topologie ist regulär durchgelaufen." in summary
