"""W4b-06b §6.3, §6.4, §6.8: one version of the texts, NR2, the end of a phases run's output,
the line separator of the logs and the wording „Phase“ in phases runs.

FakeBackend agents with the real RunManager, Orchestrator and room where a run is needed; no
model, no network. The texts are checked word for word.
"""

import inspect
import json
import time
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from phases_kit import TurnView, errors, hand_in, manager, of_kind, private, run, run_with, solo
from project_context_kit import MAIN, start_skill_contexts
from run_kit import eventually, make_workfolder, room
from test_inbox import engine

from arr.models import RunStatus
from arr.room import server, store
from arr.room.store import USER
from arr.room.types import MessageKind, MessageVia
from arr.room.waiting import WaitEnd, WaitOutcome
from arr.runs import pacing, prepare
from arr.runs.finalize import _read_log, finalize_run
from arr.runs.inspect import describe_checkpoint
from arr.runs.launch import END_TURN_TEXT, describe_entry
from arr.runs.orchestrator import ORCHESTRATOR_LOG, OrchestratorLog

__all__ = ["engine"]

PRIVATE_WAIT = "eine Nachricht des Users in deinem privaten Kanal beendet das Warten"
"""NR2 (W4b-04, checked in W4b-06b): every message of the user in the private channel ends
`wait_until`; the texts say so instead of „Antwort oder Notiz“."""


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


# --------------------------------------------------------------------------------------------
# §6.3: the project context of a start skill is the one of `main`
# --------------------------------------------------------------------------------------------


def test_the_project_context_of_every_start_skill_is_that_of_main(tmp_path: Path) -> None:
    """`_parts` lost the parts of the old topologies, `_research_block` names `submit` without
    a choice: the context of all 15 agents of the four start skills stays character for
    character (`main` at `9a7fccc`, written by `project_context_kit`)."""
    want = json.loads(MAIN.read_text("utf-8"))
    found = start_skill_contexts(tmp_path)
    assert found.keys() == want.keys() and len(found) == 15
    for key in want:
        assert found[key] == want[key], key
    assert "Dein Teil: fasst die Diskussion zusammen." in found["roundtable/summarizer"]
    assert all("Ergebnisse gibst du mit `submit` ab." in text for text in found.values())


def test_the_parts_of_an_agent_are_only_summarizing() -> None:
    from types import SimpleNamespace

    resolved: Any = SimpleNamespace(params={"summarizer": "s"})
    assert prepare._parts(resolved, "s") == ["fasst die Diskussion zusammen"]
    assert prepare._parts(resolved, "t") == []


# --------------------------------------------------------------------------------------------
# §6.4: one version of every text
# --------------------------------------------------------------------------------------------

RULE_4 = (
    "Wer jemanden fragt, schreibt `@name` (oder `to`); `@alle` fragt alle, `@user` den User "
    "(nur er antwortet darauf). Auf einen Agenten wartest du mit `wait_for_messages`. Brauchst "
    "du die Antwort des Users, warte mit `wait_until` bis zu fünf Minuten; seine Antwort und "
    "jede Nachricht des Users in deinem privaten Kanal beenden das Warten. Antwortet er nicht, "
    "arbeite mit einer begründeten Annahme weiter und nenne sie in deiner Abgabe. Wer abgibt, "
    "ist fertig; seine offene Frage gilt damit als erledigt."
)
RULE_8 = (
    "Wer seinen Teil erledigt hat, gibt ab (`submit`). Fertig heißt: Du wartest nicht mehr, "
    "bleibst aber ansprechbar. Wartest du auf die Abgabe eines anderen, warte mit "
    '`wait_until(minutes, until_result_from="<name>")`.'
)


def test_one_set_of_conversation_rules_in_every_brief(tmp_path: Path) -> None:
    from arr.models import GroupChatConfig
    from arr.room.store import RoomStore

    assert not hasattr(store, "PHASES_CONVERSATION_RULES")
    assert not hasattr(store.RoomStore, "uses_phases")
    rules = store.CONVERSATION_RULES
    assert len(rules) == 8 and rules[3] == RULE_4 and rules[7] == RULE_8
    assert not [r for r in rules if "submit_result" in r or "nie mit `wait_until`" in r]
    room_ = RoomStore.create(tmp_path / "run")  # no `run.yaml`: the same rules
    [brief] = room_.setup_channels([GroupChatConfig(name="k", members=["a", "b"])])
    assert f"4. {RULE_4}" in brief.body and f"8. {RULE_8}" in brief.body
    assert set(store._MODE_TEXT) == set(store._END_TEXT) == {"rounds", "event"}


