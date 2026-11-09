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
  (which accepts): extra permissions, MCP elicitations, user input, legacy approvals. A method
  without a well-formed negative answer, above all one arr does not know, gets a JSON-RPC
  error, never `{}` (W4-00, H1). Each of them is reported to `on_refused` (method, params,
  answer) for an event with `raw`.
- **Unknown fields** in a shell or file request are refused without asking (W4-00, H2): the
  field lists are held against the schema of the pinned Codex version.
- **A stop while it waits** (`cancel_waiting`) ends the wait first with a `decline`; only then
  can the client deliver the answer to `turn/interrupt`.
"""

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable
from typing import Any

from arr.backends.codex_sdk import RpcError
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

SCHEMA_VERSION = "0.163.0"
"""Codex version whose app-server schema the method and field lists below are held against
(`codex app-server generate-json-schema --experimental`; arr starts the client with the SDK's
default `experimentalApi: true`). `tests/test_codex_schema.py` turns red on another version."""

_REFUSALS: dict[str, dict[str, Any]] = {
    "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
    "mcpServer/elicitation/request": {"action": "decline", "content": None},
    "item/tool/requestUserInput": {"answers": {}},
    "item/tool/call": {"contentItems": [], "success": False},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
}
"""Well-formed negative answers to the server requests arr does not act on (W3-06.5, Punkt 5):
extra permissions, MCP elicitations, user input, a dynamic tool call (arr runs no tool for
the app-server), and the legacy approval methods. Every one is also surfaced to the user as an
error."""

CREDENTIAL_METHODS = frozenset({"account/chatgptAuthTokens/refresh", "attestation/generate"})
"""Requests for a token. Their answers require one, so there is no well-formed refusal: arr
answers with a JSON-RPC error (it never hands out a credential, and the app-server uses its own
`CODEX_HOME`)."""

KNOWN_METHODS = frozenset({COMMAND, FILE_CHANGE, *_REFUSALS, *CREDENTIAL_METHODS})
"""Server requests arr answers on purpose; every other method gets `METHOD_NOT_FOUND`."""

METHOD_NOT_FOUND = -32601
"""JSON-RPC 2.0 „Method not found“: the method is not one arr answers (H1)."""

REFUSED = -32000
"""JSON-RPC 2.0 implementation-defined error: a known request arr refuses on purpose."""

_UNCHECKED = "Die Anfrage konnte nicht geprüft werden und gilt als abgelehnt."
_UNCHECKABLE = (
    "Die Anfrage lässt sich nicht prüfen (keine vollständige Befehlszeile, z. B. eine Eingabe "
    "in einen laufenden Prozess). Sie ist abgelehnt. Starte den Befehl neu als ganzen Befehl."
)
_STOPPED = "Der Turn wurde gestoppt. Die Anfrage ist damit hinfällig."

# W3-06.5: an applied file change runs outside the sandbox, so its request must name every
# path it touches. What arr does not fully understand it refuses (fail closed).
FILE_PARAM_FIELDS = frozenset(
    {"itemId", "startedAtMs", "threadId", "turnId", "grantRoot", "reason"}
)
"""Fields of `FileChangeRequestApprovalParams` in the pinned Codex version (schema). A field
beyond these could carry a path arr does not check, so arr refuses the request."""

CHANGE_FIELDS = frozenset({"path", "kind", "diff"})
"""Fields of one `FileUpdateChange` (schema): the file, the kind of change, the diff."""

CHANGE_KIND_FIELDS = frozenset({"type", "move_path"})
"""Fields of a change kind: `type` (add/delete/update) and, for an update, the move target."""

CHANGE_KINDS = frozenset({"add", "delete", "update"})
"""Kinds of change arr understands; `update` may carry `move_path`."""

COMMAND_PARAM_FIELDS = frozenset(
    {
        "additionalPermissions",
        "approvalId",
        "availableDecisions",
        "command",
        "commandActions",
        "cwd",
        "environmentId",
        "itemId",
        "kind",
        "networkApprovalContext",
        "proposedExecpolicyAmendment",
        "proposedNetworkPolicyAmendments",
        "reason",
        "startedAtMs",
        "threadId",
        "turnId",
    }
)
"""Fields of `CommandExecutionRequestApprovalParams` in the pinned Codex version (schema with
the experimental fields, W4-00 H2). A field beyond these could change what an approved command
may do, so arr refuses the request. arr answers only `accept` or `decline`, so the proposed
amendments and `availableDecisions` never take effect."""

NETWORK_CONTEXT_FIELDS = frozenset({"host", "protocol"})
"""Fields of `NetworkApprovalContext` (schema): the host arr checks and its protocol."""

JSON_KINDS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}
"""JSON-Schema types → Python types of a parsed JSON value (`bool` is never an `integer`)."""

COMMAND_PARAM_KINDS: dict[str, frozenset[str]] = {
    "additionalPermissions": frozenset({"object", "null"}),
    "approvalId": frozenset({"string", "null"}),
    "availableDecisions": frozenset({"array", "null"}),
    "command": frozenset({"string", "null"}),
    "commandActions": frozenset({"array", "null"}),
    "cwd": frozenset({"string", "null"}),
    "environmentId": frozenset({"string", "null"}),
    "itemId": frozenset({"string"}),
    "kind": frozenset({"string"}),
    "networkApprovalContext": frozenset({"object", "null"}),
    "proposedExecpolicyAmendment": frozenset({"array", "null"}),
    "proposedNetworkPolicyAmendments": frozenset({"array", "null"}),
    "reason": frozenset({"string", "null"}),
    "startedAtMs": frozenset({"integer"}),
    "threadId": frozenset({"string"}),
    "turnId": frozenset({"string"}),
}
"""JSON types of each field of `CommandExecutionRequestApprovalParams` in 0.163.0 (W4-00.1,
B1), held against the schema by `tests/test_codex_schema.py`. A value of another kind is
refused without asking, never turned into text for the inbox."""

NETWORK_CONTEXT_KINDS: dict[str, frozenset[str]] = {
    "host": frozenset({"string"}),
    "protocol": frozenset({"string"}),
}
"""JSON types of the fields of `NetworkApprovalContext` (schema)."""


def _of_kind(value: Any, kinds: frozenset[str]) -> bool:
    if isinstance(value, bool):
        return False  # no field of these requests is a boolean
    return any(isinstance(value, JSON_KINDS[kind]) for kind in kinds)


def _kinds_ok(values: dict[str, Any], kinds: dict[str, frozenset[str]]) -> bool:
    """True if every present field has a value of its kind in the schema."""
    return all(_of_kind(value, kinds[name]) for name, value in values.items())


_FILE_UNREADABLE = (
    "Diese Datei-Änderung lässt sich nicht vollständig lesen und ist deshalb abgelehnt. Lege "
    "deine Dateien einzeln mit einem Add-Patch im Arbeitsordner an."
)
_FILE_UNKNOWN_FIELD = (
    "Diese Datei-Anfrage enthält ein Feld, das arr nicht kennt und das einen Pfad außerhalb "
    "der Sandbox tragen könnte. Sie ist abgelehnt. Arbeite mit Add-, Update- oder Delete-Patches "
    "in deinem Arbeitsordner."
)
_FILE_UNKNOWN_CHANGE = (
    "Diese Art von Datei-Änderung kennt arr nicht und ist deshalb abgelehnt. Nutze Add, Update "
    "oder Delete in deinem Arbeitsordner."
)
_FILE_MOVE = (
    "Eine Datei verschieben („Move to“) ist über das Patch-Werkzeug nicht erlaubt: Das Ziel "
    "läge sonst ungeprüft außerhalb der Sandbox. Verschiebe die Datei in der Shell mit „mv "
    "quelle ziel“, dann greift die Sandbox."
)
_FILE_GRANT_ROOT = (
    "„grantRoot“ (dauerhaftes Schreibrecht für einen ganzen Ordner) ist nicht erlaubt, auch "
    "nicht mit Freigabe. Schreibe einzelne Dateien in deinem Arbeitsordner."
)
_COMMAND_UNKNOWN_FIELD = (
    "Diese Befehlsanfrage enthält ein Feld, das arr nicht kennt und das die Rechte des Befehls "
    "ändern könnte. Sie ist abgelehnt. Führe den Befehl ohne Sonderrechte in deinem "
    "Arbeitsordner aus."
)
_COMMAND_UNREADABLE = (
    "Diese Befehlsanfrage enthält einen Wert, den arr nicht lesen kann (falsche Art, z. B. eine "
    "Liste statt eines Texts). Sie ist abgelehnt. Führe den Befehl ohne Sonderrechte in deinem "
    "Arbeitsordner aus."
)
_COMMAND_EXTRA_PERMISSIONS = (
    "Zusätzliche Rechte für einen Befehl („additionalPermissions“) sind nicht erlaubt, auch "
    "nicht mit Freigabe. Führe den Befehl ohne Sonderrechte in deinem Arbeitsordner aus."
)


def refusal(method: str) -> dict[str, Any]:
    """The refusing answer to a server request: `decline` for shell and file requests, a
    well-formed negative result where the protocol has one, otherwise a JSON-RPC error."""
    if method in (COMMAND, FILE_CHANGE):
        return {"decision": "decline"}
    if method in _REFUSALS:
        return dict(_REFUSALS[method])
    if method in CREDENTIAL_METHODS:
        return RpcError(REFUSED, f"arr refuses {method}: it hands out no credentials")
    return RpcError(METHOD_NOT_FOUND, f"Method not found: arr does not answer {method}")


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
            reason = self._command_refusal(params)
            if reason is not None:
                logger.info("Befehlsanfrage von %s abgelehnt: %s", self.agent_id, reason)
                self.on_denied(reason)
                return refusal(method)
            request = self._command_request(params)
        elif method == FILE_CHANGE:
            result = self._file_request(params)
            if isinstance(result, Decision):
                logger.info("Datei-Anfrage von %s abgelehnt: %s", self.agent_id, result.reason)
                self.on_denied(result.reason)
                return refusal(method)
            request = result
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
    def _command_refusal(params: dict[str, Any]) -> str | None:
        """Why a shell request is refused without asking, or None (W2-02.1 M4, W4-00 H2).

        Like a file request: an unknown field, also inside `networkApprovalContext`, and any
        extra permissions are refused before the request reaches the inbox.
        """
        network = params.get("networkApprovalContext")
        if set(params) - COMMAND_PARAM_FIELDS:
            return _COMMAND_UNKNOWN_FIELD
        if network is not None and (
            not isinstance(network, dict) or set(network) - NETWORK_CONTEXT_FIELDS
        ):
            return _COMMAND_UNKNOWN_FIELD
        if not _kinds_ok(params, COMMAND_PARAM_KINDS) or (
            network is not None and not _kinds_ok(network, NETWORK_CONTEXT_KINDS)
        ):
            return _COMMAND_UNREADABLE
        if params.get("additionalPermissions") is not None:
            return _COMMAND_EXTRA_PERMISSIONS
        if params.get("kind") not in (None, "command"):
            return _UNCHECKABLE
        host = (network or {}).get("host")
        if not (str(params.get("command") or "").strip() or host):
            return _UNCHECKABLE
        return None

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
            hosts=[host] if host else [],
            work_dir=params.get("cwd") or self.work_dir,
            reason=params.get("reason"),
            categories=categories,
        )

    def _file_request(self, params: dict[str, Any]) -> PermissionRequest | Decision:
        """Build the request with every path the change touches, or a hard deny (W3-06.5).

        An applied `fileChange` runs outside the sandbox, so the request must name the source
        and the `move_path` target of each change. Anything arr cannot fully read, a move, a
        `grantRoot` and an unknown field are refused before the request reaches the inbox.
        """
        if set(params) - FILE_PARAM_FIELDS:
            return _deny(_FILE_UNKNOWN_FIELD)
        item = self._items.get(str(params.get("itemId")))
        changes = item.get("changes") if isinstance(item, dict) else None
        if not isinstance(changes, list):
            return _deny(_FILE_UNREADABLE)
        paths: list[str] = []
        moved = False
        for change in changes:
            if not isinstance(change, dict) or set(change) - CHANGE_FIELDS:
                return _deny(_FILE_UNREADABLE)
            path, kind = change.get("path"), change.get("kind")
            if not isinstance(path, str) or not isinstance(kind, dict):
                return _deny(_FILE_UNREADABLE)
            if set(kind) - CHANGE_KIND_FIELDS or kind.get("type") not in CHANGE_KINDS:
                return _deny(_FILE_UNKNOWN_CHANGE)
            paths.append(path)
            target = kind.get("move_path")
            if target is not None:
                if not isinstance(target, str):
                    return _deny(_FILE_UNREADABLE)
                paths.append(target)  # into the request, so the PolicyEngine sees it too
                moved = True
        if params.get("grantRoot"):
            return _deny(_FILE_GRANT_ROOT)
        if moved:
            return _deny(_FILE_MOVE)
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
