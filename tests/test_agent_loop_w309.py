"""W3-09 Agent-Loop Feinschliff (docs/09 L13–L16, Nachtest V2, V5, V6, V7).

Units first (`@user` as an address, questions to the user in the room and the inbox,
`until_result_from` in the room, the prompts, the limit display), then whole discussions with
FakeBackend agents: run `…_7beb` replayed (V6), the end without an unread direct message
(L15), waiting for a partial result (L14), a question to the user and its answer (L16).
The hang cases of L13–L16 are in `test_agent_loop_hangs.py`.
"""

import json
import time
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from mcp.client.client import Client
from run_kit import (
    done,
    eventually,
    log_entries,
    make_workfolder,
    post,
    room,
    say,
    turns,
    wait,
)
from test_agent_loop import IDLE, discuss, turn_starts

from arr.models import AgentStatus, GroupChatConfig, ResolvedRun, WorkspaceMode
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.policy import ApprovalLog, PolicyEngine
from arr.permissions.types import (
    ApprovalStatus,
    ApprovalType,
    DecidedBy,
    Decision,
    DecisionAction,
)
from arr.room import waiting
from arr.room.address import Address, address, is_user_question, mentions
from arr.room.server import build_server, render_message
from arr.room.store import USER, RoomStore
from arr.room.types import MessageKind, MessageVia, RoomError, WaitKind
from arr.runs import pacing
from arr.runs.context import DiscussionEnd
from arr.runs.eventlog import render_transcript
from arr.runs.manager import QUESTION_CLOSED
from arr.runs.orchestrator import Orchestrator
from arr.runs.pacing import is_question_to, render_turn_prompt
from arr.runs.prepare import _research_block
from arr.service.api_models import AgentWait, WaitReason
from arr.service.real_api.rundir import agent_wait, flow_view
from arr.service.real_api.transcript import to_event

D1, D2, D3 = "diskutant-1", "diskutant-2", "diskutant-3"


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


@pytest.fixture
def store(tmp_path: Path) -> RoomStore:
    run = tmp_path / "run"
    for agent_id in ("a", "b", "c"):
        (run / "agents" / agent_id / "work").mkdir(parents=True)
    store = RoomStore.create(run)
    store.setup_channels([GroupChatConfig(name="main", members=["a", "b", "c"])])
    return store


def wakes(manager: Any) -> list[tuple[str, list[str]]]:
    entries = log_entries(manager.run_dir, "wake")
    return [(e["data"]["agent"], e["data"]["reasons"]) for e in entries]


def prompt_text(name: str) -> str:
    return resources.files("arr").joinpath("templates", "prompts", name).read_text("utf-8")


# --------------------------------------------------------------------------------------------
# L16: `@user` is a fixed address
# --------------------------------------------------------------------------------------------


def test_at_user_is_the_user_also_without_being_a_member() -> None:
    def resolve(name: str) -> str | None:
        return name if name in ("a", "b") else None

    assert mentions("@user: welches Band?", ["a", "b"], "a", resolve) == [USER]
    assert mentions("@User und @b", ["a", "b"], "a", resolve) == [USER, "b"]
    assert mentions("@user, siehe oben", ["a", "b"], USER, resolve) == [], "not the sender"


def test_a3_run_7beb_a_question_to_the_user_never_goes_to_an_agent(store: RoomStore) -> None:
    """Nachtest V7: „@user: …?“ with `@diskutant-2` in the same text went to diskutant-2."""
    a, b = store.for_agent("a"), store.for_agent("b")
    asked = a.post_message(
        "main",
        "@user: Welches Höhenband hat Vorrang? Aufteilung: ich messe E_P, @b sucht C_P.",
        MessageKind.QUESTION,
    )
    assert asked.to == [USER] and asked.open and is_user_question(asked)
    assert address(asked, "b", lambda _i: "a") is None
    assert b.inbox().open_questions == [] and not is_question_to(asked, "b")
    explicit = a.post_message("main", "Frage an dich", MessageKind.QUESTION, to=["user"])
    assert explicit.to == [USER]
    mixed = a.post_message("main", "@user: Stand. @b: magst du ergänzen?", to=["b"])
    assert mixed.to == ["b", USER] and not is_user_question(mixed), "a note goes to both"
    assert address(mixed, "b", lambda _i: "a") is Address.MENTION
    both = a.post_message("main", "@user @b was meint ihr?", MessageKind.QUESTION, to=["b"])
    assert is_user_question(both) and address(both, "b", lambda _i: "a") is Address.MENTION
    assert "Frage an den User" in render_message(both)
    # only the user answers a question to the user
    b.post_message("main", "Ich antworte für den User.", MessageKind.ANSWER, reply_to=both.id)
    assert store.message(both.id).open, "an agent does not close it"
    store.post("main", "Band 10–35.", sender="user", kind=MessageKind.ANSWER, reply_to=both.id,
               via=MessageVia.GUI)  # fmt: skip
    assert not store.message(both.id).open