def test_the_private_brief_says_every_message_ends_the_wait() -> None:
    assert store.PRIVATE_BRIEF == (
        "Steckbrief #{name}\n"
        "Dein privater Kanal zum User: Hier fragst du ihn mit einer `question` an `@user`, in "
        "jeder Phase, auch in einer isolierten. Nur der User liest diesen Kanal; kein anderer "
        "Agent sieht ihn. Brauchst du die Antwort, warte mit `wait_until` bis zu fünf Minuten; "
        "eine Nachricht des Users in deinem privaten Kanal beendet das Warten. Gibst du ab, gilt "
        "deine Frage als erledigt. Die Annahme, mit der du ohne Antwort weiterarbeitest, nennst "
        "du in deiner Abgabe."
    )


def test_one_version_of_the_wait_texts() -> None:
    assert server.USER_MAY_NOT_ANSWER == (
        "Deine Frage an den User ist noch offen, und er antwortet vielleicht lange nicht. "
        "Brauchst du die Antwort, warte einmal mit `wait_until` bis zu fünf Minuten; eine "
        "Antwort des Users oder eine Nachricht von ihm in deinem privaten Kanal beendet das "
        "Warten. Antwortet er bis dahin nicht, arbeite mit einer begründeten Annahme weiter und "
        "nenne sie in deiner Abgabe."
    )
    assert server.USER_WAITED_LONG == (
        "Beachte: Deine Frage an den User ist noch offen. Warte auf ihn höchstens fünf Minuten "
        "und nur einmal: Antwortet er bis dahin nicht, arbeite mit einer begründeten Annahme "
        "weiter und nenne sie in deiner Abgabe. Gibst du ab, gilt deine Frage als erledigt."
    )
    assert not hasattr(server, "USER_MAY_NOT_ANSWER_PHASES")
    assert not hasattr(server, "USER_WAITED_LONG_PHASES")
    assert "phases" not in inspect.signature(server.render_wait).parameters
    assert "phases" not in inspect.signature(server.render_wait_until).parameters
    nobody = WaitOutcome(
        ended_by=WaitEnd.NOBODY, waited_s=300, remaining_s=0, asked_s=45, limit_s=45
    )
    assert server.USER_MAY_NOT_ANSWER in server.render_wait(nobody, asked_user=True)
    text = server.render_wait_until(9_999_999_999, None, asked_user=True)
    assert text.endswith(" " + server.USER_WAITED_LONG)


def test_one_default_role() -> None:
    assert prepare.DEFAULT_ROLE == (
        "Du bist ein Research-Agent in diesem Run. Für dich ist keine Rolle festgelegt: "
        "Bearbeite deinen Auftrag selbstständig und gib mit `submit` ab.\n"
    )
    assert not hasattr(prepare, "DEFAULT_ROLE_PHASES") and not hasattr(prepare, "default_role")


def prompt(name: str) -> str:
    return resources.files("arr.templates").joinpath(f"prompts/{name}.md").read_text("utf-8")


def bullet(text: str, head: str) -> str:
    [line] = [x for x in text.splitlines() if x.startswith(f"- **{head}:**")]
    return line


@pytest.mark.parametrize("name", ["claude", "codex"])
def test_the_prompts_have_one_version(name: str) -> None:
    text = prompt(name)
    for old in ("ohne Phasen", "mit Phasen", "submit_result", "Notiz des Users", "oder Notiz"):
        assert old not in text, old
    assert bullet(text, "Ergebnisse und fertig") == (
        "- **Ergebnisse und fertig:** Dein Ergebnis gibst du mit `submit` ab. Es zählt nur, was "
        "dort ankommt. Mit deinem Teil bist du fertig, sobald du abgegeben hast, und bleibst "
        "ansprechbar; eine offene Frage an den User gilt mit der Abgabe als erledigt. In einer "
        "Diskussion `chat` bist du fertig, sobald du abgegeben hast und keinen anderen "
        "Diskutanten mehr fragst."
    )
    assert PRIVATE_WAIT in bullet(text, "Dein Kanal zum User")
    assert "oder eine Nachricht des Users in deinem privaten Kanal. Antworte" in bullet(
        text, "Phasen"
    )
    assert bullet(text, "Ansprechen und warten").endswith(
        "Auf den User wartest du wie unter „Dein Kanal zum User“."
    )


