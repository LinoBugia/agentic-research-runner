"""Waking and holding in a pass of a phase (W4b-03 §6.3, §6.4, §6.6; docs/10 §4.4, §4.5).

`Waker` listens to the messages of a pass through the orchestrator's watcher (`listen`, no
second read of the database) and does two things:

- **Agents on standby (sleepers)** are woken by every direct message to them (`address` gives
  a value of `pacing.DIRECT`: a question to them, `to` or `@name`, a reply to their message) in
  an open channel of the phase they are a member of or in their private channel, as the
  channel's `wake_on` allows; never by a question to all, `any`, their own messages or the
  orchestrator's. At once, without debounce; a wake-up that falls due during their turn waits
  for its end, and what they saw in a turn (inbox, hint, read) does not wake them again. A
  message wakes an agent at most once. The wake-up turn carries phase and pass and no
  `submission` (the room refuses `submit`), runs without `transition` and puts the agent into
  `PhaseRunner.acted`. Limits: `max_wakeups_per_agent` of the channel the message came by,
  counted per pass (the orchestrator starts the count anew in `enter_phase`), the agent's
  `turns_per_agent` and the ping-pong guard. A sleeper never holds the phase up: when the
  actions of the pass are done (`end_actions`) no new wake-up turn starts, due ones lapse (log
  `wake_skipped`, reason `phase_end`), running ones are finished.
- **Agents with an action** that wait after `wait_until` (`PhaseRunner.wait`): a direct message
  to them ends the wait (`take_message`). They get no extra turn by it.

Agents that are not in the phase hear nothing (Entscheidung 64): the waker knows only the
participants of the pass; the orchestrator gives hints only to them (`set_participants`).

`hold` keeps the phase while a question to the user is open that an agent with an action asked
in this pass (B3 in every topology, §6.6): in an open channel of the phase or in its private
channel, until the user answers or `pacing.user_question_hold_s()` after the question passed.
An answer gives the asker exactly one turn with the context of its action (a wake-up of the
channel; none above its limit). A stop or limit ends the wait at once.

W4b-04: a sleeper with an assignment of the lead (`moderated`) leaves the sleepers for its
duration (`take_out`: a running wake-up turn ends first, a due one lapses with the reason
`assignment`) and is woken like an agent with an action; `put_back` returns it after the turns
of its assignments, and a direct message it did not see in its last turn wakes it then, once.
`hold` takes an optional `abort` (a cancelled assignment ends the holding at once). In its
private channel a question of the user without `@name` is a question to its owner (NR2).

Log entries: `wake_scheduled`, `wake`, `wake_skipped` (reasons `max_wakeups_per_agent`,
`ping_pong`, `phase_end`, `turns_per_agent`, `assignment`), `ping_pong`, `wait_end` (reason
`phase_end`) and
`waiting` (`channel`, `reason: user`, `until`, `stage`; `reason: none` at the end); every entry
of the waker has `stage` (the phase) and `attempt`.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from arr.models import ResolvedPhase, WakeOn
from arr.room.address import Address, address
from arr.room.store import ORCHESTRATOR, USER, private_config, private_owner
from arr.room.types import DeliveryStatus, Message
from arr.runs.pacing import PING_PONG_NOTE, PingPongTracker, user_question_hold_s
from arr.runs.phases.state import Pass, Slot
from arr.runs.phases.view import answer_prompt

if TYPE_CHECKING:
    from arr.models import GroupChatConfig
    from arr.runs.orchestrator import Orchestrator
    from arr.runs.phases.runner import PhaseRunner

__all__ = ["REASONS", "Waker", "hold", "sleeper_prompt"]

logger = logging.getLogger(__name__)

REASONS = {
    "question": "eine Frage an dich",
    "mention": "eine Nachricht an dich",
    "reply": "eine Antwort auf deine Nachricht",
}
"""Why a sleeper is woken, as its wake-up text says it (no sender: aliases stay hidden)."""

SLEEPER_PROMPT = (
    "Du wurdest in der Phase „{phase}“ geweckt: {reasons} in {channels}. In dieser Phase bist du "
    "auf Abruf: Du hast keine eigene Action, und eine Abgabe mit `submit` gibt es für dich hier "
    "nicht. Lies deinen Posteingang, antworte mit `post_message` (mit `reply_to` oder `@name`) "
    "und beende dann deinen Turn. Eine weitere Nachricht an dich weckt dich wieder."
)


def sleeper_prompt(phase: str, reasons: Sequence[str], channels: Sequence[str]) -> str:
    """The text of a wake-up turn of a sleeper: phase, why, where; no text of another agent."""
    why = ", ".join(REASONS.get(r, r) for r in dict.fromkeys(reasons))
    where = ", ".join(f"#{c}" for c in dict.fromkeys(channels))
    return SLEEPER_PROMPT.format(phase=phase, reasons=why, channels=where)


def _wakes(how: Address | None, wake_on: Sequence[WakeOn]) -> str | None:
    """The reason a direct message wakes a member (L13: like a finished member), or None."""
    if how is Address.QUESTION and {WakeOn.QUESTION, WakeOn.MENTION} & set(wake_on):
        return "question"
    if how in (Address.MENTION, Address.REPLY) and WakeOn.MENTION in wake_on:
        return how.value
    return None


@dataclass(slots=True)
class _Due:
    """A wake-up of a sleeper that waits to start: the channels, reasons and messages."""

    channels: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    messages: list[int] = field(default_factory=list)


class Waker:
    """Wakes the sleepers of a pass and ends the waits of its agents with an action by a
    direct message (see the module docstring). One per pass; `attach` registers it with the
    orchestrator, `detach` removes it."""

    def __init__(self, runner: "PhaseRunner", phase: ResolvedPhase, pass_: Pass) -> None:
        self.runner = runner
        self.orch: Orchestrator = runner.ctx  # type: ignore[assignment]
        self.phase = phase
        self.pass_ = pass_
        participants = set(phase.participants)
        self.sleepers = [a for a in phase.standby if a not in set(phase.agents)]
        self.channels: dict[str, tuple[GroupChatConfig, list[str]]] = {}
        """Open channels of the pass → (settings, the participants among its members)."""
        for name in phase.channels:
            config = self.orch.config.channel(name)
            members = [m for m in config.members if m in participants]
            self.channels[name] = (config, members)
        for agent in phase.participants:
            private = self.orch.private_channel(agent)
            if private is not None:
                self.channels[private] = (private_config(agent), [agent])
        limit = self.orch.pacing.ping_pong_limit
        self.trackers = {
            name: PingPongTracker(limit, self.orch.sender_of) for name in self.channels
        }
        self.delegated: set[str] = set()
        """Channels whose tracker a running discussion feeds (W4b-03 §6.5)."""
        self.sleeper_pairs_only: set[str] = set()
        """Channels where the ping-pong guard counts only pairs with an agent on standby: the
        discussion channel of a `chat` in `rounds`, which has no guard of its own (W4b-03.1,
        E4)."""
        self.waking = True
        self.assigned: set[str] = set()
        """Sleepers taken out for an assignment of the lead (W4b-04, `take_out`)."""
        self.held: dict[str, list[Message]] = {}
        """Direct messages to an agent while it works on assignments: what it did not see in
        its last turn wakes it when it is put back (`put_back`)."""
        self.due: dict[str, _Due] = {}
        self.woken: set[tuple[str, int]] = set()
        self.turns: dict[str, asyncio.Task[Any]] = {}
        self.messages: dict[str, list[Message]] = {}
        """Direct messages to an agent with an action that end its `wait_until`."""
        self.changed = asyncio.Event()
        self._loop: asyncio.Task[None] | None = None

    # -- life --------------------------------------------------------------------------------

    @classmethod
    def attach(cls, runner: "PhaseRunner", phase: ResolvedPhase, pass_: Pass) -> "Waker | None":
        """A waker for the pass, registered with the orchestrator; None for a context without
        a watcher (a test context)."""
        if not callable(getattr(runner.ctx, "listen", None)):
            return None
        waker = cls(runner, phase, pass_)
        waker.orch.listen(waker)
        waker._loop = asyncio.get_running_loop().create_task(waker._run())
        return waker

    async def end_actions(self) -> None:
        """The actions of the pass are done: no new wake-up turn starts, due ones lapse (logged),
        running ones are finished, not cancelled (§6.4)."""
        self.waking = False
        for agent, due in self.due.items():
            self._log(
                "wake_skipped",
                channel=due.channels[0],
                agent=agent,
                reason="phase_end",
                messages=due.messages,
            )
        self.due.clear()
        running = list(self.turns.values())
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    async def detach(self) -> None:
        """Remove the waker; a running wake-up turn is cancelled only when the runner is."""
        self.waking = False
        self.orch.unlisten(self)
        if self._loop is not None:
            self._loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop
        for task in self.turns.values():
            task.cancel()
        for task in list(self.turns.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for agent in self.sleepers:  # a sleeper's `wait_until` ends with the phase
            if self.orch.long_wait(agent) is not None:
                self.orch.end_long_wait(agent)
                self._log("wait_end", agent=agent, reason="phase_end")

    # -- listener of the orchestrator --------------------------------------------------------

    def on_message(self, msg: Message) -> None:
        """A new message somewhere in the run (the orchestrator's watcher)."""
        entry = self.channels.get(msg.channel)
        if entry is None:
            return
        config, members = entry
        if msg.channel not in self.delegated:
            pair = self.trackers[msg.channel].observe(msg)
            quiet = msg.channel in self.sleeper_pairs_only
            if pair is not None and not (quiet and not set(pair) & set(self.sleepers)):
                self._ping_pong(msg.channel, pair)
        for member in members:
            reason = self._reason(msg, member, config)
            if reason is None or self.orch.is_busy(member):
                continue  # a busy one gets a hint; what it did not see wakes it after its turn
            self._take(member, msg, reason)
        self.changed.set()

    def on_turn_end(self, agent_id: str, undelivered: Sequence[Message]) -> None:
        """A turn ended: direct messages it neither read nor got as a hint (and that were not
        in its inbox at the start) wake a sleeper now or end the wait of an agent with an
        action."""
        seen = self.orch.turn_mark(agent_id)
        for msg in undelivered:
            entry = self.channels.get(msg.channel)
            if entry is None or msg.id <= seen or agent_id not in entry[1]:
                continue
            reason = self._reason(msg, agent_id, entry[0])
            if reason is not None:
                self._take(agent_id, msg, reason)
        self.changed.set()

    def kick(self) -> None:
        """Look again now, e.g. after a stop."""
        self.changed.set()

    # -- rules -------------------------------------------------------------------------------

    def _reason(self, msg: Message, member: str, config: "GroupChatConfig") -> str | None:
        if msg.sender in (member, ORCHESTRATOR):
            return None
        how = address(msg, member, self.orch.sender_of)
        if msg.sender == USER and private_owner(msg.channel) == member:
            # E2: in its private channel every message of the user is direct; a question
            # without `@name` is a question to its owner (W4b-04, NR2)
            if how is None:
                how = Address.MENTION
            elif how is Address.QUESTION_ALL:
                how = Address.QUESTION
        return _wakes(how, config.wake_on)

    def _take(self, agent: str, msg: Message, reason: str) -> None:
        if agent in self.sleepers:
            self._schedule(agent, msg, reason)
        else:
            self.messages.setdefault(agent, []).append(msg)
            if agent in self.assigned:  # kept apart: `clear` of a wait does not drop them
                self.held.setdefault(agent, []).append(msg)

    def _schedule(self, agent: str, msg: Message, reason: str) -> None:
        if (agent, msg.id) in self.woken or self.orch.exhausted(agent):
            return
        if not self.waking:  # the actions are done: the wake-up lapses (§6.4)
            self._log(
                "wake_skipped",
                channel=msg.channel,
                agent=agent,
                reason="phase_end",
                messages=[msg.id],
            )
            return
        due = self.due.get(agent)
        if due is None:
            due = self.due[agent] = _Due()
            self._log(
                "wake_scheduled", channel=msg.channel, agent=agent, reason=reason, due=time.time()
            )
        if msg.channel not in due.channels:
            due.channels.append(msg.channel)
        if reason not in due.reasons:
            due.reasons.append(reason)
        if msg.id not in due.messages:
            due.messages.append(msg.id)

    def _guarded(self, agent: str, channel: str) -> bool:
        return any(agent in pair for pair in self.trackers[channel].flagged)

    def _ping_pong(self, channel: str, pair: frozenset[str]) -> None:
        agents = sorted(pair)
        for agent in agents:
            self.due.pop(agent, None)
        self._log("ping_pong", channel=channel, agents=agents)
        try:
            self.orch.room.post(channel, PING_PONG_NOTE, to=agents)
        except Exception:  # the note is a courtesy; the guard holds without it
            logger.exception("Ping-Pong-Hinweis in #%s nicht gepostet", channel)

    def take_message(self, agent: str) -> Message | None:
        """A direct message to `agent` (with an action) that came after its latest turn began,
        None if there is none; the others are dropped with it."""
        found = [m for m in self.messages.pop(agent, []) if m.id > self.orch.turn_mark(agent)]
        return found[0] if found else None

    def clear(self, agent: str) -> None:
        """Forget the direct messages kept for `agent` (its wait ended or never began)."""
        self.messages.pop(agent, None)

    # -- W4b-04: a sleeper with an assignment of the lead -------------------------------------

    async def take_out(self, agent: str) -> None:
        """`agent` (on standby) begins an assignment of the lead (W4b-04 §6.4): it leaves
        `sleepers` for its duration, so it is woken like an agent with an action (a direct
        message ends its `wait_until`, no wake-up turn). A due wake-up lapses (`wake_skipped`,
        reason `assignment`; its messages are in the inbox of the first turn of the
        assignment); a running wake-up turn ends first: the assignment, its `transition` too,
        begins after it."""
        if agent in self.sleepers:
            self.sleepers.remove(agent)
            self.assigned.add(agent)
        due = self.due.pop(agent, None)
        if due is not None:
            self._log(
                "wake_skipped",
                channel=due.channels[0],
                agent=agent,
                reason="assignment",
                messages=due.messages,
            )
        running = self.turns.get(agent)
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)

    def put_back(self, agent: str) -> None:
        """The turns of the assignments of `agent` are over: it is on standby again. A direct
        message it did not see in its last turn wakes it now, once per message (after the end
        of the actions it lapses, `phase_end`)."""
        if agent not in self.assigned:
            return
        self.assigned.discard(agent)
        if agent not in self.sleepers:
            self.sleepers.append(agent)
        seen = self.orch.turn_mark(agent)
        self.messages.pop(agent, None)
        for msg in self.held.pop(agent, []):
            entry = self.channels.get(msg.channel)
            if entry is None or msg.id <= seen:
                continue
            reason = self._reason(msg, agent, entry[0])
            if reason is not None:
                self._schedule(agent, msg, reason)
        self.changed.set()

    # -- wake-up turns -----------------------------------------------------------------------

    async def _run(self) -> None:
        poll = self.orch.pacing.poll_interval_s * 4
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), poll)
            self.changed.clear()
            if self.orch.stop_requested or not self.waking:
                continue
            for agent in list(self.due):
                if agent in self.turns or self.orch.is_busy(agent):
                    continue  # due during its turn: it waits for the end of that turn
                self._fire(agent, self.due.pop(agent))

    def _fire(self, agent: str, due: _Due) -> None:
        channel = due.channels[0]
        if self.orch.exhausted(agent):
            self._log("wake_skipped", channel=channel, agent=agent, reason="turns_per_agent")
            return
        if any(self._guarded(agent, c) for c in due.channels):
            self._log("wake_skipped", channel=channel, agent=agent, reason="ping_pong")
            return
        # it counts against the first channel of its messages that is below its limit; only
        # when all of them are at their limit it lapses (W4b-03.1, G4)
        free = [
            c
            for c in due.channels
            if self.orch.wakeups(c, agent) < self.channels[c][0].max_wakeups_per_agent
        ]
        if not free:
            self._log(
                "wake_skipped",
                channel=channel,
                agent=agent,
                reason="max_wakeups_per_agent",
                limit=self.channels[channel][0].max_wakeups_per_agent,
                channels=due.channels,
            )
            return
        channel = free[0]
        count = self.orch.count_wakeup(channel, agent)
        self._log(
            "wake",
            channel=channel,
            agent=agent,
            reasons=due.reasons,
            messages=due.messages,
            count=count,
        )
        self.woken.update((agent, m) for m in due.messages)
        self.orch.mark_delivered(due.messages, DeliveryStatus.WOKEN, agent)
        self.runner.acted.add(agent)  # its first real action gets reset, compact, fresh (N-G4)
        prompt = sleeper_prompt(self.phase.name, due.reasons, due.channels)
        context = {"phase": self.phase.name, "attempt": self.pass_.attempt, "standby": True}
        task = asyncio.get_running_loop().create_task(
            self.orch.turn(agent, prompt, stage=self.phase.name, context=context)
        )
        self.turns[agent] = task

        def finished(_: asyncio.Task[Any]) -> None:
            if self.turns.get(agent) is task:
                del self.turns[agent]
            self.changed.set()

        task.add_done_callback(finished)

    def _log(self, kind: str, **data: Any) -> None:
        self.orch.log.write(kind, stage=self.phase.name, attempt=self.pass_.attempt, **data)


