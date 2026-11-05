"""Types of the group chat room: messages, channels, results, ratings, votes, assignments.

`RoomAPI` is the contract for the agent-facing room tools (docs/06-gruppenchats.md §9). The
SQLite store (W1-03) implements it, the FakeBackend (W1-04) and the orchestrator (W2-03)
program against it. Table layout: docs/02-workfolder-und-runs.md §5.
"""

from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from arr.models import AgentStatus, ChannelMode, Record


class MessageKind(StrEnum):
    """Kind of a chat message; some kinds have consequences (docs/06-gruppenchats.md §3)."""

    NOTE = "note"
    IDEA = "idea"
    CLAIM = "claim"
    PROOF = "proof"
    OBJECTION = "objection"
    QUESTION = "question"
    ANSWER = "answer"
    PROPOSAL = "proposal"
    VOTE = "vote"
    DECISION = "decision"
    RESULT = "result"
    PASS = "pass"


class MessageVia(StrEnum):
    """Channel through which a message from `user` arrived (docs/08 §5.0, Codeplan §8 W1-03)."""

    GUI = "gui"
    USER_AGENT = "user_agent"


class DeliveryStatus(StrEnum):
    """How a `user` message reached the addressed agents (docs/08-ux-konzept.md §5.0).

    `hint_mid_turn`: hint during a running turn (docs/06 §4); `woken`: the agent got a new
    turn; `next_inbox`: will be shown at the next turn start; `pending`: not delivered yet.
    """

    HINT_MID_TURN = "hint_mid_turn"
    WOKEN = "woken"
    NEXT_INBOX = "next_inbox"
    PENDING = "pending"


class VoteChoice(StrEnum):
    """Answer in a vote on a `proposal` (docs/06-gruppenchats.md §7)."""

    YES = "yes"
    NO = "no"
    ABSTAIN = "abstain"


class VoteRule(StrEnum):
    """Rule of a vote started with `call_vote` (docs/06-gruppenchats.md §5.3)."""

    MAJORITY = "majority"
    UNANIMOUS = "unanimous"


class Attachment(Record):
    """File attached to a message, in `groupchats/attachments/<kanal>/<msg-id>/` (docs/06 §3).

    `path` is relative to the run folder.
    """

    name: str
    size: int = Field(ge=0, description="Bytes")
    path: str


class Delivery(Record):
    """Delivery state of a `user` message, with the time of the last change (docs/08 §5.0)."""

    status: DeliveryStatus
    ts: float


class Message(Record):
    """A message in a channel (docs/06-gruppenchats.md §3, table `msg` in docs/02 §5).

    `to` empty means everybody. A `question` with `to` stays `open` until the addressee posts
    an `answer` with `reply_to`. `sender` is an agent ID, `orchestrator` or `user`;
    `sender_alias` is set in anonymous channels. `via` and `delivery` only for `user`.
    """

    id: int
    ts: float
    channel: str
    sender: str
    sender_alias: str | None = None
    kind: MessageKind = MessageKind.NOTE
    body: str
    to: list[str] = Field(default_factory=list)
    reply_to: int | None = None
    refs: list[Attachment] = Field(default_factory=list)
    pinned: bool = False
    open: bool = False
    via: MessageVia | None = None
    delivery: Delivery | None = None


class ChannelMember(Record):
    """Member of a channel; in anonymous channels agents only see the alias (docs/06 §2)."""

    agent: str | None = Field(description="Agent ID; None when hidden by `anonymous`")
    alias: str | None = None


class ChannelInfo(Record):
    """A channel as seen by an agent through `list_channels()` (docs/06-gruppenchats.md §2).

    The GUI has its own, richer model (docs/08 §5.0) with the same enums.
    """

    name: str
    purpose: str | None = None
    members: list[ChannelMember] = Field(default_factory=list)
    mode: ChannelMode
    moderator: str | None = None
    open: bool = True
    open_from: str | None = None
    anonymous: bool = False
    message_count: int = Field(default=0, ge=0)
    unread: int = Field(default=0, ge=0, description="Unread for the calling agent")


class Inbox(Record):
    """Result of `inbox()`: open questions, unread counts, pinned summaries (docs/06 §4, §8)."""

    open_questions: list[Message] = Field(default_factory=list)
    unread: dict[str, int] = Field(default_factory=dict, description="Channel → unread count")
    pinned: list[Message] = Field(default_factory=list, description="Briefs, „Stand“ notes")


class Result(Record):
    """A result submitted with `submit_result` (docs/01 §5, table `result` in docs/02 §5).

    `kind` is free text such as `idea`, `exploration`, `review`, `proof`; `path` is relative
    to the run folder (copied to `output/results/`).
    """

    id: int
    ts: float
    agent: str
    stage: str | None = None
    kind: str
    title: str
    body: str | None = None
    path: str | None = None
    attempt: int = Field(default=1, ge=1)


class Rating(Record):
    """A rating of a result with `submit_rating` (docs/01 §5, table `rating` in docs/02 §5)."""

    id: int
    ts: float
    rater: str
    result_id: int
    score: float
    criteria: dict[str, float] = Field(default_factory=dict)
    comment: str = ""


