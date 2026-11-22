"""W4b-04.1: the rework of `moderated` after the review.

- H1 (Entscheidung 82, Variante a): the lead's turns and follow-up are used up without a
  submission and it has an open question to the user: the moderation holds the phase by the
  rule of B3; the answer gives it a turn, and assignments from that turn run as usual (return
  turns, Entscheidung 79). Without an answer the phase ends after the deadline; after the end
  of the moderation `assign` refuses.
- M1: one rule for names, that of `inputs.shown`: the lead names a recipient with an alias by
  the alias its package shows, `assign` shows only that name, never the ID behind it.
- N3: the packages and the return turn keep their limits with 50 assignments.
- N5: `plan_launch` takes a run with `moderated`. N6: what the lead reads (rendered).

Partly after the review's probes `W4b-04_Review_Skripte/test_rv04_flow.py` (R1, R1b, R2, R9)
and `test_rv04_launch.py`."""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from phases_kit import (
    Hooks,
    TurnView,
    checkpoints,
    delegations,
    errors,
    gate,
    give,
    hand_in,
    is_assignment,
    is_return,
    manager,
    moderated_phase,
    of_kind,
    private,
    run,
    run_with,
    say,
    silent,
    solo,
)
from run_kit import eventually, make_workfolder

from arr.models import PhaseTopology, RunStatus
from arr.room.server import build_server
from arr.room.store import USER, AgentRoom, RoomStore
from arr.room.types import DelegationStatus, MessageKind, MessageVia, RoomError
from arr.runs import pacing
from arr.runs.phases import moderated, wake
from arr.runs.phases.text import MAX_PACKAGE_CHARS, package_size

AGENTS = {"agents": [{"id": a} for a in ("a", "b", "c", "x")]}
ASK = "@user Soll ich nach Fall A oder B aufteilen?"
ANSWERED = "Der User hat deine Frage beantwortet"


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


@pytest.fixture
def hold(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    value = {"s": 600.0}
    monkeypatch.setattr(wake, "user_question_hold_s", lambda: value["s"])
    monkeypatch.setattr(pacing, "user_question_hold_s", lambda: value["s"])
    return value


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, Any]]:
    """Every `assign` and `submit` of the run: (agent, tool, result or „ERROR: …“)."""
    out: list[tuple[str, str, Any]] = []
    for name in ("assign", "submit"):
        original = getattr(AgentRoom, name)

        def wrapped(self: AgentRoom, *a: Any, _o: Any = original, _n: str = name, **k: Any) -> Any:
            try:
                result = _o(self, *a, **k)
            except RoomError as exc:
                out.append((self.agent_id, _n, f"ERROR: {exc}"))
                raise
            out.append((self.agent_id, _n, result))
            return result

        monkeypatch.setattr(AgentRoom, name, wrapped)
    return out


def first(view: TurnView) -> bool:
    return "# Auftrag – Phase" in view.prompt and view.action == "work"


def user_post(mgr: Any, channel: str, body: str, **args: Any) -> int:
    return mgr.room.post(channel, body, sender=USER, via=MessageVia.GUI, **args).id


def answer_in(mgr: Any, agent: str) -> None:
    channel = private(agent)
    [question] = [m for m in mgr.room.messages(channel) if m.kind is MessageKind.QUESTION]
    user_post(mgr, channel, "Fall A.", kind=MessageKind.ANSWER, to=[agent], reply_to=question.id)


def waits(mgr: Any, channel: str) -> bool:
    return any(d.get("channel") == channel for d in of_kind(mgr, "waiting", reason="user"))


async def answering(mgr: Any) -> None:
    await eventually(lambda: waits(mgr, private("a")), within=30)
    answer_in(mgr, "a")


def kinds(log: Any) -> list[str]:
    out = []
    for v in log.of("a"):
        if ANSWERED in v.prompt:
            out.append("answer")
        elif is_return(v):
            out.append("return")
        elif "Nachfrage zu deiner Action" in v.prompt:
            out.append("ask")
        elif first(v):
            out.append("first")
        else:
            out.append("continue")
    return out


