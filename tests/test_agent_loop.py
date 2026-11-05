"""W3-07 Runner robust (docs/09 Agent-Loop): addressing, done, waiting, rounds, visibility.

Units first (addressing, `until_file`, the wait loop, the room tools, the prompts), then whole
discussions with FakeBackend agents that behave like the real models of UX-Abnahme 3 (Runs A,
B, C), then the states that wait (no hangs) and the data for the GUI.
"""

import asyncio
import json
import os
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from mcp.client.client import Client
from run_kit import (
    FAST,
    channel,
    done,
    events,
    eventually,
    log_entries,
    make_manager,
    make_settings,
    make_workfolder,
    post,
    room,
    say,
    turns,
    wait,
)

from arr.backends.fake import FakeBackend
from arr.models import AgentStatus, GroupChatConfig, RunStatus
from arr.permissions.types import PermissionRequest
from arr.room.address import Address, address, mentions
from arr.room.server import build_server
from arr.room.store import WAIT_UNTIL_MAX_MIN, AgentRoom, RoomStore
from arr.room.types import Message, MessageKind, MessageVia, RoomError, WaitKind
from arr.room.waiting import (
    DEFAULT_WAIT_MAX_S,
    WAIT_MAX_ENV,
    WAIT_MAX_S,
    WaitEnd,
    wait_for_messages,
    wait_max_s,
)
from arr.runs.context import DiscussionEnd, DiscussionOutcome, RunContext
from arr.runs.orchestrator import Orchestrator
from arr.runs.pacing import PacingSettings


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


def msg(id: int, sender: str, **fields: Any) -> Message:
    return Message(
        id=id, ts=0.0, channel="main", sender=sender, body=fields.pop("body", "x"), **fields
    )


# --------------------------------------------------------------------------------------------
# 09 §4.1: addressing
# --------------------------------------------------------------------------------------------


def test_mentions_find_members_anywhere_in_the_text() -> None:
    members = ["diskutant-1", "diskutant-2", "critic"]

    def resolve(name: str) -> str | None:
        return name if name in members else None

    text = "Ergebnis steht. Frage an @diskutant-2: gilt das für n = 9? Mail an a@critic.de"
    assert mentions(text, members, "diskutant-1", resolve) == ["diskutant-2"]
    assert mentions("@alle bitte prüfen", members, "critic", resolve) == [
        "diskutant-1",
        "diskutant-2",
    ]
    assert mentions("(@critic), @critic.", members, "diskutant-1", resolve) == ["critic"]
    assert mentions("@diskutant-1 und @nobody", members, "diskutant-1", resolve) == []


def test_address_covers_to_reply_and_question_to_all() -> None:
    senders = {1: "b", 2: "orchestrator"}
    sender_of = senders.get
    assert address(msg(5, "a", to=["b"]), "b", sender_of) is Address.MENTION
    assert address(msg(5, "a", to=["b"], kind=MessageKind.QUESTION), "b", sender_of) is (
        Address.QUESTION
    )
    assert address(msg(5, "a", reply_to=1), "b", sender_of) is Address.REPLY
    assert address(msg(5, "a", kind=MessageKind.QUESTION), "c", sender_of) is (Address.QUESTION_ALL)
    assert address(msg(5, "a", kind=MessageKind.NOTE), "c", sender_of) is None
    assert address(msg(5, "b", to=["b"]), "b", sender_of) is None, "own messages never"


def test_at_name_becomes_to_when_posted_by_agent_and_user(store: RoomStore) -> None:
    a = store.for_agent("a")
    posted = a.post_message("main", "Frage an @b: gilt Lemma A?", kind="question")
    assert posted.to == ["b"] and posted.open, "a question with @name is an open question"
    every = a.post_message("main", "@alle kurzer Stand bitte")
    assert every.to == ["b", "c"]
    user = store.post("main", "@c bist du da?", sender="user", via=MessageVia.GUI)
    assert user.to == ["c"]
    plain = store.post("main", "@c Hinweis", sender="orchestrator")
    assert plain.to == [], "the orchestrator addresses only with `to`"


# --------------------------------------------------------------------------------------------
# 09 §4.3, A8: `until_file` only looks at existence inside the working folder
# --------------------------------------------------------------------------------------------


def naive_exists(view: AgentRoom, rel: str) -> bool:
    """The counter-test (A8): what a check without the protection would do."""
    return (view.work_dir / rel).exists()


@pytest.mark.parametrize("checker", ["real", "naive"])
def test_a8_until_file_rejects_links_parents_absolute_and_outside(
    store: RoomStore, tmp_path: Path, checker: str
) -> None:
    view = store.for_agent("a")
    work = view.work_dir
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    (work / "out").mkdir()
    (work / "out" / "fertig.flag").write_text("")
    os.symlink(outside, work / "link.flag")
    os.symlink(tmp_path, work / "linkdir")
    check: Callable[[str], bool] = (
        view.until_file_exists if checker == "real" else (lambda rel: naive_exists(view, rel))
    )

    def rejected(rel: str) -> bool:
        try:
            return not check(rel)
        except RoomError:
            return True

    def protected() -> None:
        assert check("out/fertig.flag") and not check("out/fehlt.flag")
        assert rejected("link.flag"), "a link to a file outside"
        assert rejected("linkdir/secret.txt"), "a folder link to outside"
        assert rejected("../b/work"), "`..`"
        assert rejected(str(outside)), "an absolute path"
        assert rejected("../../../secret.txt"), "a path outside"

    if checker == "real":
        protected()
    else:  # without the protection the same test is red
        with pytest.raises(AssertionError):
            protected()


