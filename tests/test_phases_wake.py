"""W4b-03 §6.4: waking in every phase (A8–A11, A23) with FakeBackend agents and the real
RunManager, Orchestrator and room. Orders and states, no durations: a turn that has to wait
for the test waits for a file (`until_file`) the test writes when the log says so."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from phases_kit import (
    TurnView,
    entries,
    errors,
    hand_in,
    manager,
    of_kind,
    private,
    run,
    run_with,
    say,
    silent,
    solo,
    waker_of,
    woken,
)
from run_kit import eventually, make_workfolder, room

from arr.models import RunStatus
from arr.room.store import USER
from arr.room.types import MessageKind, MessageVia
from arr.runs.orchestrator import Orchestrator

AGENTS = {"agents": [{"id": a} for a in ("a", "s", "b")]}


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def group(*phases: dict[str, Any], **channel: Any) -> dict[str, Any]:
    members = channel.pop("members", ["a", "s"])
    return {**AGENTS, "channels": [{"name": "k", "members": members, **channel}], "phases": phases}


def beweis(name: str = "p", **extra: Any) -> dict[str, Any]:
    """A phase `solo`: `a` has the action, `s` is on standby, `#k` is open."""
    return {**solo(name, "a"), "standby": ["s"], "channels": ["k"], **extra}


def flag(name: str) -> dict[str, Any]:
    """Wait inside the turn until the test writes the file `name` into the work folder."""
    return room("wait_for_messages", seconds=40, until_file=name)


def release(mgr: Any, agent: str, name: str) -> None:
    orch = mgr.orchestrator
    (orch.prepared.agents[agent].work_dir / name).write_text("los", "utf-8")


def answers(view: TurnView) -> list[dict[str, Any]]:
    """A sleeper that answers the open question to it (and tries `submit`, refused)."""
    return [
        room("inbox", save_as="box"),
        say(
            "k",
            "Ja, Lemma 2 gilt.",
            kind="answer",
            reply_to="${box.open_questions.0.id}",
        ),
        room("submit", status="done", summary="geht nicht"),
    ]


def asks_then_hands_in(view: TurnView) -> list[dict[str, Any]]:
    """`a` asks `@s`, waits for the answer in the turn, then hands in."""
    if view.turn > 1:
        return hand_in(view)
    return [
        say("k", "@s Gilt Lemma 2?", kind="question"),
        room("wait_for_messages", seconds=40),
        *hand_in(view),
    ]


# --------------------------------------------------------------------------------------------
# A8: a direct message wakes a sleeper
# --------------------------------------------------------------------------------------------


async def test_a8_a_question_wakes_the_sleeper_once_without_a_submission(
    workfolder: Path,
) -> None:
    mgr, log = manager(workfolder, group(beweis()), {"a": asks_then_hands_in, "s": answers})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    [wake] = woken(log, "s")
    assert wake.context == {"phase": "p", "attempt": 1, "standby": True}
    assert "submission" not in wake.context and wake.stage == "p"
    assert "auf Abruf" in wake.prompt and "#k" in wake.prompt and "`submit`" in wake.prompt
    assert mgr.room.submissions(agent="s") == [], "the room refuses `submit` in a wake-up"
    [entry] = of_kind(mgr, "wake", agent="s")
    question = next(m for m in mgr.room.messages("k") if m.kind is MessageKind.QUESTION)
    assert entry["reasons"] == ["question"] and entry["messages"] == [question.id]
    assert (entry["channel"], entry["stage"], entry["attempt"], entry["count"]) == ("k", "p", 1, 1)
    assert [s.agent for s in mgr.room.submissions()] == ["a"]
    assert not question.open or not mgr.room.message(question.id).open, "s answered"


@pytest.mark.parametrize("how", ["mention", "reply", "private"])
async def test_a8_every_kind_of_direct_message_wakes(workfolder: Path, how: str) -> None:
    """`to`, a reply to its message (without `to` or `@name`) and a message of the user with
    `@s` in its private channel each wake the sleeper."""
    seen: list[str] = []

    def sleeper(view: TurnView) -> list[dict[str, Any]]:
        seen.append(view.prompt)
        return [say("k", "Erledigt.")] if not seen[:-1] else [say("k", "Noch einmal da.")]

    def a(view: TurnView) -> list[dict[str, Any]]:
        if view.turn > 1:
            return hand_in(view)
        steps: list[dict[str, Any]] = []
        if how == "mention":
            steps.append(say("k", "Schau bitte auf Lemma 2.", to=["s"]))
        elif how == "reply":
            steps += [say("k", "@s Hallo."), flag("go")]
            steps.append(room("read_messages", channel="k", only_new=False, limit=1, save_as="m"))
            steps.append(say("k", "Und noch eine Sache.", reply_to="${m.0.id}"))
        steps += [flag("done"), *hand_in(view)]
        return steps

    async def helper(mgr: Any) -> None:
        await eventually(lambda: mgr.orchestrator is not None, within=20)
        if how == "private":
            await eventually(lambda: waker_of(mgr) is not None, within=20)
            mgr.room.post(private("s"), "@s Bitte melde dich.", sender=USER, via=MessageVia.GUI)
        if how == "reply":
            await eventually(lambda: bool(of_kind(mgr, "turn_end", agent="s")), within=20)
            release(mgr, "a", "go")
        target = 2 if how == "reply" else 1
        await eventually(lambda: len(of_kind(mgr, "turn_end", agent="s")) >= target, within=20)
        release(mgr, "a", "done")

    phase = beweis()
    mgr, log = manager(workfolder, group(phase), {"a": a, "s": sleeper})
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    reasons = [e["reasons"] for e in of_kind(mgr, "wake", agent="s")]
    expected = {
        "mention": [["mention"]],
        "reply": [["mention"], ["reply"]],
        "private": [["mention"]],
    }
    assert reasons == expected[how]
    if how == "private":
        assert of_kind(mgr, "wake", agent="s")[0]["channel"] == private("s")
        assert f"#{private('s')}" in woken(log, "s")[0].prompt


async def test_a8_counter_messages_that_are_not_direct_never_wake(workfolder: Path) -> None:
    """A question to all, a note to nobody, `any` and a message of the orchestrator to `s` do
    not wake it; the direct message after them does, and only with itself."""
    ids: dict[str, int] = {}

    def a(view: TurnView) -> list[dict[str, Any]]:
        if view.turn > 1:
            return hand_in(view)
        return [
            say("k", "Hat jemand eine Idee?", kind="question"),
            say("k", "Ich rechne weiter."),
            flag("go"),
            say("k", "@s Gilt Lemma 2?", kind="question"),
            room("wait_for_messages", seconds=40),
            *hand_in(view),
        ]

    async def helper(mgr: Any) -> None:
        await eventually(lambda: waker_of(mgr) is not None, within=20)
        await eventually(lambda: len(mgr.room.messages("k")) >= 3, within=20)
        ids["orch"] = mgr.room.post("k", "Hinweis an s", to=["s"]).id
        await eventually(lambda: mgr.orchestrator._last_seen >= ids["orch"], within=20)
        release(mgr, "a", "go")

    wake_on = {"wake_on": ["mention", "question", "any"]}
    mgr, log = manager(workfolder, group(beweis(), **wake_on), {"a": a, "s": answers})
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    direct = [m.id for m in mgr.room.messages("k") if m.body == "@s Gilt Lemma 2?"]
    assert [e["messages"] for e in of_kind(mgr, "wake", agent="s")] == [direct]
    assert of_kind(mgr, "wake_scheduled", agent="s") == [
        of_kind(mgr, "wake_scheduled", agent="s")[0]
    ]
    assert len(woken(log, "s")) == 1


async def test_a8_what_it_saw_does_not_wake_it_again(workfolder: Path) -> None:
    """The sleeper sees the question in its inbox and does not answer: the open question does
    not wake it a second time (at most once per message)."""

    def a(view: TurnView) -> list[dict[str, Any]]:
        if view.turn > 1:
            return hand_in(view)
        return [say("k", "@s Gilt Lemma 2?", kind="question"), flag("go"), *hand_in(view)]

    async def helper(mgr: Any) -> None:
        await eventually(lambda: bool(of_kind(mgr, "turn_end", agent="s")), within=20)
        waker = waker_of(mgr)
        await eventually(lambda: not waker.turns and not waker.due, within=20)
        release(mgr, "a", "go")

    mgr, log = manager(workfolder, group(beweis()), {"a": a, "s": silent})
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    assert len(woken(log, "s")) == 1
    assert len(of_kind(mgr, "wake_scheduled", agent="s")) == 1
    question = next(m for m in mgr.room.messages("k") if m.kind is MessageKind.QUESTION)
    assert "Gilt Lemma 2?" in woken(log, "s")[0].prompt, "the inbox showed it"
    assert mgr.room.message(question.id).open, "unanswered, and yet no second wake-up"


async def test_a8_no_transition_before_a_wake_up_then_acted(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`s` has `context: reset` in `p` and is woken there: no `reset_session` before the
    wake-up turn (it is no action); its first real action in `q` then gets the transition
    (N-G4). Counter-check: without the wake-up, `q` is its first action and has none
    (Entscheidung 58)."""
    calls: list[str] = []
    reset = Orchestrator.reset_session

    async def reset_session(self: Any, agent_id: str) -> None:
        calls.append(agent_id)
        await reset(self, agent_id)

    monkeypatch.setattr(Orchestrator, "reset_session", reset_session)
    phases = [
        beweis(context={"s": "reset"}, folder={"s": "keep"}),
        {**solo("q", "s"), "context": "reset", "folder": "keep"},
    ]
    mgr, log = manager(workfolder, group(*phases), {"a": asks_then_hands_in, "s": answers_or_work})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert [v.stage for v in log.of("s")] == ["p", "q"]
    assert calls == ["s"], "one transition, before its action in q"
    kinds = [(k, d.get("agent")) for k, d in entries(mgr, "turn", "session_reset")]
    assert kinds == [("turn", "a"), ("turn", "s"), ("session_reset", "s"), ("turn", "s")]

    calls.clear()
    mgr, log = manager(workfolder, group(*phases), {"a": hand_in_at_once, "s": answers_or_work})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert [v.stage for v in log.of("s")] == ["q"] and calls == []


