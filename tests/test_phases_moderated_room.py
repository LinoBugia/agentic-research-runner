"""W4b-04: the room side of `moderated` (A2–A7, A12, A23): the tool `assign` in the tool list,
who may call it, recipients, formats, the text, `max_assignments` (also from two processes at
once), the lead's `submit` with open assignments (Entscheidung 79, also as a race of two
processes), a late submission of a cancelled assignment, a room without the table.

The room is built from a resolved run (`run.yaml`) without a runner; the assignment of each
turn is written as the runner writes it (`add_assignment` with the context of the action)."""

import json
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from phases_kit import manager, moderated_phase, solo
from run_kit import make_workfolder

from arr.models import GroupChatConfig
from arr.resolve import dump_yaml_model
from arr.room.server import build_server
from arr.room.store import (
    ASSIGN_FORMATS,
    MAX_ASSIGN_TASK_CHARS,
    NOTE_TURN_REFUSED,
    RoomStore,
)
from arr.room.types import AssignAPI, DelegationStatus, RoomError
from arr.runs.phases.text import MAX_ENTRY_TEXT_CHARS

AGENTS = ["lead", "sx", "sy", "zz", "rr"]
OWN = {"fields": {"ansatz": {"type": "choice", "choices": ["a", "b"]}}}


def group(
    *,
    jobs: int = 3,
    anonymous: bool = False,
    gate: dict[str, Any] | None = None,
    standby: list[str] | None = None,
) -> dict[str, Any]:
    phase = moderated_phase("lead", standby or ["sx", "sy"], jobs=jobs, channels=["k"])
    if gate is not None:
        phase["gate"] = gate
    return {
        "agents": [{"id": a} for a in AGENTS],
        "formats": {"befund": OWN},
        "channels": [{"name": "k", "members": ["lead", "sx", "sy"], "anonymous": anonymous}],
        "phases": [phase, solo("s", "zz")],
    }


def make_room(tmp_path: Path, **kwargs: Any) -> RoomStore:
    """A room of a run with a phase `moderated` „m“ (lead `lead`, on standby `sx`, `sy`;
    `zz` does `solo` „s“; `rr` takes no part) and the channel `k`."""
    workfolder = make_workfolder(tmp_path / "wf")
    mgr, _ = manager(workfolder, group(**kwargs), {})
    resolved = mgr.resolved
    run_dir = tmp_path / "run"
    for a in AGENTS:
        (run_dir / "agents" / a / "work").mkdir(parents=True)
    (run_dir / "run.yaml").write_text(dump_yaml_model(resolved), encoding="utf-8")
    store = RoomStore.create(run_dir)
    store.setup_channels(list(resolved.groupchats))
    return store


def turn(
    store: RoomStore,
    agent: str,
    *,
    stage: str = "m",
    attempt: int = 1,
    action: str | None = "work",
    fmt: str = "text",
    number: int = 1,
) -> None:
    """The assignment of a turn as the phase runner writes it (`action=None`: a wake-up turn
    of a sleeper, without a submission)."""
    context: dict[str, Any] = {"phase": stage, "attempt": attempt}
    if action is None:
        context["standby"] = True
    else:
        context["submission"] = {"format": fmt, "action": action}
    store.add_assignment(agent, number, "Turn.", stage=stage, context=context)


def hand_in(store: RoomStore, agent: str) -> Any:
    return store.for_agent(agent).submit("done", "Fertig.", body="Text.")


@pytest.fixture
def store(tmp_path: Path) -> RoomStore:
    s = make_room(tmp_path)
    turn(s, "lead")
    return s


# --------------------------------------------------------------------------------------------
# A2: offered in every phases run, schema of three required texts
# --------------------------------------------------------------------------------------------


async def test_a2_assign_in_the_tool_list_of_every_agent_with_three_texts(
    store: RoomStore,
) -> None:
    for agent in AGENTS:
        async with Client(build_server(store.for_agent(agent))) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "assign" in tools, agent
        schema = tools["assign"].input_schema
        assert sorted(schema["required"]) == ["agent", "format", "task"]
        assert {k: v["type"] for k, v in schema["properties"].items()} == {
            "agent": "string",
            "task": "string",
            "format": "string",
        }
        assert "Lead" in (tools["assign"].description or "")
    assert isinstance(store.for_agent("lead"), AssignAPI)