def test_a3_the_user_answers_in_the_channel_and_the_asker_gets_it(store: RoomStore) -> None:
    """A message of the user without addressee goes to who asked the user and closes it."""
    a = store.for_agent("a")
    asked = a.post_message("main", "@user welches Band?", MessageKind.QUESTION)
    other = store.for_agent("b").post_message("main", "@user und du?", MessageKind.QUESTION)
    answer = store.post("main", "Beide Bänder.", sender="user", via=MessageVia.GUI)
    assert answer.to == ["a", "b"] and answer.reply_to is None
    assert store.open_user_questions() == []
    assert [m.id for m in a.messages_for_me()][-1] == answer.id, "it concerns the asker"
    later = a.post_message("main", "@user noch eine?", MessageKind.QUESTION)
    store.post("main", "@c bitte du", sender="user", via=MessageVia.GUI)
    assert store.message(later.id).open, "a message to someone else answers nothing"
    assert {asked.id, other.id} & {q.id for q in store.open_user_questions()} == set()


def test_a3_question_items_in_the_inbox(tmp_path: Path) -> None:
    log = ApprovalLog(tmp_path / "approvals.jsonl")
    inbox = ApprovalInbox(PolicyEngine([], log=log))
    item = inbox.open_question("r1", "a", "main", 7, "Welches Band?")
    assert item.type is ApprovalType.QUESTION and item.deadline is None
    assert item.question is not None and item.question.text == "Welches Band?"
    assert inbox.pending() == [item]
    with pytest.raises(Exception, match="mit einem Text"):
        inbox.resolve(item.id, Decision(action=DecisionAction.DENY, decided_by=DecidedBy.USER))
    answer = Decision(action=DecisionAction.ANSWER, decided_by=DecidedBy.USER, comment="10–35")
    decided = inbox.resolve(item.id, answer)
    assert decided.status is ApprovalStatus.DECIDED and decided.decision.comment == "10–35"
    assert not inbox.answered(item.id, "zu spät"), "answered once"
    second = inbox.open_question("r1", "a", "main", 8, "Und?")
    assert inbox.answered(second.id, "Im Kanal: ja")
    third = inbox.open_question("r1", "b", "main", 9, "Noch?")
    assert inbox.close_question(third.id, QUESTION_CLOSED)
    assert inbox.get(third.id).status is ApprovalStatus.OBSOLETE
    lines = [json.loads(line) for line in log.path.read_text("utf-8").splitlines()]
    opened = [e for e in lines if e["event"] == "open"]
    assert [e["question"]["msg_id"] for e in opened] == [7, 8, 9]
    assert [e["event"] for e in lines if e["item_id"] == third.id] == ["open", "obsolete"]


# --------------------------------------------------------------------------------------------
# L14: `until_result_from` in the room
# --------------------------------------------------------------------------------------------


def test_a2_until_result_from_is_checked_with_a_clear_message(store: RoomStore) -> None:
    a = store.for_agent("a")
    state = a.wait_until(5, until_result_from="@b")
    assert state.result_from == "b" and state.kind is WaitKind.UNTIL
    assert store.waits(WaitKind.UNTIL)["a"].result_from == "b"
    with pytest.raises(RoomError, match="„zz“ gibt es in diesem Run nicht.*von b, c warten"):
        a.wait_until(5, until_result_from="zz")
    with pytest.raises(RoomError, match="eigenes Ergebnis"):
        a.wait_until(5, until_result_from="a")
    store.set_agent_status("c", AgentStatus.DONE)
    with pytest.raises(RoomError, match="„c“ ist schon fertig.*@c"):
        a.wait_until(5, until_result_from="c")
    assert a.wait_until(5).result_from is None