# --------------------------------------------------------------------------------------------
# A9: the limit of wake-ups counts per pass; turns_per_agent and the ping-pong guard hold
# --------------------------------------------------------------------------------------------


def first_turn(view: TurnView, phase: str) -> bool:
    """The first turn of an action shows the whole assignment."""
    return f"# Auftrag – Phase „{phase}“" in view.prompt


def asks_twice() -> Any:
    """`a` asks `s` twice in the first turn of every visit of `p`: after the first answer and
    once the test lets it (files `zwei-<n>`, `ende-<n>`); visit 1 hands in `done`, the others
    `not_solved`."""
    visits = {"n": 0}

    def policy(view: TurnView) -> list[dict[str, Any]]:
        status = "done" if visits["n"] <= 1 else "not_solved"
        if view.stage != "p" or not first_turn(view, "p"):
            return hand_in(view, status=status)
        visits["n"] += 1
        n = visits["n"]
        status = "done" if n == 1 else "not_solved"
        return [
            say("k", f"@s Frage 1 (Besuch {n})?", kind="question"),
            room("wait_for_messages", seconds=40),
            flag(f"zwei-{n}"),
            say("k", f"@s Frage 2 (Besuch {n})?", kind="question"),
            flag(f"ende-{n}"),
            *hand_in(view, status=status),
        ]

    return policy


