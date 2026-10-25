"""Approval inbox for permission requests, with the terminal prompt as decider of Welle 1.

A request in the grey zone (`ask` from the PolicyEngine) becomes an `ApprovalItem`; the
agent waits while all others keep working. A decider presents the item and answers it:
in Welle 1/2 `TerminalDecider` for `arr run`, from W3-03 the GUI inbox (it calls
`ApprovalInbox.resolve`), from W4-04 the gatekeeper. Without a decision until the deadline
the request counts as denied with a hint (docs/07-berechtigungen-und-freigaben.md §3 ③).

Items live in memory; every step is appended to `approvals.jsonl` (docs/02 §4).
See docs/07-berechtigungen-und-freigaben.md §3-§6.
"""

import asyncio
import contextlib
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Protocol

from arr.errors import ArrError
from arr.permissions.policy import (
    TIMEOUT_REASON,
    AlwaysRule,
    ApprovalLog,
    ApprovalLogEntry,
    Assessment,
    LogEvent,
    PolicyEngine,
    redact,
)
from arr.permissions.types import (
    ApprovalItem,
    ApprovalStatus,
    ApprovalType,
    DecidedBy,
    Decision,
    DecisionAction,
    DecisionScope,
    PermissionRequest,
    RequestCategory,
    RiskLevel,
)

__all__ = [
    "ApprovalDecider",
    "ApprovalInbox",
    "TerminalDecider",
    "format_item",
]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = timedelta(minutes=20)
"""Approval timeout without configuration (docs/07 §3 ③)."""

_ID_RE = re.compile(r"^apr_(\d+)$")


class ApprovalDecider(Protocol):
    """Whoever answers open items: terminal (W1-05), GUI inbox (W3-03), gatekeeper (W4-04).

    `decide` presents the item and returns a decision, or `None` if the decision arrives
    another way (`ApprovalInbox.resolve`). It is cancelled when the item expires.
    """

    async def decide(self, item: ApprovalItem) -> Decision | None:
        """Present `item` and return the decision, or `None`."""
        ...


