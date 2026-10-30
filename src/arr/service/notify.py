"""macOS notifications of the service (docs/08 §4.2) through `osascript`, no new dependency.

The `Notifier` hangs on the `EventHub`: a new approval always, a finished run per §4.2. Agents
determine parts of a request, so the text goes to `osascript` as an argument (`on run argv`)
and is never built into the script. It names agent, run and the kind of request; the full
command never appears (lock screen), secrets are removed. Without `osascript`, or with
`ARR_NOTIFY=off` (tests), nothing happens. A failing notification never holds anything up.
A click on it opens no deep link, `osascript` cannot do that (W5-02).
"""

import asyncio
import contextlib
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable

from arr.permissions.policy import redact
from arr.permissions.types import ApprovalType
from arr.service.api_client import EventHub
from arr.service.api_models import ApprovalEvent, ApprovalItem, LiveEvent, RunStatusEvent
from arr.service.gui.components.notification import FINAL_WORD
from arr.service.gui.format import deadline as deadline_text
from arr.service.gui.texts import CATEGORY

__all__ = ["Notifier", "Sender", "message_for", "osascript_args", "send_notification"]

log = logging.getLogger("arr.service.notify")

Sender = Callable[[str, str], None]
"""`send(text, title)`; replaced in tests, so that they never show a real notification."""

TITLE = "arr"
SEND_TIMEOUT_S = 5.0
MAX_TEXT = 220
SCRIPT = (
    "on run argv",
    "display notification (item 1 of argv) with title (item 2 of argv)",
    "end run",
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def osascript_args(text: str, title: str = TITLE, *, script: tuple[str, ...] = SCRIPT) -> list[str]:
    """The call of `osascript`: fixed script, the texts as arguments behind `--`."""
    lines = [arg for line in script for arg in ("-e", line)]
    return ["osascript", *lines, "--", text, title]


def send_notification(text: str, title: str = TITLE) -> None:
    """Show a notification; does nothing without `osascript` or with `ARR_NOTIFY=off`."""
    if os.environ.get("ARR_NOTIFY") == "off" or shutil.which("osascript") is None:
        return
    subprocess.run(
        osascript_args(text, title), capture_output=True, timeout=SEND_TIMEOUT_S, check=False
    )


def _clean(text: str) -> str:
    return _CONTROL_RE.sub(" ", redact(text)).strip()[:MAX_TEXT]


def _approval_text(item: ApprovalItem, now: float) -> str:
    if item.type is ApprovalType.ESCALATION:
        return f"Eskalation in {item.run.name}: Stufe „{item.stage}“. Der Run pausiert."
    if item.type is ApprovalType.RUN_START:
        return f"Run wartet auf Freigabe: {item.run.name}"
    agent = item.agent.id if item.agent else "Ein Agent"
    kinds = ", ".join(CATEGORY[c] for c in item.categories) or "eine Berechtigung"
    deadline = f" · {deadline_text(item.deadline, now)}" if item.deadline else ""
    return f"{agent} fragt nach: {kinds} · {item.run.name}{deadline}"


def message_for(event: LiveEvent, now: float) -> str | None:
    """Text of the notification for an event, or None if it gets none (docs/08 §4.2)."""
    if isinstance(event, ApprovalEvent):
        if event.type == "approval.created":
            return _clean(_approval_text(event.item, now))
        return None
    if isinstance(event, RunStatusEvent):
        ended = event.previous is not None and event.previous.is_active
        word = FINAL_WORD.get(event.run.status)
        if ended and word:
            return _clean(f"{word}: {event.run.name}")
    return None


class Notifier:
    """Follows the global live stream and sends notifications, one after the other."""

    def __init__(self, hub: EventHub, send: Sender = send_notification) -> None:
        self.hub = hub
        self.send = send
        self._task: asyncio.Task[None] | None = None
        self._subscription = None

    def start(self) -> None:
        """Start following the hub (in the running event loop, idempotent)."""
        if self._task is None or self._task.done():
            self._subscription = self.hub.add(None, None)
            self._task = asyncio.create_task(self._pump(), name="arr-notifier")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if self._subscription is not None:
            self._subscription.close()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _pump(self) -> None:
        assert self._subscription is not None
        async for event in self._subscription:
            try:
                text = message_for(event, time.time())
                if text:
                    await asyncio.to_thread(self.send, text, TITLE)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a notification must never hold anything up
                log.warning("Mitteilung konnte nicht gesendet werden", exc_info=True)