class Vote(Record):
    """A vote on a proposal (docs/06 §7, table `vote` in docs/02 §5)."""

    proposal_id: int
    agent: str
    choice: VoteChoice
    reason: str = ""
    ts: float


class Assignment(Record):
    """Current assignment of an agent, re-readable via `get_assignment()` (docs/03 §4.5).

    `context` is the context package (task, artifact, diffs, objections, …).
    """

    agent: str
    turn: int = Field(ge=1)
    stage: str | None = None
    prompt: str
    context: dict[str, Any] = Field(default_factory=dict)
    ts: float


class AgentStatusRecord(Record):
    """Row of table `agent_state`: status, wake-ups and note of one agent (docs/02 §5)."""

    agent: str
    status: AgentStatus
    wakeups: int = Field(default=0, ge=0)
    note: str = ""
    ts: float


class WaitKind(StrEnum):
    """How an agent waits (docs/09-agent-loop.md §4.3): inside its turn, or after it."""

    MESSAGES = "wait_for_messages"
    UNTIL = "wait_until"


class WaitState(Record):
    """Row of table `agent_wait`: an agent waits for a message, a file or its deadline.

    `file` is relative to the agent's working folder (`until_file`); `since`/`until` are Unix
    times. Written by the room tools, cleared when the wait ends or the next turn starts
    (docs/09-agent-loop.md §4.3).
    """

    agent: str
    kind: WaitKind
    since: float
    until: float
    file: str | None = None


class RoomError(Exception):
    """Expected failure of a room tool, e.g. not a member or message too long (docs/06 §9).

    The message is German and shown to the agent (the MCP server turns it into a `ToolError`).
    """


@runtime_checkable
class RoomAPI(Protocol):
    """Agent-facing room tools, bound to one agent (`agent_id`), see docs/06-gruppenchats.md §9.

    Implementations enforce membership (reading and writing only in own channels) and raise
    `RoomError` with a German message for expected failures. Methods are synchronous: the
    store works on SQLite and the MCP server runs tools in a worker thread (docs/spikes S4).
    Writes by `orchestrator` and `user` are not part of this protocol.
    """

    @property
    def agent_id(self) -> str:
        """ID of the agent this view acts for."""
        ...

    def inbox(self) -> Inbox:
        """Open questions to me, unread messages per channel, pinned summaries."""
        ...

    def list_channels(self) -> list[ChannelInfo]:
        """Channels the agent is a member of."""
        ...

    def read_messages(
        self,
        channel: str | None = None,
        only_new: bool = True,
        limit: int = 50,
        thread: int | None = None,
    ) -> list[Message]:
        """Read messages; with `only_new` advance the cursor. `thread` reads one strand."""
        ...

    def post_message(
        self,
        channel: str,
        body: str,
        kind: MessageKind = MessageKind.NOTE,
        to: list[str] | None = None,
        reply_to: int | None = None,
        refs: list[str] | None = None,
    ) -> Message:
        """Post a message. `refs` are file paths relative to the agent's working folder."""
        ...

    def get_attachment(self, msg_id: int, name: str) -> Path:
        """Return the absolute path of an attachment of a message in one of my channels."""
        ...

    def vote(self, proposal_id: int, choice: VoteChoice, reason: str = "") -> Vote:
        """Vote on a proposal; a second vote replaces the first."""
        ...

    def set_status(self, status: AgentStatus, note: str = "") -> None:
        """Report the own status, typically `working` or `done` with a short summary."""
        ...

    def submit_result(
        self,
        kind: str,
        title: str,
        body: str | None = None,
        path: str | None = None,
    ) -> Result:
        """Submit a result for the current stage; `path` is relative to the working folder."""
        ...

    def wait_until(self, minutes: float, until_file: str | None = None) -> WaitState:
        """Ask to be woken after `minutes` (at most 30), on a message to me or `until_file`.

        The agent ends its turn afterwards; the orchestrator wakes it (docs/09 §4.3). The
        blocking `wait_for_messages` is built on this view in `arr.room.waiting`.
        """
        ...

    def submit_rating(
        self,
        result_id: int,
        score: float,
        comment: str,
        criteria: dict[str, float] | None = None,
    ) -> Rating:
        """Rate a result on the scale and criteria of the rubric."""
        ...

    def get_assignment(self) -> Assignment | None:
        """Current assignment with stage and context package, or None before the first turn."""
        ...

    def give_floor(self, agent: str, prompt: str, channel: str | None = None) -> None:
        """Moderator only: give an agent the floor with a concrete question.

        `channel` may be omitted if the agent moderates exactly one channel.
        """
        ...

    def call_vote(self, proposal_id: int, rule: VoteRule = VoteRule.MAJORITY) -> None:
        """Moderator only: start a vote on a proposal."""
        ...

    def pin(self, msg_id: int) -> None:
        """Moderator only: pin a message."""
        ...

    def close_discussion(self, decision: str, channel: str | None = None) -> Message:
        """Moderator only: end the discussion and post the `decision`.

        `channel` may be omitted if the agent moderates exactly one channel.
        """
        ...
