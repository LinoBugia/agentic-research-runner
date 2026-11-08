"""Who a message is for: `@name` in the text, `@alle`, `@user`, `reply_to` (docs/09 §4.1).

Models write `@name` into the text instead of filling `to` (UX-Abnahme 3, Run B), and they
answer with `reply_to` without naming anyone (Run C). `mentions` turns `@name` of a channel
member into an addressee when the message is posted; `address` tells for one member whether a
message is meant for it. Both the room (end of `wait_for_messages`) and the orchestrator (who
is woken) use `address`, so they agree.

`@user` is a fixed address (L16): it always means the user, also when the user is no member
of the channel, and it never resolves to an agent. A question that names the user belongs to
the user: for agents in its `to` it is a mention, not a question (`is_user_question`).
"""

import re
from collections.abc import Callable, Iterable
from enum import StrEnum

from arr.room.types import Message, MessageKind

__all__ = ["ALL_MEMBERS", "USER_ADDRESS", "Address", "address", "is_user_question", "mentions"]

ALL_MEMBERS = ("alle", "all")
"""`@alle` addresses every member except the sender (L6)."""
USER_ADDRESS = "user"
"""`@user`: the user, a fixed address in every channel (L16); also the sender ID `user`."""

_MENTION_RE = re.compile(r"(?<![\w@.])@([A-Za-z0-9][A-Za-z0-9_-]*)")
"""`@name` at the start or after a non-word character, so `a@b.de` is no mention."""


class Address(StrEnum):
    """Why a message concerns a member (`address`)."""

    QUESTION = "question"  # a question with the member in `to`
    MENTION = "mention"  # the member is in `to` (also through `@name`)
    REPLY = "reply"  # `reply_to` points at a message of the member
    QUESTION_ALL = "question_all"  # a question without a recognisable addressee


def mentions(
    body: str, members: Iterable[str], sender: str, resolve: Callable[[str], str | None]
) -> list[str]:
    """Members named with `@name` in `body`, in order; `@alle` adds all but the sender.

    `resolve` maps a written name to a member ID (aliases in anonymous channels) or None.
    `@user` gives `USER_ADDRESS` without asking `resolve` (L16). Names that are no member, and
    the sender itself, are ignored: a mention is a hint, not a command that can fail.
    """
    members = list(members)
    found: list[str] = []
    for match in _MENTION_RE.finditer(body):
        name = match.group(1)
        if name.casefold() in ALL_MEMBERS:
            found += [m for m in members if m != sender]
            continue
        if name.casefold() == USER_ADDRESS:
            if sender != USER_ADDRESS:
                found.append(USER_ADDRESS)
            continue
        agent = resolve(name)
        if agent is not None and agent != sender:
            found.append(agent)
    return list(dict.fromkeys(found))


def is_user_question(msg: Message) -> bool:
    """A question to the user (`@user` or `to` with `user`): only the user answers it (L16)."""
    return msg.kind is MessageKind.QUESTION and USER_ADDRESS in msg.to


def address(msg: Message, member: str, sender_of: Callable[[int], str | None]) -> Address | None:
    """How `msg` concerns `member`, or None (own messages never concern their sender).

    `sender_of` looks up the sender of the message `reply_to` points at. A question to the
    user is a mention for the agents in its `to` (L16).
    """
    if msg.sender == member:
        return None
    if member in msg.to:
        if msg.kind is MessageKind.QUESTION and not is_user_question(msg):
            return Address.QUESTION
        return Address.MENTION
    target = sender_of(msg.reply_to) if msg.reply_to is not None else None
    if target == member:
        return Address.REPLY
    if msg.kind is MessageKind.QUESTION and not msg.to and target in (None, msg.sender):
        return Address.QUESTION_ALL
    return None
