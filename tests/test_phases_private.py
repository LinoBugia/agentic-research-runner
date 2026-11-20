"""W4b-03 §6.2: the private channel of every agent to the user (A2–A5), with FakeBackend
agents, the real RunManager, Orchestrator, room, room server, the profiles of both backends and
the real `RealApi` of the service."""

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.client.client import Client
from phases_kit import (
    TurnView,
    entries,
    errors,
    hand_in,
    manager,
    of_kind,
    parallel,
    private,
    run,
    run_with,
    say,
    solo,
)
from run_kit import eventually, make_manager, make_workfolder, room, turns, write_profiles

from arr.backends.codex_config import filesystem_entries
from arr.errors import ArrError
from arr.models import ChannelMode, RunStatus
from arr.permissions import policy
from arr.permissions.policy import RunPaths, profile_for
from arr.permissions.types import ApprovalStatus, ApprovalType, DecidedBy, Decision, DecisionAction
from arr.room.server import build_server
from arr.room.store import USER, RoomStore, private_owner
from arr.room.types import MessageKind, MessageVia, RoomError
from arr.submissions.formats import _SLUG_RE

AGENTS = {"agents": [{"id": a} for a in ("a", "b", "c")]}
ASK = "@user Darf ich annehmen, dass n prim ist?"


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


