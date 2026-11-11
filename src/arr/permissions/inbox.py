"""Approval inbox for permission requests, with the terminal prompt as decider of Welle 1.

A request in the grey zone (`ask` from the PolicyEngine) becomes an `ApprovalItem`; the
agent waits while all others keep working. A decider presents the item and answers it:
in Welle 1/2 `TerminalDecider` for `arr run`, from W3-03 the GUI inbox (it calls
`ApprovalInbox.resolve`), from W4-04 the gatekeeper. Without a decision until the deadline
the request counts as denied with a hint (docs/07-berechtigungen-und-freigaben.md §3 ③).

Equal open requests of an agent become one item with a `count`, one decision answers all of
them. Equal means the card of the first one says everything about the others (`_merge_key`):
a click grants what the user sees, nothing more. An allowed installation also covers the
package hosts it needs for a short time, so `pip install` asks once (W2-01 §7, review W2-01.1
N1).

A question of an agent to the user (`@user`, docs/09 L16) is an item too: `open_question`
opens it, nobody waits on it. It is closed by the user's answer, in the inbox (`resolve` with
`answer`) or in the channel (`answered`), or when the run ends (`close_question`, `cancel`).

With a supervising agent (`decider: gatekeeper` or `gatekeeper_then_user`, W4-04) a new
permission item first passes the `screener` (`arr.permissions.gatekeeper.GatekeeperStage`):
the hard pre-check of the mandate and, if it passes, the supervising agent. While it screens,
the item is not in the log, no card shows and nothing notifies the user. Its outcome is either
a decision (one line `decided` with `decided_by` `gatekeeper`, `mandate_rule` or `fixed_rule`)
or the item opens for the user with `escalated_by` and the reason in its `open` line. Only
such items reach the decider of the user. A withdrawn decision of the supervising agent makes
equal requests of the run skip it (docs/07 §4 Nr. 3).

Items live in memory; every step is appended to `approvals.jsonl` (docs/02 §4).
See docs/07-berechtigungen-und-freigaben.md §3-§6.
"""

import asyncio
import contextlib
import hashlib
import logging
import re
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any, Protocol

from arr.errors import ArrError
from arr.permissions.gatekeeper import ScreenContext
from arr.permissions.policy import (
    NETWORK_TOOLS,
    TIMEOUT_REASON,
    AlwaysRule,
    ApprovalLog,
    ApprovalLogEntry,
    Assessment,
    ItemKey,
    LogEvent,
    PolicyEngine,
    is_codex_escalation,
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
    EscalatedBy,
    EscalationOption,
    PermissionRequest,
    RequestCategory,
    Risk,
    RiskLevel,
    UserQuestion,
)

__all__ = [
    "ApprovalDecider",
    "ApprovalInbox",
    "DeciderUnavailable",
    "Screener",
    "TerminalDecider",
    "format_escalation",
    "format_item",
    "install_hosts",
    "item_id_prefix",
]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = timedelta(minutes=20)
"""Approval timeout without configuration (docs/07 §3 ③)."""


INSTALL_PASS_S = 300.0
"""How long an allowed installation covers its package hosts for the same agent."""

PACKAGE_HOSTS = frozenset({"pypi.org", "files.pythonhosted.org"})
"""Hosts an allowed `pip`/`uv` installation may reach without a second question."""

_PYTHON_INSTALLER_RE = re.compile(r"(?:^|[\s/])(?:pip[0-9.]*|uv)\s")


def install_hosts(request: PermissionRequest) -> frozenset[str]:
    """Package hosts that come with allowing this request (none for anything but pip/uv)."""
    command = request.command or ""
    if RequestCategory.INSTALL in request.categories and _PYTHON_INSTALLER_RE.search(command):
        return PACKAGE_HOSTS
    return frozenset()


def item_id_prefix(run_id: str) -> str:
    """Run part of an item ID: `apr_<16 hex of sha1(run_id)>`; IDs are `<prefix>_<NNNN>`.

    The counter runs per run and continues after a restart from `approvals.jsonl`. 64 bits of
    the hash keep IDs of different runs apart (6 hex collided at ≈ 3 % per 1000 runs, W1-05.1
    N5). Old IDs with 6 hex stay valid: readers take IDs as they stand in `approvals.jsonl`.
    """
    return "apr_" + hashlib.sha1(run_id.encode("utf-8")).hexdigest()[:16]


class DeciderUnavailable(ArrError):
    """A decider cannot ask (no real terminal, input closed); the item becomes obsolete."""


class ApprovalDecider(Protocol):
    """Whoever answers open items: terminal (W1-05), GUI inbox (W3-03), gatekeeper (W4-04).

    `decide` presents the item and returns a decision, or `None` if the decision arrives
    another way (`ApprovalInbox.resolve`). It is cancelled when the item expires and may
    raise `DeciderUnavailable` if it cannot ask at all.
    """

    async def decide(self, item: ApprovalItem) -> Decision | None:
        """Present `item` and return the decision, or `None`."""
        ...