async def test_a2_wait_until_tool_takes_until_result_from(
    store: RoomStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(waiting.PROVIDER_ENV, "codex")
    async with Client(build_server(store.for_agent("a"))) as client:
        result = await client.call_tool("wait_until", {"minutes": 2, "until_result_from": "b"})
        text = result.content[0].text  # type: ignore[union-attr]
        assert "sobald eine Nachricht an dich kommt oder b ein Ergebnis abgibt" in text
        bad = await client.call_tool("wait_until", {"minutes": 2, "until_result_from": "x"})
        assert bad.is_error and "gibt es in diesem Run nicht" in bad.content[0].text  # type: ignore[union-attr]


def test_a2_the_wait_for_a_result_in_log_and_api() -> None:
    entries = [
        {"ts": 1.0, "kind": "turn", "data": {"agent": D1}},
        {"ts": 2.0, "kind": "waiting", "data": {"agent": D1, "reason": "result_from",
                                                 "result_from": D2, "until": 1802.0}},
        {"ts": 2.0, "kind": "turn_end", "data": {"agent": D1}},
    ]  # fmt: skip
    view = flow_view(entries)
    record: Any = type("R", (), {"final": False})()
    wait = agent_wait(record, D1, AgentStatus.WAITING, view, {})
    assert wait == AgentWait(
        reason=WaitReason.RESULT,
        text=f"wartet auf Ergebnis von {D2}",
        until=1802.0,
        result_from=D2,
    )


# --------------------------------------------------------------------------------------------
# A5: the prompts explain roles, parts, `@name`, `@user`, done and waiting (09 §4.7, V2)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["claude.md", "codex.md"])
def test_a5_fixed_protocol_explains_addresses_done_and_waiting(name: str) -> None:
    text = prompt_text(name)
    assert "`@name`" in text and "`@user`" in text and "nur er antwortet" in text
    assert "ansprechbar" in text and "Jede Nachricht an dich weckt dich" in text
    assert 'until_result_from="<name>"' in text and "Teil" in text
    assert "macOS" in text and "`timeout`" in text, "V2: no GNU timeout"
    assert "Rolle" in text and "anderen Agenten" in text


def test_a5_the_research_block_names_own_part_and_the_others(tmp_path: Path) -> None:
    data = {
        "project": "demo",
        "task": "x",
        "topology": {"kind": "waterfall"},
        "limits": {},
        "approvals": {},
        "agents": [
            {"id": "author", "base_id": "author", "provider": "claude", "model": "m",
             "auth": "p", "role": "roles/author.md", "channels": ["main"]},
            {"id": "judge-1", "base_id": "judge", "provider": "codex", "model": "m",
             "auth": "p", "role": "roles/judge.md", "channels": ["main"]},
            {"id": "judge-2", "base_id": "judge", "provider": "codex", "model": "m",
             "auth": "p", "channels": ["blind"]},
        ],
        "groupchats": [
            {"name": "main", "members": ["author", "judge-1"], "mode": "moderated",
             "moderator": "judge-1"},
            {"name": "blind", "members": ["judge-2", "author"], "anonymous": True},
        ],
        "stages": [{"name": "beweis", "agent": "author", "produces": "proof.md"}],
        "raters": ["judge-1", "judge-2"],
    }  # fmt: skip
    resolved = ResolvedRun.model_validate(data)
    judge = _research_block("r1", resolved, resolved.agents[1], WorkspaceMode.COPY)
    assert "Du bist `judge-1` (Rolle: `judge`)." in judge
    assert "- Dein Teil: bewertet Ergebnisse; moderiert #main." in judge
    assert "`author` (Rolle: `author`; Stufe „beweis“, liefert `proof.md`)" in judge
    assert "`judge-2` (Rolle: `judge`; bewertet Ergebnisse)" in judge
    author = _research_block("r1", resolved, resolved.agents[0], WorkspaceMode.COPY)
    assert "judge-2" not in author, "an anonymous channel hides who is behind the alias"
    assert "nur als „Mitglied A/B/…“" in author


# --------------------------------------------------------------------------------------------
# A6 / V5: an allowed rate-limit window is no „Nutzungslimit erreicht“
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "shown"), [("allowed", False), ("allowed_warning", False), ("rejected", True)]
)
def test_a6_v5_rate_limit_only_shown_when_it_holds_up(status: str, shown: bool) -> None:
    line = {"seq": 3, "ts": 10.0, "turn": 1, "kind": "rate_limit",
            "data": {"status": status, "resets_at": 1794150600.0, "utilization": 0.05}}  # fmt: skip
    event = to_event(line, None, run_dir=None, agent_id=D1)
    assert (event is not None) is shown


