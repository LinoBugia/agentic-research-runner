"""Spike S4: shared SQLite helpers for the room server and the load test."""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel(name TEXT PRIMARY KEY, created REAL, config TEXT);
CREATE TABLE IF NOT EXISTS member(channel TEXT, agent TEXT, alias TEXT,
    PRIMARY KEY (channel, agent));
CREATE TABLE IF NOT EXISTS msg(id INTEGER PRIMARY KEY, ts REAL, sender TEXT, channel TEXT,
    kind TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS cursor(agent TEXT, channel TEXT, last_id INTEGER,
    PRIMARY KEY (agent, channel));
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a connection in autocommit mode; transactions are explicit."""
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db(path: str) -> None:
    con = connect(path)
    con.executescript(SCHEMA)
    con.close()