def test_until_file_never_reads_the_file(store: RoomStore) -> None:
    view = store.for_agent("a")
    fifo = view.work_dir / "pipe.flag"
    os.mkfifo(fifo)  # opening a FIFO for reading would block forever
    assert view.until_file_exists("pipe.flag")


def test_wait_until_is_capped_and_checks_the_file_at_once(store: RoomStore) -> None:
    view = store.for_agent("a")
    before = time.time()
    state = view.wait_until(500, "out/fertig.flag")
    assert state.kind is WaitKind.UNTIL and state.file == "out/fertig.flag"
    assert state.until - before <= WAIT_UNTIL_MAX_MIN * 60 + 1
    assert store.waits()["a"] == state
    with pytest.raises(RoomError, match="Arbeitsordner"):
        view.wait_until(5, "/etc/passwd")
    with pytest.raises(RoomError, match="größer als 0"):
        view.wait_until(0)


# --------------------------------------------------------------------------------------------
# 09 §4.3, A5: `wait_for_messages` – message, file, deadline; limit per backend
# --------------------------------------------------------------------------------------------


def test_wait_limits_per_backend_from_the_spike() -> None:
    assert WAIT_MAX_S == {"claude": 45.0, "codex": 25.0}
    assert wait_max_s({WAIT_MAX_ENV: "25"}) == 25.0
    assert wait_max_s({WAIT_MAX_ENV: "kaputt"}) == wait_max_s({}) == DEFAULT_WAIT_MAX_S


async def test_wait_for_messages_ends_on_a_message_to_me(store: RoomStore) -> None:
    b = store.for_agent("b")
    store.for_agent("a").post_message("main", "alt, nicht an b")

    async def later() -> None:
        await asyncio.sleep(0.2)
        store.for_agent("c").post_message("main", "nur Notiz")  # does not end the wait
        await asyncio.sleep(0.2)
        store.for_agent("a").post_message("main", "@b siehst du das?")

    task = asyncio.create_task(later())
    outcome = await wait_for_messages(b, 10, max_s=10, poll_s=0.02)
    await task
    assert outcome.ended_by is WaitEnd.MESSAGE and 0.3 < outcome.waited_s < 2
    assert [m.body for m in outcome.messages][1:] == [
        "alt, nicht an b",
        "nur Notiz",
        "@b siehst du das?",
    ], "the unread messages (after the brief), now read"
    assert b.unread_counts() == ({"main": 0}, 0)
    assert "b" not in store.waits(), "the wait row is gone"


async def test_wait_for_messages_ends_on_file_and_on_deadline(store: RoomStore) -> None:
    a = store.for_agent("a")

    async def write() -> None:
        await asyncio.sleep(0.2)
        assert store.waits()["a"].kind is WaitKind.MESSAGES, "visible while it waits"
        (a.work_dir / "out").mkdir()
        (a.work_dir / "out" / "fertig.flag").write_text("")

    task = asyncio.create_task(write())
    by_file = await wait_for_messages(a, 10, "out/fertig.flag", max_s=10, poll_s=0.02)
    await task
    assert by_file.ended_by is WaitEnd.FILE and by_file.file == "out/fertig.flag"
    by_time = await wait_for_messages(a, 60, max_s=0.3, poll_s=0.02)
    assert by_time.ended_by is WaitEnd.TIMEOUT and by_time.limit_s == 0.3
    assert 0.3 <= by_time.waited_s < 1.0, "never longer than the limit of the backend"
    with pytest.raises(RoomError, match="Verweis"):
        os.symlink("/etc", a.work_dir / "etc")
        await wait_for_messages(a, 5, "etc/hosts", max_s=5)


@pytest.mark.parametrize(("limit", "env"), [(45, "45"), (25, "25")])
async def test_room_tools_over_mcp(
    store: RoomStore, monkeypatch: pytest.MonkeyPatch, limit: int, env: str
) -> None:
    monkeypatch.setenv(WAIT_MAX_ENV, env)
    async with Client(build_server(store.for_agent("a"))) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert f"höchstens {limit} s je Aufruf" in tools["wait_for_messages"].description
        store.for_agent("b").post_message("main", "@a antworte bitte", kind="question")
        result = await client.call_tool("wait_for_messages", {"seconds": 300})
        text = result.content[0].text
        assert not result.is_error and "Es gibt eine Nachricht an dich" in text
        assert "@a antworte bitte" in text
        result = await client.call_tool("wait_until", {"minutes": 5, "until_file": "x.flag"})
        assert "Beende jetzt deinen Turn" in result.content[0].text
        assert "die Datei „x.flag“ da ist" in result.content[0].text
        result = await client.call_tool("wait_for_messages", {"until_file": "../x"})
        assert result.is_error and "Arbeitsordner" in result.content[0].text