async def test_a2_assign_over_the_mcp_server(store: RoomStore) -> None:
    async with Client(build_server(store.for_agent("lead"))) as client:
        result = await client.call_tool(
            "assign", {"agent": "sx", "task": "Rechne.", "format": "text"}
        )
        text = result.content[0].text  # type: ignore[union-attr]
        assert "Auftrag #1 an sx vergeben" in text and "Vergeben: 1 von 3" in text
        assert "ungelesen:" in text  # with the footer
        second = await client.call_tool(
            "assign", {"agent": "sx", "task": "Mehr.", "format": "list"}
        )
        assert "wartet dahinter" in second.content[0].text  # type: ignore[union-attr]
    async with Client(build_server(store.for_agent("sx"))) as client:
        refused = await client.call_tool("assign", {"agent": "sy", "task": "x", "format": "text"})
        assert refused.is_error
        assert "nur der Lead" in refused.content[0].text  # type: ignore[union-attr]
    assert [d.number for d in store.delegations()] == [1, 2]


# --------------------------------------------------------------------------------------------
# A3: who may assign
# --------------------------------------------------------------------------------------------


def refused(store: RoomStore, agent: str, *args: str, match: str = "nur der Lead") -> None:
    before = len(store.delegations())
    with pytest.raises(RoomError, match=match):
        store.for_agent(agent).assign(*(args or ("sx", "Auftrag.", "text")))
    assert len(store.delegations()) == before  # nothing written


def test_a3_the_lead_may(store: RoomStore) -> None:
    given = store.for_agent("lead").assign("sx", "Rechne Teil 1.", "text")
    assert (given.number, given.lead, given.agent, given.status) == (
        1,
        "lead",
        "sx",
        DelegationStatus.QUEUED,
    )


def test_a3_a_sleeper_in_its_wake_up_turn(store: RoomStore) -> None:
    turn(store, "sx", action=None)
    refused(store, "sx", "sy", "x", "text")
    store.for_agent("lead").assign("sx", "x", "text")  # the lead of the same pass may


def test_a3_a_recipient_in_its_assignment_no_nesting(store: RoomStore) -> None:
    store.for_agent("lead").assign("sx", "x", "text")
    turn(store, "sx", action="assign-1")
    refused(store, "sx", "sy", "y", "text")


def test_a3_a_rater_and_an_agent_of_another_topology(tmp_path: Path) -> None:
    store = make_room(
        tmp_path,
        gate={
            "type": "judge",
            "raters": ["rr"],
            "action": {"task": "tasks/bewerten.md", "submission": {"format": "review"}},
            "rule": "majority",
            "routes": {"accept": "next", "minor": "next", "major": "next", "reject": "stop"},
        },
    )
    turn(store, "rr", action="gate", fmt="review")
    refused(store, "rr")
    turn(store, "zz", stage="s")
    refused(store, "zz")
    turn(store, "lead")
    store.for_agent("lead").assign("sx", "x", "text")


def test_a3_an_agent_without_any_turn_and_one_of_an_old_room(tmp_path: Path) -> None:
    store = make_room(tmp_path)
    refused(store, "lead")  # no assignment yet
    (store.run_dir / "run.yaml").unlink()
    store2 = RoomStore(store.run_dir)
    turn(store2, "lead")
    refused(store2, "lead")


def test_a3_the_lead_only_in_its_action(store: RoomStore) -> None:
    """The lead itself in a turn of its phase that is not its action (a rating action, a
    turn without a submission): refused (the role is checked by the expected action as well
    as by the phase)."""
    turn(store, "lead", action="gate", fmt="review", number=2)
    refused(store, "lead")
    turn(store, "lead", action=None, number=3)
    refused(store, "lead")
    turn(store, "lead", action="assign-1", number=4)
    refused(store, "lead")
    turn(store, "lead", number=5)
    store.for_agent("lead").assign("sx", "x", "text")


def test_a3_the_lead_after_its_accepted_submission_also_in_an_answer_turn(
    store: RoomStore,
) -> None:
    hand_in(store, "lead")
    refused(store, "lead", match="schon abgegeben")
    turn(store, "lead", number=2)  # the turn after the user's answer (B3): the same package
    refused(store, "lead", match="schon abgegeben")
    turn(store, "lead", attempt=2, number=3)  # a new pass: it leads again
    store.for_agent("lead").assign("sx", "x", "text")


def test_a3_in_the_note_turn(store: RoomStore) -> None:
    store.begin_note_turn("lead")
    refused(store, "lead", match=NOTE_TURN_REFUSED[:30])
    store.end_note_turn("lead")
    store.for_agent("lead").assign("sx", "x", "text")