def test_a6_v5_transcript_md_says_reached_only_when_it_holds_up(tmp_path: Path) -> None:
    def text(status: str) -> str:
        data = {"status": status, "resets_at": 1794150600.0, "utilization": 0.05}
        line = {"seq": 1, "ts": 10.0, "turn": 1, "kind": "rate_limit", "data": data}
        return render_transcript([line])

    allowed = text("allowed")
    assert "erreicht" not in allowed and "Hinweis zum Nutzungslimit: weiter erlaubt" in allowed
    assert "Auslastung 5 %" in allowed and "Fenster bis" in allowed
    assert "Nutzungslimit erreicht (rejected)" in text("rejected")


# --------------------------------------------------------------------------------------------
# A1 / V6: run `…_7beb` replayed – a request to a finished agent wakes it (L13, L15)
# --------------------------------------------------------------------------------------------

ASK_USER = (
    "@user: Welches Höhenband hat Vorrang, t 10–35 oder 35–60? Ohne Antwort nehme ich 10–35. "
    "Aufteilung: ich (diskutant-1) messe E_P, @diskutant-2 sucht den Korrekturfaktor C_P."
)
REQUEST = (
    "@user: Beide Bänder sind untersucht (Ergebnis #4). @diskutant-2: C_P wurde nur für t0=10 "
    "und 35 gemessen – magst du es für t0=40–55 ergänzen?"
)


def run_7beb() -> dict[str, Any]:
    """The four turns of `…_7beb` that matter, in the order of the run (V6, V7)."""
    return {
        D1: turns(
            [post("main", ASK_USER, kind="question"), wait(0.3),
             post("main", "Eigene Messung E_P: Rouché für P≈1000.", kind="result")],
            [wait(0.2), post("main", REQUEST, to=[D2]),
             done("E_P in beiden Bändern gemessen, Ergebnis abgegeben.")],
        ),
        D2: turns(
            [post("main", "Einverstanden, Band 10–35. C_P für t0=10, 35.", kind="result", to=[D1]),
             done("C_P gemessen")],
            [post("main", "C_P für t0=40–55 ergänzt.", kind="result")],
        ),
    }  # fmt: skip