# --------------------------------------------------------------------------------------------
# 09 §4.7, Nachtrag F1/F3: prompts
# --------------------------------------------------------------------------------------------


def prompt_text(name: str) -> str:
    return resources.files("arr.templates").joinpath("prompts", name).read_text("utf-8")


def test_claude_prompt_background_only_with_run_in_background() -> None:
    text = prompt_text("claude.md")
    assert "run_in_background" in text and "`&`, `nohup` oder `disown`" in text
    assert "Run" in text and "Not-Aus" in text, "the reason is given"
    assert "wait_until(minutes, until_file=" in text and "wait_for_messages" in text
    assert "45 s" in text and "@name" in text


def test_codex_prompt_runs_long_scripts_in_the_foreground() -> None:
    text = prompt_text("codex.md")
    assert "wait_for_messages" in text and "25 s" in text
    assert "eigenen Werkzeug `wait`" in text, "kept apart from Codex' own `wait`"
    assert "is not a function" in text and "neuen `exec`" in text
    assert "Vordergrund" in text
    assert "setsid" not in text and "until_file" not in text and "wait_until" not in text


def test_brief_names_at_name_waiting_and_how_to_finish(store: RoomStore) -> None:
    brief = store.messages("main")[0].body
    assert "`@name`" in brief and "wait_for_messages" in brief
    assert "Ergebnis ab" in brief and "Runden gibt es nur, wenn niemand mehr schreibt" in brief


# --------------------------------------------------------------------------------------------
# Whole discussions with FakeBackend agents that behave like the real models
# --------------------------------------------------------------------------------------------

D1, D2, D3 = "diskutant-1", "diskutant-2", "diskutant-3"
IDLE = {"idle_after_s": 0.1}


async def discuss(
    workfolder: Path,
    scripts: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    pacing: PacingSettings = FAST,
    during: Callable[[Orchestrator], Any] | None = None,
    within: float = 20,
    run_extra: dict[str, Any] | None = None,
    **kwargs: Any,
) -> tuple[Any, DiscussionOutcome, float]:
    """One `event` discussion in #main; returns manager, outcome and its duration."""
    results: list[DiscussionOutcome] = []
    rounds = kwargs.pop("rounds", None)

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        helper = asyncio.create_task(during(ctx)) if during is not None else None
        results.append(await ctx.discuss("main", "Löst die Aufgabe.", rounds=rounds))
        if helper is not None:
            await helper

    manager = make_manager(
        workfolder,
        topology,
        scripts,
        groupchats=[channel("main", list(scripts), mode="event", **(config or {}))],
        pacing=pacing,
        run_extra={"limits": {"wall_time": "60s"}, **(run_extra or {})},
        **kwargs,
    )
    started = time.monotonic()
    assert await asyncio.wait_for(manager.run(), within) is RunStatus.DONE
    return manager, results[0], time.monotonic() - started


def turn_starts(run_dir: Path, agent_id: str) -> list[dict[str, Any]]:
    return [e for e in events(run_dir, agent_id) if e["kind"] == "turn_start"]


def first_ts(manager: Any, sender: str) -> float:
    return min(m.ts for m in manager.room.messages("main") if m.sender == sender)


def no_wall_time(manager: Any) -> None:
    """Not ended by `wall_time` (A6): no limit entry, a regular `discussion_end`."""
    assert log_entries(manager.run_dir, "limit") == []
    assert log_entries(manager.run_dir, "discussion_end")


# --- A2: Run B – „Frage an @diskutant-2“ in the text, `reply_to`, `@alle`, user `@name` ----


async def test_a2_run_b_at_name_in_the_text_wakes_at_once(workfolder: Path) -> None:
    question = "Ergebnis: 60 hat 12 Teiler. Frage an @diskutant-2: Stimmt das auch für 120?"
    scripts = {
        D1: turns([wait(0.2), post("main", question, kind="result")]),
        D2: turns([say("bereit")], [post("main", "Ja, 16 Teiler.", kind="answer"), done()]),
    }
    manager, outcome, took = await discuss(workfolder, scripts)  # debounce 20 s by default
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and took < 5
    [wake] = log_entries(manager.run_dir, "wake")
    assert wake["data"]["agent"] == D2 and wake["data"]["reasons"] == ["mention"]
    woken = turn_starts(manager.run_dir, D2)[1]
    assert woken["ts"] - first_ts(manager, D1) < 1, "at once, not after the 20 s debounce"
    assert "Du wurdest in #main geweckt: eine Nachricht an dich" in woken["data"]["prompt"]
    [result] = [m for m in manager.room.messages("main") if m.kind is MessageKind.RESULT]
    assert result.to == [D2], "@name became `to`"


