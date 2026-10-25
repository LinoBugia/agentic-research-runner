"""Minimal stdio MCP server standing in for arr-room (mcp 2.x MCPServer).

Identity comes from AGENT_ID in the environment; posts go to ROOM_LOG (JSONL).
"""

import json
import os
import time

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="room")


@server.tool()
def whoami() -> str:
    """Return the agent id this room connection belongs to."""
    return f"agent_id={os.environ.get('AGENT_ID', '<unset>')}"


@server.tool()
def post(channel: str, text: str) -> str:
    """Post a message to a channel of the group chat."""
    entry = {"ts": time.time(), "agent": os.environ.get("AGENT_ID"), "channel": channel}
    entry["text"] = text
    with open(os.environ["ROOM_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return "ok, posted. unread: #main 0, questions for you 0"


if __name__ == "__main__":
    server.run("stdio")
