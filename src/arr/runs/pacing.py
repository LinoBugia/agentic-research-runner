"""Chat pacing: inbox at turn start, hints mid-turn, ping-pong guard, `rounds` and `event`.

How agents learn about new messages (docs/06-gruppenchats.md §4):

- **Inbox at turn start:** `render_turn_prompt` puts open questions to the agent (in full),
  unread messages of `user` (in full, marked as having priority, docs/06 §10) and the number
  of new messages per channel in front of the assignment.
- **Hints mid-turn:** `compose_hint` builds the short text for `AgentBackend.deliver_hint`;
  `HintGate` allows at most one hint per 60 s or per 5 tool calls of a turn; a question with
  `to` to the agent (`is_question_to`) skips that window (W2-05 §7, W3-02).

Who is when on turn (docs/06 §5.1, §5.2, docs/09 §4) is a *discussion phase*:
`Orchestrator.discuss` (implemented by `run_discussion`). `rounds` gives all members a turn
per round. `event` gives everyone the kick-off; then a message that concerns an idle or
waiting member wakes it at once (`arr.room.address`: `to`, `@name`, `reply_to`, a question to
all), `any` after `debounce_s`. A finished member is reachable (L13): every direct message to
it (`@name`, a reply to its message, a question to it) wakes it, and after that turn it is
finished again unless it reports otherwise. A `wait_until` ends with its file, with the next
result of the agent it waits for (L14) or with its deadline; with nobody working and an idle
member, a *round* starts after `idle_after_s`, at most `rounds` of them, also while others
wait long (L11). The discussion ends with `all_done` only when no direct message to a member
is left unread (L15). Wake-up limit (also for the wake-ups of `wait_until`, L10), ping-pong
guard and `quiet_after_s` stay as safety nets. Every wait is logged (`waiting`, `round`,
`wake_scheduled`). Outside a discussion phase nobody is woken automatically; hints and the
inbox work always.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from arr.backends.base import TurnOutcome
from arr.errors import ArrError
from arr.models import AgentStatus, ChannelMode, GroupChatConfig, RoundOrder, WakeOn
from arr.permissions.types import DecisionAction
from arr.room import waiting
from arr.room.address import Address, address, is_user_question
from arr.room.store import ORCHESTRATOR, USER, WAIT_UNTIL_REPEAT
from arr.room.types import DeliveryStatus, Inbox, Message, MessageKind, Result
from arr.runs.context import DiscussionEnd, DiscussionOutcome, TurnJob

if TYPE_CHECKING:
    from arr.room.store import AgentRoom
    from arr.runs.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ROUNDS",
    "HintGate",
    "PacingSettings",
    "PingPongTracker",
    "compose_hint",
    "is_question_to",
    "render_turn_prompt",
    "run_discussion",
]

DEFAULT_ROUNDS = 3
"""Rounds of a `rounds` discussion when the topology gives none."""

DIRECT = frozenset({Address.QUESTION, Address.MENTION, Address.REPLY})
"""A direct message to a member (L13, L15): `to`/`@name`, a reply to its message, a question."""
MAX_DIRECT_IN_PROMPT = 10
"""Direct messages shown in full at the start of a turn; older ones are only counted."""
RESULT_EXCERPT_CHARS = 1500
"""How much of a result body the wake-up of `until_result_from` shows (L14)."""

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
    decision; one more and the guard steps in. `turn_silence_warn_s`: a turn without any
    event for this long gets a warning `turn_silent`, it is not aborted (L5, docs/09 §4.5).
    """

    poll_interval_s: float = 0.25
    hint_interval_s: float = 60.0
    hint_tool_calls: int = 5
    ping_pong_limit: int = 6
    turn_silence_warn_s: float = 600.0


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
    agent_id: str,
    prompt: str,
    inbox: Inbox | None,
    user_messages: Sequence[Message] = (),
    direct: Sequence[Message] = (),
) -> str:
    """The prompt of a turn: inbox first, then the assignment (docs/06 §4, §10).

    The inbox lists open questions to the agent in full (questions of `user` first), then
    unread messages of `user` in full with a priority mark, then the other unread messages to
    the agent (`direct`: `@name`, replies to its messages; at most `MAX_DIRECT_IN_PROMPT`, the
    newest), so a finished agent woken by a request sees it (L13); then the number of new
    messages per channel. Without an inbox (agent in no channel) the prompt is returned
    unchanged. The agent reads other messages itself, so the prompt stays short.
    """
    if inbox is None:
        return prompt
    questions = sorted(inbox.open_questions, key=lambda m: (m.sender != USER, m.id))
    asked = {m.id for m in questions}
    from_user = [m for m in user_messages if m.id not in asked]
    shown = asked | {m.id for m in from_user}
    to_me = [m for m in direct if m.id not in shown][-MAX_DIRECT_IN_PROMPT:]
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
    if to_me:
        rendered = "\n\n".join(_render(m, me=agent_id) for m in to_me)
        parts.append("Nachrichten an dich (ungelesen):\n\n" + rendered)
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