class Screener(Protocol):
    """Decides a new permission item before it reaches the user (W4-04, the gatekeeper).

    `screen` returns a final decision or an `EscalatedBy` (the item goes to the user with this
    note). `failed` is the outcome when `screen` itself broke; both must fail closed.
    """

    async def screen(self, item: ApprovalItem, context: ScreenContext) -> Decision | EscalatedBy:
        """Decide `item` or pass it on to the user."""
        ...

    def failed(self, item: ApprovalItem) -> Decision | EscalatedBy:
        """The outcome when screening raised."""
        ...

    def revoked(self, item: ApprovalItem) -> Decision | EscalatedBy:
        """The outcome for a request equal to a withdrawn decision of the screener."""
        ...


class ApprovalInbox:
    """Queue of open permission requests and escalations of one run (docs/07 §3 ③, §6).

    `handle` is the single entry point for the adapters: it asks the PolicyEngine and, on
    `ask`, opens an item and waits for a decision or the timeout. `ask_escalation` is the
    entry point of `RunContext.escalate` (W2-03). `resolve` is where the
    terminal, the GUI or the gatekeeper put their decision; it may be called from another
    thread (GUI). `escalate` records who passed an item on to the user. Final decisions with
    scope agent/run/always are passed to `PolicyEngine.remember`; new „Immer“ rules go to
    `on_rule` so the caller can store them in `arr.yaml`, hints for the user to `on_note` and
    to the decider's `notify` (if it has one).

    Items, grants and new rules live in memory: after a restart open items are gone (the
    waiting agents too) and grants with scope agent/run no longer apply, so those requests
    ask again (fail-safe).
    """

    def __init__(
        self,
        engine: PolicyEngine,
        *,
        decider: ApprovalDecider | None = None,
        timeout: timedelta | float = DEFAULT_TIMEOUT,
        log: ApprovalLog | None = None,
        on_rule: Callable[[AlwaysRule], None] | None = None,
        on_note: Callable[[str], None] | None = None,
        screener: Screener | None = None,
    ) -> None:
        self.engine = engine
        self.decider = decider
        self.screener = screener
        self.timeout = timeout.total_seconds() if isinstance(timeout, timedelta) else timeout
        self.log = log if log is not None else engine.log
        self.on_rule = on_rule
        self.on_note = on_note
        self._lock = threading.RLock()
        self._items: dict[str, ApprovalItem] = {}
        self._waiters: dict[str, asyncio.Future[Decision | None]] = {}
        self._waiting: dict[str, int] = {}
        self._merge_keys: dict[str, tuple[Any, ...]] = {}
        self._decider_tasks: dict[str, asyncio.Task[None]] = {}
        self._install_passes: dict[str, tuple[float, frozenset[str], str, DecidedBy]] = {}
        self._counters: dict[str, int] = {}
        self._screening: set[str] = set()
        """Items the screener decides right now: not in the log, never joined."""
        self._item_keys: dict[str, frozenset[ItemKey]] = {}
        self._revoked_items: set[ItemKey] = set()
        """Grey items of withdrawn decisions of the supervising agent (docs/07 §4 Nr. 3)."""

    # -- queries ------------------------------------------------------------------------

    def get(self, item_id: str) -> ApprovalItem:
        """Return an item by ID; `ArrError` if it does not exist."""
        with self._lock:
            try:
                return self._items[item_id]
            except KeyError:
                raise ArrError(f"Die Freigabe „{item_id}“ gibt es nicht.") from None

    def pending(self) -> list[ApprovalItem]:
        """Open items, oldest first."""
        with self._lock:
            return [i for i in self._items.values() if i.status is ApprovalStatus.OPEN]

    def items(self) -> list[ApprovalItem]:
        """All items of this inbox, oldest first."""
        with self._lock:
            return list(self._items.values())

    # -- flow -----------------------------------------------------------------------------

    async def handle(self, request: PermissionRequest) -> Decision:
        """Decide a request: PolicyEngine first, the inbox only for `ask` (docs/07 §3)."""
        assessment = self.engine.assess(request)
        decision = assessment.decision
        if decision.action is DecisionAction.ASK:
            covered = self._by_install_pass(assessment)
            return covered if covered is not None else await self.ask(assessment)
        if decision.action is DecisionAction.ALLOW and decision.decided_by is DecidedBy.USER:
            self._grant_install_pass(
                assessment.request, "aus einer früheren Entscheidung", DecidedBy.USER
            )
        return decision

    async def ask(self, assessment: Assessment) -> Decision:
        """Open an item for an `ask`, or join the equal open one, and wait for its decision.

        Equal is what `_merge_key` says. A join shares item, deadline and decision. Never
        raises because of the decider; when the last waiting caller is cancelled, the item
        becomes obsolete.
        """
        loop = asyncio.get_running_loop()
        screening: asyncio.Task[None] | None = None
        with self._lock:
            item = self._equal_open(assessment)
            if item is None:
                item = self._new(assessment)
                waiter: asyncio.Future[Decision | None] = loop.create_future()
                self._waiters[item.id] = waiter
                self._waiting[item.id] = 1
                if self.screener is not None:
                    self._screening.add(item.id)
                    screening = asyncio.create_task(self._screen(item, assessment))
                    self._decider_tasks[item.id] = screening
                else:
                    item = self._publish(item, assessment)
            else:
                item = self._join(item, assessment)
                waiter = self._waiters[item.id]
                self._waiting[item.id] += 1
        try:
            if screening is not None:
                # a stop (`cancel`) answers the waiter while the screener still works
                await asyncio.wait({screening, waiter}, return_when=asyncio.FIRST_COMPLETED)
                item = self.get(item.id)
            remaining = max(0.0, (item.deadline or 0.0) - time.time())
            decision = await asyncio.wait_for(asyncio.shield(waiter), remaining)
            assert decision is not None  # only escalations are released without a decision
            return decision
        except TimeoutError:
            return self._expire(item.id)
        except asyncio.CancelledError:
            with self._lock:
                last = self._waiting.get(item.id) == 1
            if last:
                self.cancel(item.id, "Die wartende Anfrage wurde abgebrochen.")
            raise
        finally:
            await self._leave(item.id)

    async def ask_escalation(
        self,
        run_id: str,
        stage: str,
        summary: str,
        options: Sequence[EscalationOption],
    ) -> Decision | None:
        """Open an escalation of a topology and wait for the user's choice (docs/07 §6).

        Escalations have no deadline. Only a decision `action: option` by `user` with scope
        `once` ends the wait (E15, enforced in `resolve`). Returns None when the item was
        cancelled, e.g. because the run was stopped. The decider (if any) is asked as for
        permission requests.
        """
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[Decision | None] = loop.create_future()
        with self._lock:
            item = ApprovalItem(
                id=self._next_id(run_id),
                type=ApprovalType.ESCALATION,
                run_id=run_id,
                stage=stage,
                summary=summary,
                options=list(dict.fromkeys(options)),
            )
            self._items[item.id] = item
            self._waiters[item.id] = waiter
            self._waiting[item.id] = 1
            self._write(LogEvent.OPEN, item, note=f"Eskalation in Stufe „{stage}“: {summary}")
            if self.decider is not None:
                self._decider_tasks[item.id] = asyncio.create_task(self._run_decider(item))
        try:
            return await asyncio.shield(waiter)
        except asyncio.CancelledError:
            self.cancel(item.id, "Die wartende Eskalation wurde abgebrochen.")
            raise
        finally:
            await self._leave(item.id)

    def open_question(
        self, run_id: str, agent_id: str, channel: str, msg_id: int, text: str
    ) -> ApprovalItem:
        """Open an item „Frage“ for a question of an agent to the user (L16).

        Nobody waits on it and no decider is asked: the agent waits in the channel with
        `wait_for_messages`. The item has no deadline; it closes with the answer or the run.
        """
        question = UserQuestion(agent_id=agent_id, channel=channel, msg_id=msg_id, text=text)
        with self._lock:
            item = ApprovalItem(
                id=self._next_id(run_id),
                type=ApprovalType.QUESTION,
                run_id=run_id,
                agent_id=agent_id,
                question=question,
            )
            self._items[item.id] = item
            self._write(LogEvent.OPEN, item, note=f"Frage von {agent_id} in #{channel}")
        return item

    def answered(self, item_id: str, text: str, *, via: str = "channel") -> bool:
        """The user answered a question outside the inbox, e.g. in the channel; False if it
        was not open any more (answered in the inbox meanwhile)."""
        decision = Decision(
            action=DecisionAction.ANSWER,
            decided_by=DecidedBy.USER,
            reason="Im Kanal beantwortet." if via == "channel" else "Beantwortet.",
            comment=text.strip() or "(ohne Text)",
        )
        with self._lock:
            item = self.get(item_id)
            if item.status is not ApprovalStatus.OPEN or item.type is not ApprovalType.QUESTION:
                return False
            item = self._update(item, ApprovalStatus.DECIDED, decision)
            self._write(LogEvent.DECIDED, item)
        return True

    def close_question(self, item_id: str, note: str) -> bool:
        """A question nobody answered becomes obsolete, e.g. at the end of the run; False if
        it was not open."""
        with self._lock:
            item = self.get(item_id)
            if item.status is not ApprovalStatus.OPEN or item.type is not ApprovalType.QUESTION:
                return False
            item = self._update(item, ApprovalStatus.OBSOLETE, None)
            self._write(LogEvent.OBSOLETE, item, note=note)
        return True

    def resolve(self, item_id: str, decision: Decision) -> ApprovalItem:
        """Record the decision for an open item and wake the waiting agent (thread-safe).

        Only `allow` and `deny` fit a permission request; `always` only by the user (both
        enforced by the types). Raises `ArrError` if the item is not open any more. If the
        request contains a secret, an „Immer“ decision is kept with scope `run` (no rule in
        `arr.yaml`) and the user gets a hint. An escalation takes only one of its options,
        chosen by `user` with scope `once` (E15); anything else raises `ArrError`. A question
        to the user takes only an `answer` of `user` with a text; the caller posts it into the
        channel.
        """
        with self._lock:
            item = self.get(item_id)
            if item.status is not ApprovalStatus.OPEN:
                raise ArrError(
                    f"Die Freigabe „{item_id}“ ist nicht mehr offen ({item.status}). "
                    "Eine Entscheidung ist nicht mehr nötig."
                )
            if item.type is ApprovalType.QUESTION:
                if decision.action is not DecisionAction.ANSWER:
                    raise ArrError("Auf eine Frage antwortest du mit einem Text.")
                item = self._decide_user_only(item, decision)
                self._write(LogEvent.DECIDED, item)
                return item
            if item.type is ApprovalType.ESCALATION:
                item = self._decide_escalation(item, decision)
                self._write(LogEvent.DECIDED, item)
                self._wake(item.id, decision)
                return item
            if decision.action not in (DecisionAction.ALLOW, DecisionAction.DENY):
                raise ArrError("Eine Berechtigungsanfrage lässt sich nur erlauben oder ablehnen.")
            if decision.risk is None:
                decision = decision.model_copy(update={"risk": item.risk})
            assert item.request is not None
            remembered = self.engine.remember(item.id, item.request, decision)
            decision = remembered.decision
            item = self._update(item, ApprovalStatus.DECIDED, decision)
            self._write(LogEvent.DECIDED, item, note=remembered.note)
            if decision.action is DecisionAction.ALLOW and not _high_risk(item.risk):
                self._grant_install_pass(item.request, item.id, decision.decided_by)
        self._wake(item.id, decision)  # first: a broken `on_rule` must not leave the agent waiting
        if remembered.rule is not None and self.on_rule is not None:
            self.on_rule(remembered.rule)
        if remembered.note is not None:
            self._notify(remembered.note)
        return item

    def _decide_user_only(self, item: ApprovalItem, decision: Decision) -> ApprovalItem:
        """Store a decision only the user makes, once (E15): escalations and answers."""
        if decision.decided_by is not DecidedBy.USER or decision.scope is not DecisionScope.ONCE:
            raise ArrError(
                "Eskalationen und Antworten an Agenten gibst in v1 nur du selbst, und zwar "
                "einmalig (E15). Die Entscheidung wurde nicht übernommen."
            )
        return self._update(item, ApprovalStatus.DECIDED, decision)

    def _decide_escalation(self, item: ApprovalItem, decision: Decision) -> ApprovalItem:
        """Check a choice for an escalation (E15: user, once, an offered option) and store it."""
        if decision.decided_by is not DecidedBy.USER or decision.scope is not DecisionScope.ONCE:
            raise ArrError(
                "Eskalationen entscheidest in v1 nur du selbst, und zwar einmalig (E15). "
                "Die Entscheidung wurde nicht übernommen."
            )
        if decision.action is not DecisionAction.OPTION or decision.option not in item.options:
            offered = ", ".join(o.value for o in item.options)
            raise ArrError(f"Wähle eine der angebotenen Optionen: {offered}.")
        return self._update(item, ApprovalStatus.DECIDED, decision)

    def escalate(self, item_id: str, escalated_by: EscalatedBy) -> ApprovalItem:
        """Record who passed an open item on to the user, and why (docs/08 §5.0, W4-04)."""
        with self._lock:
            item = self.get(item_id)
            if item.status is not ApprovalStatus.OPEN:
                raise ArrError(f"Die Freigabe „{item_id}“ ist nicht mehr offen ({item.status}).")
            item = self._update(item, ApprovalStatus.OPEN, None, escalated_by=escalated_by)
            self._write(LogEvent.ESCALATED, item, note=escalated_by.note or None)
        return item

    def cancel(self, item_id: str, note: str = "Der Run wurde gestoppt.") -> ApprovalItem:
        """Mark an open item obsolete and release the agent with a `deny` (stop, docs/07 §7).

        An escalation and a question to the user become obsolete without a decision; the
        waiter of an escalation gets None.
        """
        with self._lock:
            item = self.get(item_id)
            if item.status is not ApprovalStatus.OPEN:
                return item
            if item.type in (ApprovalType.ESCALATION, ApprovalType.QUESTION):
                item = self._update(item, ApprovalStatus.OBSOLETE, None)
                self._write(LogEvent.OBSOLETE, item, note=note)
                self._wake(item.id, None)
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
        """Withdraw a decision (docs/07 §4); later requests ask again.

        Scope agent/run: the grant is removed. A one-time decision of the gatekeeper only
        applied to its request; it is marked revoked without further effect. Withdrawing an
        installation also ends the host passes, so the package hosts ask again.
        """
        with self._lock:
            item = self.get(item_id)
            if not item.revocable or item.decision is None:
                raise ArrError(
                    f"Die Entscheidung „{item_id}“ lässt sich nicht zurückziehen. Zurückziehen "
                    "geht nur bei Entscheidungen des Aufsichts-Agenten und bei deinen eigenen "
                    "mit Reichweite „Agent“ oder „Run“."
                )
            note = None
            if item.decision.decided_by is DecidedBy.GATEKEEPER:
                self._revoked_items |= self._item_keys.get(item_id, frozenset())
                note = (
                    "Gleiche Anfragen entscheidet in diesem Run nicht mehr der Aufsichts-Agent, "
                    "sie kommen zu dir."
                )
            elif item.decision.scope is DecisionScope.ONCE:
                note = "Einmalige Entscheidung: Sie galt nur für ihre Anfrage."
            else:
                try:
                    self.engine.revoke(item_id)  # logs the revocation
                except ArrError:
                    note = "Die Entscheidung war nicht mehr aktiv (z. B. nach einem Neustart)."
            if item.request is not None and install_hosts(item.request):
                self._install_passes.clear()
            item = self._update(item, ApprovalStatus.REVOKED, item.decision)
            if note is not None:
                self._write(LogEvent.REVOKED, item, note=note)
        return item

    # -- internals ----------------------------------------------------------------------

    async def _leave(self, item_id: str) -> None:
        """One caller stopped waiting; the last one takes waiter and decider task with it."""
        with self._lock:
            left = self._waiting.get(item_id, 1) - 1
            self._waiting[item_id] = left
            if left > 0:
                return
            self._waiting.pop(item_id, None)
            self._waiters.pop(item_id, None)
            self._merge_keys.pop(item_id, None)
            task = self._decider_tasks.pop(item_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _equal_open(self, assessment: Assessment) -> ApprovalItem | None:
        key = _merge_key(assessment)
        for item_id, known in self._merge_keys.items():
            item = self._items[item_id]
            if known == key and item.status is ApprovalStatus.OPEN:
                return item
        return None

    def _join(self, item: ApprovalItem, assessment: Assessment) -> ApprovalItem:
        item = self._update(item, ApprovalStatus.OPEN, None, count=item.count + 1)
        self._write(
            LogEvent.DECISION,
            item,
            request=assessment.request,
            decision=assessment.decision,
            note="Gleiche Anfrage, mit der offenen zusammengefasst.",
        )
        return item

    def _grant_install_pass(
        self, request: PermissionRequest | None, source: str, decided_by: DecidedBy
    ) -> None:
        """Let an allowed pip/uv installation reach its package hosts for a while; the host
        requests count as decided by whoever allowed the installation."""
        hosts = install_hosts(request) if request is not None else frozenset()
        if hosts:
            until = time.time() + INSTALL_PASS_S
            self._install_passes[request.agent_id] = (until, hosts, source, decided_by)  # type: ignore[union-attr]

    def _by_install_pass(self, assessment: Assessment) -> Decision | None:
        """The answer for a host request an allowed installation covers, else None.

        Only plain host requests of the sandbox qualify (Claude's `SandboxNetworkAccess`),
        never a shell request or one that names URLs; a high risk always asks.
        """
        request, risk = assessment.request, assessment.decision.risk
        with self._lock:
            until, hosts, source, decided_by = self._install_passes.get(
                request.agent_id, (0.0, frozenset(), "", DecidedBy.USER)
            )
        wanted = {h.rsplit(":", 1)[0].rstrip(".").lower() for h in request.hosts}
        if decided_by is DecidedBy.GATEKEEPER and assessment.items & self._revoked_items:
            return None  # the user withdrew the gatekeeper's decision for such a request
        if (
            time.time() >= until
            or request.tool not in NETWORK_TOOLS
            or request.urls
            or not wanted
            or not wanted <= hosts
            or _high_risk(risk)
        ):
            return None
        decision = Decision(
            action=DecisionAction.ALLOW,
            decided_by=decided_by,
            reason=f"Gehört zur erlaubten Installation {source}.",
            risk=risk,
        )
        if self.log is not None:
            entry = ApprovalLogEntry(event=LogEvent.DECISION, request=request, decision=decision)
            self.log.write(entry)
        return decision

    def _next_id(self, run_id: str) -> str:
        prefix = item_id_prefix(run_id)
        if prefix not in self._counters:
            pattern = re.compile(rf"^{prefix}_(\d+)$")
            numbers = [0]
            for entry in self.log.entries() if self.log is not None else []:
                match = pattern.match(str(entry.get("item_id") or ""))
                if match:
                    numbers.append(int(match.group(1)))
            self._counters[prefix] = max(numbers)
        self._counters[prefix] += 1
        return f"{prefix}_{self._counters[prefix]:04d}"

    def _new(self, assessment: Assessment) -> ApprovalItem:
        """A new permission item in memory; `_publish` or the screener's outcome follows."""
        now = time.time()
        request = assessment.request
        item = ApprovalItem(
            id=self._next_id(request.run_id),
            type=ApprovalType.PERMISSION,
            created_at=now,
            run_id=request.run_id,
            agent_id=request.agent_id,
            request=request,
            risk=assessment.decision.risk,
            deadline=None if self.screener is not None else now + self.timeout,
            summary=assessment.decision.reason,
        )
        self._items[item.id] = item
        self._item_keys[item.id] = assessment.items
        return item

    def _publish(
        self, item: ApprovalItem, assessment: Assessment, escalated_by: EscalatedBy | None = None
    ) -> ApprovalItem:
        """Open the item for the user: deadline, log line `open`, merging, the user's decider."""
        now = time.time()
        item = self._update(
            item,
            ApprovalStatus.OPEN,
            None,
            created_at=now,
            deadline=now + self.timeout,
            escalated_by=escalated_by,
        )
        key = _merge_key(assessment)
        if key is not None:
            self._merge_keys[item.id] = key
        self._write(LogEvent.OPEN, item, decision=assessment.decision)
        if self.decider is not None:
            self._decider_tasks[item.id] = asyncio.create_task(self._run_decider(item))
        return item

    async def _screen(self, item: ApprovalItem, assessment: Assessment) -> None:
        """Let the screener decide a new item, or open it for the user (fail closed)."""
        assert self.screener is not None
        with self._lock:
            history = [
                i
                for i in self._items.values()
                if i.decision is not None and i.decision.decided_by is DecidedBy.GATEKEEPER
            ]

        def revoked() -> bool:  # asked anew at every step of the screen (M1)
            with self._lock:
                return bool(assessment.items & self._revoked_items)

        context = ScreenContext(assessment=assessment, history=history, revoked=revoked)
        outcome: Decision | EscalatedBy
        try:
            outcome = await self.screener.screen(item, context)
        except asyncio.CancelledError:
            with self._lock:
                self._screening.discard(item.id)
            raise
        except Exception:
            logger.exception("Aufsichts-Stufe für %s fehlgeschlagen", item.id)
            try:
                outcome = self.screener.failed(item)
            except Exception:
                logger.exception("Rückfall der Aufsichts-Stufe für %s fehlgeschlagen", item.id)
                outcome = _fallback_deny(item)
        self._settle_screened(item.id, assessment, outcome)

    def _settle_screened(
        self, item_id: str, assessment: Assessment, outcome: Decision | EscalatedBy
    ) -> None:
        """Apply the screener's outcome: open for the user, or decide (one line `decided`).

        Whatever the screener says, it never allows more than once and never as anyone but the
        supervising agent; anything else becomes a deny.
        """
        with self._lock:
            self._screening.discard(item_id)
            item = self._items[item_id]
            if item.status is not ApprovalStatus.OPEN:
                return  # stopped meanwhile
            if (
                isinstance(outcome, Decision)
                and outcome.decided_by is DecidedBy.GATEKEEPER
                and assessment.items & self._revoked_items
                and self.screener is not None
            ):
                # withdrawn while it was screened: the user decides, at once (M1)
                try:
                    outcome = self.screener.revoked(item)
                except Exception:
                    logger.exception("Rückfall nach Zurückziehen für %s fehlgeschlagen", item_id)
                    outcome = _fallback_deny(item)
            if isinstance(outcome, EscalatedBy):
                self._publish(item, assessment, outcome)
                return
            decision = outcome
            allowed = decision.action is DecisionAction.ALLOW
            if decision.action not in (DecisionAction.ALLOW, DecisionAction.DENY) or (
                allowed
                and (
                    decision.decided_by is not DecidedBy.GATEKEEPER
                    or decision.scope is not DecisionScope.ONCE
                )
            ):
                decision = _fallback_deny(item)
            if decision.risk is None:
                decision = decision.model_copy(update={"risk": item.risk})
            item = self._update(item, ApprovalStatus.DECIDED, decision)
            self._write(LogEvent.DECIDED, item)
            if decision.action is DecisionAction.ALLOW and not _high_risk(item.risk):
                self._grant_install_pass(item.request, item.id, decision.decided_by)
        self._wake(item_id, decision)

    def _expire(self, item_id: str) -> Decision:
        with self._lock:
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
        except DeciderUnavailable as error:
            self.cancel(item.id, str(error))
            return
        except Exception:
            # A broken decider must never end the agent's client (S2 §15); the item stays
            # open for other deciders until the deadline.
            logger.exception("Entscheider für %s fehlgeschlagen", item.id)
            return
        if decision is not None and self.get(item.id).status is ApprovalStatus.OPEN:
            try:
                self.resolve(item.id, decision)
            except (ArrError, ValueError):
                logger.exception("Entscheidung für %s ungültig", item.id)

    def _notify(self, text: str) -> None:
        if self.on_note is not None:
            self.on_note(text)
        notify = getattr(self.decider, "notify", None)
        if callable(notify):
            notify(text)

    def _update(
        self,
        item: ApprovalItem,
        status: ApprovalStatus,
        decision: Decision | None,
        **changes: Any,
    ) -> ApprovalItem:
        data = item.model_dump(by_alias=True)
        data.update(status=status, decision=decision, **changes)
        updated = ApprovalItem.model_validate(data)
        self._items[item.id] = updated
        return updated

    def _wake(self, item_id: str, decision: Decision | None) -> None:
        with self._lock:
            waiter = self._waiters.get(item_id)
        if waiter is None:
            return
        loop = waiter.get_loop()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _set_once(waiter, decision)
        else:
            # Called from another thread (GUI): hand the result to the agent's loop.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_set_once, waiter, decision)

    def _write(
        self,
        event: LogEvent,
        item: ApprovalItem,
        *,
        request: PermissionRequest | None = None,
        decision: Decision | None = None,
        note: str | None = None,
    ) -> None:
        if self.log is None:
            return
        self.log.write(
            ApprovalLogEntry(
                event=event,
                item_id=item.id,
                request=request or item.request,
                decision=decision or item.decision,
                question=item.question if event is LogEvent.OPEN else None,
                escalated_by=item.escalated_by if event is LogEvent.OPEN else None,
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
    "  immer   – feste Regel bis zum Ende dieses Runs, für alle Agenten (nicht in arr.yaml)\n"
    "  nein    – ablehnen"
)
_RULE = "─" * 72


_ESCALATION_ANSWERS = {
    "nochmal": EscalationOption.RETRY,
    "weiter": EscalationOption.CONTINUE,
    "beenden": EscalationOption.END,
}
_ESCALATION_WORDS = {
    EscalationOption.RETRY: "noch ein Versuch",
    EscalationOption.CONTINUE: "trotzdem weiter",
    EscalationOption.END: "Run beenden",
}


def _escalation_choices(item: ApprovalItem) -> str:
    lines = ["Antworte mit einem ganzen Wort und Enter:"]
    lines += [
        f"  {word:<8}– {_ESCALATION_WORDS[option]}"
        for word, option in _ESCALATION_ANSWERS.items()
        if option in item.options
    ]
    return "\n".join(lines)


def format_escalation(item: ApprovalItem) -> str:
    """German text of an escalation of a topology for the terminal (docs/03 §4.4)."""
    lines = [_RULE, f"Eskalation {item.id} · der Run ist pausiert", f"Run:    {item.run_id}"]
    if item.stage:
        lines.append(f"Stufe:  {item.stage}")
    if item.summary:
        lines.append(f"Stand:  {item.summary}")
    lines += ["", _escalation_choices(item)]
    return redact("\n".join(lines))


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
    if item.escalated_by is not None:
        who = (
            "Vollmacht (von arr geprüft)"
            if item.escalated_by.decider is DecidedBy.MANDATE_RULE
            else "Aufsichts-Agent"
        )
        lines.append(f"Warum bei dir:          {who}: {item.escalated_by.note}")
    lines.append("")
    lines.append(_CHOICES)
    return redact("\n".join(lines))


class TerminalDecider:
    """Asks in the terminal whether to allow a request (docs/07 §6: Phase 1 without GUI).

    Asks only with a real terminal: standard input and output must both be a TTY, also when
    a terminal object (`read_line`) is passed (W1-05.1 review N4; tests pass `is_terminal`).
    Otherwise, e.g. with a pipe, it refuses at once (`DeciderUnavailable` → item obsolete),
    so nobody can approve „through a pipe“ past the user (docs/07 §4 Nr. 3, §6). Answers are
    whole German words, never a single key or an empty line; „immer“ must be confirmed.
    Prompts are serialized, so parallel requests are asked one after the other. A line typed
    for an expired request is discarded, never applied to the next one.
    """

    def __init__(
        self,
        *,
        read_line: Callable[[str], str] | None = None,
        write: Callable[[str], None] | None = None,
        is_terminal: Callable[[], bool] | None = None,
    ) -> None:
        self._read_line = read_line or input
        self._write = write or (lambda text: print(text, flush=True))
        self._is_terminal = is_terminal or _stdio_is_tty
        self._lock = asyncio.Lock()
        self._pending: asyncio.Future[str | None] | None = None

    def notify(self, text: str) -> None:
        """Show a hint of the inbox, e.g. why „immer“ became „run“."""
        self._write(f"Hinweis: {text}")

    async def decide(self, item: ApprovalItem) -> Decision | None:
        """Show the item and read the answer; refuse without a real terminal."""
        if not self._is_terminal():
            raise DeciderUnavailable(_NO_TTY)
        async with self._lock:
            if item.deadline is not None and item.deadline <= time.time():
                return None
            try:
                if item.type is ApprovalType.ESCALATION:
                    self._write(format_escalation(item))
                    return await self._ask_escalation(item)
                self._write(format_item(item))
                return await self._ask(item)
            except asyncio.CancelledError:
                self._write(
                    f"Die Anfrage {item.id} ist abgelaufen oder hinfällig und wurde abgelehnt. "
                    "Eine Eingabe dazu wird verworfen."
                )
                raise

    async def _ask_escalation(self, item: ApprovalItem) -> Decision:
        words = {
            word: option for word, option in _ESCALATION_ANSWERS.items() if option in item.options
        }
        while True:
            line = await self._read("Deine Wahl: ")
            if line is None:
                raise DeciderUnavailable(_CLOSED)
            option = words.get(line.strip().casefold())
            if option is None:
                self._write(f"„{line.strip()}“ verstehe ich nicht.\n{_escalation_choices(item)}")
                continue
            return Decision(
                action=DecisionAction.OPTION,
                option=option,
                decided_by=DecidedBy.USER,
                reason=f"Im Terminal gewählt: {_ESCALATION_WORDS[option]}.",
            )

    async def _ask(self, item: ApprovalItem) -> Decision:
        while True:
            line = await self._read("Deine Entscheidung: ")
            if line is None:
                raise DeciderUnavailable(_CLOSED)
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
                    "„Immer“ gilt bis zum Ende dieses Runs für alle Agenten, bei Netzwerk für den "
                    "ganzen Host. arr run speichert die Regel nicht in arr.yaml. Zum Bestätigen "
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


_NO_TTY = (
    "Keine Freigabe ohne echtes Terminal: Ein- oder Ausgabe ist kein Terminal (z. B. eine "
    "Pipe). " + TIMEOUT_REASON
)
_CLOSED = "Im Terminal ist keine Eingabe möglich. " + TIMEOUT_REASON


def _stdio_is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _merge_key(assessment: Assessment) -> tuple[Any, ...] | None:
    """What makes two requests one card, None for a request that always gets its own.

    Plain host requests of the sandbox are equal by agent and host. Anything else must match in
    all the card says about the action, the command byte for byte: whitespace can be code
    (indentation, quotes, what a shell does not count as blank). A high risk, a Codex escalation
    and a request the card cannot describe are never merged.
    """
    request = assessment.request
    if (
        not assessment.items
        or assessment.no_rules
        or is_codex_escalation(request)
        or _high_risk(assessment.decision.risk)
        or not (request.command or request.paths or request.urls or request.hosts)
    ):
        return None
    urls = frozenset(request.urls)
    if request.tool in NETWORK_TOOLS and not request.command:
        return (request.agent_id, request.tool, assessment.items, urls)
    return (
        request.agent_id,
        request.tool,
        request.command,
        frozenset(request.paths),
        frozenset(request.hosts),
        urls,
        request.work_dir,
        assessment.items,
    )


def _high_risk(risk: Risk | None) -> bool:
    return risk is not None and risk.level is RiskLevel.HIGH


def _fallback_deny(item: ApprovalItem) -> Decision:
    """Deny by arr's own rule when the supervising agent's stage gave no valid outcome."""
    return Decision(
        action=DecisionAction.DENY,
        decided_by=DecidedBy.FIXED_RULE,
        reason="Der Aufsichts-Agent konnte nicht entscheiden, deshalb abgelehnt. " + TIMEOUT_REASON,
        risk=item.risk,
    )


def _set_once(future: "asyncio.Future[Any]", value: Any) -> None:
    if not future.done():
        future.set_result(value)