# --------------------------------------------------------------------------------------------
# H1
# --------------------------------------------------------------------------------------------


async def test_h1_ask_first_then_delegate_in_the_answer_turn(
    workfolder: Path, hold: dict[str, float], calls: list[Any]
) -> None:
    """R1b: the lead asks in its first turn and ends its turns without `wait_until`; the user
    answers while the moderation holds; the answer turn gives two assignments, both run and
    come back, the lead hands in in its return turn."""

    def a(view: TurnView) -> list[dict[str, Any]]:
        if ANSWERED in view.prompt:
            return [give("b", "Teil 1"), give("c", "Teil 2")]
        if first(view):
            return [say(private("a"), ASK, kind="question")]
        if is_return(view):
            back = [r for r in view.context["assignments"] if r["status"] == "returned"]
            return hand_in(view) if len(back) == 2 else silent(view)
        return []

    policies = {"a": a, "b": lambda v: hand_in(v), "c": lambda v: hand_in(v)}
    mgr, log = manager(
        workfolder, {**AGENTS, "phases": [moderated_phase("a", ["b", "c"])]}, policies
    )
    assert await run_with(mgr, answering) is RunStatus.DONE, errors(mgr)
    rows = delegations(mgr)
    assert [(d.number, d.agent, d.status) for d in rows] == [
        (1, "b", DelegationStatus.RETURNED),
        (2, "c", DelegationStatus.RETURNED),
    ]
    assert kinds(log)[:5] == ["first", "continue", "continue", "ask", "answer"]
    assert set(kinds(log)[5:]) == {"return"}
    assert [r for _, t, r in calls if t == "assign" and isinstance(r, str)] == []
    lead = next(s for s in mgr.orchestrator.submissions(agent="a"))
    [gate_] = checkpoints(mgr, "gate")
    assert gate_["results"] == [lead.id] and gate_["missing"] == []


async def test_h1_assign_and_submit_in_the_answer_turn_after_the_lead_ran_out(
    workfolder: Path, hold: dict[str, float], calls: list[Any]
) -> None:
    """R1: `turns: 1`; in the answer turn the lead gives #1 and hands in: the submission is
    refused and names #1 (Entscheidung 79); #1 runs and comes back, the return turn hands in
    and is accepted; the next phase gets the lead's submission."""

    def a(view: TurnView) -> list[dict[str, Any]]:
        if ANSWERED in view.prompt:
            return [give("b", "Noch schnell"), *hand_in(view)]
        if first(view):
            return [say(private("a"), ASK, kind="question")]
        return hand_in(view) if is_return(view) else []

    phase = moderated_phase("a", ["b", "c"], turns=1)
    group = {**AGENTS, "phases": [phase, solo("s", "x")]}
    mgr, log = manager(
        workfolder, group, {"a": a, "b": lambda v: hand_in(v), "x": lambda v: hand_in(v)}
    )
    assert await run_with(mgr, answering) is RunStatus.DONE, errors(mgr)
    submits = [r for ag, t, r in calls if ag == "a" and t == "submit"]
    assert isinstance(submits[0], str) and "#1 an b" in submits[0]
    assert not isinstance(submits[-1], str)
    assert [d.status for d in delegations(mgr)] == [DelegationStatus.RETURNED]
    assert kinds(log) == ["first", "ask", "answer", "return"]
    [gate_] = [g for g in checkpoints(mgr, "gate") if g["agent"] == "a"]
    assert gate_["missing"] == [] and gate_["results"] == [submits[-1].id]
    x = log.of("x")[0]
    assert [i["id"] for i in x.context.get("inputs") or []] == [submits[-1].id]