# --------------------------------------------------------------------------------------------
# A4: the recipient
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["lead", "@lead", "zz", "rr", "user", "orchestrator", "niemand", "", "@"]
)
def test_a4_only_an_agent_on_standby(store: RoomStore, name: str) -> None:
    with pytest.raises(RoomError) as info:
        store.for_agent("lead").assign(name, "x", "text")
    text = str(info.value)
    assert "sx, sy" in text, text
    assert store.delegations() == []


def test_a4_at_in_front_and_the_table_holds_the_real_id(store: RoomStore) -> None:
    assert store.for_agent("lead").assign("@sy", "x", "text").agent == "sy"


def test_a4_an_alias_in_an_anonymous_channel_and_no_hidden_id(tmp_path: Path) -> None:
    store = make_room(tmp_path, anonymous=True)
    turn(store, "lead")
    lead = store.for_agent("lead")
    alias = lead.shown_name("sy")
    assert alias != "sy" and alias.startswith("Mitglied")
    assert lead.assign(alias, "x", "text").agent == "sy"
    assert lead.assign(alias.split()[-1], "y", "text").agent == "sy"
    for name in ("sy", "sx", "zz-unbekannt"):
        with pytest.raises(RoomError) as info:
            lead.assign(name, "x", "text")
        text = str(info.value)
        named = text.split("Aufträge gehen an:")[1]
        assert "sx" not in named and "sy" not in named, text
        assert lead.shown_name("sx") in named
    with pytest.raises(RoomError) as info:
        lead.assign(lead.shown_name("lead"), "x", "text")
    assert "Dir selbst" in str(info.value)


# --------------------------------------------------------------------------------------------
# A5: formats
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", [*ASSIGN_FORMATS, "befund"])
def test_a5_formats_accepted(store: RoomStore, fmt: str) -> None:
    assert store.for_agent("lead").assign("sx", "x", fmt).format == fmt


@pytest.mark.parametrize("fmt", ["rating", "decision", "unbekannt", "", "TEXT"])
def test_a5_formats_refused_with_the_list(store: RoomStore, fmt: str) -> None:
    with pytest.raises(RoomError) as info:
        store.for_agent("lead").assign("sx", "x", fmt)
    assert "Erlaubt sind: text, artifact, review, list, befund." in str(info.value)
    assert store.delegations() == []


# --------------------------------------------------------------------------------------------
# A6: the text of the assignment
# --------------------------------------------------------------------------------------------


def test_a6_cleaned_and_trimmed_stored(store: RoomStore) -> None:
    raw = "  ‮Rechne​ Teil 1.\U000e0041\x07\n  "
    assert store.for_agent("lead").assign("sx", raw, "text").task == "Rechne Teil 1."


@pytest.mark.parametrize("raw", ["", "   ", "​‮\U000e0041", "\n\t\n"])
def test_a6_empty_refused(store: RoomStore, raw: str) -> None:
    refused(store, "lead", "sx", raw, "text", match="leer")


def test_a6_length_measured_after_cleaning_never_cut(store: RoomStore) -> None:
    assert MAX_ASSIGN_TASK_CHARS == MAX_ENTRY_TEXT_CHARS == 4000
    lead = store.for_agent("lead")
    exact = "x" * MAX_ASSIGN_TASK_CHARS
    assert lead.assign("sx", exact, "text").task == exact
    hidden = "y" * MAX_ASSIGN_TASK_CHARS + "​" * 50  # longer only before cleaning
    assert lead.assign("sx", hidden, "text").task == "y" * MAX_ASSIGN_TASK_CHARS
    refused(store, "lead", "sx", exact + "z", "text", match="4001 Zeichen")


# --------------------------------------------------------------------------------------------
# A7: max_assignments per pass, also from two processes
# --------------------------------------------------------------------------------------------


def test_a7_above_the_limit_is_an_error_and_a_new_pass_counts_anew(tmp_path: Path) -> None:
    store = make_room(tmp_path, jobs=2)
    turn(store, "lead")
    lead = store.for_agent("lead")
    lead.assign("sx", "1", "text")
    lead.assign("sy", "2", "text")
    store.cancel_delegations("m", 1)  # cancelled ones count as well
    refused(store, "lead", "sx", "3", "text", match="2 von höchstens 2")
    turn(store, "lead", attempt=2, number=2)
    assert [lead.assign("sx", str(i), "text").number for i in range(2)] == [1, 2]
    refused(store, "lead", match="2 von höchstens 2")