async def test_a9_the_limit_counts_per_pass_retry_and_next_visit(workfolder: Path) -> None:
    """`max_wakeups_per_agent: 1`: in every pass the first question wakes `s`, the second is
    skipped (`wake_skipped`); the `retry` (pass 2) and the next visit through `goto` (pass 3)
    count anew."""

    async def helper(mgr: Any) -> None:
        for n in (1, 2, 3):
            await eventually(lambda n=n: len(of_kind(mgr, "turn_end", agent="s")) >= n, 30)
            await eventually(lambda: waker_of(mgr) is not None and not waker_of(mgr).turns, 30)
            release(mgr, "a", f"zwei-{n}")
            await eventually(lambda n=n: len(of_kind(mgr, "wake_skipped", agent="s")) >= n, 30)
            release(mgr, "a", f"ende-{n}")

    gate = {"type": "signal", "field": "status", "routes": {"done": "retry", "not_solved": "next"}}
    phases = [
        beweis(max_visits=3, on_exhausted="stop", gate=gate),
        {
            **solo("q", "b"),
            "max_visits": 2,
            "on_exhausted": "stop",
            "gate": {"type": "done", "routes": {"done": {"goto": "p"}}},
        },
    ]
    policies = {"a": asks_twice(), "s": answers, "b": hand_in_at_once}
    mgr, log = manager(workfolder, group(*phases, max_wakeups_per_agent=1), policies)
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    wakes = of_kind(mgr, "wake", agent="s")
    assert [(w["attempt"], w["count"]) for w in wakes] == [(1, 1), (2, 1), (3, 1)]
    skipped = of_kind(mgr, "wake_skipped", agent="s")
    assert [(w["attempt"], w["reason"], w["limit"]) for w in skipped] == [
        (1, "max_wakeups_per_agent", 1),
        (2, "max_wakeups_per_agent", 1),
        (3, "max_wakeups_per_agent", 1),
    ]
    passes = [
        (d["stage"], d["attempt"], d["how"]) for d in of_kind(mgr, "checkpoint", phase="pass_start")
    ]
    assert passes[:4] == [
        ("p", 1, "normal"),
        ("p", 2, "retry"),
        ("q", 1, "normal"),
        ("p", 3, "normal"),
    ]


