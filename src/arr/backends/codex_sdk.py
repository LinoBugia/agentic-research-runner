"""The one layer over the internals of `openai-codex` (pinned to 0.163.0).

`AsyncCodex` has no public way to send approvals to arr: its approval modes are `deny_all`
(no requests, the sandbox decides) and `auto_review` (an OpenAI reviewer model decides, and
the SDK's default handler accepts everything). arr needs reviewer `user` plus its own handler
(docs/07 §7, docs/spikes/S2_codex.md §10, §15). That takes three private accesses, all here:

- the approval handler of the sync client (`codex._client._sync._approval_handler`),
- the low-level `thread/start` and `thread/resume` with `approvalsReviewer: user`,
- the raw message hooks of the sync client, to see server requests and their answers.

`internals_available()` checks the version and the attributes. If they are missing, the
backend falls back to `deny_all`: no approvals, the sandbox blocks hard. `auto_review` is
never used anywhere in arr.
"""

import logging
from collections.abc import Callable
from typing import Any

__all__ = [
    "SDK_VERSION",
    "ApprovalHandler",
    "RpcError",
    "internals_available",
    "install_approval_handler",
    "install_message_hooks",
    "resume_thread",
    "start_thread",
]

logger = logging.getLogger(__name__)

SDK_VERSION = "0.163.0"
"""The only `openai-codex` version whose internals this layer knows (CLAUDE.md, pyproject)."""

ApprovalHandler = Callable[[str, dict[str, Any] | None], dict[str, Any]]
"""Handler of the sync client: `(method, params) → result`, runs in its reader thread."""


def _sync_client(codex: Any) -> Any:
    return codex._client._sync


def internals_available(codex: Any) -> bool:
    """True if the pinned SDK version and the private attributes this layer uses are there."""
    try:
        from openai_codex import __version__
    except ImportError:
        return False
    if __version__ != SDK_VERSION:
        logger.warning(
            "openai-codex %s statt %s: Freigaben nur über die Sandbox", __version__, SDK_VERSION
        )
        return False
    try:
        sync = _sync_client(codex)
    except AttributeError:
        return False
    needed = ("_approval_handler", "_read_message", "_write_message", "thread_start")
    return all(hasattr(sync, name) for name in needed)


class RpcError(dict[str, Any]):
    """A JSON-RPC error as the answer to a server request, instead of a result (W4-00, H1).

    The SDK's reader loop always sends the handler's return value as `result`. A handler that
    returns an `RpcError` gets `{"id": …, "error": {"code": …, "message": …}}` sent instead,
    by the writer `install_approval_handler` puts in front (`_send_errors`).
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(code=code, message=message)


def install_approval_handler(codex: Any, handler: ApprovalHandler) -> None:
    """Replace the SDK's accept-everything default handler (before the client starts).

    The writer is wrapped too, so that an `RpcError` from the handler goes out as a JSON-RPC
    error and never as a `result` the app-server could read as consent.
    """
    sync = _sync_client(codex)
    sync._approval_handler = handler
    sync._write_message = _send_errors(sync._write_message)


def _send_errors(write: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    def write_answer(payload: dict[str, Any]) -> None:
        result = payload.get("result")
        if isinstance(result, RpcError) and "id" in payload:
            payload = {"id": payload["id"], "error": dict(result)}
        write(payload)

    return write_answer


def install_message_hooks(
    codex: Any,
    *,
    on_in: Callable[[dict[str, Any]], None] | None = None,
    on_out: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Observe raw JSON-RPC lines (server requests are invisible to the public stream).

    The hooks run in the client's reader/writer threads and must not raise; errors are logged.
    """
    sync = _sync_client(codex)
    read, write = sync._read_message, sync._write_message

    def hooked_read() -> dict[str, Any]:
        message = read()
        if on_in is not None:
            try:
                on_in(message)
            except Exception:  # noqa: BLE001 - observing must never end the client
                logger.exception("Fehler beim Mitlesen einer Codex-Nachricht")
        return message

    def hooked_write(payload: dict[str, Any]) -> None:
        if on_out is not None:
            try:
                on_out(payload)
            except Exception:  # noqa: BLE001
                logger.exception("Fehler beim Mitlesen einer Codex-Nachricht")
        write(payload)

    sync._read_message = hooked_read
    sync._write_message = hooked_write


def _thread_params(
    *,
    cwd: str,
    model: str,
    developer_instructions: str,
    config: dict[str, Any],
    approvals: bool,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "cwd": cwd,
        "model": model,
        "developerInstructions": developer_instructions,
        "config": config,
        # Never "sandbox": the permission profile (`default_permissions`) replaces it.
    }
    if approvals:
        params |= {"approvalPolicy": "on-request", "approvalsReviewer": "user"}
    else:
        params |= {"approvalPolicy": "never"}  # = ApprovalMode.deny_all
    return params


async def start_thread(codex: Any, *, approvals: bool, **settings: Any) -> tuple[Any, Any]:
    """Start a thread with reviewer `user` (or `never` without internals).

    Returns `(AsyncThread, ThreadStartResponse)`.
    """
    from openai_codex import AsyncThread

    await codex._ensure_initialized()
    started = await codex._client.thread_start(_thread_params(approvals=approvals, **settings))
    return AsyncThread(codex, started.thread.id), started


async def resume_thread(
    codex: Any, thread_id: str, *, approvals: bool, **settings: Any
) -> tuple[Any, Any]:
    """Resume a thread with all settings sent again (S2 §9). Returns `(AsyncThread, response)`."""
    from openai_codex import AsyncThread

    await codex._ensure_initialized()
    params = _thread_params(approvals=approvals, **settings)
    resumed = await codex._client.thread_resume(thread_id, params)
    return AsyncThread(codex, resumed.thread.id), resumed