RACE = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path
    from arr.room.store import RoomStore
    from arr.room.types import RoomError
    run_dir, go, what, count = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
    room = RoomStore(run_dir, busy_timeout_s=60).for_agent("lead")
    while not go.exists():
        time.sleep(0.001)
    out = []
    for i in range(count):
        try:
            if what == "assign":
                out.append(["ok", room.assign("sx" if i % 2 else "sy", f"Teil {i}", "text").number])
            else:
                out.append(["ok", room.submit("done", "Fertig.", body="Text.").id])
        except RoomError as exc:
            out.append(["no", str(exc)[:40]])
    print(json.dumps(out))
    """
)


def race(store: RoomStore, tmp_path: Path, jobs: list[tuple[str, int]]) -> list[list[Any]]:
    go = tmp_path / "go"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", RACE, str(store.run_dir), str(go), what, str(count)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for what, count in jobs
    ]
    go.write_text("go")
    outs = []
    for proc in procs:
        out, _ = proc.communicate(timeout=120)
        assert proc.returncode == 0
        outs.append(json.loads(out))
    return outs


def test_a7_two_processes_never_pass_the_limit(tmp_path: Path) -> None:
    store = make_room(tmp_path, jobs=5)
    turn(store, "lead")
    outs = race(store, tmp_path, [("assign", 8), ("assign", 8)])
    numbers = sorted(n for out in outs for ok, n in out if ok == "ok")
    assert numbers == [1, 2, 3, 4, 5]
    assert [d.number for d in store.delegations()] == [1, 2, 3, 4, 5]
    assert sum(ok == "no" for out in outs for ok, _ in out) == 11


# --------------------------------------------------------------------------------------------
# A12: the lead's `submit` with open assignments (Entscheidung 79)
# --------------------------------------------------------------------------------------------


def test_a12_the_example_of_entscheidung_79(store: RoomStore) -> None:
    lead = store.for_agent("lead")
    lead.assign("sx", "1", "text")
    with pytest.raises(RoomError) as first:
        hand_in(store, "lead")
    assert "#1 an sx" in str(first.value) and "#2" not in str(first.value)
    assert "noch einmal" in str(first.value)
    lead.assign("sy", "2", "list")
    with pytest.raises(RoomError) as second:
        hand_in(store, "lead")
    assert "#1 an sx" in str(second.value) and "#2 an sy (Format „list“, wartet)" in str(
        second.value
    )
    sub = hand_in(store, "lead")
    assert sub.action == "work"
    assert [(d.status, d.named) for d in store.delegations()] == [
        (DelegationStatus.CANCELLED, True),
        (DelegationStatus.CANCELLED, True),
    ]
    assert len(store.submissions(agent="lead")) == 1


def test_a12_named_running_and_new_ones_named_again(store: RoomStore) -> None:
    lead = store.for_agent("lead")
    first = lead.assign("sx", "1", "text")
    assert store.start_delegation(first.id)
    with pytest.raises(RoomError, match="läuft"):
        hand_in(store, "lead")
    lead.assign("sx", "2", "text")
    with pytest.raises(RoomError, match="#2 an sx"):
        hand_in(store, "lead")
    hand_in(store, "lead")
    assert {d.status for d in store.delegations()} == {DelegationStatus.CANCELLED}


def test_a12_without_open_assignments_at_once(store: RoomStore) -> None:
    lead = store.for_agent("lead")
    given = lead.assign("sx", "1", "text")
    store.start_delegation(given.id)
    store.end_delegation(given.id, None)
    assert hand_in(store, "lead").action == "work"
    assert store.delegation(given.id).status is DelegationStatus.MISSING  # type: ignore[union-attr]
    assert store.delegation(given.id).named is False  # type: ignore[union-attr]


def test_a12_a_lead_without_any_assignment(store: RoomStore) -> None:
    assert hand_in(store, "lead").action == "work"


def test_a12_an_assignment_between_naming_and_filing_is_named_first(
    store: RoomStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race of §6.6 made certain: #1 is named, then the lead's second `submit` runs, and
    while its files are staged (after the naming, before the transaction that files it) a
    new assignment #2 comes in (another connection, as from a second process). The submission
    is refused and names #2; nothing is cancelled; the next `submit` is accepted."""
    lead = store.for_agent("lead")
    lead.assign("sx", "1", "text")
    with pytest.raises(RoomError, match="#1 an sx"):
        hand_in(store, "lead")
    stage = RoomStore._stage_submission
    once = {"done": False}

    def staged(self: RoomStore, *args: Any, **kwargs: Any) -> Any:
        if not once["done"]:
            once["done"] = True
            RoomStore(self.run_dir).for_agent("lead").assign("sy", "2", "text")
        return stage(self, *args, **kwargs)

    monkeypatch.setattr(RoomStore, "_stage_submission", staged)
    with pytest.raises(RoomError) as info:
        hand_in(store, "lead")
    assert "#2 an sy" in str(info.value)
    assert store.submissions(agent="lead") == []
    assert [(d.status, d.named) for d in store.delegations()] == [
        (DelegationStatus.QUEUED, True),
        (DelegationStatus.QUEUED, True),
    ]
    hand_in(store, "lead")
    assert {d.status for d in store.delegations()} == {DelegationStatus.CANCELLED}