async def test_a2_run_c_reply_to_wakes_the_author(workfolder: Path) -> None:
    scripts = {
        D1: turns([wait(0.3), post("main", "Das stimmt nicht für n = 9.", kind="objection",
                                   reply_to=2), done()]),
        D2: turns([post("main", "Lemma A gilt für alle n ≡ 1 mod 4.", kind="claim")],
                  [post("main", "Stimmt, ich korrigiere.", reply_to=3), done()]),
    }  # fmt: skip
    manager, outcome, _ = await discuss(workfolder, scripts)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    [wake] = log_entries(manager.run_dir, "wake")
    assert wake["data"]["agent"] == D2 and wake["data"]["reasons"] == ["reply"]


async def test_a2_at_all_wakes_every_other_member(workfolder: Path) -> None:
    scripts = {
        D1: turns([wait(0.2), post("main", "@alle bitte kurz den Stand."), done()]),
        D2: turns([say("bereit")], [post("main", "Stand: offen."), done()]),
        D3: turns([say("bereit")], [post("main", "Stand: gelöst."), done()]),
    }
    manager, outcome, _ = await discuss(workfolder, scripts)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    woken = sorted(e["data"]["agent"] for e in log_entries(manager.run_dir, "wake"))
    assert woken == [D2, D3]


async def test_a2_user_message_with_at_name_wakes_the_agent(workfolder: Path) -> None:
    async def user(orch: Orchestrator) -> None:
        await eventually(lambda: orch.agent_status(D2) is AgentStatus.IDLE and not orch.is_busy(D2))
        orch.room.post("main", "@diskutant-2 bist du noch dran?", sender="user", via=MessageVia.GUI)

    scripts = {
        D1: turns([post("main", "Mein Ergebnis.", kind="result")]),
        D2: turns([say("bereit")], [post("main", "Ja, gleich fertig.", kind="result")]),
    }
    manager, outcome, _ = await discuss(workfolder, scripts, during=user)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    [wake] = log_entries(manager.run_dir, "wake")
    assert wake["data"]["agent"] == D2
    [user_msg] = [m for m in manager.room.messages("main") if m.sender == "user"]
    assert user_msg.to == [D2] and user_msg.delivery.status.value == "woken"


async def test_question_without_addressee_goes_to_idle_members_not_finished_ones(
    workfolder: Path,
) -> None:
    scripts = {
        D1: turns([wait(0.3), post("main", "Kennt jemand einen Beweis?", kind="question"), done()]),
        D2: turns([say("bereit")], [post("main", "Ja: Induktion.", reply_to=4), done()]),
        D3: turns([post("main", "Mein Ergebnis.", kind="result")]),
    }
    manager, outcome, _ = await discuss(workfolder, scripts)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    woken = [e["data"] for e in log_entries(manager.run_dir, "wake")]
    assert [(w["agent"], w["reasons"]) for w in woken] == [(D2, ["question_all"])]


# --- A3: Run C – a result without `done` finishes the agent ---------------------------------


async def test_a3_run_c_result_without_done_ends_with_all_done_at_once(workfolder: Path) -> None:
    scripts = {
        D1: turns([post("main", "Ergebnis von 1.", kind="result"), done()]),
        D2: turns([wait(0.1), post("main", "Mein Ergebnis: 12.", kind="result")]),
    }
    manager, outcome, took = await discuss(workfolder, scripts)  # quiet_after_s 300 s
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and took < 5, "no quiet time"
    assert manager.orchestrator.agent_status(D2) is AgentStatus.DONE
    reasons = {
        e["data"]["agent"]: e["data"]["reason"] for e in log_entries(manager.run_dir, "agent_done")
    }
    assert reasons == {D1: "reported", D2: "result"}


async def test_submit_result_finishes_but_an_open_own_question_keeps_working(
    workfolder: Path,
) -> None:
    submit = room("submit_result", kind="idea", title="Ansatz", body="Induktion")
    scripts = {
        D1: turns([submit]),
        D2: turns([submit, post("main", "@diskutant-1 passt das?", kind="question")],
                  [done()]),
    }  # fmt: skip
    manager, outcome, _ = await discuss(workfolder, scripts, IDLE)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    first = log_entries(manager.run_dir, "agent_done")[0]["data"]
    assert first == {"agent": D1, "reason": "result", "turn": 1}
    assert len(turn_starts(manager.run_dir, D2)) == 2, "its open question kept it in"


async def test_done_stays_and_only_a_question_wakes_it_again(workfolder: Path) -> None:
    scripts = {
        D1: turns([post("main", "Fertig.", kind="result")],
                  [post("main", "Antwort: ja.", kind="answer", reply_to=3)]),
        D2: turns([wait(0.3), post("main", "Notiz für @diskutant-1"),
                   post("main", "@diskutant-1 gilt das?", kind="question"), done()]),
    }  # fmt: skip
    manager, outcome, _ = await discuss(workfolder, scripts)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    [wake] = log_entries(manager.run_dir, "wake")
    assert wake["data"]["agent"] == D1 and wake["data"]["reasons"] == ["question"]
    assert manager.orchestrator.agent_status(D1) is AgentStatus.DONE, "done again afterwards"


