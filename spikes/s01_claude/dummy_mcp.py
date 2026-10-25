"""Dummy stdio MCP server for the spike: read_messages (q09) and ping (q10).

mcp 2.x renamed FastMCP to MCPServer (mcp.server.fastmcp raises ModuleNotFoundError).
AGENT_ID comes from the environment, like the planned arr-room server.
"""

import os
import sys
from pathlib import Path

from mcp.server import MCPServer

server = MCPServer("arr-dummy")
LOG = os.environ.get("DUMMY_MCP_LOG")


def _log(line: str) -> None:
    if LOG:
        with Path(LOG).open("a") as fh:
            fh.write(line + "\n")


@server.tool()
def read_messages(channel: str = "main") -> str:
    """Read unread messages of a group chat channel addressed to you."""
    _log(f"read_messages channel={channel} agent={os.environ.get('AGENT_ID')}")
    return (
        f"#{channel} – 1 ungelesene Nachricht\n"
        "[critic → explorer] Frage: Wie lautet die 10. Fibonacci-Zahl? "
        "Nenne sie bitte in deiner Schlussantwort mit dem Präfix 'Antwort an critic:'."
    )


@server.tool()
def ping() -> str:
    """Health check of the arr bridge. Returns pong and the agent id."""
    _log(f"ping agent={os.environ.get('AGENT_ID')}")
    return f"pong from arr-dummy (agent={os.environ.get('AGENT_ID', '?')})"


if __name__ == "__main__":
    print("arr-dummy MCP server starting", file=sys.stderr)
    server.run("stdio")