class ApprovalInbox:
    """Queue of open permission requests of one run (docs/07 §3 ③, §6).

    `handle` is the single entry point for the adapters: it asks the PolicyEngine and, on
    `ask`, opens an item and waits for a decision or the timeout. `resolve` is where the
    terminal, the GUI or the gatekeeper put their decision. Final decisions with scope
    agent/run/always are passed to `PolicyEngine.remember`; new „Immer“ rules go to
    `on_rule` so the caller can store them in `arr.yaml`.
    """

    def __init__(
        self,
        engine: PolicyEngine,
        *,
        decider: ApprovalDecider | None = None,
        timeout: timedelta | float = DEFAULT_TIMEOUT,
        log: ApprovalLog | None = None,
        on_rule: Callable[[AlwaysRule], None] | None = None,
    ) -> None:
        self.engine = engine
        self.decider = decider
        self.timeout = timeout.total_seconds() if isinstance(timeout, timedelta) else timeout
        self.log = log if log is not None else engine.log
        self.on_rule = on_rule
        self._items: dict[str, ApprovalItem] = {}
        self._waiters: dict[str, asyncio.Future[Decision]] = {}
        self._counter = self._last_id()

    # -- queries ------------------------------------------------------------------------

    def get(self, item_id: str) -> ApprovalItem:
        """Return an item by ID; `ArrError` if it does not exist."""
        try:
            return self._items[item_id]
        except KeyError:
            raise ArrError(f"Die Freigabe „{item_id}“ gibt es nicht.") from None

    def pending(self) -> list[ApprovalItem]:
        """Open items, oldest first."""
        return [i for i in self._items.values() if i.status is ApprovalStatus.OPEN]

    def items(self) -> list[ApprovalItem]:
        """All items of this inbox, oldest first."""
        return list(self._items.values())

    # -- flow -----------------------------------------------------------------------------

    async def handle(self, request: PermissionRequest) -> Decision:
        """Decide a request: PolicyEngine first, the inbox only for `ask` (docs/07 §3)."""
        assessment = self.engine.assess(request)
        if assessment.decision.action is not DecisionAction.ASK:
            return assessment.decision
        return await self.ask(assessment)

    async def ask(self, assessment: Assessment) -> Decision:
        """Open an item for an `ask` and wait for its decision or the timeout.

        Never raises because of the decider; a cancelled wait marks the item obsolete.
        """
        item = self._open(assessment)
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[Decision] = loop.create_future()
        self._waiters[item.id] = waiter
        task = asyncio.create_task(self._run_decider(item)) if self.decider else None
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), self.timeout)
        except TimeoutError:
            return self._expire(item.id)
        except asyncio.CancelledError:
            if self._items[item.id].status is ApprovalStatus.OPEN:
                self.cancel(item.id, "Die wartende Anfrage wurde abgebrochen.")
            raise
        finally:
            self._waiters.pop(item.id, None)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def resolve(self, item_id: str, decision: Decision) -> ApprovalItem:
        """Record the decision for an open item and wake the waiting agent.

        Only `allow` and `deny` fit a permission request; `always` only by the user (both
        enforced by the types). Raises `ArrError` if the item is not open any more.
        """
        item = self.get(item_id)
        if item.status is not ApprovalStatus.OPEN:
            raise ArrError(
                f"Die Freigabe „{item_id}“ ist nicht mehr offen ({item.status}). "
                "Eine Entscheidung ist nicht mehr nötig."
            )
        if decision.action not in (DecisionAction.ALLOW, DecisionAction.DENY):
            raise ArrError("Eine Berechtigungsanfrage lässt sich nur erlauben oder ablehnen.")
        if decision.risk is None:
            decision = decision.model_copy(update={"risk": item.risk})
        item = self._update(item, ApprovalStatus.DECIDED, decision)
        self._write(LogEvent.DECIDED, item)
        assert item.request is not None
        rule = self.engine.remember(item.id, item.request, decision)
        if rule is not None and self.on_rule is not None:
            self.on_rule(rule)
        self._wake(item.id, decision)
        return item

    def cancel(self, item_id: str, note: str = "Der Run wurde gestoppt.") -> ApprovalItem:
        """Mark an open item obsolete and release the agent with a `deny` (stop, docs/07 §7)."""
        item = self.get(item_id)
        if item.status is not ApprovalStatus.OPEN:
            return item
        decision = Decision(
            action=DecisionAction.DENY,
            decided_by=DecidedBy.FIXED_RULE,
            reason=f"{note} Die Anfrage ist damit hinfällig.",
            risk=item.risk,
        )
        item = self._update(item, ApprovalStatus.OBSOLETE, decision)
        self._write(LogEvent.OBSOLETE, item, note=note)
        self._wake(item.id, decision)
        return item

    def cancel_all(self, note: str = "Der Run wurde gestoppt.") -> None:
        """Cancel every open item, e.g. before `interrupt()` on a stop (S2 §15)."""
        for item in self.pending():
            self.cancel(item.id, note)

    def revoke(self, item_id: str) -> ApprovalItem:
        """Withdraw a decision with scope agent/run (docs/07 §4); later requests ask again."""
        item = self.get(item_id)
        if not item.revocable:
            raise ArrError(
                f"Die Entscheidung „{item_id}“ lässt sich nicht zurückziehen. Zurückziehen "
                "geht nur bei Entscheidungen des Aufsichts-Agenten und bei deinen eigenen mit "
                "Reichweite „Agent“ oder „Run“."
            )
        self.engine.revoke(item_id)  # logs the revocation
        return self._update(item, ApprovalStatus.REVOKED, item.decision)

    # -- internals ----------------------------------------------------------------------

    def _last_id(self) -> int:
        if self.log is None:
            return 0
        numbers = [0]
        for entry in self.log.entries():
            match = _ID_RE.match(str(entry.get("item_id") or ""))
            if match:
                numbers.append(int(match.group(1)))
        return max(numbers)

    def _open(self, assessment: Assessment) -> ApprovalItem:
        self._counter += 1
        now = time.time()
        request = assessment.request
        item = ApprovalItem(
            id=f"apr_{self._counter:04d}",
            type=ApprovalType.PERMISSION,
            created_at=now,
            run_id=request.run_id,
            agent_id=request.agent_id,
            request=request,
            risk=assessment.decision.risk,
            deadline=now + self.timeout,
            summary=assessment.decision.reason,
        )
        self._items[item.id] = item
        self._write(LogEvent.OPEN, item, decision=assessment.decision)
        return item

    def _expire(self, item_id: str) -> Decision:
        item = self._items[item_id]
        if item.status is not ApprovalStatus.OPEN and item.decision is not None:
            return item.decision
        decision = Decision(
            action=DecisionAction.DENY,
            decided_by=DecidedBy.FIXED_RULE,
            reason=TIMEOUT_REASON,
            risk=item.risk,
        )
        item = self._update(item, ApprovalStatus.EXPIRED, decision)
        self._write(LogEvent.EXPIRED, item)
        return decision

    async def _run_decider(self, item: ApprovalItem) -> None:
        assert self.decider is not None
        try:
            decision = await self.decider.decide(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A broken decider must never end the agent's client (S2 §15); the item stays
            # open for other deciders until the deadline.
            logger.exception("Entscheider für %s fehlgeschlagen", item.id)
            return
        if decision is not None and self._items[item.id].status is ApprovalStatus.OPEN:
            try:
                self.resolve(item.id, decision)
            except (ArrError, ValueError):
                logger.exception("Entscheidung für %s ungültig", item.id)

    def _update(
        self, item: ApprovalItem, status: ApprovalStatus, decision: Decision | None
    ) -> ApprovalItem:
        data = item.model_dump(by_alias=True)
        data.update(status=status, decision=decision)
        updated = ApprovalItem.model_validate(data)
        self._items[item.id] = updated
        return updated

    def _wake(self, item_id: str, decision: Decision) -> None:
        waiter = self._waiters.get(item_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(decision)

    def _write(
        self,
        event: LogEvent,
        item: ApprovalItem,
        *,
        decision: Decision | None = None,
        note: str | None = None,
    ) -> None:
        if self.log is None:
            return
        self.log.write(
            ApprovalLogEntry(
                event=event,
                item_id=item.id,
                request=item.request,
                decision=decision or item.decision,
                note=note,
            )
        )


# --------------------------------------------------------------------------------------------
# Terminal prompt (arr run, Welle 2)
# --------------------------------------------------------------------------------------------

_CATEGORY_WORDS = {
    RequestCategory.NETWORK: "Netzwerk",
    RequestCategory.INSTALL: "Installation",
    RequestCategory.READ_OUTSIDE: "Lesen außerhalb der Erlaubnisliste",
    RequestCategory.WRITE_OUTSIDE: "Schreiben außerhalb des Runs",
    RequestCategory.PRIVILEGED: "Systemrechte",
    RequestCategory.OTHER: "Sonstiges",
}
_RISK_WORDS = {RiskLevel.LOW: "niedrig", RiskLevel.MEDIUM: "mittel", RiskLevel.HIGH: "hoch"}
_SCOPE_WORDS = {
    DecisionScope.ONCE: "einmalig",
    DecisionScope.AGENT: "für diesen Agenten im Run",
    DecisionScope.RUN: "für den ganzen Run",
    DecisionScope.ALWAYS: "immer",
}
_ANSWERS = {
    "einmal": DecisionScope.ONCE,
    "agent": DecisionScope.AGENT,
    "run": DecisionScope.RUN,
    "immer": DecisionScope.ALWAYS,
}
_DENY_WORDS = frozenset({"nein", "ablehnen"})
_DEFAULT_DENY = "Abgelehnt. Arbeite ohne weiter oder frage im Chat nach."
_CHOICES = (
    "Antworte mit einem ganzen Wort und Enter:\n"
    "  einmal  – nur diese Anfrage erlauben\n"
    "  agent   – für diesen Agenten im Run erlauben\n"
    "  run     – für den ganzen Run erlauben\n"
    "  immer   – immer erlauben (wird eine feste Regel in arr.yaml)\n"
    "  nein    – ablehnen"
)
_RULE = "─" * 72


def format_item(item: ApprovalItem) -> str:
    """German text of a permission request for the terminal; secrets are removed."""
    request = item.request
    lines = [_RULE]
    deadline = (
        f" · Frist bis {time.strftime('%H:%M:%S', time.localtime(item.deadline))}"
        if item.deadline
        else ""
    )
    lines.append(f"Freigabe-Anfrage {item.id}{deadline}")
    lines.append(f"Run:            {item.run_id}")
    if request is not None:
        who = request.agent_id
        extras = [x for x in (f"Rolle {request.role}" if request.role else None,) if x]
        if request.provider:
            extras.append(str(request.provider))
        if extras:
            who += f" ({', '.join(extras)})"
        lines.append(f"Agent:          {who}")
        lines.append(f"Werkzeug:       {request.tool}")
        if request.command:
            lines.append(f"Befehl:         {request.command}")
        if request.paths:
            lines.append(f"Pfade:          {', '.join(request.paths)}")
        if request.urls:
            lines.append(f"URLs:           {', '.join(request.urls)}")
        if request.hosts:
            lines.append(f"Hosts:          {', '.join(request.hosts)}")
        if request.work_dir:
            lines.append(f"Arbeitsordner:  {request.work_dir}")
        if request.categories:
            words = ", ".join(_CATEGORY_WORDS[c] for c in request.categories)
            lines.append(f"Kategorien:     {words}")
    if item.risk is not None:
        lines.append(f"Risiko:         {_RISK_WORDS[item.risk.level]} – {item.risk.text}")
    if request is not None:
        lines.append(f"Begründung des Agenten: {request.reason or '(keine angegeben)'}")
    if item.summary:
        lines.append(f"Einschätzung von arr:   {item.summary}")
    lines.append("")
    lines.append(_CHOICES)
    return redact("\n".join(lines))


class TerminalDecider:
    """Asks in the terminal whether to allow a request (docs/07 §6: Phase 1 without GUI).

    Answers are whole German words, never a single key or an empty line (docs/07 §6); „immer“
    must be confirmed. Prompts are serialized, so parallel requests are asked one after
    the other. `read_line` and `write` can be replaced for tests. A line typed for an
    expired request is discarded, never applied to the next one.
    """

    def __init__(
        self,
        *,
        read_line: Callable[[str], str] = input,
        write: Callable[[str], None] | None = None,
    ) -> None:
        self._read_line = read_line
        self._write = write or (lambda text: print(text, flush=True))
        self._lock = asyncio.Lock()
        self._pending: asyncio.Future[str | None] | None = None

    async def decide(self, item: ApprovalItem) -> Decision | None:
        """Show the item and read the answer; `deny` if the terminal is closed."""
        async with self._lock:
            if item.deadline is not None and item.deadline <= time.time():
                return None
            try:
                self._write(format_item(item))
                return await self._ask(item)
            except asyncio.CancelledError:
                self._write(
                    f"Die Anfrage {item.id} ist abgelaufen oder hinfällig und wurde abgelehnt. "
                    "Eine Eingabe dazu wird verworfen."
                )
                raise

    async def _ask(self, item: ApprovalItem) -> Decision:
        while True:
            line = await self._read("Deine Entscheidung: ")
            if line is None:
                return _closed_terminal(item)
            answer = line.strip().casefold()
            if answer in _DENY_WORDS:
                text = await self._read("Begründung für den Agenten (Enter für Standard): ")
                reason = (text or "").strip() or _DEFAULT_DENY
                return Decision(
                    action=DecisionAction.DENY,
                    decided_by=DecidedBy.USER,
                    reason=f"Im Terminal abgelehnt: {reason}",
                    risk=item.risk,
                )
            scope = _ANSWERS.get(answer)
            if scope is DecisionScope.ALWAYS:
                confirm = await self._read(
                    "„Immer“ wird eine feste Regel für alle künftigen Runs. Zum Bestätigen "
                    "„ja immer“ eingeben: "
                )
                if (confirm or "").strip().casefold() != "ja immer":
                    self._write("Nicht bestätigt. Bitte erneut entscheiden.")
                    continue
            if scope is None:
                self._write(f"„{line.strip()}“ verstehe ich nicht.\n{_CHOICES}")
                continue
            return Decision(
                action=DecisionAction.ALLOW,
                scope=scope,
                decided_by=DecidedBy.USER,
                reason=f"Im Terminal erlaubt ({_SCOPE_WORDS[scope]}).",
                risk=item.risk,
            )

    async def _read(self, prompt: str) -> str | None:
        stale = self._pending
        if stale is not None and not stale.done():
            # The thread of an expired prompt still waits for a line; take and drop it.
            await asyncio.shield(stale)
            self._write(
                "Diese Eingabe gehörte zu einer abgelaufenen Anfrage. Bitte erneut eingeben."
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str | None] = loop.create_future()
        self._pending = future

        def worker() -> None:
            try:
                line: str | None = self._read_line(prompt)
            except (EOFError, KeyboardInterrupt, OSError):
                line = None
            # The loop may be gone when a late line arrives (run ended meanwhile).
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_set_once, future, line)

        threading.Thread(target=worker, name="arr-approval-input", daemon=True).start()
        line = await asyncio.shield(future)
        self._pending = None
        return line


def _set_once(future: "asyncio.Future[str | None]", value: str | None) -> None:
    if not future.done():
        future.set_result(value)


def _closed_terminal(item: ApprovalItem) -> Decision:
    return Decision(
        action=DecisionAction.DENY,
        decided_by=DecidedBy.FIXED_RULE,
        reason="Im Terminal ist keine Eingabe möglich. " + TIMEOUT_REASON,
        risk=item.risk,
    )