async def hold(
    runner: "PhaseRunner",
    phase: ResolvedPhase,
    pass_: Pass,
    slots: list[Slot],
    abort: Callable[[], bool] | None = None,
) -> None:
    """B3 in every topology (§6.6): while a question to the user is open that an agent of
    `slots` asked in this pass, in an open channel of the phase or its private channel, wait
    until the user answers or `user_question_hold_s()` after the question passed. Each answer
    gives its asker one turn with the context of its action (a wake-up of the channel, none
    above its limit); a new submission in it counts. A stop or limit ends the wait at once,
    and so does `abort` when it turns true (W4b-04: a cancelled assignment), before an answer
    turn as well. A context without the orchestrator's questions holds nothing."""
    orch: Orchestrator = runner.ctx  # type: ignore[assignment]
    if not callable(getattr(orch, "open_user_questions", None)):
        return
    askers = {s.agent: s for s in slots if s.context is not None}
    if not askers:
        return
    channels = [c for c in phase.channels]
    for agent in askers:
        private = orch.private_channel(agent)
        if private is not None:
            channels.append(private)
    since = pass_.started
    handled: set[int] = set()
    watched: dict[int, tuple[Any, str]] = {}
    reported: set[str] = set()
    stopped = abort if abort is not None else (lambda: False)
    try:
        while not orch.stop_requested and not stopped():
            try:
                orch.poll()  # an answer just given closes its question first
            except Exception:  # the watcher keeps polling; a read error ends nothing
                logger.exception("Lesen der Gruppenchats in Phase %s fehlgeschlagen", phase.name)
            for channel in channels:
                for question in orch.open_user_questions(channel):
                    mine = question.agent in askers and question.asked >= since
                    if mine and question.msg_id not in handled:
                        watched.setdefault(question.msg_id, (question, channel))
            answered: dict[str, list[tuple[Any, str]]] = {}
            for msg_id, (question, channel) in list(watched.items()):
                if orch.user_question_open(msg_id):
                    continue
                del watched[msg_id]
                handled.add(msg_id)
                if orch.user_question_answered(question.item_id):
                    answered.setdefault(question.agent, []).append((question, channel))
            if answered and not stopped():
                await asyncio.gather(
                    *(
                        _answer_turn(runner, phase, pass_, askers[agent], items)
                        for agent, items in answered.items()
                    )
                )
                continue
            now = time.time()
            hold_s = user_question_hold_s()
            due = {ch: q.asked + hold_s for q, ch in watched.values() if q.asked + hold_s > now}
            if not due:
                break
            for channel in sorted(set(due) - reported):
                reported.add(channel)
                orch.log.write(
                    "waiting",
                    channel=channel,
                    reason="user",
                    until=max(u for c, u in due.items() if c == channel),
                    stage=phase.name,
                    attempt=pass_.attempt,
                )
            await asyncio.sleep(orch.pacing.poll_interval_s)
    finally:
        for channel in sorted(reported):
            orch.log.write("waiting", channel=channel, reason="none", stage=phase.name)


