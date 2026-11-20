"""W4b-03 §6.10 (A18): the contract with the runner and auto-compact, against the real
`Orchestrator`. A note turn before a wake-up or a turn of a discussion counts as a turn of the
agent, not as a wake-up and not as a turn of the action; a turn that returns late (waiting for
a free place of its profile) is one wake-up, counted once."""

from pathlib import Path
from typing import Any

import pytest
from phases_kit import (
    TurnView,
    errors,
    hand_in,
    manager,
    of_kind,
    parallel,
    run,
    say,
    solo,
    woken,
)
from run_kit import make_workfolder, room

from arr.models import RunStatus
from arr.runs.compact import NOTE_HEADING

NOTE = [{"do": "text", "text": "### Stand\nWeiter.\n### Offene Punkte\nKeine."}]
FILL = {"do": "usage", "input": 600, "output": 5}


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def turn_log(mgr: Any, agent: str) -> list[tuple[str, int, str | None]]:
    out = []
    for kind, data in [(k, d) for k, d in _entries(mgr) if d.get("agent") == agent]:
        if kind in ("turn", "turn_end"):
            out.append((kind, data["turn"], data.get("purpose")))
        elif kind == "wake":
            out.append(("wake", data["count"], None))
    return out


def _entries(mgr: Any) -> list[tuple[str, dict[str, Any]]]:
    from phases_kit import entries

    return entries(mgr, "turn", "turn_end", "wake")


async def test_a18_a_note_turn_before_a_wake_up_is_counted_once_as_a_turn(
    workfolder: Path,
) -> None:
    def a(view: TurnView) -> list[dict[str, Any]]:
        return [
            say("k", "@s Eins?"),
            room("wait_for_messages", seconds=40),
            say("k", "@s Zwei?"),
            room("wait_for_messages", seconds=40),
            *hand_in(view),
        ]

    def s(view: TurnView) -> list[dict[str, Any]]:
        if view.prompt.startswith(NOTE_HEADING):
            return NOTE
        return [say("k", "@a Ja."), FILL]

    group = {
        "agents": [{"id": "a"}, {"id": "s", "compact": {"at": 500}}],
        "channels": [{"name": "k", "members": ["a", "s"]}],
        "phases": [{**solo("p", "a"), "standby": ["s"], "channels": ["k"]}],
    }
    mgr, log = manager(workfolder, group, {"a": a, "s": s})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert turn_log(mgr, "s") == [
        ("wake", 1, None),
        ("turn", 1, None),
        ("turn_end", 1, None),
        ("wake", 2, None),
        ("turn", 2, "compact"),
        ("turn_end", 2, "compact"),
        ("turn", 3, None),
        ("turn_end", 3, None),
    ]
    assert mgr.orchestrator.agents["s"].turns_used == 3
    wake_turns = [v for v in woken(log, "s") if not v.prompt.startswith(NOTE_HEADING)]
    assert len(wake_turns) == 2, "two wake-ups, the note turn is none"


async def test_a18_a_note_turn_before_a_turn_of_the_discussion(workfolder: Path) -> None:
    """`d1` crosses its threshold at the kick-off: the note turn comes before its next turn
    of the discussion, which still carries its package; the discussion counts its own turns."""

    seen = {"turns": 0}

    def d1(view: TurnView) -> list[dict[str, Any]]:
        if view.prompt.startswith(NOTE_HEADING):
            return NOTE
        seen["turns"] += 1
        return [say("k", "Erster Gedanke."), FILL] if seen["turns"] == 1 else hand_in(view)

    def d2(view: TurnView) -> list[dict[str, Any]]:
        return [say("k", "Noch etwas.")] if "# Auftrag –" in view.prompt else hand_in(view)

    phase = {**parallel("p", ["d1", "d2"]), "topology": "chat", "channels": ["k"], "rounds": 2}
    group = {
        "agents": [{"id": "d1", "compact": {"at": 500}}, {"id": "d2"}],
        "channels": [{"name": "k", "members": ["d1", "d2"], "mode": "rounds"}],
        "phases": [phase],
    }
    mgr, log = manager(workfolder, group, {"d1": d1, "d2": d2})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert [p for _, _, p in turn_log(mgr, "d1") if _ != "wake"][::2] == [None, "compact", None]
    [end] = of_kind(mgr, "discussion_end")
    assert end["turns"] == 4, "two rounds of two, the note turn is not a turn of the discussion"
    assert all(v.context.get("submission") for v in log.of("d1"))
    assert mgr.orchestrator.agents["d1"].turns_used == 3


async def test_a18_a_wake_up_that_waits_for_its_place_is_counted_once(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One place in the profile: `a` holds it while it asks `s`. The wake-up of `s` is counted
    at once and its turn begins only when `a` frees the place (`wait_until`); `a` is woken by
    the answer and hands in. One wake-up of `s`, counted once."""
    import phases_kit
    import run_kit

    monkeypatch.setattr(
        phases_kit, "make_settings", lambda: run_kit.make_settings(**{"fake-auth": 1})
    )

    def a(view: TurnView) -> list[dict[str, Any]]:
        if "# Auftrag –" not in view.prompt:
            return hand_in(view)
        return [
            say("k", "@s Gilt Lemma 2?", kind="question"),
            room("wait_for_messages", seconds=40),  # returns: s waits for the place (L12)
            room("wait_until", minutes=5),
        ]

    def s(view: TurnView) -> list[dict[str, Any]]:
        return [room("read_messages"), say("k", "@a Ja.")]

    group = {
        "agents": [{"id": "a"}, {"id": "s"}],
        "channels": [{"name": "k", "members": ["a", "s"]}],
        "phases": [{**solo("p", "a"), "standby": ["s"], "channels": ["k"]}],
    }
    mgr, log = manager(workfolder, group, {"a": a, "s": s})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert [w["count"] for w in of_kind(mgr, "wake", agent="s")] == [1]
    order = [
        (k, d["agent"])
        for k, d in _entries(mgr)
        if k in ("turn", "turn_end", "wake") and d.get("agent") in ("a", "s")
    ]
    assert order == [
        ("turn", "a"),
        ("wake", "s"),
        ("turn_end", "a"),
        ("turn", "s"),
        ("turn_end", "s"),
        ("turn", "a"),
        ("turn_end", "a"),
    ]
    assert any(d.get("reason") == "capacity" for d in of_kind(mgr, "waiting", agent="s"))
    assert "Eine Nachricht an dich ist da" in log.of("a")[1].prompt
