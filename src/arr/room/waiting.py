"""`wait_for_messages`: an agent waits inside its turn and stays reachable (docs/09 §4.3).

The tool blocks until a message concerns the agent (`arr.room.address.address`), the file
`until_file` exists in its working folder, or `seconds` have passed. One call lasts at most
`WAIT_MAX_S` of its backend: the Claude CLI held a blocking tool for 310 s, Codex aborts after
300 s and its `exec` hands back after 30 s, so Codex gets 25 s (spike W3-07, F3). The room
server reads the limit from `WAIT_MAX_ENV`, set by the backend. The same loop serves the
FakeBackend.

While it waits, the row in `agent_wait` tells the orchestrator and the GUI why and until when.
"""

import asyncio
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from arr.models import Provider
from arr.room.store import AgentRoom
from arr.room.types import Message, RoomError, WaitKind

__all__ = [
    "DEFAULT_WAIT_MAX_S",
    "POLL_S",
    "WAIT_MAX_ENV",
    "WAIT_MAX_S",
    "WaitEnd",
    "WaitOutcome",
    "wait_for_messages",
    "wait_max_s",
]

WAIT_MAX_ENV = "ARR_WAIT_MAX_S"
WAIT_MAX_S = {Provider.CLAUDE: 45.0, Provider.CODEX: 25.0}
DEFAULT_WAIT_MAX_S = 45.0
POLL_S = 0.5
"""How often the room is checked while an agent waits."""


class WaitEnd(StrEnum):
    """Why `wait_for_messages` returned."""

    MESSAGE = "message"
    FILE = "file"
    TIMEOUT = "timeout"


@dataclass(slots=True)
class WaitOutcome:
    """Result of one `wait_for_messages` call; `messages` are the unread ones (now read)."""

    ended_by: WaitEnd
    waited_s: float
    remaining_s: float
    limit_s: float
    asked_s: float
    file: str | None = None
    messages: list[Message] = field(default_factory=list)


def wait_max_s(environ: Mapping[str, str] | None = None) -> float:
    """Limit of one call from `WAIT_MAX_ENV`, `DEFAULT_WAIT_MAX_S` if unset or invalid."""
    value = (os.environ if environ is None else environ).get(WAIT_MAX_ENV, "")
    try:
        limit = float(value)
    except ValueError:
        return DEFAULT_WAIT_MAX_S
    return limit if math.isfinite(limit) and limit > 0 else DEFAULT_WAIT_MAX_S


async def wait_for_messages(
    room: AgentRoom,
    seconds: float,
    until_file: str | None = None,
    *,
    max_s: float,
    poll_s: float = POLL_S,
    abort: Callable[[], bool] = lambda: False,
) -> WaitOutcome:
    """Wait up to `min(seconds, max_s)` for a message to me, `until_file` or the deadline.

    A message to me that is still unread ends the wait at once, also one that came before
    the call. On a message the agent's unread messages are returned and count as read, like
    `read_messages()`. A bad `until_file` raises `RoomError` before any waiting.
    """
    if not isinstance(seconds, int | float) or not math.isfinite(seconds) or seconds <= 0:
        raise RoomError(f"„seconds“ muss eine Zahl größer als 0 sein, höchstens {max_s:g}.")
    limit = min(float(seconds), max_s)
    state = await asyncio.to_thread(room.begin_wait, WaitKind.MESSAGES, limit, until_file)
    started = time.monotonic()
    try:
        while True:
            waited = time.monotonic() - started
            if await asyncio.to_thread(room.messages_for_me):
                messages = await asyncio.to_thread(room.read_messages)
                remaining = max(0.0, limit - waited)
                return WaitOutcome(
                    WaitEnd.MESSAGE, waited, remaining, limit, seconds, state.file, messages
                )
            if state.file is not None and await asyncio.to_thread(
                room.until_file_exists, state.file
            ):
                return WaitOutcome(
                    WaitEnd.FILE, waited, max(0.0, limit - waited), limit, seconds, state.file
                )
            if waited >= limit or abort():
                return WaitOutcome(WaitEnd.TIMEOUT, waited, 0.0, limit, seconds, state.file)
            await asyncio.sleep(min(poll_s, limit - waited))
    finally:
        await asyncio.to_thread(room.end_wait, WaitKind.MESSAGES)