# --- A4: Run A – idle rounds, `rounds` limits them, `discussion_end` reports them -----------


async def test_a4_run_a_idle_rounds_until_at_most_n(workfolder: Path) -> None:
    talk = post("main", "Noch ein Gedanke.")
    scripts = {
        D1: turns([talk], repeat_last=True),
        D2: turns([talk], repeat_last=True),
    }
    manager, outcome, _ = await discuss(workfolder, scripts, IDLE, rounds=2)
    assert outcome.ended_by is DiscussionEnd.ROUNDS and outcome.rounds == 2
    rounds = [e["data"] for e in log_entries(manager.run_dir, "round")]
    assert [(r["number"], r["of"]) for r in rounds] == [(1, 2), (2, 2)]
    assert sorted(rounds[0]["agents"]) == [D1, D2]
    [end] = log_entries(manager.run_dir, "discussion_end")
    assert end["data"]["ended_by"] == "rounds" and end["data"]["rounds"] == 2
    prompts = [e["data"]["prompt"] for e in turn_starts(manager.run_dir, D1)]
    assert "Runde 1 von 2 in #main" in prompts[1] and "Runde 2 von 2" in prompts[2]
    idle = [e["data"] for e in log_entries(manager.run_dir, "waiting") if e["data"].get("channel")]
    assert {"idle"} <= {w["reason"] for w in idle}, "the idle time is logged with `until`"
    assert all(w["until"] for w in idle if w["reason"] == "idle")


async def test_a4_exchange_then_results_ends_all_done_after_the_round(workfolder: Path) -> None:
    submit = post("main", "Ergebnis nach dem Austausch.", kind="result")
    scripts = {
        D1: turns([post("main", "Idee 1")], [submit]),
        D2: turns([post("main", "Idee 2")], [submit]),
    }
    manager, outcome, _ = await discuss(workfolder, scripts, IDLE)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and outcome.rounds == 1
    [end] = log_entries(manager.run_dir, "discussion_end")
    assert end["data"]["rounds"] == 1


async def test_rounds_are_ordered_by_unread_messages_to_the_member(workfolder: Path) -> None:
    scripts = {
        D1: turns([wait(0.2), post("main", "@diskutant-3 Frage an dich", kind="question")],
                  [say("nichts")], repeat_last=True),
        D2: turns([say("nichts")], repeat_last=True),
        D3: turns([wait(0.6)], [say("nichts")], repeat_last=True),
    }  # fmt: skip
    manager, _, _ = await discuss(workfolder, scripts, IDLE, rounds=1)
    [first] = log_entries(manager.run_dir, "round")
    assert first["data"]["agents"][0] == D3, "the member with an unread question first"


# --- A5: `wait_for_messages` and `wait_until` through the FakeBackend, both backends --------


@pytest.mark.parametrize(
    ("provider", "model", "limit"), [("claude", "fake-model", 45.0), ("codex", "fake-model", 25.0)]
)
async def test_a5_wait_for_messages_in_the_turn_per_backend(
    workfolder: Path, provider: str, model: str, limit: float
) -> None:
    waiting = room("wait_for_messages", seconds=600, save_as="waited")
    scripts = {
        D1: turns([waiting, done()]),
        D2: turns([wait(0.3), post("main", "@diskutant-1 hier die Antwort"), done()]),
    }
    agents = [{"id": D1, "provider": provider, "model": model, "auth": "fake-auth"}, {"id": D2}]
    manager, outcome, _ = await discuss(workfolder, scripts, agents=agents)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    [result] = [e for e in events(manager.run_dir, D1) if e["kind"] == "tool_result"][:1]
    data = json.loads(result["data"]["content"])
    assert data["ended_by"] == "message" and data["messages"]
    [entry] = [e["data"] for e in log_entries(manager.run_dir, "waiting") if e["data"].get("agent")]
    assert entry["reason"] == "wait_for_messages"
    assert entry["until"] - result["ts"] <= limit + 1, "the limit of the backend"
    assert not log_entries(manager.run_dir, "wake"), "no wake-up needed"


async def test_a5_wait_for_messages_returns_at_the_limit(workfolder: Path) -> None:
    async def short(orch: Orchestrator) -> None:  # the fake uses the limit of its provider
        pass

    scripts = {D1: turns([room("wait_for_messages", seconds=0.3), done()])}
    manager, outcome, took = await discuss(workfolder, scripts, during=short)
    result = next(e for e in events(manager.run_dir, D1) if e["kind"] == "tool_result")
    assert json.loads(result["data"]["content"])["ended_by"] == "timeout"
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and took < 5


