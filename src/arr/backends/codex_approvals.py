"""Server requests of the Codex app-server → arr's approval inbox (docs/07 §3 ③, S2 §10, §15).

`ApprovalBridge` is the handler of the sync client (`codex_sdk.install_approval_handler`).
It runs in the client's reader thread and blocks it while it waits, so:

- **It never raises.** An exception would end the whole client (`_reader_loop` → `fail_all`);
  every error becomes a `decline` plus a log line.
- **Shell and file requests** become a `PermissionRequest` for `ApprovalInbox.handle` (fixed
  rules first, the inbox for `ask`), awaited on the backend's event loop. `allow` → `accept`
  (once: grants for the agent or the run are kept by arr's PolicyEngine, not by Codex),
  `deny` → `decline`. Codex has no field for a reason, so the reason goes to `on_denied`,
  which the backend sends into the turn by `steer`.
- **Fail-closed:** a shell request that is not of kind `command` (e.g. `writeStdin`, input
  into a running process) or carries neither a command nor a network host cannot be checked
  and is declined (W2-02.1 M4).
- **All other requests** are answered explicitly with a refusal, never with the SDK default
  (which accepts): extra permissions, MCP elicitations, user input, legacy approvals. Each of
  them is reported to `on_refused` (method, params, answer) for an event with `raw`.
- **A stop while it waits** (`cancel_waiting`) ends the wait first with a `decline`; only then
  can the client deliver the answer to `turn/interrupt`.
"""

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable
from typing import Any

from arr.models import Provider
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.types import (
    DecidedBy,
    Decision,
    DecisionAction,
    PermissionRequest,
    RequestCategory,
)

__all__ = ["ApprovalBridge", "refusal"]

logger = logging.getLogger(__name__)

COMMAND = "item/commandExecution/requestApproval"
FILE_CHANGE = "item/fileChange/requestApproval"

_REFUSALS: dict[str, dict[str, Any]] = {
    "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
    "mcpServer/elicitation/request": {"action": "decline", "content": None},
    "item/tool/requestUserInput": {"answers": {}},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
}

_UNCHECKED = "Die Anfrage konnte nicht geprüft werden und gilt als abgelehnt."
_UNCHECKABLE = (
    "Die Anfrage lässt sich nicht prüfen (keine vollständige Befehlszeile, z. B. eine Eingabe "
    "in einen laufenden Prozess). Sie ist abgelehnt. Starte den Befehl neu als ganzen Befehl."
)
_STOPPED = "Der Turn wurde gestoppt. Die Anfrage ist damit hinfällig."


def refusal(method: str) -> dict[str, Any]:
    """The refusing answer to a server request (`decline` for shell and file requests)."""
    if method in (COMMAND, FILE_CHANGE):
        return {"decision": "decline"}
    return dict(_REFUSALS.get(method, {}))


def _deny(reason: str) -> Decision:
    return Decision(action=DecisionAction.DENY, decided_by=DecidedBy.FIXED_RULE, reason=reason)


