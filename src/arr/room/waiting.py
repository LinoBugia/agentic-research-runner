"""`wait_for_messages`: an agent waits inside its turn and stays reachable (docs/09 §4.3).

The tool blocks until a message concerns the agent (`arr.room.address.address`), the file
`until_file` exists in its working folder, or `seconds` have passed. One call lasts at most
`WAIT_MAX_S` of its backend: the Claude CLI held a blocking tool for 310 s, Codex aborts after
300 s and its `exec` hands back after 30 s, so Codex gets 25 s (spike W3-07, F3). The room
server reads the limit from `WAIT_MAX_ENV`, set by the backend. The same loop serves the
FakeBackend.

While it waits, the row in `agent_wait` tells the orchestrator and the GUI why and until when.

Limits across calls (table `wait_guard`, docs/09 §4.3):

- L7: in one turn the calls wait at most `STREAK_MAX_S` (5 min) in all without a new message
  to the agent. The call that reaches it returns with `WaitEnd.NOBODY` („nobody answers“);
  the next call asks the orchestrator to end the turn (`WaitEnd.TURN_END`). A new message
  to the agent starts the 5 min again.
- L12: while another agent waits for a free place of the agent's profile (`yield_to`, set
  by the orchestrator), a call returns at once with `WaitEnd.CAPACITY` and points to
  `wait_until`, which ends the turn and frees the place; a second call ends the turn.

A call that ends the turn blocks until the backend interrupts it (`abort`) or `max_s`.
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
    "PROVIDER_ENV",
    "STREAK_MAX_S",
    "WAIT_MAX_ENV",
    "WAIT_MAX_S",
    "WaitEnd",
    "WaitOutcome",
    "wait_for_messages",
    "wait_max_s",
]

WAIT_MAX_ENV = "ARR_WAIT_MAX_S"
PROVIDER_ENV = "ARR_PROVIDER"
"""Provider of the agent, set by the backend for the room server (tool descriptions)."""
WAIT_MAX_S = {Provider.CLAUDE: 45.0, Provider.CODEX: 25.0}
DEFAULT_WAIT_MAX_S = 45.0
POLL_S = 0.5
"""How often the room is checked while an agent waits."""
STREAK_MAX_S = 300.0
"""L7: waiting in one turn without a new message to the agent, over all calls."""


class WaitEnd(StrEnum):
    """Why `wait_for_messages` returned."""

    MESSAGE = "message"
    FILE = "file"
    TIMEOUT = "timeout"
    NOBODY = "nobody"  # L7: 5 min without a new message to the agent, over all calls
    CAPACITY = "capacity"  # L12: another agent waits for a free place of the profile
    TURN_END = "turn_end"  # called again after `nobody` or `capacity`: arr ends the turn


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
    streak_max_s: float | None = None,
) -> WaitOutcome:
    """Wait up to `min(seconds, max_s)` for a message to me, `until_file` or the deadline.

    A message to me that is still unread ends the wait at once, also one that came before
    the call. On a message the agent's unread messages are returned and count as read, like
    `read_messages()`. A bad `until_file` raises `RoomError` before any waiting. The limits
    across calls (L7, L12) are in the module docstring; `streak_max_s` defaults to
    `STREAK_MAX_S`.
    """
    if not isinstance(seconds, int | float) or not math.isfinite(seconds) or seconds <= 0:
        raise RoomError(f"„seconds“ muss eine Zahl größer als 0 sein, höchstens {max_s:g}.")
    streak = STREAK_MAX_S if streak_max_s is None else streak_max_s
    guard = await asyncio.to_thread(room.wait_guard)
    newest = await asyncio.to_thread(room.newest_for_me)
    waited_before = guard.waited
    if guard.seen is not None and newest > guard.seen:
        waited_before, guard.last_end = 0.0, None  # a new message to me: the 5 min restart
    ask = float(seconds)

    async def note(end: WaitEnd, waited: float, **fields: object) -> None:
        total = 0.0 if end is WaitEnd.MESSAGE else waited_before + waited
        await asyncio.to_thread(
            room.update_wait_guard, waited=total, seen=newest, last_end=end.value, **fields
        )

    repeat = guard.last_end in (WaitEnd.NOBODY, WaitEnd.CAPACITY)
    if guard.yield_to is not None or waited_before >= streak:
        if repeat:
            reason = WaitEnd.CAPACITY if guard.yield_to is not None else WaitEnd.NOBODY
            await note(WaitEnd.TURN_END, 0.0, end_turn=reason.value)
            waited = await _until_aborted(abort, max_s, poll_s)
            return WaitOutcome(WaitEnd.TURN_END, waited, 0.0, max_s, ask)
        end = WaitEnd.CAPACITY if guard.yield_to is not None else WaitEnd.NOBODY
        await note(end, 0.0)
        return WaitOutcome(end, 0.0, 0.0, max_s, ask)
    limit = min(ask, max_s, streak - waited_before)
    state = await asyncio.to_thread(room.begin_wait, WaitKind.MESSAGES, limit, until_file)
    started = time.monotonic()
    outcome: WaitOutcome | None = None
    try:
        while True:
            waited = time.monotonic() - started
            if await asyncio.to_thread(room.messages_for_me):
                messages = await asyncio.to_thread(room.read_messages)
                remaining = max(0.0, limit - waited)
                outcome = WaitOutcome(
                    WaitEnd.MESSAGE, waited, remaining, limit, ask, state.file, messages
                )
            elif state.file is not None and await asyncio.to_thread(
                room.until_file_exists, state.file
            ):
                outcome = WaitOutcome(
                    WaitEnd.FILE, waited, max(0.0, limit - waited), limit, ask, state.file
                )
            elif (await asyncio.to_thread(room.wait_guard)).yield_to is not None:
                outcome = WaitOutcome(WaitEnd.CAPACITY, waited, 0.0, limit, ask, state.file)
            elif waited >= limit or abort():
                end = WaitEnd.NOBODY if waited_before + waited >= streak else WaitEnd.TIMEOUT
                outcome = WaitOutcome(end, waited, 0.0, limit, ask, state.file)
            if outcome is not None:
                return outcome
            await asyncio.sleep(min(poll_s, limit - waited))
    finally:
        await asyncio.to_thread(room.end_wait, WaitKind.MESSAGES)
        if outcome is not None:
            await note(outcome.ended_by, outcome.waited_s)


async def _until_aborted(abort: Callable[[], bool], max_s: float, poll_s: float) -> float:
    """Block until the backend interrupts the turn (`abort`) or `max_s`; seconds waited."""
    started = time.monotonic()
    while True:
        waited = time.monotonic() - started
        if abort() or waited >= max_s:
            return waited
        await asyncio.sleep(min(poll_s, max_s - waited))