@pytest.mark.parametrize("trigger", ["message", "file", "timeout"])
async def test_a5_wait_until_ends_the_turn_and_arr_wakes(workfolder: Path, trigger: str) -> None:
    minutes = 0.01 if trigger == "timeout" else 10
    file = "out/fertig.flag" if trigger == "file" else None
    long_wait = room("wait_until", minutes=minutes, until_file=file)

    async def act(orch: Orchestrator) -> None:
        await eventually(lambda: orch.long_wait(D1) is not None)
        if trigger == "file":
            work = orch.prepared.agents[D1].work_dir
            (work / "out").mkdir(exist_ok=True)
            (work / "out" / "fertig.flag").write_text("")
        elif trigger == "message":
            orch.room.post("main", "@diskutant-1 neue Daten", sender="user", via=MessageVia.GUI)

    scripts = {D1: turns([long_wait], [done()]), D2: turns([done()])}
    manager, outcome, took = await discuss(workfolder, scripts, during=act)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and took < 10
    [waiting] = [
        e["data"] for e in log_entries(manager.run_dir, "waiting") if e["data"].get("agent") == D1
    ]
    assert waiting["reason"] == "wait_until" and waiting["file"] == file
    [wake] = log_entries(manager.run_dir, "wake")
    expected = {"message": ["mention"], "file": ["file"], "timeout": ["timeout"]}[trigger]
    assert wake["data"]["agent"] == D1 and wake["data"]["reasons"] == expected
    assert turn_starts(manager.run_dir, D1)[1]["data"]["prompt"].startswith("## Posteingang")
    assert manager.room.waits() == {}, "the wait is cleared"


async def test_wait_until_never_waits_past_wall_time(workfolder: Path) -> None:
    async def act(orch: Orchestrator) -> None:
        await eventually(lambda: orch.long_wait(D1) is not None)
        assert orch.wall_end is not None
        assert orch.long_wait(D1).until <= orch.wall_end
        orch.room.post("main", "@diskutant-1 weiter", sender="user", via=MessageVia.GUI)

    scripts = {D1: turns([room("wait_until", minutes=30)], [done()])}
    await discuss(workfolder, scripts, during=act, run_extra={"limits": {"wall_time": "2m"}})


# --- A6: no state waits without a deadline --------------------------------------------------


async def test_a6_two_agents_wait_for_each_other(workfolder: Path) -> None:
    scripts = {
        D1: turns([room("wait_for_messages", seconds=0.2), room("wait_until", minutes=0.005)],
                  [done()]),
        D2: turns([room("wait_for_messages", seconds=0.2), room("wait_until", minutes=0.005)],
                  [done()]),
    }  # fmt: skip
    manager, outcome, took = await discuss(workfolder, scripts)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and took < 10
    reasons = [e["data"]["reason"] for e in log_entries(manager.run_dir, "waiting")]
    assert {"wait_for_messages", "wait_until", "agents"} <= set(reasons)
    no_wall_time(manager)


class AskingMidTurn(FakeBackend):
    """Asks the inbox after the steps of its first turn, like a tool call that needs approval."""

    holder: dict[str, Any] = {}

    async def run_turn(self, prompt: str, *, stage: str | None = None) -> Any:
        async for event in super().run_turn(prompt, stage=stage):
            if event.kind.value == "turn_end" and self._turn == 1:
                inbox = self.holder["manager"].inbox
                request = PermissionRequest(
                    run_id=self.run_id,
                    agent_id=self.config.id,
                    provider="claude",
                    tool="SandboxNetworkAccess",
                    hosts=["oeis.org"],
                    reason="Folge nachschlagen",
                )
                await inbox.handle(request)  # expires after `approvals.timeout`
            yield event


def asking_factory(scripts: dict[str, Any], asking: str) -> Callable[..., FakeBackend]:
    def factory(prepared: Any) -> FakeBackend:
        view = RoomStore(prepared.run_dir).for_agent(prepared.id, prepared.work_dir)
        cls = AskingMidTurn if prepared.id == asking else FakeBackend
        return cls(prepared.file.config, scripts[prepared.id], run_id="r", room=view)

    return factory


async def test_a6_a9_open_approval_waits_with_deadline_and_keeps_done(workfolder: Path) -> None:
    question = post("main", "@diskutant-1 noch eine Frage?", kind="question")
    scripts = {
        D1: turns([done("fertig, dann noch eine Frage ans Netz")], [done()]),
        D2: turns([wait(0.3), question, done()]),
    }
    holder: dict[str, Any] = {}
    AskingMidTurn.holder = holder
    results: list[DiscussionOutcome] = []

    async def topology(ctx: RunContext) -> None:
        results.append(await ctx.discuss("main", "Los."))

    manager = make_manager(
        workfolder,
        topology,
        scripts,
        groupchats=[channel("main", [D1, D2], mode="event")],
        run_extra={"approvals": {"timeout": "1s"}, "limits": {"wall_time": "60s"}},
    )
    manager.backend_factory = asking_factory(scripts, D1)
    holder["manager"] = manager
    assert await asyncio.wait_for(manager.run(), 20) is RunStatus.DONE
    assert results[0].ended_by is DiscussionEnd.ALL_DONE
    approval = [
        e["data"]
        for e in log_entries(manager.run_dir, "waiting")
        if e["data"].get("reason") == "approval"
    ]
    assert any(a.get("agent") == D1 and a["until"] for a in approval), "agent waits, with deadline"
    assert any(a.get("channel") == "main" for a in approval), "„Diskussion wartet auf Freigabe“"
    assert manager.orchestrator.agent_status(D1) is AgentStatus.DONE, "done survived `blocked`"
    done_at = [e for e in log_entries(manager.run_dir, "agent_done") if e["data"]["agent"] == D1]
    assert done_at[0]["data"]["reason"] == "reported" and done_at[0]["data"]["turn"] == 1
    assert log_entries(manager.run_dir, "hint") == [], "no hint while the approval waits"
    [wake] = log_entries(manager.run_dir, "wake")
    assert wake["data"]["agent"] == D1 and wake["data"]["reasons"] == ["question"], "after it"
    no_wall_time(manager)