def test_a12_assign_against_submit_from_two_processes(tmp_path: Path) -> None:
    """Never an accepted submission of the lead with an open assignment arr did not name:
    after the race every assignment is cancelled and named, or no submission was accepted."""
    for round_ in range(6):
        store = make_room(tmp_path / str(round_), jobs=20)
        turn(store, "lead")
        race(store, tmp_path / str(round_), [("assign", 10), ("submit", 6)])
        subs = store.submissions(agent="lead")
        rows = store.delegations()
        if subs:
            assert all(d.status is DelegationStatus.CANCELLED and d.named for d in rows), rows
        else:
            assert all(d.status is DelegationStatus.QUEUED for d in rows)


# --------------------------------------------------------------------------------------------
# A13 (room): a late submission of a cancelled assignment; only a running one is accepted
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "match"),
    [
        ("queued", "läuft gerade nicht"),
        ("cancelled", "ist abgebrochen"),
        ("returned", "schon beendet"),
        ("missing", "schon beendet"),
    ],
)
def test_a13_a_recipient_hands_in_only_while_it_runs(
    store: RoomStore, state: str, match: str
) -> None:
    given = store.for_agent("lead").assign("sx", "1", "text")
    turn(store, "sx", action="assign-1")
    if state != "queued":
        store.start_delegation(given.id)
    if state == "cancelled":
        store.cancel_delegations("m", 1)
    elif state in ("returned", "missing"):
        store.end_delegation(given.id, 99 if state == "returned" else None)
    with pytest.raises(RoomError, match=match):
        hand_in(store, "sx")
    assert store.submissions(agent="sx") == []


def test_a13_a_running_one_is_accepted_and_filed_with_its_action(store: RoomStore) -> None:
    given = store.for_agent("lead").assign("sx", "1", "list")
    turn(store, "sx", action="assign-1", fmt="list")
    store.start_delegation(given.id)
    sub = store.for_agent("sx").submit(
        "done", "Liste.", fields={"items": [{"title": "a", "text": "b"}]}
    )
    assert (sub.action, sub.stage, sub.attempt, sub.format) == ("assign-1", "m", 1, "list")
    assert sub.path.startswith("output/submissions/")


def test_a13_status_moves_only_from_an_allowed_state(store: RoomStore) -> None:
    given = store.for_agent("lead").assign("sx", "1", "text")
    assert not store.end_delegation(given.id, None)  # queued: not running
    assert store.start_delegation(given.id) and not store.start_delegation(given.id)
    assert [d.id for d in store.cancel_delegations("m", 1)] == [given.id]
    assert not store.start_delegation(given.id)  # a cancelled one never runs again
    assert not store.end_delegation(given.id, 5)
    assert store.cancel_delegations("m", 1) == []
    assert store.delegation(given.id).status is DelegationStatus.CANCELLED  # type: ignore[union-attr]


# --------------------------------------------------------------------------------------------
# A23 (room): reading methods and a room without the table
# --------------------------------------------------------------------------------------------


def test_a23_reading_filters_and_order(store: RoomStore) -> None:
    lead = store.for_agent("lead")
    for agent in ("sy", "sx", "sy"):
        lead.assign(agent, f"für {agent}", "text")
    assert [d.number for d in store.delegations(stage="m", attempt=1)] == [1, 2, 3]
    assert [d.number for d in store.delegations(agent="sy")] == [1, 3]
    assert store.delegations(attempt=2) == [] and store.delegations(stage="s") == []
    first = store.delegations()[0]
    assert store.delegation(first.id) == first and store.delegation(999) is None
    assert first.action == "assign-1" and first.open


def test_a23_an_old_room_without_the_table_has_no_assignments(tmp_path: Path) -> None:
    run_dir = tmp_path / "old"
    store = RoomStore.create(run_dir)
    store.setup_channels([GroupChatConfig(name="k", members=["a", "b"])])
    with sqlite3.connect(store.db_path) as con:
        con.execute("DROP TABLE delegation")
    assert store.delegations() == []
    assert store.delegations(stage="m", attempt=1, agent="a") == []
    assert store.delegation(1) is None
    assert store.cancel_delegations("m", 1) == []
