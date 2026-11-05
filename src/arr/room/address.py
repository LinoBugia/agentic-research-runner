"""Who a message is for: `@name` in the text, `@alle`, `reply_to` (docs/09 §4.1).

Models write `@name` into the text instead of filling `to` (UX-Abnahme 3, Run B), and they
answer with `reply_to` without naming anyone (Run C). `mentions` turns `@name` of a channel
member into an addressee when the message is posted; `address` tells for one member whether a
message is meant for it. Both the room (end of `wait_for_messages`) and the orchestrator (who
is woken) use `address`, so they agree.
"""

import re
from collections.abc import Callable, Iterable
from enum import StrEnum

from arr.room.types import Message, MessageKind

__all__ = ["ALL_MEMBERS", "Address", "address", "mentions"]

ALL_MEMBERS = ("alle", "all")
"""`@alle` addresses every member except the sender (L6)."""

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
    Names that are no member, and the sender itself, are ignored: a mention is a hint, not a
    command that can fail.
    """
    members = list(members)
    found: list[str] = []
    for match in _MENTION_RE.finditer(body):
        name = match.group(1)
        if name.casefold() in ALL_MEMBERS:
            found += [m for m in members if m != sender]
            continue
        agent = resolve(name)
        if agent is not None and agent != sender:
            found.append(agent)
    return list(dict.fromkeys(found))


def address(msg: Message, member: str, sender_of: Callable[[int], str | None]) -> Address | None:
    """How `msg` concerns `member`, or None (own messages never concern their sender).

    `sender_of` looks up the sender of the message `reply_to` points at.
    """
    if msg.sender == member:
        return None
    if member in msg.to:
        return Address.QUESTION if msg.kind is MessageKind.QUESTION else Address.MENTION
    target = sender_of(msg.reply_to) if msg.reply_to is not None else None
    if target == member:
        return Address.REPLY
    if msg.kind is MessageKind.QUESTION and not msg.to and target in (None, msg.sender):
        return Address.QUESTION_ALL
    return None