async def test_a9_enter_phase_of_the_same_pass_keeps_the_count(workfolder: Path) -> None:
    """The runner calls `enter_phase` again in the same pass (per action): that resets
    nothing; another pass of the agent does. The old topologies, which never call it, count
    per run."""
    from run_kit import make_manager, turns

    seen: dict[str, int] = {}

    async def topology(ctx: Any) -> None:
        await ctx.enter_phase("p", 1, ["a", "s"])
        ctx.count_wakeup("k", "s")
        ctx.count_wakeup("k", "a")
        await ctx.enter_phase("p", 1, ["s"])  # the same pass again
        seen["same"] = ctx.wakeups("k", "s")
        await ctx.enter_phase("p", 2, ["s"])  # the retry
        seen["retry_s"], seen["retry_a"] = ctx.wakeups("k", "s"), ctx.wakeups("k", "a")
        ctx.count_wakeup("k", "s")
        await ctx.enter_phase("q", 1, ["s"])  # another phase
        seen["other"] = ctx.wakeups("k", "s")
        ctx.count_wakeup("k", "b")
        await ctx.checkpoint("stufe-2")  # an old topology: stages, no `enter_phase`
        seen["run"] = ctx.wakeups("k", "b")

    mgr = make_manager(workfolder, topology, {a: turns() for a in ("a", "s", "b")})
    assert await mgr.run() is RunStatus.DONE
    assert seen == {"same": 1, "retry_s": 0, "retry_a": 1, "other": 0, "run": 1}


async def test_a9_an_exhausted_sleeper_is_never_woken(workfolder: Path) -> None:
    """`turns_per_agent: 1`: the first question takes the sleeper's only turn; the second
    does not even schedule a wake-up."""

    def a(view: TurnView) -> list[dict[str, Any]]:
        return [
            say("k", "@s Frage 1?", kind="question"),
            room("wait_for_messages", seconds=40),
            flag("zwei"),
            say("k", "@s Frage 2?", kind="question"),
            *hand_in(view),
        ]

    async def helper(mgr: Any) -> None:
        await eventually(lambda: bool(of_kind(mgr, "turn_end", agent="s")), within=30)
        await eventually(lambda: waker_of(mgr) is not None and not waker_of(mgr).turns, 30)
        release(mgr, "a", "zwei")

    mgr, log = manager(
        workfolder,
        group(beweis()),
        {"a": a, "s": answers},
        limits={"turns_per_agent": 1, "wall_time": "60s"},
    )
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    assert len(of_kind(mgr, "wake_scheduled", agent="s")) == 1
    assert len(woken(log, "s")) == 1 and mgr.orchestrator.exhausted("s")