async def test_a6_capacity_full_is_a_visible_wait(workfolder: Path) -> None:
    scripts = {D1: turns([wait(0.3), done()]), D2: turns([wait(0.3), done()])}
    manager, outcome, _ = await discuss(
        workfolder, scripts, settings=make_settings(**{"fake-auth": 1})
    )
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    capacity = [
        e["data"]
        for e in log_entries(manager.run_dir, "waiting")
        if e["data"].get("reason") == "capacity" and e["data"].get("agent")
    ]
    assert capacity and all(c["profile"] == "fake-auth" for c in capacity)
    assert {c["agent"] for c in capacity} <= {D1, D2}
    channel_waits = [e["data"]["reason"] for e in log_entries(manager.run_dir, "waiting")]
    assert "capacity" in channel_waits
    no_wall_time(manager)


async def test_a6_silent_agent_gets_a_warning_and_the_discussion_still_ends(
    workfolder: Path,
) -> None:
    pacing = PacingSettings(poll_interval_s=0.01, turn_silence_warn_s=0.2)
    scripts = {
        D1: turns([{"do": "hang", "seconds": 0.5}], [say("nichts")], repeat_last=True),
        D2: turns([post("main", "Ergebnis", kind="result")]),
    }
    manager, outcome, took = await discuss(
        workfolder, scripts, {**IDLE, "quiet_after_s": 0.3}, pacing=pacing
    )
    [warning] = log_entries(manager.run_dir, "turn_silent")
    assert warning["data"]["agent"] == D1 and warning["data"]["seconds"] >= 0
    assert outcome.ended_by in (DiscussionEnd.QUIET, DiscussionEnd.ROUNDS) and took < 10
    no_wall_time(manager)


async def test_a6_quiet_ends_only_after_a_round_that_moved_nothing(workfolder: Path) -> None:
    scripts = {
        D1: turns([post("main", "Start")], [say("nichts")], repeat_last=True),
        D2: turns([say("nichts")], repeat_last=True),
    }
    manager, outcome, _ = await discuss(
        workfolder, scripts, {"idle_after_s": 0.05, "quiet_after_s": 0.4}, rounds=50
    )
    assert outcome.ended_by is DiscussionEnd.QUIET and 1 <= outcome.rounds < 50
    no_wall_time(manager)


# --- A9: `turns_per_agent` ends only the agent ----------------------------------------------


async def test_a9_turns_per_agent_ends_only_that_agent(workfolder: Path) -> None:
    scripts = {
        D1: turns([post("main", "Gedanke 1")], [post("main", "Gedanke 2")], repeat_last=True),
        D2: turns([post("main", "Mein Ergebnis", kind="result")]),
    }
    manager, outcome, _ = await discuss(
        workfolder, scripts, IDLE, run_extra={"limits": {"turns_per_agent": 2, "wall_time": "60s"}}
    )
    assert outcome.ended_by is DiscussionEnd.ALL_DONE
    reasons = {
        e["data"]["agent"]: e["data"]["reason"] for e in log_entries(manager.run_dir, "agent_done")
    }
    assert reasons == {D1: "limit", D2: "result"}
    assert log_entries(manager.run_dir, "limit") == [], "the run itself did not stop"
    assert manager.orchestrator.agents[D1].turns_used == 2


async def test_a9_turns_per_agent_stops_the_run_when_nobody_is_left(workfolder: Path) -> None:
    talk = post("main", "weiter")
    scripts = {D1: turns([talk], repeat_last=True), D2: turns([talk], repeat_last=True)}
    results: list[DiscussionOutcome] = []

    async def topology(ctx: RunContext) -> None:
        results.append(await ctx.discuss("main", "Los."))

    manager = make_manager(
        workfolder,
        topology,
        scripts,
        groupchats=[channel("main", [D1, D2], mode="event", **IDLE)],
        run_extra={"limits": {"turns_per_agent": 2}},
    )
    assert await asyncio.wait_for(manager.run(), 20) is RunStatus.DONE
    assert results[0].ended_by in (DiscussionEnd.LIMIT, DiscussionEnd.ALL_DONE)
    [limit] = log_entries(manager.run_dir, "limit")
    assert limit["data"]["limit"] == "turns_per_agent"


# --- Hints: what `wait_for_messages` handed over is not hinted again ------------------------


