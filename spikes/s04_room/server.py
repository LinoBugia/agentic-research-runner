"""Spike S4: minimal room server (MCP stdio, SQLite WAL, enforced membership).

One process per agent. Identity comes from AGENT_ID, the database path from ROOM_DB.
ROOM_TXN selects the write transaction style: "immediate" (default) or "deferred".

mcp 2.x: `FastMCP` from `mcp.server.fastmcp` is now `MCPServer` from `mcp.server.mcpserver`.
"""

import os
import sqlite3
import time

import db
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

AGENT_ID = os.environ["AGENT_ID"]
DB = os.environ["ROOM_DB"]
TXN = os.environ.get("ROOM_TXN", "immediate")

mcp = MCPServer("room")


def connect() -> sqlite3.Connection:
    return db.connect(DB)


def _begin_write(con: sqlite3.Connection) -> None:
    con.execute("BEGIN IMMEDIATE" if TXN == "immediate" else "BEGIN")


def _require_member(con: sqlite3.Connection, channel: str) -> None:
    row = con.execute(
        "SELECT 1 FROM member WHERE channel=? AND agent=?", (channel, AGENT_ID)
    ).fetchone()
    if row is None:
        raise ToolError(f"not a member of #{channel}")


@mcp.tool()
def list_channels() -> str:
    """Channels this agent is a member of."""
    con = connect()
    try:
        rows = con.execute(
            "SELECT channel FROM member WHERE agent=? ORDER BY channel", (AGENT_ID,)
        ).fetchall()
        return ", ".join(r[0] for r in rows)
    finally:
        con.close()


@mcp.tool()
def post_message(channel: str, body: str, kind: str = "note") -> str:
    """Post a message to a channel you are a member of."""
    con = connect()
    try:
        _require_member(con, channel)
        _begin_write(con)
        cur = con.execute(
            "INSERT INTO msg(ts,sender,channel,kind,body) VALUES(?,?,?,?,?)",
            (time.time(), AGENT_ID, channel, kind, body),
        )
        con.execute("COMMIT")
        return f"posted #{cur.lastrowid}"
    finally:
        if con.in_transaction:
            con.execute("ROLLBACK")
        con.close()


@mcp.tool()
def read_messages(channel: str, only_new: bool = True, limit: int = 50) -> str:
    """Read messages from a channel (by default only those you have not seen yet)."""
    con = connect()
    try:
        _require_member(con, channel)
        # Read and cursor update in one transaction so the cursor matches what was returned.
        _begin_write(con)
        last = 0
        if only_new:
            row = con.execute(
                "SELECT last_id FROM cursor WHERE agent=? AND channel=?", (AGENT_ID, channel)
            ).fetchone()
            last = row[0] if row else 0
        rows = con.execute(
            "SELECT id,sender,kind,body FROM msg WHERE channel=? AND id>? ORDER BY id LIMIT ?",
            (channel, last, limit),
        ).fetchall()
        if rows:
            con.execute(
                "INSERT INTO cursor VALUES(?,?,?) ON CONFLICT(agent,channel) "
                "DO UPDATE SET last_id=max(last_id, excluded.last_id)",
                (AGENT_ID, channel, rows[-1][0]),
            )
        con.execute("COMMIT")
        return "\n\n".join(f"[#{i} {s} · {k}]\n{b}" for i, s, k, b in rows) or "(no new messages)"
    finally:
        if con.in_transaction:
            con.execute("ROLLBACK")
        con.close()


if __name__ == "__main__":
    mcp.run()  # stdio