async def _answer_turn(
    runner: "PhaseRunner",
    phase: ResolvedPhase,
    pass_: Pass,
    slot: Slot,
    items: list[tuple[Any, str]],
) -> None:
    """One turn for the asker after the user's answer (one for all answers it got at once),
    counted as a wake-up of the channel of the first one; none above its limit."""
    orch: Orchestrator = runner.ctx  # type: ignore[assignment]
    channel = items[0][1]
    owner = private_owner(channel)
    config = private_config(owner) if owner is not None else orch.config.channel(channel)
    agent = slot.agent
    if orch.stop_requested:
        return
    if orch.exhausted(agent):  # W4b-03.1 (G5): no turn left, so no wake-up either
        orch.log.write(
            "wake_skipped",
            channel=channel,
            agent=agent,
            reason="turns_per_agent",
            stage=phase.name,
            attempt=pass_.attempt,
        )
        return
    if orch.wakeups(channel, agent) >= config.max_wakeups_per_agent:
        orch.log.write(
            "wake_skipped",
            channel=channel,
            agent=agent,
            reason="max_wakeups_per_agent",
            limit=config.max_wakeups_per_agent,
            stage=phase.name,
            attempt=pass_.attempt,
        )
        return
    count = orch.count_wakeup(channel, agent)
    orch.log.write(
        "wake",
        channel=channel,
        agent=agent,
        reasons=["answer"],
        messages=[q.msg_id for q, _ in items],
        count=count,
        stage=phase.name,
        attempt=pass_.attempt,
    )
    assert slot.context is not None
    await orch.turn(agent, answer_prompt(slot.context), stage=phase.name, context=slot.context)
    found = runner._newest(slot, phase.name, pass_.attempt, slot.mark)
    if found is not None:
        slot.found = found
