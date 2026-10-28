"""Chat pacing: inbox at turn start, hints mid-turn, ping-pong guard, `rounds` and `event`.

How agents learn about new messages (docs/06-gruppenchats.md §4):

- **Inbox at turn start:** `render_turn_prompt` puts open questions to the agent (in full),
  unread messages of `user` (in full, marked as having priority, docs/06 §10) and the number
  of new messages per channel in front of the assignment.
- **Hints mid-turn:** `compose_hint` builds the short text for `AgentBackend.deliver_hint`;
  `HintGate` allows at most one hint per 60 s or per 5 tool calls of a turn.

Who is when on turn (docs/06 §5.1, §5.2) is a *discussion phase*: `Orchestrator.discuss`
(implemented by `run_discussion`). `rounds` gives all members a turn per round; `event` gives
everyone the kick-off and then wakes idle agents on `wake_on` events, debounced, with a
wake-up limit, never by their own messages, with the ping-pong guard, until `quiet_after_s`,
all members `done` or a limit. Outside a discussion phase nobody is woken automatically;
hints and the inbox work always.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from arr.backends.base import TurnOutcome
from arr.errors import ArrError
from arr.models import AgentStatus, ChannelMode, GroupChatConfig, RoundOrder, WakeOn
from arr.room.store import ORCHESTRATOR, USER
from arr.room.types import DeliveryStatus, Inbox, Message, MessageKind
from arr.runs.context import TurnJob

if TYPE_CHECKING:
    from arr.runs.orchestrator import Orchestrator

__all__ = [
    "DEFAULT_ROUNDS",
    "DiscussionEnd",
    "DiscussionOutcome",
    "HintGate",
    "PacingSettings",
    "PingPongTracker",
    "compose_hint",
    "render_turn_prompt",
    "run_discussion",
]

DEFAULT_ROUNDS = 3
"""Rounds of a `rounds` discussion when the topology gives none."""

PROGRESS_KINDS = frozenset({MessageKind.CLAIM, MessageKind.PROOF, MessageKind.DECISION})
"""Kinds that count as progress for the ping-pong guard (docs/06 §5.2)."""

PING_PONG_NOTE = "Bitte Stand zusammenfassen oder Dissens festhalten."


@dataclass(frozen=True, slots=True)
class PacingSettings:
    """Timing of the orchestrator; the defaults follow docs/06 §4 and §5.2.

    `poll_interval_s`: how often the room is checked for new messages (they come from the
    agents' room processes). `hint_interval_s` / `hint_tool_calls`: a further hint in the
    same turn needs this much time or this many tool calls since the last one.
    `ping_pong_limit`: two agents may exchange this many messages without a claim, proof or
    decision; one more and the guard steps in.
    """

    poll_interval_s: float = 0.25
    hint_interval_s: float = 60.0
    hint_tool_calls: int = 5
    ping_pong_limit: int = 6


# --------------------------------------------------------------------------------------------
# Inbox at turn start (docs/06 §4, §10)
# --------------------------------------------------------------------------------------------


def _render(msg: Message, *, me: str) -> str:
    head = f"[#{msg.id} {msg.sender} · {msg.kind.value}"
    if msg.reply_to is not None:
        head += f" → #{msg.reply_to}"
    if me in msg.to:
        head += " · an dich"
    head += f" · #{msg.channel}]"
    lines = [head, msg.body]
    lines += [f"Anhang: {a.name} – get_attachment({msg.id}, {a.name!r})" for a in msg.refs]
    return "\n".join(lines)


def render_turn_prompt(
    agent_id: str, prompt: str, inbox: Inbox | None, user_messages: Sequence[Message] = ()
) -> str:
    """The prompt of a turn: inbox first, then the assignment (docs/06 §4, §10).

    The inbox lists open questions to the agent in full (questions of `user` first), then
    unread messages of `user` in full with a priority mark, then the number of new messages
    per channel. Without an inbox (agent in no channel) the prompt is returned unchanged.
    The agent reads other messages itself, so the prompt stays short.
    """
    if inbox is None:
        return prompt
    questions = sorted(inbox.open_questions, key=lambda m: (m.sender != USER, m.id))
    asked = {m.id for m in questions}
    from_user = [m for m in user_messages if m.id not in asked]
    parts = ["## Posteingang"]
    if questions:
        rendered = "\n\n".join(
            ("★ Vom User: " if m.sender == USER else "") + _render(m, me=agent_id)
            for m in questions
        )
        parts.append("Offene Fragen an dich (beantworte sie zuerst):\n\n" + rendered)
    else:
        parts.append("Offene Fragen an dich: keine")
    if from_user:
        rendered = "\n\n".join(_render(m, me=agent_id) for m in from_user)
        parts.append("★ Nachrichten vom User (haben Vorrang):\n\n" + rendered)
    user_counts: dict[str, int] = {}
    for msg in user_messages:
        user_counts[msg.channel] = user_counts.get(msg.channel, 0) + 1
    counts = []
    for channel, n in inbox.unread.items():
        extra = f" (davon {user_counts[channel]} vom User)" if user_counts.get(channel) else ""
        counts.append(f"#{channel} {n}{extra}")
    parts.append("Neue Nachrichten: " + (", ".join(counts) if counts else "keine"))
    if any(inbox.unread.values()):
        parts.append("Lies sie mit read_messages(), wenn sie für deinen Auftrag wichtig sind.")
    return "\n\n".join(parts) + "\n\n## Auftrag\n\n" + prompt


# --------------------------------------------------------------------------------------------
# Hints mid-turn (docs/06 §4)
# --------------------------------------------------------------------------------------------


def compose_hint(agent_id: str, messages: Iterable[Message]) -> str:
    """Short hint such as „📨 1 Frage an dich in #main · 2 neue Nachrichten in #main“.

    Senders are not named (aliases in anonymous channels); messages of `user` are counted
    separately. The agent fetches the content itself.
    """
    questions: dict[str, int] = {}
    others: dict[str, int] = {}
    users: dict[str, int] = {}
    for msg in messages:
        if msg.kind is MessageKind.QUESTION and agent_id in msg.to:
            questions[msg.channel] = questions.get(msg.channel, 0) + 1
        else:
            others[msg.channel] = others.get(msg.channel, 0) + 1
        if msg.sender == USER:
            users[msg.channel] = users.get(msg.channel, 0) + 1
    parts = []
    for channel, n in questions.items():
        parts.append(f"{n} {'Frage' if n == 1 else 'Fragen'} an dich in #{channel}")
    for channel, n in others.items():
        noun = "neue Nachricht" if n == 1 else "neue Nachrichten"
        parts.append(f"{n} {noun} in #{channel}")
    text = "📨 " + " · ".join(parts)
    if users:
        text += " (davon " + ", ".join(f"{n} vom User in #{c}" for c, n in users.items()) + ")"
    return text + ". Lies sie mit inbox() oder read_messages()."


@dataclass(slots=True)
class HintGate:
    """At most one hint per `interval_s` or per `tool_calls` tool calls in a turn (docs/06 §4).

    The first hint of a turn may go at once. After that a hint needs either `interval_s`
    seconds or `tool_calls` tool calls since the previous one, whichever comes first.
    """

    interval_s: float = 60.0
    tool_calls: int = 5
    clock: Callable[[], float] = time.monotonic
    _last_at: float | None = None
    _last_calls: int = 0

    def allows(self, tool_calls: int) -> bool:
        """True if a hint may be delivered now, given the tool calls of the turn so far."""
        if self._last_at is None:
            return True
        waited = self.clock() - self._last_at >= self.interval_s
        return waited or tool_calls - self._last_calls >= self.tool_calls

    def delivered(self, tool_calls: int) -> None:
        """Record a delivered hint."""
        self._last_at = self.clock()
        self._last_calls = tool_calls


# --------------------------------------------------------------------------------------------
# Ping-pong guard (docs/06 §5.2)
# --------------------------------------------------------------------------------------------


class PingPongTracker:
    """Counts messages between two agents without progress and flags the pair once.

    A message goes from its sender to one other agent if `to` names exactly that agent or
    it replies to a message of that agent. A claim, proof or decision between the two resets
    their count; more than `limit` messages without one flag the pair (only once).
    `sender_of` looks up the sender of older messages that the tracker has not seen.
    """

    def __init__(self, limit: int, sender_of: Callable[[int], str | None]) -> None:
        self.limit = limit
        self._sender_of = sender_of
        self._senders: dict[int, str] = {}
        self._counts: dict[frozenset[str], int] = {}
        self.flagged: set[frozenset[str]] = set()

    def _other(self, msg: Message) -> str | None:
        if len(msg.to) == 1:
            return msg.to[0]
        if msg.reply_to is not None:
            sender = self._senders.get(msg.reply_to) or self._sender_of(msg.reply_to)
            if sender not in (None, ORCHESTRATOR, USER):
                return sender
        return None

    def observe(self, msg: Message) -> frozenset[str] | None:
        """Account for one message; return the pair if it just crossed the limit."""
        self._senders[msg.id] = msg.sender
        if msg.sender in (ORCHESTRATOR, USER):
            return None
        other = self._other(msg)
        if other is None or other == msg.sender:
            return None
        pair = frozenset({msg.sender, other})
        if msg.kind in PROGRESS_KINDS:
            self._counts[pair] = 0
            return None
        self._counts[pair] = self._counts.get(pair, 0) + 1
        if self._counts[pair] > self.limit and pair not in self.flagged:
            self.flagged.add(pair)
            return pair
        return None


# --------------------------------------------------------------------------------------------
# Discussion phases (docs/06 §5.1, §5.2)
# --------------------------------------------------------------------------------------------


class DiscussionEnd(StrEnum):
    """Why a discussion phase ended (logged as `discussion_end` in `orchestrator.jsonl`)."""

    ROUNDS = "rounds"  # rounds: N rounds played
    ALL_PASSED = "all_passed"  # rounds: nobody posted anything but `pass` in a round
    ALL_DONE = "all_done"  # all members reported `done`
    QUIET = "quiet"  # event: `quiet_after_s` without messages and nobody working
    STOPPED = "stopped"  # user stop
    LIMIT = "limit"  # a run limit was reached


@dataclass(slots=True)
class DiscussionOutcome:
    """Result of `Orchestrator.discuss`: why it ended, rounds, wake-ups and all turns."""

    channel: str
    mode: ChannelMode
    ended_by: DiscussionEnd
    rounds: int = 0
    wakeups: dict[str, int] = field(default_factory=dict)
    outcomes: list[TurnOutcome] = field(default_factory=list)


_ROUND_PROMPT = (
    "Lies die neuen Nachrichten in #{channel}, arbeite weiter und poste deinen Beitrag. "
    "Hast du in dieser Runde nichts beizutragen, poste `pass`. Bist du fertig, melde `done` "
    "mit set_status und einem kurzen Fazit."
)
_KICKOFF_PROMPT = (
    "Diskussion in #{channel}: Lies den Steckbrief und die Nachrichten und beteilige dich. "
    "Bist du fertig, melde `done` mit set_status und einem kurzen Fazit."
)
_WAKE_REASONS = {
    "question": "eine offene Frage an dich",
    "mention": "eine Nachricht an dich",
    "any": "neue Nachrichten",
}
_WAKE_PROMPT = (
    "Du wurdest in #{channel} geweckt: {reason}. Lies deinen Posteingang und antworte, wenn "
    "du etwas beitragen kannst. Kannst du nichts mehr beitragen, melde `done` mit set_status."
)


async def run_discussion(
    orch: "Orchestrator",
    channel: str,
    prompt: str | None,
    *,
    stage: str | None,
    rounds: int | None,
    context: Mapping[str, Any] | None,
    kickoff: bool,
) -> DiscussionOutcome:
    """Run one discussion phase in `channel` according to its `mode` (docs/06 §5)."""
    config = orch.config.channel(channel)
    if config.mode is ChannelMode.ROUNDS:
        return await _run_rounds(orch, config, prompt, stage, rounds or DEFAULT_ROUNDS, context)
    if config.mode is ChannelMode.EVENT:
        return await _EventDiscussion(orch, config, prompt, stage, context, kickoff).run()
    raise ArrError(f"Die Taktung „moderated“ (Kanal #{channel}) gibt es erst ab Welle 4.")


def _stop_end(orch: "Orchestrator") -> DiscussionEnd:
    return DiscussionEnd.LIMIT if orch.limit_reached else DiscussionEnd.STOPPED


async def _run_rounds(
    orch: "Orchestrator",
    config: GroupChatConfig,
    prompt: str | None,
    stage: str | None,
    rounds: int,
    context: Mapping[str, Any] | None,
) -> DiscussionOutcome:
    """`rounds`: all members per round, until N rounds, all `pass`/silent/`done`, or a stop."""
    outcome = DiscussionOutcome(config.name, config.mode, DiscussionEnd.ROUNDS)
    task = prompt or _ROUND_PROMPT.format(channel=config.name)
    for number in range(1, rounds + 1):
        if orch.stop_requested:
            outcome.ended_by = _stop_end(orch)
            break
        members = [m for m in config.members if orch.agent_status(m) is not AgentStatus.DONE]
        if not members:
            outcome.ended_by = DiscussionEnd.ALL_DONE
            break
        outcome.rounds = number
        text = f"Runde {number} von {rounds} in #{config.name}.\n\n{task}"
        mark = orch.last_message_id()
        if config.order is RoundOrder.PARALLEL:
            jobs = [TurnJob(m, text, context) for m in members]
            outcome.outcomes += await orch.turns(jobs, stage=stage)
        else:
            for member in members:
                if orch.stop_requested:
                    break
                outcome.outcomes.append(await orch.turn(member, text, stage=stage, context=context))
        if orch.stop_requested:
            outcome.ended_by = _stop_end(orch)
            break
        posted = {
            m.sender
            for m in orch.room.messages(config.name, after_id=mark)
            if m.sender in members and m.kind is not MessageKind.PASS
        }
        if not posted:
            done = all(orch.agent_status(m) is AgentStatus.DONE for m in config.members)
            outcome.ended_by = DiscussionEnd.ALL_DONE if done else DiscussionEnd.ALL_PASSED
            break
    return outcome


@dataclass(slots=True)
class _Wake:
    due: float
    reasons: list[str] = field(default_factory=list)
    messages: list[int] = field(default_factory=list)


class _EventDiscussion:
    """`event`: kick-off for everybody, then wake idle members on `wake_on` (docs/06 §5.2)."""

    def __init__(
        self,
        orch: "Orchestrator",
        config: GroupChatConfig,
        prompt: str | None,
        stage: str | None,
        context: Mapping[str, Any] | None,
        kickoff: bool,
    ) -> None:
        self.orch = orch
        self.config = config
        self.prompt = prompt or _KICKOFF_PROMPT.format(channel=config.name)
        self.kickoff = kickoff
        self.stage = stage
        self.context = context
        self.members = list(config.members)
        self.loop = asyncio.get_running_loop()
        self.changed = asyncio.Event()
        self.last_activity = self.loop.time()
        self.wakes: dict[str, _Wake] = {}
        self.busy: dict[str, asyncio.Task[TurnOutcome]] = {}
        self.no_autowake: set[str] = set()
        self.skipped: set[str] = set()
        self.ping_pong = PingPongTracker(orch.pacing.ping_pong_limit, orch.sender_of)
        self.outcome = DiscussionOutcome(config.name, config.mode, DiscussionEnd.QUIET)

    # -- notifications from the orchestrator -------------------------------------------

    def on_message(self, msg: Message) -> None:
        """A new message somewhere in the run (called by the orchestrator's watcher)."""
        if msg.channel != self.config.name:
            return
        self.last_activity = self.loop.time()
        pair = self.ping_pong.observe(msg)
        if pair is not None:
            self._stop_ping_pong(pair)
        for member in self.members:
            reason = self._trigger(msg, member)
            if reason is not None and not self._busy(member):
                self._schedule(member, reason, msg)
        self.changed.set()

    def on_turn_end(self, agent_id: str, undelivered: Sequence[Message]) -> None:
        """A member's turn ended; triggers it got no hint for during the turn wake it later.

        Messages it saw (at turn start in its inbox or as a hint) do not wake it again, even if
        a question stays open; otherwise an agent that does not answer would be woken until
        `max_wakeups_per_agent`. The open question stays in its next inbox.
        """
        if agent_id not in self.members:
            return
        self.last_activity = self.loop.time()
        for msg in undelivered:
            reason = self._trigger(msg, agent_id)
            if reason is not None:
                self._schedule(agent_id, reason, msg)
        self.changed.set()

    def kick(self) -> None:
        """Re-check the end conditions now, e.g. after a stop."""
        self.changed.set()

    # -- rules ---------------------------------------------------------------------------

    def _trigger(self, msg: Message, member: str) -> str | None:
        """Wake reason of `msg` for `member`, or None (own messages never wake)."""
        if msg.sender in (member, ORCHESTRATOR) or member in self.no_autowake:
            return None
        wake_on = self.config.wake_on
        if msg.kind is MessageKind.QUESTION and member in msg.to and WakeOn.QUESTION in wake_on:
            return "question"
        if member in msg.to and WakeOn.MENTION in wake_on:
            return "mention"
        if WakeOn.ANY in wake_on and self.orch.agent_status(member) is not AgentStatus.DONE:
            return "any"
        return None

    def _busy(self, member: str) -> bool:
        return member in self.busy or self.orch.is_busy(member)

    def _schedule(self, member: str, reason: str, msg: Message) -> None:
        wake = self.wakes.get(member)
        if wake is None:
            wake = self.wakes[member] = _Wake(due=self.loop.time() + self.config.debounce_s)
        if reason not in wake.reasons:
            wake.reasons.append(reason)
        if msg.id not in wake.messages:
            wake.messages.append(msg.id)

    def _stop_ping_pong(self, pair: frozenset[str]) -> None:
        agents = sorted(pair)
        self.no_autowake.update(agents)
        for agent in agents:
            self.wakes.pop(agent, None)
        self.orch.log.write("ping_pong", channel=self.config.name, agents=agents)
        self.orch.room.post(self.config.name, PING_PONG_NOTE, to=agents)

    # -- loop ------------------------------------------------------------------------------

    def _start(self, tg: asyncio.TaskGroup, member: str, prompt: str) -> None:
        task = tg.create_task(
            self.orch.turn(member, prompt, stage=self.stage, context=self.context)
        )
        self.busy[member] = task

        def finished(done: "asyncio.Task[TurnOutcome]") -> None:
            self.busy.pop(member, None)
            if not done.cancelled() and done.exception() is None:
                self.outcome.outcomes.append(done.result())
            self.changed.set()

        task.add_done_callback(finished)

    def _fire(self, tg: asyncio.TaskGroup, member: str, wake: _Wake) -> None:
        if self._busy(member) or member in self.no_autowake:
            return  # it sees the messages in its inbox anyway
        count = self.orch.wakeups(self.config.name, member)
        if count >= self.config.max_wakeups_per_agent:
            if member not in self.skipped:
                self.skipped.add(member)
                self.orch.log.write(
                    "wake_skipped",
                    channel=self.config.name,
                    agent=member,
                    reason="max_wakeups_per_agent",
                    limit=self.config.max_wakeups_per_agent,
                )
            return
        count = self.orch.count_wakeup(self.config.name, member)
        self.outcome.wakeups[member] = count
        reason = ", ".join(_WAKE_REASONS[r] for r in wake.reasons)
        self.orch.log.write(
            "wake",
            channel=self.config.name,
            agent=member,
            reasons=wake.reasons,
            messages=wake.messages,
            count=count,
        )
        self.orch.mark_delivered(wake.messages, DeliveryStatus.WOKEN)
        self._start(tg, member, _WAKE_PROMPT.format(channel=self.config.name, reason=reason))

    def _ended(self, now: float) -> DiscussionEnd | None:
        if self.orch.stop_requested:
            return _stop_end(self.orch)
        if self.busy or self.wakes or any(self.orch.is_busy(m) for m in self.members):
            return None
        if all(self.orch.agent_status(m) is AgentStatus.DONE for m in self.members):
            return DiscussionEnd.ALL_DONE
        if now - self.last_activity >= self.config.quiet_after_s:
            return DiscussionEnd.QUIET
        return None

    async def run(self) -> DiscussionOutcome:
        self.orch.listen(self)
        try:
            async with asyncio.TaskGroup() as tg:
                if self.kickoff:
                    for member in self.members:
                        if self.orch.agent_status(member) is not AgentStatus.DONE:
                            self._start(tg, member, self.prompt)
                while True:
                    now = self.loop.time()
                    for member, wake in sorted(self.wakes.items(), key=lambda i: i[1].due):
                        if wake.due <= now and not self.orch.stop_requested:
                            del self.wakes[member]
                            self._fire(tg, member, wake)
                    ended = self._ended(now)
                    if ended is not None:
                        self.outcome.ended_by = ended
                        break
                    deadlines = [w.due for w in self.wakes.values()]
                    if not self.busy:
                        deadlines.append(self.last_activity + self.config.quiet_after_s)
                    timeout = max(0.0, min(deadlines) - now) if deadlines else None
                    self.changed.clear()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self.changed.wait(), timeout)
        finally:
            self.orch.unlisten(self)
        return self.outcome