def test_the_same_statements_are_the_same_in_both_prompts() -> None:
    claude, codex = prompt("claude"), prompt("codex")
    for head in ("Ergebnisse und fertig", "Phasen", "Dein Kanal zum User"):
        assert bullet(claude, head) == bullet(codex, head), head


def test_the_prompts_of_the_old_topologies_are_gone() -> None:
    folder = resources.files("arr.templates").joinpath("prompts")
    names = {p.name for p in folder.iterdir()}
    assert not {"review_judge.md", "exploration_explorer.md"} & names


def test_the_discussion_texts_name_submit() -> None:
    assert pacing._text("round").endswith("oder melde `done` mit set_status.")
    assert all("`submit`" in pacing._text(key) for key in pacing._SUBMIT_TEXTS)


# --------------------------------------------------------------------------------------------
# NR2: every message of the user in the private channel ends `wait_until`
# --------------------------------------------------------------------------------------------


def asks_then_waits(view: TurnView) -> list[dict[str, Any]]:
    """Turn 1: a question to the user in the private channel, then `wait_until`; next turn
    hands in."""
    if "# Auftrag – Phase „p“" in view.prompt:
        ask = {"channel": private("a"), "body": "@user Ist n prim?", "kind": "question"}
        return [room("post_message", **ask), room("wait_until", minutes=20)]
    return hand_in(view)


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        (MessageKind.NOTE, "Nur eine Notiz."),
        (MessageKind.QUESTION, "Welches n meinst du?"),
        (MessageKind.ANSWER, "Ja."),
        (MessageKind.NOTE, "@a Weiter."),
    ],
    ids=["note", "question-without-name", "answer", "note-with-name"],
)
async def test_nr2_every_message_of_the_user_in_the_private_channel_ends_wait_until(
    workfolder: Path, kind: MessageKind, body: str
) -> None:
    """The wait (20 min) ends with the message: the next turn starts within the test's 30 s,
    and it shows the message. Without a message nothing would end it before 20 min."""
    found: dict[str, Any] = {}

    async def helper(mgr: Any) -> None:
        await eventually(lambda: bool(of_kind(mgr, "waiting", agent="a", reason="wait_until")), 30)
        [question] = [m for m in mgr.room.messages(private("a")) if m.sender == "a"]
        reply = question.id if kind is MessageKind.ANSWER else None
        mgr.room.post(
            private("a"), body, sender=USER, kind=kind, reply_to=reply, via=MessageVia.GUI
        )
        found["posted_at"] = time.time()
        await eventually(lambda: len(of_kind(mgr, "turn", agent="a")) >= 2, 30)

    mgr, log = manager(
        workfolder, {"agents": [{"id": "a"}], "phases": [solo("p", "a")]}, {"a": asks_then_waits}
    )
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    first, second = log.of("a")
    assert body in second.prompt
    [wait] = of_kind(mgr, "waiting", agent="a", reason="wait_until")
    assert wait["until"] - found["posted_at"] > 600, (
        "the deadline was far away: the message ended it"
    )
    assert len(of_kind(mgr, "turn", agent="a")) == 2


# --------------------------------------------------------------------------------------------
# §6.8: escalation note, END_TURN_TEXT, the wording „Phase“, the line separator
# --------------------------------------------------------------------------------------------