def is_question_to(msg: Message, agent_id: str) -> bool:
    """True for a question that names `agent_id` in `to`; a question to the user is none (L16)."""
    return msg.kind is MessageKind.QUESTION and agent_id in msg.to and not is_user_question(msg)


def compose_hint(agent_id: str, messages: Iterable[Message]) -> str:
    """Short hint such as „📨 1 Frage an dich in #main · 2 neue Nachrichten in #main“.

    Senders are not named (aliases in anonymous channels); messages of `user` are counted
    separately. The agent fetches the content itself.
    """
    questions: dict[str, int] = {}
    others: dict[str, int] = {}
    users: dict[str, int] = {}
    for msg in messages:
        if is_question_to(msg, agent_id):
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
    seconds or `tool_calls` tool calls since the previous one, whichever comes first. A hint
    with a question to this agent (`urgent`) goes at once: otherwise the agent finds the
    question only through its own `inbox()` at the end of the turn (W2-05 §7).
    """

    interval_s: float = 60.0
    tool_calls: int = 5
    clock: Callable[[], float] = time.monotonic
    _last_at: float | None = None
    _last_calls: int = 0

    def allows(self, tool_calls: int, *, urgent: bool = False) -> bool:
        """True if a hint may be delivered now, given the tool calls of the turn so far."""
        if urgent or self._last_at is None:
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
    """Counts the exchange between two agents without progress and flags the pair once.

    A message goes from its sender to one other agent if `to` names exactly that agent or
    it replies to a message of that agent; a message to the user (L16) goes to no agent. Only
    an exchange counts, a message in the other direction than the last one of the pair: many
    messages of one agent to another in a row count once (W3-09.1, B3), so a burst of requests
    to a finished agent does not lock it out. A claim, proof or decision between the two resets
    their count; more than `limit` changes of direction without one flag the pair (only once).
    `sender_of` looks up the sender of older messages that the tracker has not seen.
    """

    def __init__(self, limit: int, sender_of: Callable[[int], str | None]) -> None:
        self.limit = limit
        self._sender_of = sender_of
        self._senders: dict[int, str] = {}
        self._counts: dict[frozenset[str], int] = {}
        self._last: dict[frozenset[str], str] = {}
        """Sender of the last counted message of a pair."""
        self.flagged: set[frozenset[str]] = set()

    def _other(self, msg: Message) -> str | None:
        if len(msg.to) == 1:
            return msg.to[0] if msg.to[0] not in (ORCHESTRATOR, USER) else None
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
            self._last.pop(pair, None)
            return None
        if self._last.get(pair) == msg.sender:
            return None  # the same direction again: no exchange (B3)
        self._last[pair] = msg.sender
        self._counts[pair] = self._counts.get(pair, 0) + 1
        if self._counts[pair] > self.limit and pair not in self.flagged:
            self.flagged.add(pair)
            return pair
        return None


# --------------------------------------------------------------------------------------------
# Discussion phases (docs/06 §5.1, §5.2)
# --------------------------------------------------------------------------------------------


_ROUND_PROMPT = (
    "Lies die neuen Nachrichten in #{channel}, arbeite weiter und poste deinen Beitrag. "
    "Hast du in dieser Runde nichts beizutragen, poste `pass`. Ist dein Teil erledigt, gib "
    "dein Ergebnis ab oder melde `done` mit set_status und einem kurzen Fazit."
)
_KICKOFF_PROMPT = (
    "Diskussion in #{channel}: Lies den Steckbrief und die Nachrichten und beteilige dich. "
    "Ist dein Teil erledigt, gib dein Ergebnis ab oder melde `done` mit set_status."
)
_IDLE_ROUND_PROMPT = (
    "Runde {number} von {of} in #{channel}: Lies die neuen Nachrichten, antworte, gib dein "
    "Ergebnis ab oder melde fertig."
)
_WAKE_REASONS = {
    "question": "eine Frage an dich",
    "mention": "eine Nachricht an dich",
    "reply": "eine Antwort auf deine Nachricht",
    "question_all": "eine Frage an alle",
    "any": "neue Nachrichten",
    "file": "die Datei, auf die du wartest, ist da",
    "timeout": "deine Wartezeit ist um",
    "result_from": "{agent} hat ein Ergebnis abgegeben",
    "result_missing": "{agent} ist fertig, ohne ein neues Ergebnis abzugeben",
}
_WAKE_PROMPT = (
    "Du wurdest in #{channel} geweckt: {reason}. Lies deinen Posteingang und antworte, wenn "
    "du etwas beitragen kannst. Ist dein Teil erledigt, gib dein Ergebnis ab oder melde "
    "`done`; du bleibst ansprechbar."
)
_SELF_WAKES = frozenset({"file", "timeout", "result_from", "result_missing"})
"""Wake-ups the agent asked for with `wait_until`; they count against `max_wakeups_per_agent`
like all others (L10), but the ping-pong guard does not stop them."""
_AWAITED = frozenset({"file", "result_from", "result_missing"})
"""Self wake-ups by what the agent waited for: they reset the count of deadlines (L10)."""
_CHANNEL_WAIT_ORDER = ("agents", "approval", "capacity")


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
    """Run one discussion phase in `channel` according to its `mode` (docs/06 §5, docs/09 §4).

    `rounds` (default 3) is the number of rounds in `rounds` mode and the number of idle
    rounds in `event` mode (L3).
    """
    config = orch.config.channel(channel)
    if config.mode is ChannelMode.ROUNDS:
        outcome = await _run_rounds(orch, config, prompt, stage, rounds or DEFAULT_ROUNDS, context)
    elif config.mode is ChannelMode.EVENT:
        discussion = _EventDiscussion(
            orch, config, prompt, stage, context, kickoff, rounds or DEFAULT_ROUNDS
        )
        outcome = await discussion.run()
    else:
        raise ArrError(f"Die Taktung „moderated“ (Kanal #{channel}) gibt es erst ab Welle 4.")
    if outcome.ended_by not in (DiscussionEnd.STOPPED, DiscussionEnd.LIMIT):
        await _hold_for_user_questions(orch, config, stage, context, outcome)
    return outcome


def user_question_hold_s() -> float:
    """B3 (User 11.11., option a): how long after a question to the user its discussion phase
    waits for the answer at most: the L7 deadline (`waiting.STREAK_MAX_S`, 5 min, V10)."""
    return waiting.STREAK_MAX_S


_ANSWER_PROMPT = (
    "Du wurdest in #{channel} geweckt: Der User hat deine Frage beantwortet. Die Antwort steht "
    "in deinem Posteingang. Prüfe deine Annahme damit. Ändert sich dein Ergebnis, gib es neu "
    "ab; sonst melde `done`."
)


async def _hold_for_user_questions(
    orch: "Orchestrator",
    config: GroupChatConfig,
    stage: str | None,
    context: Mapping[str, Any] | None,
    outcome: DiscussionOutcome,
) -> None:
    """B3 (W4-06.1, User 11.11., option a): the phase does not end while a member's question
    to the user in this channel is open, until the user answers or `user_question_hold_s()`
    after the question passed. An answer wakes its asker once (L13, counted like every
    wake-up); then the phase looks again. After the deadline V10 (c) holds: the question stays
    open until the run ends, the agent worked on with an assumption. No new limit for agents:
    a stop or limit ends the wait at once.
    """
    watched: dict[int, Any] = {}
    reported = False
    while True:
        try:
            orch.poll()  # an answer just given closes its question first
        except Exception:  # the watcher keeps polling; a read error ends nothing
            logger.exception("Lesen des Gruppenchats in #%s fehlgeschlagen", config.name)
        if orch.stop_requested:
            outcome.ended_by = _stop_end(orch)
            break
        for question in orch.open_user_questions(config.name):
            watched.setdefault(question.msg_id, question)
        answered = []
        for msg_id, question in list(watched.items()):
            if orch.user_question_open(msg_id):
                continue
            del watched[msg_id]
            item = orch.inbox.get(question.item_id)
            if item.decision is not None and item.decision.action is DecisionAction.ANSWER:
                answered.append(question)
        for question in answered:
            await _wake_for_answer(orch, config, stage, context, question, outcome)
        if answered:
            continue
        now = time.time()
        hold = user_question_hold_s()
        due = [q.asked + hold for q in watched.values()]
        due = [d for d in due if d > now]
        if not due:
            break
        if not reported:
            reported = True
            orch.log.write("waiting", channel=config.name, reason="user", until=max(due))
        await asyncio.sleep(orch.pacing.poll_interval_s)
    if reported:
        orch.log.write("waiting", channel=config.name, reason="none")


async def _wake_for_answer(
    orch: "Orchestrator",
    config: GroupChatConfig,
    stage: str | None,
    context: Mapping[str, Any] | None,
    question: Any,
    outcome: DiscussionOutcome,
) -> None:
    """The asker's turn after the user's answer (L13), within `max_wakeups_per_agent`; it
    counts in `outcome.wakeups` like every wake-up of the phase (B13)."""
    agent = question.agent
    if orch.wakeups(config.name, agent) >= config.max_wakeups_per_agent:
        orch.log.write(
            "wake_skipped",
            channel=config.name,
            agent=agent,
            reason="max_wakeups_per_agent",
            limit=config.max_wakeups_per_agent,
        )
        return
    count = orch.count_wakeup(config.name, agent)
    outcome.wakeups[agent] = count
    orch.log.write(
        "wake", channel=config.name, agent=agent, reasons=["answer"], messages=[], count=count
    )
    prompt = _ANSWER_PROMPT.format(channel=config.name)
    outcome.outcomes.append(await orch.turn(agent, prompt, stage=stage, context=context))


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
        orch.log.write("round", channel=config.name, number=number, of=rounds, agents=members)
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
    else:
        if all(orch.agent_status(m) is AgentStatus.DONE for m in config.members):
            outcome.ended_by = DiscussionEnd.ALL_DONE
    return outcome


@dataclass(slots=True)
class _Wake:
    due: float
    reasons: list[str] = field(default_factory=list)
    messages: list[int] = field(default_factory=list)
    awaited: str | None = None
    """`result_from`/`result_missing`: the agent it waited for, as the waiting agent sees it
    (its alias behind an anonymous channel, L14, W3-09.1 B2)."""
    result: str | None = None
    """`result_from`: the result, shown in the wake-up prompt (only what it may read, B1)."""
    final: bool = False
    """L15: woken once for an unread message before the end, also behind the ping-pong guard."""


@dataclass(frozen=True, slots=True)
class _ChannelWait:
    """What the discussion waits for, logged as `waiting {channel, …}` on every change."""

    reason: str  # none | idle | agents | approval | capacity | wake | mixed
    until: float | None = None  # Unix time
    next: str | None = None  # idle: what comes at `until`: `round` or `end`
    parts: tuple[str, ...] = ()  # mixed: the waits of the running turns, e.g. agents, capacity


class _EventDiscussion:
    """`event` (docs/06 §5.2, docs/09 §4.3, §4.4): kick-off, wake-ups, idle rounds.

    A member is in one state: working (turn running, maybe blocked by an approval or inside
    `wait_for_messages`), waiting for a free place, waiting long (`wait_until`), idle, or done
    and reachable (L13). The discussion ends with `all_done` when all members are done and no
    direct message to one of them is unread (L15; such a message wakes its addressee once),
    with `rounds` after the last idle round, with `quiet` when `quiet_after_s` passed without
    a message although a round ran, or with `stopped`/`limit`. Every state that waits has a
    deadline: the turn's own limits, `approvals.timeout`, the `wait_until` deadline,
    `idle_after_s`.

    A member waiting long holds nobody up (L11): rounds and `quiet` go on for the idle ones,
    and the end of the discussion ends its wait (`wait_end`). Its wake-ups count against
    `max_wakeups_per_agent`, and after `WAIT_UNTIL_REPEAT` deadlines in a row without a
    message or its file it is idle instead of woken (L10).
    """

    def __init__(
        self,
        orch: "Orchestrator",
        config: GroupChatConfig,
        prompt: str | None,
        stage: str | None,
        context: Mapping[str, Any] | None,
        kickoff: bool,
        rounds: int,
    ) -> None:
        self.orch = orch
        self.config = config
        self.prompt = prompt or _KICKOFF_PROMPT.format(channel=config.name)
        self.kickoff = kickoff
        self.stage = stage
        self.context = context
        self.rounds = rounds
        self.members = list(config.members)
        self.loop = asyncio.get_running_loop()
        self.changed = asyncio.Event()
        self.last_activity = self.loop.time()
        self.last_message = self.loop.time()
        self.last_round: float | None = None
        self.wakes: dict[str, _Wake] = {}
        self.busy: dict[str, asyncio.Task[TurnOutcome]] = {}
        self.no_autowake: set[str] = set()
        self.skipped: set[str] = set()
        self.ping_pong = PingPongTracker(orch.pacing.ping_pong_limit, orch.sender_of)
        self.outcome = DiscussionOutcome(config.name, config.mode, DiscussionEnd.QUIET)
        self.reported: _ChannelWait | None = None
        self.final_wakes: set[tuple[str, int]] = set()
        """L15: (member, message) woken once because the message was unread at the end."""

    # -- notifications from the orchestrator -------------------------------------------

    def on_message(self, msg: Message) -> None:
        """A new message somewhere in the run (called by the orchestrator's watcher)."""
        if msg.channel != self.config.name:
            return
        self.last_activity = self.last_message = self.loop.time()
        pair = self.ping_pong.observe(msg)
        if pair is not None:
            self._stop_ping_pong(pair)
        for member in self.members:
            reason = self._trigger(msg, member)
            if reason is not None and not self._busy(member):
                self._schedule(member, reason, msg)
        self.changed.set()

    def on_turn_end(self, agent_id: str, undelivered: Sequence[Message]) -> None:
        """A member's turn ended; triggers it neither read nor got as a hint wake it now.

        Messages it saw (at turn start in its inbox, as a hint or read in the turn) do not
        wake it again, even if a question stays open; otherwise an agent that does not
        answer would be woken until `max_wakeups_per_agent`. The open question stays in its
        next inbox.
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

    def _trigger(self, msg: Message, member: str, *, guarded: bool = True) -> str | None:
        """Wake reason of `msg` for `member`, or None (docs/09 §4.1, §4.2).

        Own messages and messages of the orchestrator never wake. A finished member wakes
        for every direct message to it (L13: a question, `@name`, a reply to its message), not
        for a question to all or `any`; an exhausted one (`turns_per_agent`) never; a member
        that waits with `wait_until` only for messages that concern it. With `guarded` the
        ping-pong guard (`no_autowake`) holds; L15 asks without it.
        """
        if msg.sender in (member, ORCHESTRATOR) or (guarded and member in self.no_autowake):
            return None
        if self.orch.exhausted(member):
            return None
        wake_on = self.config.wake_on
        how = address(msg, member, self.orch.sender_of)
        if how is Address.QUESTION and {WakeOn.QUESTION, WakeOn.MENTION} & set(wake_on):
            return "question"
        if how in (Address.MENTION, Address.REPLY) and WakeOn.MENTION in wake_on:
            return how.value
        if self.orch.agent_status(member) is AgentStatus.DONE:
            return None
        if how is Address.QUESTION_ALL and WakeOn.QUESTION in wake_on:
            return how.value
        if WakeOn.ANY in wake_on and self.orch.long_wait(member) is None:
            return "any"
        return None

    def _busy(self, member: str) -> bool:
        """A turn of `member` runs or waits to start; a finished task whose callback has not
        run yet does not count (its turn has already reported its end)."""
        task = self.busy.get(member)
        return (task is not None and not task.done()) or self.orch.is_busy(member)

    def _schedule(self, member: str, reason: str, msg: Message | None = None, **info: Any) -> _Wake:
        """Wake `member`: at once (L8), only `any` after `debounce_s` (docs/06 §5.2).

        `info` goes into the log entry `wake_scheduled` (e.g. `unread` for L15, `result_from`).
        """
        now = self.loop.time()
        delay = self.config.debounce_s if reason == "any" else 0.0
        wake = self.wakes.get(member)
        if wake is None:
            wake = self.wakes[member] = _Wake(due=now + delay)
            due = time.time() + delay
            self.orch.log.write(
                "wake_scheduled",
                channel=self.config.name,
                agent=member,
                reason=reason,
                due=due,
                **info,
            )
        elif delay == 0.0:
            wake.due = min(wake.due, now)  # a message to it does not wait for a debounce
        if reason not in wake.reasons:
            wake.reasons.append(reason)
        if msg is not None and msg.id not in wake.messages:
            wake.messages.append(msg.id)
        self.changed.set()
        return wake

    def _stop_ping_pong(self, pair: frozenset[str]) -> None:
        agents = sorted(pair)
        self.no_autowake.update(agents)
        for agent in agents:
            self.wakes.pop(agent, None)
        self.orch.log.write("ping_pong", channel=self.config.name, agents=agents)
        self.orch.room.post(self.config.name, PING_PONG_NOTE, to=agents)

    def _check_long_waits(self) -> None:
        """`wait_until`: wake when the file is there, when the awaited agent submitted a result
        or finished without one (L14), or when the deadline has passed."""
        now = time.time()
        for member in self.members:
            wait = self.orch.long_wait(member)
            if wait is None or self._busy(member) or member in self.wakes:
                continue
            awaited = wait.result_from
            result = self.orch.new_result(awaited, wait.since, wait.mark) if awaited else None
            if wait.file is not None and self.orch.until_file_exists(member, wait.file):
                self._schedule(member, "file")
            elif awaited is not None and result is not None:
                wake = self._schedule(member, "result_from", result_from=awaited)
                wake.awaited = self.orch.view(member).shown_name(awaited)
                wake.result = _result_text(result, wake.awaited, self.orch.view(member))
            elif (
                awaited is not None
                and self.orch.agent_status(awaited) is AgentStatus.DONE
                and not self._busy(awaited)
            ):
                wake = self._schedule(member, "result_missing", result_from=awaited)
                wake.awaited = self.orch.view(member).shown_name(awaited)
            elif wait.until <= now:
                self._schedule(member, "timeout")

    # -- loop ------------------------------------------------------------------------------

    def _start(self, tg: asyncio.TaskGroup, member: str, prompt: str) -> None:
        self.orch.end_long_wait(member)
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
        own = set(wake.reasons) <= _SELF_WAKES
        if not (own or wake.final) and member in self.no_autowake:
            return
        if _AWAITED & set(wake.reasons):
            self.orch.reset_until_streak(member)
        elif wake.reasons == ["timeout"]:
            streak = self.orch.count_until_timeout(member)
            if streak >= WAIT_UNTIL_REPEAT:
                self._skip(member, "wait_until_repeat", WAIT_UNTIL_REPEAT)
                self.orch.end_long_wait(member)  # it is idle now, the rounds take it
                return
        count = self.orch.wakeups(self.config.name, member)
        if count >= self.config.max_wakeups_per_agent:
            if member not in self.skipped:
                self.skipped.add(member)
                self._skip(member, "max_wakeups_per_agent", self.config.max_wakeups_per_agent)
            self.orch.end_long_wait(member)  # no wake-up left: it is idle, rounds go on
            return
        count = self.orch.count_wakeup(self.config.name, member)
        self.outcome.wakeups[member] = count
        reason = ", ".join(_WAKE_REASONS[r].format(agent=wake.awaited) for r in wake.reasons)
        self.orch.log.write(
            "wake",
            channel=self.config.name,
            agent=member,
            reasons=wake.reasons,
            messages=wake.messages,
            count=count,
        )
        self.orch.mark_delivered(wake.messages, DeliveryStatus.WOKEN, member)
        prompt = _WAKE_PROMPT.format(channel=self.config.name, reason=reason)
        if wake.result is not None:
            prompt += "\n\n" + wake.result
        self._start(tg, member, prompt)

    def _skip(self, member: str, reason: str, limit: int) -> None:
        self.orch.log.write(
            "wake_skipped", channel=self.config.name, agent=member, reason=reason, limit=limit
        )

    def _idle_members(self) -> list[str]:
        """Members a round is for: not done, not working, not waiting long (L11)."""
        return [
            m
            for m in self.members
            if self.orch.agent_status(m) is not AgentStatus.DONE
            and self.orch.long_wait(m) is None
            and not self._busy(m)
        ]

    def _start_round(self, tg: asyncio.TaskGroup) -> None:
        members = self._idle_members()
        unread = {m: self.orch.addressed_unread(m, self.config.name) for m in members}
        members.sort(key=lambda m: unread[m] == 0)  # unread messages to them first
        number = self.outcome.rounds = self.outcome.rounds + 1
        self.last_round = self.last_activity = self.loop.time()
        self.orch.log.write(
            "round", channel=self.config.name, number=number, of=self.rounds, agents=members
        )
        prompt = _IDLE_ROUND_PROMPT.format(number=number, of=self.rounds, channel=self.config.name)
        for member in members:
            self._start(tg, member, prompt)

    def _all_done(self) -> bool:
        return all(self.orch.agent_status(m) is AgentStatus.DONE for m in self.members)

    def _wake_for_unread(self) -> bool:
        """L15: before `all_done`, wake every member a direct message to is unread for.

        Unread is a message after the member's cursor in this channel and after the start of
        its last turn (what was in a turn's inbox counts as seen); it must wake the member at
        all (`_trigger` without the ping-pong guard: exhausted, `wake_on`). The guard does not
        hold here (W3-09.1, B3): each message wakes its addressee at most once this way, and
        `max_wakeups_per_agent` holds, so the end comes. True if a wake-up was scheduled.
        """
        found = False
        for member in self.members:
            if self._busy(member):
                continue
            seen = max(self.orch.room.cursor(member, self.config.name), self.orch.turn_mark(member))
            for msg in self.orch.room.messages(self.config.name, after_id=seen):
                if (member, msg.id) in self.final_wakes:
                    continue
                if address(msg, member, self.orch.sender_of) not in DIRECT:
                    continue
                reason = self._trigger(msg, member, guarded=False)
                if reason is None:
                    continue
                self.final_wakes.add((member, msg.id))
                self._schedule(member, reason, msg, unread=msg.id).final = True
                found = True
        return found

    def _working_wait(self) -> _ChannelWait:
        """What the discussion waits for while turns run (only the running ones count).

        Each running turn waits for a free place, an approval or messages, or it works; one
        working turn makes it `none`. Different waits make `mixed` with their `parts` (B9).
        """
        parts: set[str] = set()
        untils: list[float] = []
        for member in (m for m in self.members if self._busy(m)):
            if self.orch.waits_for_capacity(member):
                parts.add("capacity")
            elif self.orch.agent_status(member) is AgentStatus.BLOCKED:
                parts.add("approval")
            elif (wait := self.orch.in_turn_wait(member)) is not None:
                parts.add("agents")
                untils.append(wait.until)
            else:
                return _ChannelWait("none")
        until = min(untils, default=None)
        if len(parts) == 1:
            reason = parts.pop()
            return _ChannelWait(reason, until if reason == "agents" else None)
        ordered = tuple(p for p in _CHANNEL_WAIT_ORDER if p in parts)
        return _ChannelWait("mixed", until, parts=ordered)

    def _report(self, wait: _ChannelWait) -> None:
        if wait != self.reported:
            self.reported = wait
            self.orch.log.write(
                "waiting",
                channel=self.config.name,
                reason=wait.reason,
                until=wait.until,
                next=wait.next,
                round=self.outcome.rounds,
                of=self.rounds,
                **({"parts": list(wait.parts)} if wait.parts else {}),
            )

    def _step(self, tg: asyncio.TaskGroup, now: float) -> DiscussionEnd | float | None:
        """One pass: an end, the next deadline (loop time), or None (wait for a change)."""
        if self.orch.stop_requested:
            return _stop_end(self.orch)
        if any(self._busy(m) for m in self.members):
            self._report(self._working_wait())
            due = [w.due for m, w in self.wakes.items() if not self._busy(m)]
            return min(due, default=None)
        if self.wakes:
            due = min(w.due for w in self.wakes.values())
            self._report(_ChannelWait("wake", time.time() + max(0.0, due - now)))
            return due
        if self._all_done():
            if self._wake_for_unread():  # L15
                return now
            return DiscussionEnd.ALL_DONE
        long_waits = [w for m in self.members if (w := self.orch.long_wait(m)) is not None]
        wake_at: float | None = None  # loop time at which a `wait_until` may wake
        if long_waits:
            until = min(w.until for w in long_waits)
            wake_at = now + max(0.0, until - time.time())
            if any(w.file or w.result_from for w in long_waits):
                wake_at = min(wake_at, now + 1.0)
            if not self._idle_members():  # only members waiting long are left
                self._report(_ChannelWait("agents", until))
                return wake_at
        # idle: rounds and quiet for the members that neither work nor wait (docs/09 §4.4,
        # L11: a member waiting long holds nobody up)
        quiet_at = self.last_message + self.config.quiet_after_s
        if self.last_round is not None and self.last_round >= self.last_message and now >= quiet_at:
            return DiscussionEnd.QUIET
        round_at = self.last_activity + self.config.idle_after_s
        if self.outcome.rounds >= self.rounds:
            if now >= round_at:
                return DiscussionEnd.ROUNDS
            self._report(_ChannelWait("idle", time.time() + round_at - now, "end"))
            return min(round_at, wake_at or round_at)
        if now >= round_at:
            self._start_round(tg)
            return None
        self._report(_ChannelWait("idle", time.time() + round_at - now, "round"))
        due = min(round_at, quiet_at) if self.last_round is not None else round_at
        return min(due, wake_at or due)

    def _end_long_waits(self, ended_by: DiscussionEnd) -> None:
        """The discussion is over: so are the `wait_until` of its members (L11)."""
        for member in self.members:
            if self.orch.long_wait(member) is not None:
                self.orch.end_long_wait(member)
                self.orch.log.write(
                    "wait_end",
                    channel=self.config.name,
                    agent=member,
                    reason="discussion_end",
                    ended_by=ended_by.value,
                )

    async def run(self) -> DiscussionOutcome:
        self.orch.listen(self)
        try:
            async with asyncio.TaskGroup() as tg:
                if self.kickoff:
                    for member in self.members:
                        if self.orch.agent_status(member) is not AgentStatus.DONE:
                            self._start(tg, member, self.prompt)
                while True:
                    # take the newest messages first: a question posted just before its
                    # sender finished must wake its addressee, not end the discussion
                    try:
                        self.orch.poll()
                    except Exception:  # the watcher keeps polling; a read error ends nothing
                        logger.exception(
                            "Lesen des Gruppenchats in #%s fehlgeschlagen", self.config.name
                        )
                    self._check_long_waits()
                    now = self.loop.time()
                    for member, wake in sorted(self.wakes.items(), key=lambda i: i[1].due):
                        # a wake-up from the end of a turn waits until that turn is over
                        due = wake.due <= now and not self._busy(member)
                        if due and not self.orch.stop_requested:
                            del self.wakes[member]
                            self._fire(tg, member, wake)
                    self.changed.clear()
                    step = self._step(tg, now)
                    if isinstance(step, DiscussionEnd):
                        self.outcome.ended_by = step
                        break
                    if self.busy and step is None:
                        poll = self.orch.pacing.poll_interval_s * 4  # channel state, waits
                        step = now + poll
                    timeout = None if step is None else max(0.0, step - now)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self.changed.wait(), timeout)
        finally:
            self.orch.unlisten(self)
            self._end_long_waits(self.outcome.ended_by)
            if self.reported is not None:
                self._report(_ChannelWait("none"))
        return self.outcome


def _result_text(result: Result | Message, shown: str, view: "AgentRoom") -> str | None:
    """A result for the wake-up of `until_result_from` (L14): title, file, start of the body.

    Only what the waiting agent may read itself (W3-09.1, B1, B2): a result of `submit_result`
    (run-wide) or a message `result` in a channel of its own, else None (the wake-up then only
    says that a result came). Names as it sees them (`shown`); a result file is named only if
    its path does not carry the ID of an agent hidden behind an alias.
    """
    if isinstance(result, Message):
        if not view.can_read(result.channel):
            return None
        head = f"Ergebnis von {shown} (Nachricht #{result.id} in #{result.channel}):"
        body = result.body
    elif view.store.is_sealed(result.id):
        return None  # a result of a sealed stage stays hidden until its release (W4-03 F4)
    else:
        head = f"Ergebnis #{result.id} von {shown}: „{result.title}“"
        if result.path:
            head += f" (Datei: {result.path})" if shown == result.agent else " (als Datei)"
        body = result.body or ""
    if len(body) > RESULT_EXCERPT_CHARS:
        body = body[:RESULT_EXCERPT_CHARS] + " …"
    return head + ("\n\n" + body if body else "")