def old_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rules before W3-09: a finished member wakes only for a question, no L15."""
    trigger = pacing._EventDiscussion._trigger

    def before(self: Any, msg: Any, member: str) -> str | None:
        done_now = self.orch.agent_status(member) is AgentStatus.DONE
        if done_now and address(msg, member, self.orch.sender_of) is not Address.QUESTION:
            return None
        return trigger(self, msg, member)

    monkeypatch.setattr(pacing._EventDiscussion, "_trigger", before)
    monkeypatch.setattr(pacing._EventDiscussion, "_wake_for_unread", lambda self: False)


@pytest.mark.parametrize("naive", [False, True], ids=["l13", "naive"])
async def test_a1_run_7beb_the_request_to_finished_diskutant_2_wakes_it(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """V6: diskutant-2 is done; the note „@diskutant-2: … magst du … ergänzen?“ wakes it, it
    answers, then the discussion ends with nothing unread. Counter-check with the old rule:
    `all_done` with the request unread, as in the run."""
    if naive:
        old_rules(monkeypatch)
    manager, outcome, _ = await discuss(workfolder, run_7beb(), IDLE)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    [request] = [m for m in manager.room.messages("main") if "ergänzen?" in m.body]
    assert request.to == [D2, USER]
    later = [t for t in turn_starts(manager.run_dir, D2) if t["ts"] > request.ts]
    if naive:
        assert later == [], "V6: the request was never read"
        return
    assert wakes(manager) == [(D2, ["mention"])], "by the note, never by the question to the user"
    [woken] = later
    assert "Nachrichten an dich (ungelesen)" in woken["data"]["prompt"]
    assert "magst du es für t0=40–55 ergänzen?" in woken["data"]["prompt"]
    assert manager.orchestrator.agent_status(D2) is AgentStatus.DONE, "done again"
    # V7: the question to the user went to the user, as an item „Frage“
    [ask] = [m for m in manager.room.messages("main") if m.kind is MessageKind.QUESTION]
    assert ask.to == [USER]
    [item] = [i for i in manager.inbox.items() if i.type is ApprovalType.QUESTION]
    assert item.question is not None and item.question.msg_id == ask.id
    assert item.status is ApprovalStatus.OBSOLETE, "closed with the end of the run"
    states = [e["data"]["state"] for e in log_entries(manager.run_dir, "user_question")]
    assert states == ["open", "closed"]


@pytest.mark.parametrize("naive", [False, True], ids=["l15", "naive"])
async def test_a1_l15_an_unread_direct_message_wakes_once_before_all_done(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """D2 gets the request as a hint mid-turn, reads nothing and finishes with its result. L15:
    before `all_done` arr wakes it once; afterwards the discussion ends. Counter-check without
    L15: `all_done` with the request unread."""
    if naive:
        monkeypatch.setattr(pacing._EventDiscussion, "_wake_for_unread", lambda self: False)
    scripts = {
        D1: turns([wait(0.2), post("main", "@diskutant-2 bitte noch n = 9 prüfen"), done()]),
        D2: turns([wait(1.0), post("main", "Mein Ergebnis.", kind="result")], [say("gesehen")]),
    }
    manager, outcome, _ = await discuss(workfolder, scripts, IDLE)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    assert [e["data"]["agent"] for e in log_entries(manager.run_dir, "hint")] == [D2]
    scheduled = log_entries(manager.run_dir, "wake_scheduled")
    unread = [e["data"] for e in scheduled if "unread" in e["data"]]
    if naive:
        assert unread == [] and manager.orchestrator.agents[D2].turns_used == 1
        return
    assert [(u["agent"], u["reason"]) for u in unread] == [(D2, "mention")]
    assert manager.orchestrator.agents[D2].turns_used == 2, "woken once, not again"


# --------------------------------------------------------------------------------------------
# A2 / L14: waiting for the result of another agent
# --------------------------------------------------------------------------------------------


async def test_a2_until_result_from_wakes_at_the_result(workfolder: Path) -> None:
    submit = room("submit_result", kind="lemma", title="Lemma B", body="Beweis durch Induktion.")
    scripts = {
        D1: turns([room("wait_until", minutes=0.5, until_result_from=D2)], [done()]),
        D2: turns([wait(0.6), submit]),
    }
    manager, outcome, took = await discuss(workfolder, scripts, IDLE)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and took < 15, "not after the 30 s"
    [waits] = [
        e["data"]
        for e in log_entries(manager.run_dir, "waiting")
        if e["data"].get("agent") == D1 and e["data"]["reason"] == "result_from"
    ]
    assert waits["result_from"] == D2 and waits["until"] > time.time() - took + 20
    assert wakes(manager) == [(D1, ["result_from"])]
    prompt = turn_starts(manager.run_dir, D1)[1]["data"]["prompt"]
    assert f"{D2} hat ein Ergebnis abgegeben" in prompt and "„Lemma B“" in prompt
    assert "Beweis durch Induktion." in prompt


@pytest.mark.parametrize("how", ["message", "deadline", "result_message", "done_without"])
async def test_a2_until_result_from_also_ends_by_message_deadline_and_done(
    workfolder: Path, how: str
) -> None:
    waits = room("wait_until", minutes=0.5 if how != "deadline" else 0.01, until_result_from=D2)
    other = {
        "message": [wait(0.4), post("main", "@diskutant-1 welche Variante?"), wait(1.0), done()],
        "deadline": [wait(2.0), say("noch nichts")],
        "result_message": [wait(0.4), post("main", "Teil 2 fertig: 42.", kind="result")],
        "done_without": [wait(0.4), done("aufgegeben")],
    }[how]
    scripts = {D1: turns([waits], [done()]), D2: turns(other, [done()])}
    manager, outcome, took = await discuss(workfolder, scripts, IDLE)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and took < 20
    [first] = [e["data"]["reasons"] for e in log_entries(manager.run_dir, "wake")
               if e["data"]["agent"] == D1][:1]  # fmt: skip
    expected = {"message": ["mention"], "deadline": ["timeout"],
                "result_message": ["result_from"], "done_without": ["result_missing"]}  # fmt: skip
    assert first == expected[how]
    if how == "done_without":
        prompt = turn_starts(manager.run_dir, D1)[1]["data"]["prompt"]
        assert f"{D2} ist fertig, ohne ein neues Ergebnis abzugeben" in prompt


# --------------------------------------------------------------------------------------------
# A3 / L16: a question to the user, its answer, and no answer
# --------------------------------------------------------------------------------------------


async def test_a3_the_answer_in_the_channel_reaches_the_waiting_agent(workfolder: Path) -> None:
    async def user(orch: Orchestrator) -> None:
        await eventually(lambda: bool(orch.inbox.pending()), within=10)
        await eventually(lambda: orch.in_turn_wait(D1) is not None, within=10)
        orch.room.post("main", "Band 10–35.", sender="user", via=MessageVia.GUI)

    ask = post("main", "@user Welches Band hat Vorrang?", kind="question")
    scripts = {
        D1: turns([ask, room("wait_for_messages", seconds=20), post("main", "Ok.", kind="result")]),
        D2: turns([post("main", "Mein Teil.", kind="result")]),
    }
    manager, outcome, took = await discuss(workfolder, scripts, IDLE, during=user)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and took < 20
    [item] = manager.inbox.items()
    assert item.type is ApprovalType.QUESTION and item.agent_id == D1
    assert item.status is ApprovalStatus.DECIDED and item.decision.comment == "Band 10–35."
    results = [e for e in events_of(manager, D1) if e["kind"] == "tool_result"]
    ended = [json.loads(e["data"]["content"]) for e in results
             if e["data"]["content"].startswith('{"ended_by"')]  # fmt: skip
    assert ended[0]["ended_by"] == "message"
    assert [e["data"]["state"] for e in log_entries(manager.run_dir, "user_question")] == [
        "open",
        "answered",
    ]


async def test_a3_without_an_answer_the_wait_ends_by_l7_and_the_question_with_the_run(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 1.0)
    ask = post("main", "@user Welches Band hat Vorrang?", kind="question")
    loop = [room("wait_for_messages", seconds=0.4) for _ in range(20)]
    scripts = {D1: turns([ask, *loop], [done()]), D2: turns([done()])}
    manager, outcome, _ = await discuss(workfolder, scripts, IDLE)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    assert [e["data"]["reason"] for e in log_entries(manager.run_dir, "end_turn")] == ["nobody"]
    [item] = manager.inbox.items()
    assert item.status is ApprovalStatus.OBSOLETE
    lines = (manager.run_dir / "approvals.jsonl").read_text("utf-8").splitlines()
    [closed] = [e for e in map(json.loads, lines) if e["event"] == "obsolete"]
    assert closed["note"] == QUESTION_CLOSED


def events_of(manager: Any, agent_id: str) -> list[dict[str, Any]]:
    from run_kit import events

    return events(manager.run_dir, agent_id)


def test_render_turn_prompt_shows_direct_messages_in_full() -> None:
    from arr.room.types import Inbox, Message

    note = Message(id=5, ts=0.0, channel="main", sender="b", body="@a magst du ergänzen?", to=["a"])
    text = render_turn_prompt("a", "Los.", Inbox(open_questions=[], unread={"main": 1}), [], [note])
    assert "Nachrichten an dich (ungelesen):" in text and "magst du ergänzen?" in text
    assert text.index("Nachrichten an dich") < text.index("## Auftrag")


def test_direct_messages_for_the_prompt_keep_the_aliases(tmp_path: Path) -> None:
    """In an anonymous channel the prompt shows aliases, never real IDs (W1-03.1)."""
    run = tmp_path / "run"
    for agent_id in ("a", "b"):
        (run / "agents" / agent_id / "work").mkdir(parents=True)
    store = RoomStore.create(run)
    store.setup_channels([GroupChatConfig(name="blind", members=["a", "b"], anonymous=True)])
    sent = store.for_agent("b").post_message("blind", "@A magst du ergänzen?")
    [seen] = store.for_agent("a").messages_for_me()
    assert seen.id == sent.id and seen.sender == "Mitglied B" and seen.to == ["Mitglied A"]
    text = render_turn_prompt("a", "Los.", store.for_agent("a").inbox(), [], [seen])
    assert "Mitglied B" in text and "[#2 b " not in text
