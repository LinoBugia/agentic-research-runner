"""W3-09 Agent-Loop Feinschliff (docs/09 L13–L16, Nachtest V2, V5, V6, V7).

Units first (`@user` as an address, questions to the user in the room and the inbox,
`until_result_from` in the room, the prompts, the limit display), then whole discussions with
FakeBackend agents: run `…_7beb` replayed (V6), the end without an unread direct message
(L15), waiting for a partial result (L14), a question to the user and its answer (L16).
The hang cases of L13–L16 are in `test_agent_loop_hangs.py`.
"""

import asyncio
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

from arr.models import AgentStatus, GroupChatConfig, ResolvedRun, RunStatus, WorkspaceMode
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
from arr.runs.orchestrator import Orchestrator, StopReason
from arr.runs.pacing import PacingSettings, is_question_to, render_turn_prompt
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
    """Entscheidung 1 (User 08.11., W3-09.1): only `reply_to` on the question or `to`/`@name`
    of the asker answer it; a message to nobody answers nothing and goes to nobody."""
    a = store.for_agent("a")
    asked = a.post_message("main", "@user welches Band?", MessageKind.QUESTION)
    other = store.for_agent("b").post_message("main", "@user und du?", MessageKind.QUESTION)
    note = store.post("main", "Eine Notiz für alle.", sender="user", via=MessageVia.GUI)
    assert note.to == [] and note.reply_to is None
    assert {q.id for q in store.open_user_questions()} == {asked.id, other.id}
    assert note.id not in [m.id for m in a.messages_for_me()], "it does not concern the asker"
    by_name = store.post("main", "@a Band 10–35.", sender="user", via=MessageVia.GUI)
    assert by_name.to == ["a"] and not store.message(asked.id).open
    assert [m.id for m in a.messages_for_me()][-1] == by_name.id
    store.post("main", "Ja.", sender="user", reply_to=other.id, via=MessageVia.GUI)
    assert store.open_user_questions() == [], "reply_to on the question answers it"
    later = a.post_message("main", "@user noch eine?", MessageKind.QUESTION)
    store.post("main", "@c bitte du", sender="user", via=MessageVia.GUI)
    assert store.message(later.id).open, "a message to someone else answers nothing"


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
    ("status", "shown"), [("allowed", False), ("allowed_warning", True), ("rejected", True)]
)
def test_a6_v5_rate_limit_only_shown_when_it_holds_up(status: str, shown: bool) -> None:
    """`allowed` changes nothing and is not served; `allowed_warning` throttles the profile and
    is served for a neutral hint (W3-09.1, B10); „erreicht“ only for `rejected`."""
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
    """The turns of `…_7beb` that matter, in the order of the run (V6, V7).

    diskutant-1 gives no result in its first turn: in the run its open question to the user
    kept it idle; since W3-09.1 a result would make it done (Entscheidung 2), so the replay
    keeps it idle the plain way, and its next turn is the round with the request.
    """
    return {
        D1: turns(
            [post("main", ASK_USER, kind="question"), wait(0.3), say("Messung E_P läuft.")],
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
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 1.0)  # B3: the open question holds ≤ L7
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
        orch.room.post("main", "@diskutant-1 Band 10–35.", sender="user", via=MessageVia.GUI)

    ask = post("main", "@user Welches Band hat Vorrang?", kind="question")
    scripts = {
        D1: turns([ask, room("wait_for_messages", seconds=20), post("main", "Ok.", kind="result")]),
        D2: turns([post("main", "Mein Teil.", kind="result")]),
    }
    manager, outcome, took = await discuss(workfolder, scripts, IDLE, during=user)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and took < 20
    [item] = manager.inbox.items()
    assert item.type is ApprovalType.QUESTION and item.agent_id == D1
    assert item.status is ApprovalStatus.DECIDED
    assert item.decision.comment == "@diskutant-1 Band 10–35."
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


# ============================================================================================
# W3-09.1 Nacharbeit (Review B1–B6, B9, B11, Entscheidungen des Users vom 08.11.)
# ============================================================================================


def prompts_of(manager: Any, agent_id: str) -> list[str]:
    return [t["data"]["prompt"] for t in turn_starts(manager.run_dir, agent_id)]


async def discuss_two_channels(
    workfolder: Path, scripts: dict[str, Any], side: list[str]
) -> tuple[Any, Any]:
    """A discussion in #main (D1, D2) with a second channel #side of `side` members."""
    from run_kit import channel, make_manager

    results: list[Any] = []

    async def topology(ctx: Any) -> None:
        results.append(await ctx.discuss("main", "Löst die Aufgabe."))

    manager = make_manager(
        workfolder,
        topology,
        scripts,
        groupchats=[channel("main", [D1, D2], mode="event", **IDLE), channel("side", side)],
        run_extra={"limits": {"wall_time": "30s"}},
    )
    await manager.run()
    return manager, results[0]


# --- Punkt 1 (B1): a result from a channel the waiter cannot read shows no text ----------


@pytest.mark.parametrize("naive", [False, True], ids=["b1", "naive"])
async def test_w3_09_1_b1_a_result_in_a_foreign_channel_wakes_without_its_text(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """S5: D2 posts its `result` in #side without D1. D1 is woken, its prompt says only that
    a result came. Counter-check: if every channel counted as readable, the text leaks."""
    from arr.room.store import AgentRoom

    if naive:
        monkeypatch.setattr(AgentRoom, "can_read", lambda self, channel: True)
    secret = "NUR-IN-SIDE-4711"
    scripts = {
        D1: turns([room("wait_until", minutes=0.5, until_result_from=D2)], [done()]),
        D2: turns([wait(0.5), post("side", f"Teilergebnis: {secret}", kind="result"), done()]),
        D3: turns([say("ruhig")]),
    }
    manager, outcome = await discuss_two_channels(workfolder, scripts, [D2, D3])
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    assert wakes(manager)[0] == (D1, ["result_from"])
    woken = prompts_of(manager, D1)[1]
    assert f"{D2} hat ein Ergebnis abgegeben" in woken
    assert (secret in woken) is naive, "text_leaked"


# --- Punkt 2 (B2): names in the wake-up prompt as the waiting agent sees them ------------


@pytest.mark.parametrize("naive", [False, True], ids=["b2", "naive"])
async def test_w3_09_1_b2_wake_prompt_in_an_anonymous_channel_keeps_the_alias(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """S5b: D1 waits for „Mitglied B“; the wake-up prompt names „Mitglied B“, never the real
    ID, also not in the path of the result file. Counter-check: with real IDs it leaks."""
    from arr.room.store import AgentRoom

    if naive:
        monkeypatch.setattr(AgentRoom, "shown_name", lambda self, agent: agent)
    (workfolder / "projects" / "demo" / "lemma.md").write_text("Beweis.\n", "utf-8")
    scripts = {
        D1: turns([room("wait_until", minutes=0.5, until_result_from="Mitglied B")], [done()]),
        D2: turns([wait(0.5), room("submit_result", kind="lemma", title="Lemma",
                                   path="lemma.md")]),
    }  # fmt: skip
    manager, outcome, _ = await discuss(workfolder, scripts, {**IDLE, "anonymous": True})
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    woken = prompts_of(manager, D1)[1]
    assert (D2 in woken) is naive, "real_id_in_prompt"
    if not naive:
        assert "Mitglied B hat ein Ergebnis abgegeben" in woken and "(als Datei)" in woken


def test_w3_09_1_b2_m20_b5_the_real_id_of_a_hidden_member_is_refused(tmp_path: Path) -> None:
    """M20: `until_result_from` with the real ID behind an alias is refused without
    confirming it; B5: the offer names neither the hidden ID nor the own alias; M55: an agent
    without a shared channel counts by its ID."""
    run = tmp_path / "run"
    for agent_id in (D1, D2, D3):
        (run / "agents" / agent_id / "work").mkdir(parents=True)
    store = RoomStore.create(run)
    store.setup_channels(
        [
            GroupChatConfig(name="blind", members=[D1, D2], anonymous=True),
            GroupChatConfig(name="side", members=[D2, D3]),
        ]
    )
    d1 = store.for_agent(D1)
    assert d1.wait_until(1, until_result_from="Mitglied B").result_from == D2
    with pytest.raises(RoomError) as info:
        d1.wait_until(1, until_result_from=D2)
    text = str(info.value)
    assert "Mitglied B" in text and "Mitglied A" not in text, "B5: not oneself"
    assert text.count(D2) == 1, "only the echo of the name, no confirmation"
    assert d1.wait_until(1, until_result_from=D3).result_from == D3, "M55: no shared channel"
    assert d1.shown_name(D2) == "Mitglied B" and d1.shown_name(D3) == D3


# --- Punkt 3 (B3): a burst of requests to a finished agent wakes it --------------------------


def old_ping_pong(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard before W3-09.1: every message of a pair counts, L15 heeds the guard."""

    def observe(self: Any, msg: Any) -> Any:
        self._senders[msg.id] = msg.sender
        if msg.sender in ("orchestrator", USER):
            return None
        other = self._other(msg)
        if other is None or other == msg.sender:
            return None
        pair = frozenset({msg.sender, other})
        self._counts[pair] = self._counts.get(pair, 0) + 1
        if self._counts[pair] > self.limit and pair not in self.flagged:
            self.flagged.add(pair)
            return pair
        return None

    trigger = pacing._EventDiscussion._trigger
    monkeypatch.setattr(pacing.PingPongTracker, "observe", observe)
    monkeypatch.setattr(
        pacing._EventDiscussion,
        "_trigger",
        lambda self, msg, member, guarded=True: trigger(self, msg, member),
    )


@pytest.mark.parametrize("count", [7, 10])
@pytest.mark.parametrize("naive", [False, True], ids=["b3", "naive"])
async def test_w3_09_1_b3_many_requests_to_a_finished_agent_wake_it(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool, count: int
) -> None:
    """S2b: D1 sends the finished D2 7 or 10 requests in one turn. D2 is woken and every
    request stands in one of its prompts. Counter-check with the old guard: never woken."""
    if naive:
        old_ping_pong(monkeypatch)
    scripts = {
        D1: turns([wait(0.4)] + [post("main", f"@{D2} Bitte {i}") for i in range(count)]
                  + [done()]),
        D2: turns([post("main", "Ergebnis", kind="result")], [say("gesehen")], repeat_last=True),
    }  # fmt: skip
    manager, outcome, _ = await discuss(workfolder, scripts, IDLE)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    turns_d2 = manager.orchestrator.agents[D2].turns_used
    if naive:
        assert turns_d2 == 1 and log_entries(manager.run_dir, "ping_pong")
        return
    assert turns_d2 >= 2 and not log_entries(manager.run_dir, "ping_pong")
    seen = "\n".join(prompts_of(manager, D2)[1:])
    assert all(f"Bitte {i}" in seen for i in range(count)), "every request in a prompt"


def test_w3_09_1_b3_only_an_exchange_counts() -> None:
    """One-sided messages count once; back and forth counts each change; M07: a message to the
    user goes to no agent."""
    from arr.room.types import Message
    from arr.runs.pacing import PingPongTracker

    def m(i: int, sender: str, to: str) -> Message:
        return Message(id=i, ts=0.0, channel="main", sender=sender, body="x", to=[to])

    tracker = PingPongTracker(2, lambda _i: None)
    assert all(tracker.observe(m(i, "a", "b")) is None for i in range(1, 20))
    assert tracker.observe(m(20, "b", "a")) is None
    assert tracker.observe(m(21, "a", "b")) == frozenset({"a", "b"}), "third change"
    assert tracker._other(m(22, "a", USER)) is None, "M07"


async def test_w3_09_1_b3_l15_wakes_a_member_behind_the_guard_once(workfolder: Path) -> None:
    """After a real ping-pong the guard stops automatic wake-ups; before `all_done`, L15 still
    wakes each member once per unread direct message, and the discussion ends."""
    pacing_settings = PacingSettings(poll_interval_s=0.01, ping_pong_limit=2)
    scripts = {
        D1: turns([wait(0.1), post("main", "@diskutant-2 Nein.")],
                  [wait(0.1), post("main", "@diskutant-2 Nein.")],
                  [wait(0.1), post("main", "@diskutant-2 Gut, Schluss."), done()],
                  repeat_last=True),
        D2: turns([say("bereit")], [wait(0.1), post("main", "@diskutant-1 Doch.")],
                  [post("main", "Ergebnis", kind="result")], repeat_last=True),
    }  # fmt: skip
    manager, outcome, took = await discuss(workfolder, scripts, IDLE, pacing=pacing_settings)
    assert outcome.ended_by in (DiscussionEnd.ALL_DONE, DiscussionEnd.ROUNDS) and took < 20
    assert log_entries(manager.run_dir, "ping_pong"), "the guard stepped in"


# --- Punkt 4 (Entscheidung 1): a note to all answers no question to the user ------------------


async def test_w3_09_1_e1_a_note_without_addressee_answers_nothing(workfolder: Path) -> None:
    """S13: the user writes a note to all while a question to the user is open. It stays a
    note to all (`to` empty, no wake-up of the asker), the question stays open. Counter-check:
    the same text with `@name` answers it and wakes the asker."""

    async def user(orch: Orchestrator) -> None:
        await eventually(lambda: bool(orch.inbox.pending()), within=10)
        await eventually(lambda: bool(log_entries(orch.prepared.run_dir, "round")), within=10)
        orch.room.post("main", "Nur eine Notiz für alle.", sender="user", via=MessageVia.GUI)
        await eventually(lambda: orch.agents[D1].turns_used >= 2, within=10)
        orch.room.post("main", "@diskutant-1 Band 10–35.", sender="user", via=MessageVia.GUI)

    ask = post("main", "@user welches Band?", kind="question")
    scripts = {
        D1: turns([ask, say("Ich warte.")], [say("weiter warten")],
                  [post("main", "Danke.", kind="result")], repeat_last=True),
        D2: turns([post("main", "Mein Teil.", kind="result")]),
    }  # fmt: skip
    manager, outcome, _ = await discuss(workfolder, scripts, IDLE, during=user)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    note, answer = [m for m in manager.room.messages("main") if m.sender == USER]
    assert note.to == [] and answer.to == [D1]
    woken = [e["data"] for e in log_entries(manager.run_dir, "wake") if e["data"]["agent"] == D1]
    assert note.id not in [i for w in woken for i in w["messages"]], "the note woke nobody"
    assert any(answer.id in w["messages"] for w in woken), "the answer woke the asker"
    [item] = manager.inbox.items()
    assert item.status is ApprovalStatus.DECIDED and item.decision.comment == answer.body


# --- Punkt 5 (Entscheidung 2): a result while the question is open makes the asker done ------


@pytest.mark.parametrize("naive", [False, True], ids=["e2", "naive"])
async def test_w3_09_1_e2_a_result_closes_the_question_and_makes_the_asker_done(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """S9b: the asker gives its result while its question to the user is open: one turn,
    `done`, the item closes „hat ohne Antwort weitergearbeitet“, a late answer wakes it (L13).
    Counter-check with the rule before: not done, idle rounds until `rounds`."""
    from arr.runs import orchestrator

    if naive:
        monkeypatch.setattr(orchestrator, "is_user_question", lambda msg: False)

    async def user(orch: Orchestrator) -> None:
        await eventually(lambda: orch.agent_status(D1) is AgentStatus.DONE, within=10)
        assert orch.is_busy(D2), "the discussion still runs"
        orch.room.post("main", "@diskutant-1 Band 10–35.", sender="user", via=MessageVia.GUI)

    ask = post("main", "@user welches Band?", kind="question")
    scripts = {
        D1: turns([ask, post("main", "Vorläufig 10–35.", kind="result")], [say("passt")],
                  repeat_last=True),
        D2: turns([wait(2.0), post("main", "Mein Teil.", kind="result")]),
    }  # fmt: skip
    if naive:
        manager, outcome, _ = await discuss(workfolder, scripts, IDLE)
        assert outcome.ended_by is DiscussionEnd.ROUNDS
        assert manager.orchestrator.agents[D1].turns_used > 2, "idle rounds for the asker"
        return
    manager, outcome, _ = await discuss(workfolder, scripts, IDLE, during=user)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    done_by = [e["data"] for e in log_entries(manager.run_dir, "agent_done")]
    assert {"agent": D1, "reason": "result", "turn": 1} in done_by
    [item] = manager.inbox.items()
    assert item.status is ApprovalStatus.OBSOLETE
    lines = (manager.run_dir / "approvals.jsonl").read_text("utf-8").splitlines()
    [closed] = [e for e in map(json.loads, lines) if e["event"] == "obsolete"]
    assert closed["note"] == f"{D1} hat ohne Antwort weitergearbeitet."
    [ask_msg] = [m for m in manager.room.messages("main") if m.kind is MessageKind.QUESTION]
    assert not ask_msg.open, "the question is closed in the room as well"
    woken = [e["data"]["reasons"] for e in log_entries(manager.run_dir, "wake")
             if e["data"]["agent"] == D1]  # fmt: skip
    assert woken == [["mention"]], "the late answer wakes it"
    assert manager.orchestrator.agent_status(D1) is AgentStatus.DONE


async def test_w3_09_1_e2_set_status_done_keeps_the_question_until_the_run_ends(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S20: `set_status("done")` stays as it was: done at once, the question closes only
    with the run („Der Run ist zu Ende …“). Since B3 (W4-06.1) the phase waits for the answer
    up to the L7 deadline first (here shortened to 1 s)."""
    monkeypatch.setattr(waiting, "STREAK_MAX_S", 1.0)
    ask = post("main", "@user welches Band?", kind="question")
    scripts = {D1: turns([ask, done()]), D2: turns([done()])}
    manager, outcome, took = await discuss(workfolder, scripts, IDLE)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and took < 10
    lines = (manager.run_dir / "approvals.jsonl").read_text("utf-8").splitlines()
    [closed] = [e for e in map(json.loads, lines) if e["event"] == "obsolete"]
    assert closed["note"] == QUESTION_CLOSED


# --- Punkt 7 (B4, B6, B11) -------------------------------------------------------------------


def test_w3_09_1_b4_only_at_signs_name_no_agent(store: RoomStore) -> None:
    a = store.for_agent("a")
    for bad in ("@", "@@", "@ "):
        with pytest.raises(RoomError, match="nennt keinen Agenten") as info:
            a.wait_until(1, until_result_from=bad)
        assert "gibt es in diesem Run nicht" not in str(info.value)
    assert a.wait_until(1, until_result_from="").result_from is None


async def test_w3_09_1_b11_m47_an_older_result_does_not_wake(workfolder: Path) -> None:
    """S22: D2 already gave a result but is not done (open question). D1 waits for its
    result: the old one does not wake it, the next one does (M47).

    W4-00 B14: „old“ is a result from before the waiting turn; one in the waiting turn before
    `wait_until` counts (`test_w4_00_b14_…`). So D1 begins to wait in a later turn, woken by
    D3's message, after D2's first result."""
    scripts = {
        D1: turns([say("bereit")],
                  [room("wait_until", minutes=0.5, until_result_from=D2)], [done()]),
        D2: turns([post("main", f"@{D3} stimmt das?", kind="question"),
                   room("submit_result", kind="x", title="Alt", body="alt")],
                  [wait(0.8), room("submit_result", kind="x", title="Neu", body="neu"), done()],
                  repeat_last=True),
        D3: turns([wait(1.0), post("main", f"@{D1} {D2} rechnet noch."), wait(1.0),
                   post("main", "Ja.", kind="answer", reply_to=2), done()]),
    }  # fmt: skip
    manager, outcome, _ = await discuss(workfolder, scripts, IDLE)
    [result] = [e for e in log_entries(manager.run_dir, "wake") if e["data"]["agent"] == D1
                and e["data"]["reasons"] == ["result_from"]]  # fmt: skip
    woken = [p for p in prompts_of(manager, D1) if "hat ein Ergebnis abgegeben" in p]
    assert result and len(woken) == 1
    assert "„Neu“" in woken[0] and "„Alt“" not in woken[0]


def test_w3_09_1_b11_new_result_looks_only_after_the_mark(tmp_path: Path) -> None:
    """`new_result` reads the messages after the mark of the waiting turn, not the whole run."""
    from arr.runs.orchestrator import Orchestrator as Orch

    run = tmp_path / "run"
    for agent_id in ("a", "b"):
        (run / "agents" / agent_id / "work").mkdir(parents=True)
    store = RoomStore.create(run)
    store.setup_channels([GroupChatConfig(name="main", members=["a", "b"])])
    early = store.for_agent("b").post_message("main", "alt", MessageKind.RESULT)
    late = store.for_agent("b").post_message("main", "neu", MessageKind.RESULT)
    fake: Any = type("O", (), {"room": store})()
    assert Orch.new_result(fake, "b", 0.0, early.id).id == late.id
    assert Orch.new_result(fake, "b", 0.0, late.id) is None


# --- Punkt 8: tests for the mutations without a red test --------------------------------------


async def test_w3_09_1_m06_a_result_resets_the_count_of_deadlines(workfolder: Path) -> None:
    """L10: after deadlines in a row, the awaited result resets the count (M06)."""
    waits = room("wait_until", minutes=0.005, until_result_from=D2)
    scripts = {
        D1: turns([waits], [waits],
                  [room("wait_until", minutes=0.5, until_result_from=D2)], [done()]),
        D2: turns([wait(1.5), room("submit_result", kind="x", title="T", body="B")]),
    }  # fmt: skip
    manager, outcome, _ = await discuss(workfolder, scripts, IDLE)
    reasons = [r for agent, rs in wakes(manager) if agent == D1 for r in rs]
    assert "timeout" in reasons and "result_from" in reasons
    assert reasons.index("timeout") < reasons.index("result_from")
    guard = manager.room.wait_guards().get(D1)
    assert guard is None or guard.until_streak == 0


def test_w3_09_1_m08_m53_a_question_to_the_user_is_no_question_to_the_agent(
    store: RoomStore,
) -> None:
    """Hint and inbox of an agent named in `to` of a question to the user (M08, M53)."""
    from arr.runs.pacing import compose_hint

    both = store.for_agent("a").post_message(
        "main", "@user @b was meint ihr?", MessageKind.QUESTION, to=["b"]
    )
    assert both.to == ["b", USER] and not is_question_to(both, "b")
    assert "Frage" not in compose_hint("b", [both])
    assert store.for_agent("b").inbox().open_questions == []


def test_w3_09_1_m30_an_answer_needs_a_text() -> None:
    for comment in (None, "", "   "):
        with pytest.raises(ValueError, match="Text"):
            Decision(action=DecisionAction.ANSWER, decided_by=DecidedBy.USER, comment=comment)


async def test_w3_09_1_m29_stop_with_an_open_question(workfolder: Path) -> None:
    """A stop while a question to the user is open: no error, the item is obsolete (M29)."""

    async def stop(orch: Orchestrator) -> None:
        await eventually(lambda: bool(orch.inbox.pending()), within=10)
        await orch.halt(StopReason.USER)

    ask = post("main", "@user welches Band?", kind="question")
    scripts = {D1: turns([ask, wait(20)]), D2: turns([wait(20)])}
    results: list[Any] = []

    async def topology(ctx: Any) -> None:
        helper = asyncio.create_task(stop(ctx))
        results.append(await ctx.discuss("main", "Los."))
        await helper

    from run_kit import channel, make_manager

    manager = make_manager(
        workfolder, topology, scripts,
        groupchats=[channel("main", [D1, D2], mode="event", **IDLE)],
        run_extra={"limits": {"wall_time": "30s"}},
    )  # fmt: skip
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.STOPPED
    assert log_entries(manager.run_dir, "error") == []
    [item] = manager.inbox.items()
    assert item.status is ApprovalStatus.OBSOLETE


def test_w3_09_1_m51_a_question_is_no_escalation() -> None:
    from arr.service.real_api.rundir import _item_is_escalation, describe_item

    question = {"agent_id": D1, "channel": "main", "msg_id": 2, "text": "Band?"}
    entry = {"event": "open", "item_id": "apr_1", "question": question}
    assert not _item_is_escalation(entry)
    assert describe_item(entry) == f"{D1} fragt dich: Band?"
    assert _item_is_escalation({"event": "open", "item_id": "apr_2"})


def test_w3_09_1_m34_the_card_skips_an_allowed_rate_limit(tmp_path: Path) -> None:
    """The „jetzt“ line of the card: an `allowed` window after a tool shows the tool; a
    `rejected` one shows „Nutzungslimit erreicht“ (M34)."""
    from arr.service.real_api.rundir import AgentRecord, _activity
    from arr.service.real_api.transcript import TranscriptIndex

    def activity(status: str) -> Any:
        path = tmp_path / f"{status}.jsonl"
        lines = [
            {"seq": 1, "ts": 1.0, "turn": 1, "kind": "tool_call",
             "data": {"id": "t1", "name": "Bash", "input": {"command": "ls"}}},
            {"seq": 2, "ts": 2.0, "turn": 1, "kind": "rate_limit",
             "data": {"status": status, "resets_at": 1794150600.0}},
        ]  # fmt: skip
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), "utf-8")
        index = TranscriptIndex(path)
        index.refresh()
        return _activity(AgentRecord(D1, tmp_path, None, index), final=False)

    assert activity("allowed").label == "Bash"
    assert activity("rejected").label == "Nutzungslimit erreicht"


# --------------------------------------------------------------------------------------------
# W4-00: B14 (S26), B15 (S31), B16 (S30, M58), B18 (S10, M65)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("naive", [False, True], ids=["b14", "naive"])
async def test_w4_00_b14_a_result_in_the_waiting_turn_counts(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """S26: D2 submits while D1 is still in its turn; D1 calls `wait_until(result_from=D2)`
    after that. The result counts: D1 is woken with it at once (`result_from`) and is never
    told „ohne ein neues Ergebnis“. Counter-check with the old window (from the call):
    D1 sleeps until D2 is done and hears `result_missing`."""
    if naive:
        monkeypatch.setattr(Orchestrator, "_result_since", lambda self, agent, wait: wait.since)
    scripts = {
        D1: turns([wait(0.8), room("wait_until", minutes=0.5, until_result_from=D2)], [done()]),
        D2: turns([wait(0.3), room("submit_result", kind="x", title="Früh", body="früh"),
                   wait(1.5), done()]),
    }  # fmt: skip
    manager, outcome, took = await discuss(workfolder, scripts, IDLE)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and took < 15
    [first] = [r for a, r in wakes(manager) if a == D1][:1]
    woken = prompts_of(manager, D1)[1]
    if naive:
        assert first == ["result_missing"] and "ohne ein neues Ergebnis" in woken
        return
    assert first == ["result_from"]
    assert f"{D2} hat ein Ergebnis abgegeben" in woken and "„Früh“" in woken
    assert "ohne ein neues Ergebnis" not in woken


def _old_close_user_questions(self: Orchestrator, note: str) -> None:
    """`close_user_questions` before W4-00 (B15): `closed` only if the inbox still had it."""
    for msg_id, item_id in list(self._questions.items()):
        del self._questions[msg_id]
        self.room.close_question(msg_id)
        if self.inbox.close_question(item_id, note):
            self.log.write("user_question", msg=msg_id, item=item_id, state="closed")


@pytest.mark.parametrize("naive", [False, True], ids=["b15", "naive"])
async def test_w4_00_b15_a_stop_logs_the_closed_question(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """S31: a stop with a question to the user open. `cancel_all` makes the item `obsolete`
    first; the log still says `closed` with `reason: stopped`. Counter-check: the old code
    leaves the log at `open`."""
    from test_agent_loop_hangs import run_case

    if naive:
        monkeypatch.setattr(Orchestrator, "close_user_questions", _old_close_user_questions)

    async def stopper(manager: Any) -> None:
        await eventually(lambda: bool(manager.orchestrator.inbox.pending()), within=10)
        await asyncio.sleep(0.2)
        manager.stop()

    ask = post("main", "@user welches Band?", kind="question")
    scripts = {D1: turns([ask, wait(20)]), D2: turns([wait(20)])}
    manager, line = await run_case(workfolder, scripts, IDLE, during=stopper, wall="30s")
    assert line["ended_by"] is DiscussionEnd.STOPPED and not log_entries(manager.run_dir, "error")
    [item] = manager.inbox.items()
    assert item.status is ApprovalStatus.OBSOLETE
    assert RoomStore(manager.run_dir).open_user_questions() == []
    entries = [e["data"] for e in log_entries(manager.run_dir, "user_question")]
    if naive:
        assert [e["state"] for e in entries] == ["open"]
        return
    assert [e["state"] for e in entries] == ["open", "closed"]
    assert entries[1]["reason"] == "stopped" and entries[1]["item"] == item.id


async def test_w4_00_b15_a_limit_logs_the_closed_question_too(workfolder: Path) -> None:
    """The same gap with `wall_time`: `cancel_all` runs for a limit as well."""
    from test_agent_loop_hangs import run_case

    ask = post("main", "@user welches Band?", kind="question")
    scripts = {D1: turns([ask, wait(20)]), D2: turns([wait(20)])}
    manager, _line = await run_case(workfolder, scripts, IDLE, wall="2s")
    entries = [e["data"] for e in log_entries(manager.run_dir, "user_question")]
    assert [(e["state"], e.get("reason")) for e in entries] == [("open", None), ("closed", "limit")]


async def test_w4_00_b16_l15_wakes_the_member_behind_the_guard_with_its_request(
    workfolder: Path,
) -> None:
    """S30: after a real ping-pong (guard) D2 sends 15 requests to the finished D1 in one turn.
    After `ping_pong` a `wake_scheduled {unread}` for D1 must come, and the requests must be in
    its prompt (M58: with L15 behind the guard again, neither happens)."""
    scripts = {
        D1: turns([post("main", "Ergebnis 1", kind="result"), wait(0.3), post("main", f"@{D2} a?")],
                  [wait(0.05), post("main", f"@{D2} b?")],
                  [wait(0.05), post("main", f"@{D2} c?")],
                  [wait(0.05), post("main", f"@{D2} d?")],
                  [say("gesehen")], repeat_last=True),
        D2: turns([post("main", "Ergebnis 2", kind="result")],
                  [wait(0.05), post("main", f"@{D1} x!")],
                  [wait(0.05), post("main", f"@{D1} y!")],
                  [wait(0.05), post("main", f"@{D1} z!")],
                  [wait(0.05), *[post("main", f"@{D1} Bitte {i}") for i in range(15)]],
                  [say("ruhig")], repeat_last=True),
    }  # fmt: skip
    manager, outcome, took = await discuss(workfolder, scripts, IDLE, within=40)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and took < 30
    entries = log_entries(manager.run_dir)
    [guard] = [i for i, e in enumerate(entries) if e["kind"] == "ping_pong"][:1]
    behind = [
        e["data"]
        for e in entries[guard:]
        if e["kind"] == "wake_scheduled" and "unread" in e["data"] and e["data"]["agent"] == D1
    ]
    assert behind, "L15 wakes the member behind the guard"
    guarded_at = entries[guard]["ts"]
    later = [t["data"]["prompt"] for t in turn_starts(manager.run_dir, D1) if t["ts"] > guarded_at]
    assert any("Bitte" in prompt for prompt in later), "the request is in its prompt"


async def test_w4_00_b18_the_end_of_the_run_closes_the_question_in_the_room(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S10: after the run `open_user_questions()` is empty, not only the inbox item (M65).
    B3 (W4-06.1): the phase waits for the answer up to the L7 deadline (shortened to 1 s)."""
    from test_agent_loop_hangs import run_case

    monkeypatch.setattr(waiting, "STREAK_MAX_S", 1.0)

    ask = post("main", "@user welches Band?", kind="question")
    scripts = {D1: turns([ask, done()]), D2: turns([done()])}
    manager, line = await run_case(workfolder, scripts, IDLE, wall="30s")
    assert not line["hang"]
    [item] = manager.inbox.items()
    assert item.status is ApprovalStatus.OBSOLETE
    store = RoomStore(manager.run_dir)
    assert store.open_user_questions() == [], "closed in the room with the run"
    [question] = [m for m in store.messages("main") if m.kind is MessageKind.QUESTION]
    assert not question.open
