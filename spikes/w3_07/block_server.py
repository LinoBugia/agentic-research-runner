"""Spike W3-07: a stdio MCP server whose only tool blocks for `seconds`.

It stands in for the room server (same name `arr-room`), so the CLI and the app-server treat
it like the real one. Each call writes start and end into `SPIKE_LOG` (one JSON line).
"""

import asyncio
import json
import os
import time

from mcp.server.mcpserver import MCPServer

server = MCPServer("arr-room")


def _log(**data: object) -> None:
    path = os.environ.get("SPIKE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": time.time(), **data}) + "\n")


@server.tool(description="Blockiert `seconds` Sekunden und meldet dann, wie lange es war.")
async def block(seconds: float) -> str:
    started = time.monotonic()
    _log(event="start", seconds=seconds)
    try:
        await asyncio.sleep(seconds)
    except BaseException as exc:
        _log(event="cancelled", after=time.monotonic() - started, type=type(exc).__name__)
        raise
    waited = time.monotonic() - started
    _log(event="end", after=waited)
    return f"blockiert {waited:.1f} s"


if __name__ == "__main__":
    server.run()
