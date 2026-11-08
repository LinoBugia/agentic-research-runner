"""macOS notifications of the service (docs/08 §4.2) through `osascript`, no new dependency.

The `Notifier` hangs on the `EventHub`: a new approval always, a finished run per §4.2 unless
its switch in the app settings is off (read at every event, so a change counts at once; a
missing switch means on). The other switches of §5.11 have no event here yet. Agents
determine parts of a request, so the text goes to `osascript` as an argument (`on run argv`)
and is never built into the script. It names agent, run and the kind of request; the full
command never appears (lock screen), secrets are removed. A question of an agent to the user
(docs/09 L16) names agent and run and the start of the question, cut at `MAX_TEXT`. Without
`osascript`, or with `ARR_NOTIFY=off` (tests), nothing happens. A failing notification never
holds anything up. A click on it opens no deep link, `osascript` cannot do that (W5-02).
"""

import asyncio
import contextlib
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping

from arr.permissions.policy import redact
from arr.permissions.types import ApprovalType
from arr.service.api_client import EventHub
from arr.service.api_models import (
    ApprovalEvent,
    ApprovalItem,
    LiveEvent,
    NotificationEvent,
    RunStatusEvent,
)
from arr.service.gui.components.approval_card import risk_text
from arr.service.gui.components.notification import FINAL_WORD
from arr.service.gui.format import deadline as deadline_text
from arr.service.gui.texts import CATEGORY
from arr.service.state import read_app_settings

__all__ = [
    "Notifier",
    "Sender",
    "message_for",
    "osascript_args",
    "send_notification",
    "switched_on",
]

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
    if item.type is ApprovalType.QUESTION:
        agent = item.agent.id if item.agent else "Ein Agent"
        # the run before the question: `MAX_TEXT` cuts the end, never who asks where
        return f"{agent} fragt dich ({item.run.name}): {item.question_text or ''}"
    if item.type is ApprovalType.ESCALATION:
        return f"Eskalation in {item.run.name}: Stufe „{item.stage}“. Der Run pausiert."
    if item.type is ApprovalType.RUN_START:
        return f"Run wartet auf Freigabe: {item.run.name}"
    agent = item.agent.id if item.agent else "Ein Agent"
    kinds = ", ".join(CATEGORY[c] for c in item.categories) or "eine Berechtigung"
    deadline = f" · {deadline_text(item.deadline, now)}" if item.deadline else ""
    risk = f" · Risiko {risk_text(item)}" if item.risk is not None else ""  # W3-08 U8
    # the deadline before risk and run: `MAX_TEXT` cuts the end, never the deadline (W3-08.1)
    return f"{agent} fragt nach: {kinds}{deadline}{risk} · {item.run.name}"


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


def switched_on(event: LiveEvent) -> bool:
    """Whether the switch of §5.11 lets this event through; approvals always pass."""
    if not isinstance(event, RunStatusEvent):
        return True
    saved = read_app_settings().get("notifications")
    switches = saved if isinstance(saved, Mapping) else {}
    return switches.get(NotificationEvent.RUN_FINISHED.value) is not False


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
                if text and switched_on(event):
                    await asyncio.to_thread(self.send, text, TITLE)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a notification must never hold anything up
                log.warning("Mitteilung konnte nicht gesendet werden", exc_info=True)