@pytest.fixture(autouse=True)
def no_user_temp_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tmp_path` lies in the user's temp folder, which the profiles deny; not the point."""
    monkeypatch.setattr(policy, "user_temp_dirs", lambda: [])


def hand_in_at_once(view: TurnView) -> list[dict[str, Any]]:
    return hand_in(view)


# --------------------------------------------------------------------------------------------
# A2: one private channel per agent before the first turn; none in the old topologies
# --------------------------------------------------------------------------------------------


async def test_a2_every_agent_gets_its_private_channel_before_the_first_turn(
    workfolder: Path,
) -> None:
    group = {
        **AGENTS,
        "channels": [{"name": "k", "members": ["a", "b"]}],
        "phases": [solo("p", "a"), {**parallel("q", ["b", "c"]), "channels": ["k"]}],
    }
    mgr, log = manager(workfolder, group, {a: hand_in_at_once for a in "abc"})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    infos = {c.name: c for c in mgr.room.channels()}
    assert sorted(infos) == ["k", "user.a", "user.b", "user.c"]
    for agent in "abc":
        name = private(agent)
        assert [m.agent for m in infos[name].members] == [agent], "the agent alone"
        assert private_owner(name) == agent and not _SLUG_RE.fullmatch(name), "no slug"
        assert "/" not in name and not name.startswith(".") and ".." not in name
        config = mgr.room.channel_config(name)
        assert (config.name, config.mode, config.anonymous) == (name, ChannelMode.EVENT, False)
        assert config.members == [agent] and config.max_wakeups_per_agent == 12
        brief = mgr.room.messages(name)[0]
        assert brief.pinned and brief.sender == "orchestrator"
        assert "Nur der User liest diesen Kanal" in brief.body
        assert mgr.room.cursor(agent, name) == brief.id, "the brief counts as read"
    first_turn = next(i for i, (k, _) in enumerate(entries(mgr)) if k == "turn")
    opened = [i for i, (k, d) in enumerate(entries(mgr)) if d.get("by") == "private"]
    assert len(opened) == 3 and max(opened) < first_turn
    assert "user.a" not in (mgr.run_dir / "run.yaml").read_text("utf-8")


async def test_a2_the_old_topologies_get_no_private_channel(workfolder: Path) -> None:
    from arr.runs.topologies import solo as old_solo

    mgr = make_manager(
        workfolder,
        old_solo.run,
        {"a": turns([room("submit_result", kind="proof", title="T", body="B")])},
        run_extra={"topology": {"kind": "solo"}},
    )
    assert await mgr.run() is RunStatus.DONE, errors(mgr)
    assert mgr.room.channels() == []
    assert not [d for _, d in entries(mgr, "channel_open") if d.get("by") == "private"]


async def test_a2_the_name_is_a_safe_folder_and_file_name(workfolder: Path) -> None:
    """An attachment in the private channel lies in `attachments/user.a/<msg>/`, the export
    in `groupchats/user.a.md|jsonl`."""

    def a(view: TurnView) -> list[dict[str, Any]]:
        (view.work_dir / "notiz.md").write_text("Rechnung.", "utf-8")
        return [
            say(private("a"), "@user Siehe Anhang.", kind="question", refs=["notiz.md"]),
            *hand_in(view),
        ]

    mgr, log = manager(workfolder, {**AGENTS, "phases": [solo("p", "a")]}, {"a": a})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    [msg] = [m for m in mgr.room.messages(private("a")) if m.sender == "a"]
    path = mgr.run_dir / msg.refs[0].path
    assert path.is_file() and path.parent.parent.name == "user.a"
    assert path.parent.parent.parent == mgr.run_dir / "groupchats" / "attachments"
    exported = mgr.room.export_channel(private("a"))
    assert exported.markdown == mgr.run_dir / "groupchats" / "user.a.md"
    assert exported.jsonl.name == "user.a.jsonl" and exported.messages == 2


# --------------------------------------------------------------------------------------------
# A3: no other agent gets in
# --------------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> RoomStore:
    run = tmp_path / "run"
    for agent in ("a", "b"):
        (run / "agents" / agent / "work").mkdir(parents=True)
    store = RoomStore.create(run)
    from arr.models import GroupChatConfig

    store.create_channel(GroupChatConfig(name="k", members=["a", "b"]))
    store.create_private_channel("a")
    store.create_private_channel("b")
    (run / "agents" / "a" / "work" / "geheim.md").write_text("geheim", "utf-8")
    return store


def same(error: str, name: str, missing: str) -> str:
    return error.replace(name, missing)


def test_a3_another_agent_gets_into_no_private_channel(store: RoomStore) -> None:
    a, b = store.for_agent("a"), store.for_agent("b")
    secret = a.post_message(
        "user.a", "Geheime Frage", kind="question", to=["user"], refs=["geheim.md"]
    )

    def error(call: Any, arg: Any) -> str:
        with pytest.raises(RoomError) as info:
            call(arg)
        return str(info.value)

    channel_calls = (
        lambda ch: b.read_messages(ch),
        lambda ch: b.post_message(ch, "Hallo"),
        lambda ch: b.read_messages(ch, only_new=False),
    )
    for call in channel_calls:
        mine = error(call, "user.a")
        assert mine == same(error(call, "user.zz"), "user.zz", "user.a")
        assert mine == same(error(call, "gibtsnicht"), "gibtsnicht", "user.a")
    missing = 10_000
    for call in (
        lambda m: b.read_messages(thread=m),
        lambda m: b.get_attachment(m, "geheim.md"),
    ):
        assert error(call, secret.id) == same(error(call, missing), str(missing), str(secret.id))
    assert "gibt es in #k nicht" in error(
        lambda m: b.post_message("k", "Antwort", reply_to=m), secret.id
    )
    assert [c.name for c in b.list_channels()] == ["k", "user.b"]
    assert [c.name for c in a.list_channels()] == ["k", "user.a"]
    assert "user.a" not in json.dumps(b.inbox().model_dump(mode="json"))
    assert [m.channel for m in b.read_messages()] == ["k"], "nothing of user.a"
    assert a.read_messages("user.a") == []  # the owner reads it (its brief counts as read)


async def test_a3_over_the_room_server_the_answer_equals_a_missing_channel(
    store: RoomStore,
) -> None:
    async with Client(build_server(store.for_agent("b"))) as client:
        texts = {}
        for name in ("user.a", "user.zz"):
            result = await client.call_tool("post_message", {"channel": name, "body": "x"})
            assert result.is_error
            texts[name] = result.content[0].text
        assert texts["user.a"] == texts["user.zz"].replace("user.zz", "user.a")
        listed = (await client.call_tool("list_channels", {})).content[0].text
        assert "#user.b" in listed and "user.a" not in listed


async def test_a3_channel_messages_and_packages_never_carry_it(workfolder: Path) -> None:
    """`channel_messages` refuses a private channel like an unknown one; no context package and
    no prompt of another agent names it."""
    seen: dict[str, Any] = {}

    def a(view: TurnView) -> list[dict[str, Any]]:
        return [say(private("a"), "Notiz an mich.", to=["user"]), *hand_in(view)]

    def b(view: TurnView) -> list[dict[str, Any]]:
        orch = holder["mgr"].orchestrator
        for name in ("user.a", "gibtsnicht"):
            try:
                orch.channel_messages(name)
            except ArrError as exc:
                seen[name] = str(exc)
        return hand_in(view)

    holder: dict[str, Any] = {}
    phases = [solo("p", "a"), {**solo("q", "b"), "inputs": [{"from": "p", "content": "full"}]}]
    mgr, log = manager(workfolder, {**AGENTS, "phases": phases}, {"a": a, "b": b})
    holder["mgr"] = mgr
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert seen["user.a"] == seen["gibtsnicht"].replace("gibtsnicht", "user.a")
    for view in log.views:
        if view.agent != "a":
            text = view.prompt + json.dumps(view.context, ensure_ascii=False)
            assert "user.a" not in text, view.agent
    [own] = log.of("a")
    assert own.context["user_channel"] == "user.a" and "#user.a" in own.prompt


async def test_a3_the_attachment_folder_is_in_no_read_list_of_another(workfolder: Path) -> None:
    """Profile level, both backends: the folder `groupchats/attachments/user.a/` is readable for
    no other agent (Claude: read list of the profile; Codex: `attachments` is hidden, only the
    channels of `run.yaml` are opened), while the attachments of a shared channel are."""

    def a(view: TurnView) -> list[dict[str, Any]]:
        (view.work_dir / "x.md").write_text("x", "utf-8")
        return [
            say(private("a"), "Anhang.", refs=["x.md"]),
            say("k", "Anhang für b.", refs=["x.md"]),
            *hand_in(view),
        ]

    group = {
        "agents": [
            {"id": "a"},
            {"id": "b", "provider": "codex", "model": "gpt-6", "auth": "fake-auth"},
            {"id": "c"},
        ],
        "channels": [{"name": "k", "members": ["a", "b", "c"]}],
        "phases": [{**solo("p", "a"), "channels": ["k"]}],
    }
    mgr, log = manager(workfolder, group, {"a": a})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    run_dir = mgr.run_dir
    att = run_dir / "groupchats" / "attachments"
    [private_file] = list((att / "user.a").rglob("x.md"))
    [shared_file] = list((att / "k").rglob("x.md"))
    paths = RunPaths(workfolder, run_dir)
    for agent in mgr.resolved.agents:
        profile = profile_for(agent, paths)
        if agent.id != "a":
            assert not profile.can_read(private_file), agent.id
        assert profile.can_read(shared_file), agent.id
        if agent.id == "b":  # Codex
            from arr.backends.codex import _run_channels

            assert _run_channels(run_dir) == ["k"]
            found = filesystem_entries(profile, paths, agent.channels, run_channels=["k"])
            assert codex_access(found, private_file) == "deny"
            assert codex_access(found, shared_file) == "read"


def codex_access(entries: dict[str, str], path: Path) -> str:
    """The most specific path entry (as the Codex sandbox applies it)."""
    text = str(path)
    best, access = "", entries.get(":root", "deny")
    for key, value in entries.items():
        if any(ch in key for ch in "*?[{") or key == ":root":
            continue
        if (text == key or text.startswith(key.rstrip("/") + "/")) and len(key) > len(best):
            best, access = key, value
    return access


# --------------------------------------------------------------------------------------------
# A4: always open, also in an isolated phase
# --------------------------------------------------------------------------------------------


async def test_a4_in_an_isolated_phase_only_the_private_channel_is_open(workfolder: Path) -> None:
    seen: dict[str, dict[str, str]] = {}
    holder: dict[str, Any] = {}

    def judge(view: TurnView) -> list[dict[str, Any]]:
        mine = holder["mgr"].room.for_agent(view.agent)
        out: dict[str, str] = {}
        for name, call in (
            (
                "private",
                lambda: mine.post_message(private(view.agent), "@user Frage?", kind="question"),
            ),
            ("read_k", lambda: mine.read_messages("k")),
            ("write_k", lambda: mine.post_message("k", "Unter uns?")),
        ):
            try:
                call()
                out[name] = "ok"
            except RoomError as exc:
                out[name] = str(exc)
        seen[view.agent] = out
        return hand_in(view, verdict="accept")

    phase = {**parallel("pruefung", ["a", "b"], "review"), "isolated": True}
    group = {**AGENTS, "channels": [{"name": "k", "members": ["a", "b"]}], "phases": [phase]}
    mgr, log = manager(workfolder, group, {"a": judge, "b": judge})
    holder["mgr"] = mgr
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    for agent in ("a", "b"):
        assert seen[agent]["private"] == "ok"
        assert seen[agent]["read_k"].startswith("Der Kanal #k ist gerade geschlossen.")
        assert seen[agent]["write_k"].startswith("Der Kanal #k ist gerade geschlossen.")
    assert {d["channel"] for d in of_kind(mgr, "user_question", state="open")} == {
        "user.a",
        "user.b",
    }


# --------------------------------------------------------------------------------------------
# A5: a question to the user from the private channel
# --------------------------------------------------------------------------------------------


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


@pytest.mark.parametrize("way", ["inbox", "channel", "user_agent"])
async def test_a5_the_question_becomes_an_item_and_only_the_user_answers(
    workfolder: Path,
    arr_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    way: str,
) -> None:
    """The question is an item „Frage“ with the private channel and in the log. The answer
    through the inbox (`_post_answer` of the service) or in the channel closes it and reaches
    only the asker; a message of Claude Code (`via: user_agent`) answers nothing. The end of
    the run closes what is open. Afterwards the service reads the run without an error."""
    from arr.runs.phases import wake
    from arr.service.real_api.approvals import _post_answer

    hold = {"s": 600.0}
    monkeypatch.setattr(wake, "user_question_hold_s", lambda: hold["s"])
    found: dict[str, Any] = {}

    async def user(mgr: Any) -> None:
        await eventually(lambda: question_item(mgr) is not None, within=30)
        item = question_item(mgr)
        found["item"] = item
        await eventually(lambda: bool(of_kind(mgr, "waiting", reason="user")), within=30)
        if way == "inbox":  # what `decide_approval` of the service does
            service_item = SimpleNamespace(
                agent=SimpleNamespace(id="a"),
                channel=item.question.channel,
                msg_id=item.question.msg_id,
            )
            _post_answer(mgr.room, service_item, "Ja, n ist prim.")
            decision = Decision(
                action=DecisionAction.ANSWER, decided_by=DecidedBy.USER, comment="Ja, n ist prim."
            )
            with contextlib.suppress(ArrError):  # the orchestrator may have seen it first
                mgr.inbox.resolve(item.id, decision)
        else:
            via = MessageVia.GUI if way == "channel" else MessageVia.USER_AGENT
            mgr.room.post(
                private("a"),
                "Ja, n ist prim.",
                sender=USER,
                kind=MessageKind.ANSWER,
                to=["a"],
                reply_to=item.question.msg_id,
                via=via,
            )
            if way == "user_agent":
                await eventually(
                    lambda: mgr.orchestrator._last_seen >= item.question.msg_id + 1, 30
                )
                found["still_open"] = mgr.orchestrator.user_question_open(item.question.msg_id)
                hold["s"] = 0.0

    phase = parallel("p", ["a", "b"], turns=1)
    mgr, log = manager(
        workfolder, {**AGENTS, "phases": [phase]}, {"a": asks_then_hands_in, "b": hand_in_at_once}
    )
    assert await run_with(mgr, user) is RunStatus.DONE, errors(mgr)
    item = found["item"]
    assert (item.type, item.agent_id, item.question.channel) == (
        ApprovalType.QUESTION,
        "a",
        "user.a",
    )
    states = [d["state"] for d in of_kind(mgr, "user_question")]
    final = mgr.inbox.get(item.id)
    if way == "user_agent":
        assert found["still_open"], "Claude Code answers no question to the user (E15)"
        assert states == ["open", "closed"] and final.status is ApprovalStatus.OBSOLETE
        assert not [v for v in log.of("a") if "beantwortet" in v.prompt]
    else:
        assert states == ["open", "answered"] and final.status is ApprovalStatus.DECIDED
        [after] = [v for v in log.of("a") if "Der User hat deine Frage beantwortet" in v.prompt]
        assert "Ja, n ist prim." in after.prompt
        assert all("Ja, n ist prim." not in v.prompt for v in log.of("b")), "only the asker"
    # the service reads the run with private channels without an error
    from arr.service.real_api import RealApi

    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    api = RealApi(workfolder, base_url="http://127.0.0.1:8765", scan=False)
    run_id = mgr.prepared.run_id
    detail = await api.get_run(run_id)
    assert detail.run.id == run_id
    channels = {c.name: c for c in await api.list_channels(run_id)}
    assert {"user.a", "user.b", "user.c"} <= set(channels)
    page = await api.get_messages(run_id, "user.a")
    assert [m.kind for m in page.items][:2] == ["note", "question"]
    assert [a.id for a in await api.list_agents(run_id)] == ["a", "b", "c"]
    assert await api.get_log(run_id)