async def test_the_inbox_note_of_an_escalation_is_its_summary(engine: Any) -> None:
    """Every escalation is one of a phase, its summary begins with „Phase „x“, Durchlauf n“:
    the note of the inbox is „Eskalation: <summary>“, no „Eskalation in Stufe“ (§6.8)."""
    import asyncio

    from arr.permissions.inbox import ApprovalInbox
    from arr.permissions.types import EscalationOption

    inbox = ApprovalInbox(engine)
    summary = "Phase „p“, Durchlauf 2. Gate „user“: Du entscheidest, wie es weitergeht."
    task = asyncio.create_task(
        inbox.ask_escalation("r1", "p", summary, [EscalationOption.CONTINUE])
    )
    await eventually(lambda: bool(inbox.pending()), 10)
    [opened] = [x for x in engine.log.item_lines() if x.get("event") == "open"]
    assert opened["note"] == f"Eskalation: {summary}"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_end_turn_text_knows_assignment_cancelled() -> None:
    assert END_TURN_TEXT["assignment_cancelled"] == "sein Auftrag wurde abgebrochen"
    entry = {"kind": "end_turn", "data": {"agent": "w-1", "reason": "assignment_cancelled"}}
    assert describe_entry(entry) == "⏹ arr beendet den Turn von w-1: sein Auftrag wurde abgebrochen"


@pytest.mark.parametrize(("phases", "word"), [(True, "Phase"), (False, "Stufe")])
def test_phase_or_stage_in_the_log_lines(phases: bool, word: str) -> None:
    """A phases run says „Phase“, an old run keeps „Stufe“ word for word (A12)."""
    opened = {"kind": "channel_open", "data": {"channel": "runde", "stage": "diskussion"}}
    assert describe_entry(opened, phases=phases) == f"Kanal #runde geöffnet ({word} „diskussion“)"
    data = {"stage": "diskussion", "channel": "runde", "phase": "discussion"}
    assert describe_checkpoint(data, phases=phases) == f"{word} „diskussion“: Diskussion in #runde"
    checkpoint = {"kind": "checkpoint", "data": data}
    assert describe_entry(checkpoint, phases=phases) == (
        f"● {word} „diskussion“: Diskussion in #runde"
    )


@pytest.mark.parametrize("char", [" ", " ", "\u0085"])
def test_the_logs_split_only_at_newline(tmp_path: Path, char: str) -> None:
    """`write` keeps U+2028, U+2029, U+0085 inside a value (`ensure_ascii=False`);
    `OrchestratorLog.entries()` and `finalize._read_log` split only at `"\\n"` (A14)."""
    log = OrchestratorLog(tmp_path)
    log.write("warning", message=f"vor{char}nach")
    log.write("warning", message="zweite")
    assert char in (tmp_path / ORCHESTRATOR_LOG).read_text("utf-8")
    assert [e["data"]["message"] for e in log.entries()] == [f"vor{char}nach", "zweite"]
    assert [e["data"]["message"] for e in _read_log(tmp_path)] == [f"vor{char}nach", "zweite"]


# --------------------------------------------------------------------------------------------
# §6.3 finalize: a phases run writes no results.json, ratings.json, „## Ergebnisse“
# --------------------------------------------------------------------------------------------


async def test_a_start_skill_run_has_no_old_results_in_its_output(workfolder: Path) -> None:
    from templates_kit import CASES, run_new

    case = next(c for c in CASES if c.template == "roundtable")
    mgr, _flow = await run_new(workfolder, case)
    assert errors(mgr) == []
    output = mgr.run_dir / "output"
    assert not (output / "results.json").exists() and not (output / "ratings.json").exists()
    summary = (output / "summary.md").read_text("utf-8")
    assert "## Ergebnisse" not in summary and "Keine Ergebnisse abgegeben." not in summary
    assert "## Abgaben" in summary and (output / "submissions.json").is_file()


async def test_a_run_without_submissions_still_writes_submissions_json(workfolder: Path) -> None:
    def silent(view: TurnView) -> list[dict[str, Any]]:
        return []

    mgr, _ = manager(
        workfolder, {"agents": [{"id": "a"}], "phases": [solo("p", "a")]}, {"a": silent}
    )
    await run(mgr)
    await finalize_run(mgr.prepared)  # the managers of `phases_kit` have no finalizer
    output = mgr.run_dir / "output"
    data = json.loads((output / "submissions.json").read_text("utf-8"))
    assert data["submissions"] == []
    assert not (output / "results.json").exists()