async def test_h1_without_an_answer_the_phase_ends_after_the_deadline(
    workfolder: Path, hold: dict[str, float]
) -> None:
    hold["s"] = 0.0  # the deadline of B3 has passed when the turns are used up

    def a(view: TurnView) -> list[dict[str, Any]]:
        return [say(private("a"), ASK, kind="question")] if first(view) else []

    mgr, log = manager(
        workfolder, {**AGENTS, "phases": [moderated_phase("a", ["b", "c"])]}, {"a": a}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert kinds(log) == ["first", "continue", "continue", "ask"]
    [gate_] = checkpoints(mgr, "gate")
    assert gate_["missing"] == ["a/work"]


async def test_h1_after_the_moderation_closed_assign_refuses(workfolder: Path) -> None:
    """The lead never hands in: after the pass its last assignment still is its action, but
    the pass is closed in the room: `assign` refuses with a clear sentence and writes nothing
    (before W4b-04.1 it answered „… beginnt sofort damit“ and the assignment never ran)."""
    mgr, log = manager(
        workfolder, {**AGENTS, "phases": [moderated_phase("a", ["b", "c"])]}, {"a": silent}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    room = RoomStore(mgr.run_dir)
    assert room.delegations_closed("m", 1)
    with pytest.raises(RoomError, match="Die Phase „m“ endet"):
        room.for_agent("a").assign("b", "Zu spät", "text")
    assert room.delegations() == []


def test_h1_closing_refuses_assign_in_the_room_and_a_new_pass_is_open(tmp_path: Path) -> None:
    from test_phases_moderated_room import make_room, turn

    store = make_room(tmp_path)
    turn(store, "lead")
    lead = store.for_agent("lead")
    lead.assign("sx", "1", "text")
    assert not store.delegations_closed("m", 1)
    [cancelled] = store.cancel_delegations("m", 1)
    assert cancelled.status is DelegationStatus.CANCELLED and store.delegations_closed("m", 1)
    with pytest.raises(RoomError, match="keine Aufträge"):
        lead.assign("sx", "2", "text")
    assert len(store.delegations()) == 1
    assert store.cancel_delegations("m", 1) == []  # idempotent
    turn(store, "lead", attempt=2, number=2)
    assert lead.assign("sx", "3", "text").attempt == 2


async def test_h1_a_stop_while_the_moderation_holds_for_the_answer(
    workfolder: Path, hold: dict[str, float]
) -> None:
    import asyncio
    import time

    def a(view: TurnView) -> list[dict[str, Any]]:
        return [say(private("a"), ASK, kind="question")] if first(view) else []

    mgr, log = manager(
        workfolder,
        {**AGENTS, "phases": [moderated_phase("a", ["b", "c"], turns=1)]},
        {"a": a},
        limits={"wall_time": "60s"},
    )
    task = asyncio.create_task(run(mgr, within=60))
    await eventually(lambda: waits(mgr, private("a")), within=30)
    mgr.stop()
    stopped = time.monotonic()
    assert await task is RunStatus.STOPPED
    assert time.monotonic() - stopped < 10, "within the manager's grace"
    assert RoomStore(mgr.run_dir).delegations_closed("m", 1)


# --------------------------------------------------------------------------------------------
# M1: one rule for names
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lead_shares", [False, True], ids=["lead-ohne-kanal", "lead-in-k"])
async def test_m1_the_lead_names_a_recipient_by_the_alias_its_package_shows(
    workfolder: Path, calls: list[Any], lead_shares: bool
) -> None:
    """b has the alias „Mitglied A“ in the anonymous channel `anon` (with x), the lead is not
    in it. The package shows the alias; `assign` takes it, not the ID; answers, errors, the
    warning of `submit` and the return turn show only the alias."""
    seen: dict[str, Any] = {}

    def a(view: TurnView) -> list[dict[str, Any]]:
        if first(view):
            rules = view.context.get("assign_rules") or {}
            seen["standby"] = list(rules.get("standby") or [])
            alias = seen["standby"][0]
            return [give(alias, "Per Alias"), give("b", "Per ID"), *hand_in(view)]
        if is_return(view):
            seen["return"] = view.prompt
        return hand_in(view)

    channels = [{"name": "anon", "members": ["b", "x"], "anonymous": True}]
    extra: dict[str, Any] = {}
    if lead_shares:
        channels.append({"name": "k", "members": ["a", "b"]})
        extra["channels"] = ["k"]
    phase = moderated_phase("a", ["b"], **extra)
    group = {
        **AGENTS,
        "channels": channels,
        "phases": [solo("vorher", "x", channels=["anon"]), phase],
    }
    mgr, log = manager(
        workfolder, group, {"a": a, "b": lambda v: hand_in(v), "x": lambda v: hand_in(v)}
    )
    assert await run_with(mgr, lambda m: eventually(lambda: True)) is RunStatus.DONE, errors(mgr)
    alias = seen["standby"][0]
    assert alias.startswith("Mitglied ") and seen["standby"] == [alias]
    lead = [(t, r) for ag, t, r in calls if ag == "a"]
    given = lead[0][1]
    assert not isinstance(given, str) and given.agent == "b"  # the alias was taken
    refused = lead[1][1]
    assert isinstance(refused, str) and "kein Agent auf Abruf" in refused
    held = lead[2][1]
    assert isinstance(held, str) and f"#1 an {alias}" in held
    for text in (refused, held):
        named = text.split("„b“", 1)[-1]  # the echo of the lead's own word aside
        assert " b " not in f" {named} ".replace(",", " ").replace(".", " "), text
    from arr.room.server import render_delegation

    store = RoomStore(mgr.run_dir)
    answer = render_delegation(store.for_agent("a"), store.delegations()[0])
    assert answer.startswith(f"Auftrag #1 an {alias} vergeben") and " b " not in answer
    assert f'"agent": "{alias}"' in seen["return"] and '"agent": "b"' not in seen["return"]


def test_m1_room_rule_matches_inputs_shown(tmp_path: Path) -> None:
    """In the room the lead outside the anonymous channel sees the alias by the same rule as
    `inputs.shown`; a recipient without an alias is named by its ID."""
    from test_phases_moderated_room import make_room, turn

    from arr.models import GroupChatConfig

    store = make_room(tmp_path)
    store.create_channel(GroupChatConfig(name="anon", members=["sx", "zz"], anonymous=True))
    turn(store, "lead")
    lead = store.for_agent("lead")
    alias = lead.seen_as("sx")
    assert alias.startswith("Mitglied ") and lead.seen_as("sy") == "sy"
    assert lead.shown_name("sx") == "sx"  # the old rule of the room would show the ID
    with pytest.raises(RoomError) as info:
        lead.assign("sx", "x", "text")
    assert f"Aufträge gehen an: {alias}, sy." in str(info.value)
    assert lead.assign(alias, "x", "text").agent == "sx"
    assert lead.assign("sy", "y", "text").agent == "sy"


# --------------------------------------------------------------------------------------------
# N3: limits with 50 assignments
# --------------------------------------------------------------------------------------------


async def test_n3_the_packages_and_the_return_turn_keep_their_limits(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task of 60 000 characters, 50 assignments, summaries of 800 characters: every package
    of the lead stays within `MAX_PACKAGE_CHARS` (the list is cut with a note), every return
    turn within its limit, the recipient's package as well."""
    long = "Kurzfassung " + "x" * 780

    def a(view: TurnView) -> list[dict[str, Any]]:
        if first(view):  # held until all 50 came back: one return turn for all
            return [
                *(give("b" if i % 2 else "c", f"Teil {i}: " + "y" * 3000) for i in range(50)),
                gate(),
            ]
        back = [r for r in view.context["assignments"] if r["status"] != "queued"]
        return (
            hand_in(view)
            if len(back) == 50 and not any(r["status"] == "running" for r in back)
            else silent(view)
        )

    def worker(view: TurnView) -> list[dict[str, Any]]:
        return hand_in(view, summary=long)

    phase = moderated_phase("a", ["b", "c"], jobs=50)
    tasks = {"tasks/m.md": "Auftrag. " + "z" * 60_000}
    hooks = Hooks().install(monkeypatch)

    async def helper(mgr: Any) -> None:
        await eventually(lambda: len(of_kind(mgr, "assign_end")) == 50, within=100)
        hooks.open("a")

    mgr, log = manager(
        workfolder,
        {**AGENTS, "phases": [phase]},
        {"a": a, "b": worker, "c": worker},
        tasks=tasks,
        task="Zeige es. " + "q" * 30_000,
    )
    assert await run_with(mgr, helper, within=120) is RunStatus.DONE, errors(mgr)
    assert len(delegations(mgr)) == 50
    for view in log.views:
        assert package_size(view.context) <= MAX_PACKAGE_CHARS, (view.agent, view.action)
    cut = [v for v in log.of("a") if v.context.get("assignments_cut")]
    assert cut, "the list of the lead had to be cut"
    for view in [v for v in log.of("a") if is_return(v)]:
        block = view.prompt.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
        assert len(block) <= moderated.MAX_RETURN_CHARS + 100
        data = json.loads(block)
        if data.get("returned_cut"):
            assert "Die Liste ist gekürzt" in view.prompt
    [ret] = [v for v in log.of("a") if is_return(v)]  # all 50 came back during turn 1
    data = json.loads(ret.prompt.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    assert data["returned_cut"] is True and "Die Liste ist gekürzt" in ret.prompt
    assert data["returned"][-1]["number"] == 50 and len(data["returned"]) < 50


async def test_n3_the_recipient_package_counts_the_lead_text_within_the_limit(
    workfolder: Path,
) -> None:
    """A long task of the run, a full input of 20 000 characters and the lead's text of
    4 000: the recipient's package would pass `MAX_PACKAGE_CHARS`; `fit_package` drops the
    full text of the input (with its note) instead."""

    def a(view: TurnView) -> list[dict[str, Any]]:
        return [give("b", "w" * 4000)] if first(view) else hand_in(view)

    def x(view: TurnView) -> list[dict[str, Any]]:
        return hand_in(view, body="v" * 19_900)

    phase = {**moderated_phase("a", ["b", "c"]), "inputs": [{"from": "vorher", "content": "full"}]}
    group = {**AGENTS, "phases": [solo("vorher", "x"), phase]}
    mgr, log = manager(
        workfolder,
        group,
        {"a": a, "b": lambda v: hand_in(v), "x": x},
        task="Zeige es. " + "q" * 78_000,
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    [view] = [v for v in log.of("b") if is_assignment(v)]
    assert package_size(view.context) <= MAX_PACKAGE_CHARS
    assert view.context["assigned"]["text"] == "w" * 4000
    [item] = view.context["inputs"]
    assert item["text"] is None and "zu lang" in item["note"]


# --------------------------------------------------------------------------------------------
# N5, N6
# --------------------------------------------------------------------------------------------


def test_n5_plan_launch_takes_moderated(
    workfolder: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_phases_start import RUN_FILE, launch_setup

    from arr.runs.launch import plan_launch

    run_file = launch_setup(workfolder, arr_home, monkeypatch)
    text = RUN_FILE.replace(
        "topology: solo\n", "topology: moderated\n    max_assignments: 2\n    standby: [b]\n"
    )
    run_file.write_text(text, "utf-8")
    launch = plan_launch(run_file, cwd=workfolder)
    assert launch.resolved.phases[0].topology is PhaseTopology.MODERATED


async def test_n6_the_lead_reads_its_rules_and_the_list(workfolder: Path) -> None:
    """R9: the rendered first prompt shows `assign_rules`, `get_assignment` (MCP) in the
    return turn shows the list `assignments` with the returned one."""
    seen: dict[str, str] = {}

    def a(view: TurnView) -> list[dict[str, Any]]:
        if first(view):
            seen["first"] = view.prompt
            return [give("b", "1")]
        return hand_in(view)

    mgr, log = manager(
        workfolder,
        {**AGENTS, "phases": [moderated_phase("a", ["b", "c"])]},
        {"a": a, "b": lambda v: hand_in(v)},
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    async with Client(build_server(RoomStore(mgr.run_dir).for_agent("a"))) as client:
        shown = (await client.call_tool("get_assignment", {})).content[0].text  # type: ignore[union-attr]
    assert '"assign_rules"' in seen["first"] and '"max_assignments": 3' in seen["first"]
    assert "## Aufträge an Agenten auf Abruf" in seen["first"]
    assert '"assignments"' in shown and '"status": "returned"' in shown
    assert not [v for v in log.of("b") if not is_assignment(v)]
