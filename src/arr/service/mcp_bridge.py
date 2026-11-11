"""`arr mcp`: stdio MCP server that forwards tool calls to the running service.

See docs/04-service-gui-und-schnittstellen.md §1, §2 (W4-01). Claude Code starts it through the
`.mcp.json` of a project. The bridge offers the tools of `arr.service.user_api` (their list
comes from this installation, so Claude Code sees them also while arr is not running) and
sends every call to `/mcp/` of the service:

- **Where and with what:** address, name and token of the service are read from
  `~/.arr/service.json` (0600) at every call, never from the start arguments, the environment
  or `.mcp.json`: other processes of the user can read arguments and environment
  (`KERN_PROCARGS2`, SECURITY.md 5.3). The token goes only into the `Authorization` header to
  `127.0.0.1`, with the service's own name as `Host`.
- **Which project:** the folder the bridge was started in (Claude Code starts it in the
  project) goes along as header `x-arr-project-dir`. The service checks it; the bridge keeps
  no state that security depends on.
- **arr not running:** every tool answers with a German message that says how to start arr.

A new connection per call: a restarted service (new token, new name) is found at the next call.
"""

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from arr.service.state import ServiceInfo, read_service_info

log = logging.getLogger("arr.service.mcp_bridge")

NOT_RUNNING = (
    "arr läuft nicht. Starte arr (bis es das Dock-Symbol gibt: „arr serve“ im Terminal), danach "
    "geht es hier weiter."
)
LOST = (
    "Die Verbindung zu arr ist abgebrochen. Prüfe, ob arr noch läuft, und versuch es noch einmal."
)
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 720.0
"""Longer than the longest `wait_run` (`user_api.WAIT_MAX_S`)."""


def service_target() -> ServiceInfo | None:
    """The running service of `service.json`, or None (no file, or its process is gone)."""
    info = read_service_info()
    if info is None or not info.alive():
        return None
    return info


def _error(text: str) -> Any:
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


async def forward(name: str, arguments: dict[str, Any], folder: Path) -> Any:
    """Call the tool `name` on the service; a `CallToolResult` in any case."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    from arr.service.guard import service_host
    from arr.service.user_api import MCP_MOUNT, PROJECT_HEADER

    info = service_target()
    if info is None:
        return _error(NOT_RUNNING)
    headers = {
        "Host": service_host(info.host, info.port),
        "Authorization": f"Bearer {info.token}",
        PROJECT_HEADER: quote(str(folder)),
    }
    timeout = httpx2.Timeout(CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S)
    url = f"http://127.0.0.1:{info.port}{MCP_MOUNT}/"
    try:
        async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http:
            transport = streamable_http_client(url, http_client=http)
            async with Client(transport, read_timeout_seconds=READ_TIMEOUT_S) as client:
                return await client.call_tool(name, arguments)
    except Exception as exc:  # noqa: BLE001 - Claude Code gets a sentence, never details
        if _refused(exc):
            return _error(NOT_RUNNING)
        log.warning("Aufruf von %s an arr fehlgeschlagen: %s", name, type(exc).__name__)
        return _error(LOST)


def _refused(exc: BaseException) -> bool:
    """True if the service did not answer at all (nothing listens on the port)."""
    import httpx2

    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, httpx2.ConnectError | ConnectionRefusedError):
            return True
        stack.extend(getattr(current, "exceptions", ()))  # exception groups of anyio
        for nested in (current.__cause__, current.__context__):
            if nested is not None:
                stack.append(nested)
    return False


def build_bridge(folder: Path | None = None) -> Any:
    """The stdio server: the tools of this installation, every call forwarded."""
    from mcp.server import MCPServer

    from arr.service.user_api import build_server

    where = Path(folder) if folder is not None else Path(os.getcwd())
    tools = build_server(None)

    class Bridge(MCPServer):
        async def list_tools(self) -> Any:
            return await tools.list_tools()

        async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
            return await forward(name, dict(arguments or {}), where)

    return Bridge("arr", instructions="arr: Research-Groups für Mathe-Projekte (docs/04 §2).")


def run_bridge() -> None:
    """`arr mcp`: serve on stdin/stdout until Claude Code closes the pipe."""
    logging.basicConfig(level=logging.WARNING, force=True)  # stderr; stdout is the protocol
    for noisy in ("httpx", "httpx2", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    build_bridge().run("stdio")