async def test_a9_the_ping_pong_guard_holds_between_action_and_sleeper(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `ping_pong_limit: 1` the second change of direction between `a` and `s` flags the
    pair: the orchestrator posts its note, and the next question to `s` does not wake it."""
    import phases_kit

    from arr.runs.pacing import PING_PONG_NOTE, PacingSettings

    monkeypatch.setattr(phases_kit, "FAST", PacingSettings(poll_interval_s=0.01, ping_pong_limit=1))

    def a(view: TurnView) -> list[dict[str, Any]]:
        return [
            say("k", "@s Frage 1?", kind="question"),
            room("wait_for_messages", seconds=40),
            flag("zwei"),
            say("k", "@s Frage 2?", kind="question"),
            flag("ende"),
            *hand_in(view),
        ]

    async def helper(mgr: Any) -> None:
        await eventually(lambda: bool(of_kind(mgr, "ping_pong")), within=30)
        await eventually(lambda: waker_of(mgr) is not None and not waker_of(mgr).turns, 30)
        release(mgr, "a", "zwei")
        await eventually(lambda: bool(of_kind(mgr, "wake_skipped", agent="s")), within=30)
        release(mgr, "a", "ende")

    mgr, log = manager(workfolder, group(beweis()), {"a": a, "s": answers})
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    [flagged] = of_kind(mgr, "ping_pong")
    assert flagged["agents"] == ["a", "s"] and flagged["channel"] == "k"
    assert any(m.body == PING_PONG_NOTE for m in mgr.room.messages("k"))
    assert [w["reason"] for w in of_kind(mgr, "wake_skipped", agent="s")] == ["ping_pong"]
    assert len(woken(log, "s")) == 1


def answers_or_work(view: TurnView) -> list[dict[str, Any]]:
    return answers(view) if view.context.get("standby") else hand_in(view)


def hand_in_at_once(view: TurnView) -> list[dict[str, Any]]:
    return hand_in(view)


# --------------------------------------------------------------------------------------------
# A10: a sleeper never holds the phase up
# --------------------------------------------------------------------------------------------


async def test_a10_a_running_wake_up_turn_ends_before_the_gate(workfolder: Path) -> None:
    """`a` hands in while the wake-up turn of `s` runs: the turn is finished, not cancelled,
    before the gate; no wake-up turn begins after it."""

    def a(view: TurnView) -> list[dict[str, Any]]:
        return [say("k", "@s Gilt Lemma 2?", kind="question"), flag("weiter"), *hand_in(view)]

    def s(view: TurnView) -> list[dict[str, Any]]:
        return [room("read_messages"), flag("frei"), say("k", "@a Ja.")]

    async def helper(mgr: Any) -> None:
        await eventually(lambda: bool(of_kind(mgr, "turn", agent="s")), within=30)
        release(mgr, "a", "weiter")
        await eventually(lambda: waker_of(mgr) is not None and not waker_of(mgr).waking, 30)
        release(mgr, "s", "frei")

    mgr, log = manager(workfolder, group(beweis()), {"a": a, "s": s})
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    order = [
        (k, d.get("phase") if k == "checkpoint" else d.get("agent"))
        for k, d in entries(mgr, "turn", "turn_end", "checkpoint")
        if k != "checkpoint" or d.get("phase") == "gate"
    ]
    assert order == [
        ("turn", "a"),
        ("turn", "s"),
        ("turn_end", "a"),
        ("turn_end", "s"),
        ("checkpoint", "gate"),
    ]
    [end] = of_kind(mgr, "turn_end", agent="s")
    assert end["status"] == "ok", "finished, not interrupted"


async def test_a10_a_due_wake_up_lapses_and_open_questions_hold_nothing(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wake-up that could not start yet (here: the waker never gets to fire it) lapses with
    the actions (`wake_skipped`, `phase_end`); the open question to `s` and the open question of
    `s` to the user hold nothing: the gate follows at once, without `waiting`."""
    from arr.runs.phases import wake

    async def never(self: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(wake.Waker, "_run", never)

    def a(view: TurnView) -> list[dict[str, Any]]:
        return [say("k", "@s Gilt Lemma 2?", kind="question"), *hand_in(view)]

    mgr, log = manager(workfolder, group(beweis()), {"a": a, "s": answers})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    [lapsed] = of_kind(mgr, "wake_skipped", agent="s")
    question = next(m for m in mgr.room.messages("k") if m.kind is MessageKind.QUESTION)
    assert (lapsed["reason"], lapsed["messages"]) == ("phase_end", [question.id])
    assert woken(log, "s") == [] and of_kind(mgr, "waiting", reason="user") == []


async def test_a10_a_question_of_the_sleeper_to_the_user_holds_nothing(workfolder: Path) -> None:
    def a(view: TurnView) -> list[dict[str, Any]]:
        return [say("k", "@s Gilt Lemma 2?", kind="question"), flag("weiter"), *hand_in(view)]

    def s(view: TurnView) -> list[dict[str, Any]]:
        return [say(private("s"), "@user Darf ich annehmen, dass n prim ist?", kind="question")]

    async def helper(mgr: Any) -> None:
        await eventually(lambda: bool(of_kind(mgr, "user_question", agent="s")), within=30)
        release(mgr, "a", "weiter")

    mgr, log = manager(workfolder, group(beweis()), {"a": a, "s": s})
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    assert of_kind(mgr, "waiting", reason="user") == []
    [opened] = of_kind(mgr, "user_question", agent="s")
    assert opened["channel"] == private("s") and opened["state"] == "open"
    assert [d["state"] for d in of_kind(mgr, "user_question", msg=opened["msg"])] == [
        "open",
        "closed",
    ], "it closes with the run"


# --------------------------------------------------------------------------------------------
# A11: an agent with an action in `solo` and `parallel`
# --------------------------------------------------------------------------------------------


def waits_until(view: TurnView) -> list[dict[str, Any]]:
    """Wait with `wait_until` in the first turn of the action, hand in in the next."""
    if first_turn(view, view.stage or ""):
        return [room("wait_until", minutes=5)]
    return hand_in(view)


async def test_a11_a_direct_message_ends_wait_until_with_its_reason(workfolder: Path) -> None:
    """A note of the user in `#user.a` does not end the wait, `@a` does; the next turn says
    why. No extra turn: two turns of three."""
    bodies = ("Nur eine Notiz.", "@a Bitte weiter.")

    async def helper(mgr: Any) -> None:
        await eventually(lambda: bool(of_kind(mgr, "waiting", agent="a", reason="wait_until")), 30)
        note = mgr.room.post(private("a"), bodies[0], sender=USER, via=MessageVia.GUI)
        await eventually(lambda: mgr.orchestrator._last_seen >= note.id, within=30)
        mgr.room.post(private("a"), bodies[1], sender=USER, via=MessageVia.GUI)

    mgr, log = manager(workfolder, {**AGENTS, "phases": [solo("p", "a")]}, {"a": waits_until})
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    first, second = log.of("a")
    assert "Eine Nachricht an dich ist da" in second.prompt
    assert "Turn 2 von 3" in second.prompt
    assert all(b in second.prompt for b in bodies), "both in the inbox, the wait ended later"


async def test_a11_no_turn_beyond_turns_and_none_after_the_submission(workfolder: Path) -> None:
    """`parallel` with `turns: 1`: `a` hands in at once; a message to it later gives it no
    turn. `b` waits; a message ends the wait, and its next turn is the one follow-up."""

    async def helper(mgr: Any) -> None:
        await eventually(lambda: bool(of_kind(mgr, "waiting", agent="b", reason="wait_until")), 30)
        await eventually(lambda: bool(mgr.room.submissions(agent="a")), within=30)
        mgr.room.post(private("a"), "@a Noch etwas?", sender=USER, via=MessageVia.GUI)
        mgr.room.post(private("b"), "@b Weiter.", sender=USER, via=MessageVia.GUI)

    phase = parallel_phase("p", ["a", "b"], turns=1)
    mgr, log = manager(
        workfolder, {**AGENTS, "phases": [phase]}, {"a": hand_in_at_once, "b": waits_until}
    )
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    assert len(log.of("a")) == 1
    assert len(log.of("b")) == 2 and log.of("b")[1].follow_up


async def test_a11_messages_never_give_more_than_turns_plus_one(workfolder: Path) -> None:
    """An agent that only ever waits, woken by a message every time: `turns` (2) turns and the
    follow-up, then its submission is missing."""

    def always_waits(view: TurnView) -> list[dict[str, Any]]:
        return [room("wait_until", minutes=5)]

    async def helper(mgr: Any) -> None:
        for n in (1, 2, 3):
            await eventually(
                lambda n=n: len(of_kind(mgr, "waiting", agent="a", reason="wait_until")) >= n, 30
            )
            mgr.room.post(private("a"), f"@a Weiter ({n}).", sender=USER, via=MessageVia.GUI)

    mgr, log = manager(
        workfolder, {**AGENTS, "phases": [solo("p", "a", turns=2)]}, {"a": always_waits}
    )
    assert await run_with(mgr, helper) is RunStatus.DONE, errors(mgr)
    assert len(log.of("a")) == 3 and log.of("a")[2].follow_up
    [gate] = of_kind(mgr, "checkpoint", phase="gate")
    assert gate["missing"] == ["a/work"]


# --------------------------------------------------------------------------------------------
# A23: `enter_phase` before every turn, also of an agent on standby (contract with W4b-09)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("naive", [False, True], ids=["runner", "naive"])
async def test_a23_agent_phase_names_the_running_pass_at_every_turn(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch, naive: bool
) -> None:
    """`s` has an action in `vorher` and is on standby in `p` (two passes through `retry`):
    at the start of each of its turns `agent_phase` names the pass it runs in. Counter-check
    (`naive`): an `enter_phase` that leaves out the agents on standby lets the wake-up turn
    run in the old phase."""
    holder: dict[str, Any] = {}
    seen: list[tuple[str, Any]] = []
    if naive:
        enter = Orchestrator.enter_phase

        async def enter_phase(self: Any, stage: str, attempt: int, agents: Any) -> None:
            await enter(self, stage, attempt, [a for a in agents if stage != "p" or a != "s"])

        monkeypatch.setattr(Orchestrator, "enter_phase", enter_phase)

    def s(view: TurnView) -> list[dict[str, Any]]:
        seen.append((view.stage, holder["mgr"].orchestrator.agent_phase("s")))
        return answers(view) if view.context.get("standby") else hand_in(view)

    def a(view: TurnView) -> list[dict[str, Any]]:
        if not first_turn(view, "p"):
            return hand_in(view)
        return [
            say("k", "@s Gilt Lemma 2?", kind="question"),
            room("wait_for_messages", seconds=40),
            *hand_in(view, status="done" if view.attempt == 1 else "not_solved"),
        ]

    gate = {"type": "signal", "field": "status", "routes": {"done": "retry", "not_solved": "next"}}
    phases = [solo("vorher", "s"), beweis(max_visits=2, on_exhausted="next", gate=gate)]
    mgr, log = manager(workfolder, group(*phases), {"a": a, "s": s})
    holder["mgr"] = mgr
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    expected = [("vorher", ("vorher", 1)), ("p", ("p", 1)), ("p", ("p", 2))]
    if naive:
        assert seen != expected and seen[1] == ("p", ("vorher", 1)), "the check sees it"
    else:
        assert seen == expected


def parallel_phase(name: str, agents: list[str], **extra: Any) -> dict[str, Any]:
    from phases_kit import parallel

    return parallel(name, agents, **extra)