class ApprovalBridge:
    """Handler for one agent's app-server; see the module docstring."""

    def __init__(
        self,
        inbox: ApprovalInbox,
        *,
        run_id: str,
        agent_id: str,
        role: str | None,
        work_dir: str,
        loop: asyncio.AbstractEventLoop,
        on_denied: Callable[[str], None],
        on_refused: Callable[[str, dict[str, Any], dict[str, Any]], None] | None = None,
    ) -> None:
        self.inbox = inbox
        self.run_id = run_id
        self.agent_id = agent_id
        self.role = role
        self.work_dir = work_dir
        self.loop = loop
        self.on_denied = on_denied
        self.on_refused = on_refused
        self._items: dict[str, dict[str, Any]] = {}
        self._waiting: set[concurrent.futures.Future[Decision]] = set()
        self._lock = threading.Lock()
        self._stopped = False

    # -- called from the client's reader thread --------------------------------------------

    def observe(self, message: dict[str, Any]) -> None:
        """Raw message hook: remember started items (a file request carries only the item ID)."""
        if message.get("method") != "item/started":
            return
        item = (message.get("params") or {}).get("item") or {}
        if item.get("type") in ("fileChange", "commandExecution") and item.get("id"):
            self._items[item["id"]] = item

    def __call__(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        try:
            return self._answer(method, params or {})
        except Exception:  # noqa: BLE001 - an exception would end the whole Codex client
            logger.exception("Freigabe-Anfrage %s von %s nicht beantwortet", method, self.agent_id)
            return refusal(method)

    def _answer(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == COMMAND:
            if not self._checkable(params):
                logger.info("Nicht prüfbare Befehlsanfrage von %s abgelehnt", self.agent_id)
                self.on_denied(_UNCHECKABLE)
                return refusal(method)
            request = self._command_request(params)
        elif method == FILE_CHANGE:
            request = self._file_request(params)
        else:
            logger.info("Codex-Anfrage %s von %s abgelehnt", method, self.agent_id)
            answer = refusal(method)
            if self.on_refused is not None:
                self.on_refused(method, params, answer)
            return answer
        decision = self._decide(request)
        if decision.action is DecisionAction.ALLOW:
            return {"decision": "accept"}
        if decision.reason:
            self.on_denied(decision.reason)
        return {"decision": "decline"}

    @staticmethod
    def _checkable(params: dict[str, Any]) -> bool:
        if params.get("kind") not in (None, "command"):
            return False
        host = (params.get("networkApprovalContext") or {}).get("host")
        return bool(str(params.get("command") or "").strip() or host)

    def _request(self, **fields: Any) -> PermissionRequest:
        return PermissionRequest(
            run_id=self.run_id,
            agent_id=self.agent_id,
            role=self.role,
            provider=Provider.CODEX,
            **fields,
        )

    def _command_request(self, params: dict[str, Any]) -> PermissionRequest:
        network = params.get("networkApprovalContext") or {}
        host = network.get("host")
        categories = [RequestCategory.NETWORK] if host else []
        return self._request(
            tool="commandExecution",
            tool_call_id=params.get("itemId"),
            command=params.get("command") or "",
            hosts=[str(host)] if host else [],
            work_dir=params.get("cwd") or self.work_dir,
            reason=params.get("reason"),
            categories=categories,
        )

    def _file_request(self, params: dict[str, Any]) -> PermissionRequest:
        item = self._items.get(str(params.get("itemId")), {})
        paths = [str(c["path"]) for c in item.get("changes") or [] if c.get("path")]
        if params.get("grantRoot"):
            paths.append(str(params["grantRoot"]))
        return self._request(
            tool="fileChange",
            tool_call_id=params.get("itemId"),
            paths=paths,
            work_dir=self.work_dir,
            reason=params.get("reason"),
        )

    def _decide(self, request: PermissionRequest) -> Decision:
        with self._lock:
            if self._stopped:
                return _deny(_STOPPED)
            future = asyncio.run_coroutine_threadsafe(self.inbox.handle(request), self.loop)
            self._waiting.add(future)
        try:
            return future.result()  # the inbox has its own deadline (docs/07 §3 ③)
        except concurrent.futures.CancelledError:
            return _deny(_STOPPED)
        except Exception:  # noqa: BLE001
            logger.exception("Freigabe für %s nicht entschieden", self.agent_id)
            return _deny(_UNCHECKED)
        finally:
            with self._lock:
                self._waiting.discard(future)

    # -- called from the event loop ----------------------------------------------------------

    @property
    def waiting(self) -> bool:
        """True while the handler waits for a decision."""
        with self._lock:
            return bool(self._waiting)

    def cancel_waiting(self, *, stop: bool = True) -> None:
        """End every wait with a `decline`; with `stop`, later requests are declined at once."""
        with self._lock:
            self._stopped = self._stopped or stop
            waiting = list(self._waiting)
        for item in self.inbox.pending():
            if item.agent_id == self.agent_id:
                self.inbox.cancel(item.id, "Der Turn wurde gestoppt.")
        for future in waiting:
            future.cancel()
