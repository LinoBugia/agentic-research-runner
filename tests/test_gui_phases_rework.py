"""W4b-07.1: the points B3–B11 of the review of W4b-07.

B3 query parameters such as „²“ never make an error page; B4 one cleaning rule for agent text
in the new displays; B5 the escalation card without Markdown escapes; B6 the visible sentence at
the hint field; B7 „Phase“ instead of „Stufe“ and „Notiz-Turn“; B8 a phase a finished run never
entered; B9 the option text without a stage; B10 a field named `note_next`; B11 the note of the
user in a private channel through the service, as the GUI sends it."""

import asyncio
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
from test_gui import gui_user

from arr.models import RunStatus
from arr.permissions import policy
from arr.permissions.types import ApprovalStatus, ApprovalType, EscalationOption
from arr.room.types import MessageKind
from arr.runs.finalize import one_line as markdown_line
from arr.runs.launch import describe_entry
from arr.service.agent_view import block, line
from arr.service.api_models import PostMessageRequest, StageStatus
from arr.service.gui.components.approval_card import PHASE_HINT_LABEL, PHASE_HINT_PLACEHOLDER
from arr.service.gui.components.base import query_int
from arr.service.gui.components.stage_bar import stage_bar_html
from arr.service.mock_api import PHASES_SKILL, R_PHASES, Dataset, MockApi
from arr.service.real_api.approvals import _phase_option_infos, unescape_markdown
from arr.service.real_api.phase_plan import one_line
from arr.service.real_api.phase_run import submission_infos
from arr.service.real_api.rundir import RunRecord, stage_infos
from arr.service.user_api import agent_text

HIDDEN = "\u202e\u2060\ufeff\u061c\U000e0041\U000e0042\x1b"
"""Bidi override, word joiner, BOM, Arabic letter mark, two tag characters, ESC."""


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


@pytest.fixture(autouse=True)
def no_user_temp_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "user_temp_dirs", lambda: [])


# --------------------------------------------------------------------------------------------
# B3: query parameters
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["²", "١٢", "12a", "", "1" * 20, "-3"])
def test_b3_query_int_takes_only_ascii_digits(bad: str) -> None:
    assert query_int(bad) is None
    assert query_int("42") == 42


@pytest.mark.parametrize("bad", ["²", "١٢"])
@pytest.mark.parametrize(
    "path",
    [
        f"/skills/{PHASES_SKILL}?tab=yaml&zeile={{}}",
        f"/runs/{R_PHASES}?abgabe={{}}",
        f"/runs/{R_PHASES}?ansicht=ergebnisse&ergebnis={{}}",
        f"/runs/{R_PHASES}?agent=ideator-1&seq={{}}",
        f"/runs/{R_PHASES}?kanal=user.ideator-1&nachricht={{}}",
    ],
)
async def test_b3_no_error_page_for_a_digit_that_is_no_number(path: str, bad: str) -> None:
    async with gui_user(MockApi(Dataset.PHASES)) as user:
        await user.open(path.format(bad))  # raises on a server error


# --------------------------------------------------------------------------------------------
# B4: one cleaning rule
# --------------------------------------------------------------------------------------------


def test_b4_lines_and_blocks_lose_format_and_control_characters() -> None:
    text = f"a{HIDDEN}b\nc\u2028d\te"
    assert line(text) == "ab c d e"
    assert block(text) == "ab\nc\nd\te"
    assert one_line(f"x{HIDDEN}y") == "xy", "the phase plan uses the same rule"
    for value in (text, "x" * 50 + HIDDEN):
        assert line(value, 400) == " ".join(agent_text(value).split()), "same as read_results"


def test_b4_submission_cards_are_cleaned() -> None:
    from arr.room.types import Submission

    sub = Submission(
        id=1,
        ts=1.0,
        agent="a",
        shown_as="a",
        stage="p",
        format="text",
        status="done",
        summary=f"Kurz{HIDDEN}fassung",
        body=f"Text{HIDDEN}\nzweite Zeile",
        files=[f"datei{HIDDEN}.md"],
        fields={f"feld{HIDDEN}": f"Wert{HIDDEN}\nZeile 2", "liste": [f"x{HIDDEN}"]},
        path="output/x",
    )
    store = SimpleNamespace(submissions=lambda: [sub])
    [info] = submission_infos(SimpleNamespace(run_id="r", log=SimpleNamespace(entries=[])), store)
    assert info.summary == "Kurzfassung" and info.files == ["datei.md"]
    assert info.body_excerpt == "Text zweite Zeile"
    assert info.fields == {"feld": "Wert\nZeile 2", "liste": ["x"]}
    dump = info.model_dump_json()
    for char in HIDDEN:
        assert char not in dump


# --------------------------------------------------------------------------------------------
# B5, B9: escalation card
# --------------------------------------------------------------------------------------------