async def test_messages_read_while_waiting_are_not_hinted_again(workfolder: Path) -> None:
    scripts = {
        D1: turns([room("wait_for_messages", seconds=30), wait(0.3), done()]),
        D2: turns([wait(0.2), post("main", "@diskutant-1 hier"), done()]),
    }
    manager, _, _ = await discuss(workfolder, scripts)
    assert log_entries(manager.run_dir, "hint") == []
    assert not [e for e in log_entries(manager.run_dir, "wake") if e["data"]["agent"] == D1]


# --- Ist §9: further cases ------------------------------------------------------------------


async def test_user_note_without_addressee_wakes_nobody_but_reaches_the_next_round(
    workfolder: Path,
) -> None:
    """Ist §9 case 3: no wake-up, delivery `next_inbox`; the idle round brings it (09 §3.4)."""

    async def user(orch: Orchestrator) -> None:
        await eventually(lambda: not orch.is_busy(D1) and not orch.is_busy(D2))
        orch.room.post("main", "Alles klar bei euch?", sender="user", via=MessageVia.GUI)

    scripts = {D1: turns([say("bereit")], [done()]), D2: turns([say("bereit")], [done()])}
    manager, outcome, _ = await discuss(workfolder, scripts, IDLE, during=user)
    assert outcome.ended_by is DiscussionEnd.ALL_DONE and outcome.rounds == 1
    assert log_entries(manager.run_dir, "wake") == []
    [note] = [m for m in manager.room.messages("main") if m.sender == "user"]
    assert note.delivery.status.value == "next_inbox"
    prompt = turn_starts(manager.run_dir, D1)[1]["data"]["prompt"]
    assert "★ Nachrichten vom User" in prompt and "Alles klar bei euch?" in prompt


async def test_hint_that_never_arrives_wakes_after_the_turn(workfolder: Path) -> None:
    """Ist §9 case 9: like Claude without a further tool call, the hint stays undelivered."""
    results: list[DiscussionOutcome] = []

    async def topology(ctx: RunContext) -> None:
        results.append(await ctx.discuss("main", "Los."))

    scripts = {
        D1: turns([wait(0.2), post("main", "@diskutant-2 schau mal"), done()]),
        D2: turns([wait(0.6)], [done()]),
    }
    manager = make_manager(
        workfolder, topology, scripts, groupchats=[channel("main", [D1, D2], mode="event")]
    )
    original = manager.backend_factory

    def factory(prepared: Any) -> Any:
        backend = original(prepared)
        if prepared.id == D2:

            async def never(text: str) -> bool:
                return False

            backend.deliver_hint = never
        return backend

    manager.backend_factory = factory
    assert await asyncio.wait_for(manager.run(), 20) is RunStatus.DONE
    assert results[0].ended_by is DiscussionEnd.ALL_DONE
    assert log_entries(manager.run_dir, "hint") == []
    [wake] = log_entries(manager.run_dir, "wake")
    assert wake["data"]["agent"] == D2 and wake["data"]["reasons"] == ["mention"]


# --- the data for the GUI (A7) without a running service ------------------------------------


def entry(kind: str, **data: Any) -> dict[str, Any]:
    return {"ts": 0.0, "kind": kind, "data": data}


def test_flow_view_reads_waits_rounds_and_silence() -> None:
    from arr.service.real_api.rundir import agent_warning, discussion_state, flow_view

    log = [
        entry("discussion_start", channel="main"),
        entry("turn", agent="a"),
        entry("waiting", agent="a", reason="wait_until", until=500.0, file="x.flag"),
        entry("turn_end", agent="a"),
        entry("waiting", agent="b", reason="capacity", profile="max", until=None),
        entry("round", channel="main", number=1, of=3, agents=["c"]),
        entry("turn", agent="c"),
        entry("turn_silent", agent="c", seconds=720),
        entry("waiting", channel="main", reason="idle", until=900.0, next="end"),
    ]
    view = flow_view(log)
    assert view.waits["a"]["data"]["reason"] == "wait_until", "survives its turn end"
    assert view.waits["b"]["data"]["reason"] == "capacity"
    assert agent_warning("c", view) == "seit 12 min kein Ereignis im Turn"
    state = discussion_state(view)
    assert state is not None and (state.text, state.until, state.round) == ("Ruhe, Ende", 900.0, 1)
    view = flow_view([*log, entry("turn", agent="b"), entry("turn_end", agent="c")])
    assert "b" not in view.waits and agent_warning("c", view) is None
    assert discussion_state(flow_view([*log, entry("discussion_end", channel="main")])) is None


def test_scan_summary_keeps_file_line_and_kind_only() -> None:
    from arr.service.real_api.rundir import secret_scan

    data = {
        "clean": False,
        "files": 12,
        "findings": [{"file": "a.md", "line": 3, "kind": "anthropic_key", "value": "sk-ant-x"}],
    }
    scan = secret_scan([entry("secret_scan", **data)])
    assert scan is not None and not scan.clean and scan.files == 12
    assert scan.model_dump()["findings"] == [{"file": "a.md", "line": 3, "kind": "anthropic_key"}]
    assert "sk-ant" not in scan.model_dump_json()
    assert secret_scan([]) is None