def test_b5_the_card_shows_the_agent_text_without_markdown_escapes() -> None:
    original = (
        'Zwei Ideen <img src=x onerror="alert(1)"> [klick](javascript:alert(2)) **fett** a\\b'
    )
    escaped = markdown_line(original, 400)
    assert "\\<img" in escaped
    assert unescape_markdown(escaped) == original


async def test_b5_a_real_escalation_reaches_the_card_unescaped(
    workfolder: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_fake_profiles(arr_home)
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    api = real_api(workfolder)
    seen: list[Any] = []
    summary = "Ideen <img src=x> **fett** [x](y)"

    class Look:
        async def decide(self, core: Any) -> Any:
            from arr.permissions.types import DecidedBy, Decision, DecisionAction

            seen.append(await api.get_approval(core.id))
            return Decision(
                action=DecisionAction.OPTION, option=EscalationOption.END, decided_by=DecidedBy.USER
            )

    mgr, _ = fixture_manager(
        workfolder, "beispiel", {"ideator": answer(summary=summary)}, decider=Look()
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    [item] = seen
    assert any(line.endswith(f": {summary}") for line in item.summary_lines), item.summary_lines


def test_b9_options_without_a_stage_name_the_phase_once() -> None:
    options = _phase_option_infos(list(EscalationOption), None, 2, None)
    retry = next(o for o in options if o.key is EscalationOption.RETRY)
    assert retry.help.startswith("Die Phase läuft noch einmal (Durchlauf 3)")
    assert "Die Phase Die Phase" not in retry.help
    with_stage = _phase_option_infos([EscalationOption.RETRY], "ideen", 1, None)[0]
    assert with_stage.help.startswith("Die Phase „ideen“ läuft noch einmal (Durchlauf 2)")


# --------------------------------------------------------------------------------------------
# B6: the hint field
# --------------------------------------------------------------------------------------------


async def test_b6_the_sentence_at_the_hint_field_is_visible_text() -> None:
    from test_gui_phases_mock import see

    async with gui_user(MockApi(Dataset.PHASES)) as user:
        await user.open("/freigaben")
        html = await see(user, PHASE_HINT_LABEL, "Gate „user“")
    assert f'placeholder="{PHASE_HINT_PLACEHOLDER}"' in html or PHASE_HINT_PLACEHOLDER in html
    assert "Hinweis (optional, geht nur bei" not in html, "the long placeholder is gone"


# --------------------------------------------------------------------------------------------
# B7: wording
# --------------------------------------------------------------------------------------------


def test_b7_a_turn_of_a_phases_run_names_its_phase() -> None:
    entry = {"kind": "turn", "data": {"agent": "prover", "turn": 3, "stage": "beweis"}}
    assert describe_entry(entry, phases=True) == "▶ prover: Turn 3 · Phase „beweis“"
    assert describe_entry(entry) == "▶ prover: Turn 3 · Stufe „beweis“", "old runs word for word"
    note = {"kind": "turn", "data": {**entry["data"], "purpose": "compact"}}
    assert describe_entry(note, phases=True) == "▶ prover: Turn 3 · Phase „beweis“ (Notiz-Turn)"


async def test_b7_the_log_of_a_phases_run_says_phase(workfolder: Path) -> None:
    from arr.service.real_api.rundir import log_entries

    mgr, _ = fixture_manager(workfolder, "solo", {"worker": answer()})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    record = RunRecord(mgr.run_dir)
    record.refresh()
    turns = [e.text for e in log_entries(record) if e.text.startswith("▶ worker")]
    assert turns == ["▶ worker: Turn 1 · Phase „arbeit“"]


# --------------------------------------------------------------------------------------------
# B8: a phase a finished run never entered
# --------------------------------------------------------------------------------------------


async def test_b8_a_phase_never_entered_and_the_summary_counts_it(workfolder: Path) -> None:
    from phases_kit import Choose

    agents = ["ideator", "explorer-1", "explorer-2", "strategist", "judge-1", "judge-2"]
    policies: dict[str, Any] = {a: answer() for a in [*agents, "prover", "writer"]}
    mgr, _ = fixture_manager(
        workfolder, "beispiel", policies, decider=Choose(EscalationOption.CONTINUE)
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    record = RunRecord(mgr.run_dir)
    record.refresh()
    bar = stage_infos(record, record.load_resolved())
    states = {s.label: s.status for s in bar}
    assert states["revision"] is StageStatus.SKIPPED, "accept skips the revision"
    assert {s for n, s in states.items() if n != "revision"} == {StageStatus.DONE}
    html = stage_bar_html(bar, record.load_resolved().topology.kind)
    assert "6 · revision" in html and "(nicht betreten)" in html
    assert "6 von 7 erledigt" in html


def test_b8_a_running_run_keeps_open_phases_open() -> None:
    from arr.service.real_api.phase_run import phase_stage_infos

    entries = [{"kind": "checkpoint", "data": {"stage": "a", "phase": "pass_start", "attempt": 1}}]
    bar = phase_stage_infos(
        entries, None, ["a", "b"], active=True, failed_run=False, escalation_open=False
    )
    assert [s.status for s in bar] == [StageStatus.RUNNING, StageStatus.TODO]


# --------------------------------------------------------------------------------------------
# B10: a field named `note_next`
# --------------------------------------------------------------------------------------------


def test_b10_a_field_named_note_next_is_never_shown() -> None:
    from arr.room.types import Submission

    sub = Submission(
        id=1,
        ts=1.0,
        agent="a",
        shown_as="a",
        format="eigen",
        status="done",
        summary="s",
        note_next="SPALTE",
        fields={"note_next": "FELD-NOTE-NEXT", "urteil": "gut"},
        path="output/x",
    )
    store = SimpleNamespace(submissions=lambda: [sub])
    [info] = submission_infos(SimpleNamespace(run_id="r", log=SimpleNamespace(entries=[])), store)
    assert info.fields == {"urteil": "gut"}
    assert "FELD-NOTE-NEXT" not in info.model_dump_json() and "SPALTE" not in info.model_dump_json()


# --------------------------------------------------------------------------------------------
# B11: the note of the user through the service (as the GUI sends it: „An“ without `to`)
# --------------------------------------------------------------------------------------------

ASK = "@user Darf ich annehmen, dass n prim ist?"


def asks_then_hands_in(view: TurnView) -> list[dict[str, Any]]:
    if "Der User hat deine Frage beantwortet" in view.prompt:
        return hand_in(view, summary="nach der Antwort")
    if view.follow_up or "Ich schaue gleich nach" in view.prompt:
        return [{"do": "text", "text": "…"}]
    return [say(private(view.agent), ASK, kind="question")]


async def test_b11_a_note_through_the_service_leaves_the_question_open(
    workfolder: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arr.runs.phases import wake
    from arr.service.real_api import rundir
    from arr.service.real_api.base import RunHandle

    monkeypatch.setattr(wake, "user_question_hold_s", lambda: 600.0)
    # the lock of `run.lock` is this process's own here; the service runs in another process
    monkeypatch.setattr(rundir, "run_lock_held", lambda run_dir: True)
    write_fake_profiles(arr_home)
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    api = real_api(workfolder)
    found: dict[str, Any] = {}

    def question(mgr: Any) -> Any:
        pending = [i for i in mgr.inbox.pending() if i.type is ApprovalType.QUESTION]
        return pending[0] if pending else None

    async def user(mgr: Any) -> None:
        await eventually(lambda: getattr(mgr, "inbox", None) is not None and question(mgr), 30)
        item = question(mgr)
        await eventually(lambda: bool(of_kind(mgr, "waiting", reason="user")), within=30)
        never = asyncio.get_running_loop().create_future()
        handle = RunHandle(mgr.run_dir.name, mgr.run_dir, launch=None, task=never, manager=mgr)  # type: ignore[arg-type]
        api.state.registry.add(handle)
        note = await api.runs.post_message(
            mgr.run_dir.name,
            "user.a",
            PostMessageRequest(body="Ich schaue gleich nach.", kind=MessageKind.NOTE),
        )
        found["note_to"] = list(note.to)
        await eventually(lambda: mgr.orchestrator._last_seen >= note.id, 30)
        found["after_note"] = mgr.orchestrator.user_question_open(item.question.msg_id)
        service = await api.get_approval(item.id)
        found["status_after_note"] = service.status
        from arr.service.real_api.approvals import _post_answer

        _post_answer(mgr.room, service, "Ja, n ist prim.")
        never.cancel()
        for follower in list(api.runs._followers):
            follower.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await follower

    group = {"agents": [{"id": "a"}], "phases": [parallel("p", ["a"], turns=1)]}
    mgr, _ = manager(workfolder, group, {"a": asks_then_hands_in})
    assert await run_with(mgr, user) is RunStatus.DONE, errors(mgr)
    assert found["note_to"] == [], "the GUI sends no `to` (W4b-03.1 E2)"
    assert found["after_note"] and found["status_after_note"] is ApprovalStatus.OPEN
    assert [d["state"] for d in of_kind(mgr, "user_question")] == ["open", "answered"]


def test_b8_the_summary_of_a_phases_bar_takes_the_scores_of_the_passes() -> None:
    """`passes` replace `attempts` for phases also in the collapsed summary (W4b-07.1)."""
    from arr.service.api_models import PassInfo, StageInfo

    done = [
        StageInfo(
            key=n,
            index=i,
            label=n,
            status=StageStatus.DONE,
            passes=[PassInfo(n=1, value="pass", score=8.5, pass_score=7, passed=True)],
        )
        for i, n in enumerate(["a", "b"], start=1)
    ]
    running = StageInfo(key="c", index=3, label="c", status=StageStatus.RUNNING)
    html = stage_bar_html([*done, running], "phases")
    assert "1–2 erledigt" in html and "D1 8,5" in html
