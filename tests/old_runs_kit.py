"""Finished runs of the old topologies, for the tests that read them (W4b-06b §6.6).

Since W4b-06b only `topology: phases` runs; runs from before stay readable. Two ways to get
such a run without an old topology:

- `restore(workfolder, name)`: a run folder written once by the code of `main` (`9a7fccc`,
  before the removal) with FakeBackend agents, in `tests/fixtures/old_runs/<name>/`. The room
  is stored as SQL text (`groupchats/room.sql`); the absolute paths of the assignments carry
  `@@WORKFOLDER@@`, filled in here. `agents/*/work/` and `base/` are left out.
- `add_result`, `add_rating`: rows of the tables `result` and `rating` (the `SCHEMA` stays), as
  `AgentRoom.submit_result` and `submit_rating` wrote them, for a folder of `make_run_folder`.

`expected/<name>.json` holds what the reading code of `main` gave for each run (see
`test_old_runs_read.py`).
"""

import json
import shutil
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from arr.room.store import RoomStore

OLD_RUNS = Path(__file__).parent / "fixtures" / "old_runs"
PLACEHOLDER = "@@WORKFOLDER@@"

REVIEW = "20261001-1000_demo_review_a001"
"""`review`: two judges in an anonymous channel, one revision, verdicts in the reviews."""
WATERFALL_FAILED = "20261002-1000_demo_waterfall_a002"
"""`waterfall`: the stage `skizze` fails, the user ends at the escalation."""
WATERFALL = "20261003-1000_demo_waterfall_a003"
"""`waterfall`: both stages pass after one rework, with ratings and criteria."""
SOLO = "20261004-1000_demo_solo_a004"
"""`solo`: one agent, one proof, a message in `#main`."""

NAMES = (REVIEW, WATERFALL_FAILED, WATERFALL, SOLO)


def restore(workfolder: Path, name: str) -> Path:
    """Copy the old run `name` into `workfolder/runs/` with its room; returns the run folder."""
    target = workfolder / "runs" / name
    shutil.copytree(OLD_RUNS / name, target)
    dump = target / "groupchats" / "room.sql"
    text = dump.read_text("utf-8").replace(PLACEHOLDER, str(workfolder))
    con = sqlite3.connect(target / "groupchats" / "room.sqlite")
    try:
        con.executescript(text)
    finally:
        con.close()
    dump.unlink()
    for folder in ("attachments", "sealed", "staging"):
        (target / "groupchats" / folder).mkdir(exist_ok=True)
    return target


def add_result(
    store: RoomStore,
    agent: str,
    kind: str,
    title: str,
    body: str | None = None,
    *,
    path: str | None = None,
    stage: str | None = None,
    attempt: int = 1,
) -> int:
    """A row of `result` as an old run left it; returns its ID (`path` relative to the run)."""
    con = sqlite3.connect(store.run_dir / "groupchats" / "room.sqlite")
    try:
        with con:
            cur = con.execute(
                "INSERT INTO result(ts, agent, stage, kind, title, body, path, attempt) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (time.time(), agent, stage, kind, title, body, path, attempt),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid
    finally:
        con.close()


def add_rating(
    store: RoomStore,
    rater: str,
    result_id: int,
    score: float,
    comment: str,
    criteria: Mapping[str, float] | None = None,
) -> None:
    """A row of `rating` as an old run left it (one per rater and result)."""
    con = sqlite3.connect(store.run_dir / "groupchats" / "room.sqlite")
    try:
        with con:
            con.execute(
                "INSERT INTO rating(ts, rater, result_id, score, criteria, comment) "
                "VALUES(?,?,?,?,?,?)",
                (time.time(), rater, result_id, score, json.dumps(dict(criteria or {})), comment),
            )
    finally:
        con.close()


def normalized(value: Any, workfolder: Path) -> Any:
    """`value` as JSON with the workfolder replaced by `@@WORKFOLDER@@` (for the snapshots)."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return json.loads(text.replace(str(workfolder), PLACEHOLDER))


async def readings(api: Any, workfolder: Path, name: str) -> dict[str, Any]:
    """What the reading code gives for the old run `name` (normalized, for the snapshots):
    `get_run`, `get_results`, `get_log`, `list_channels`, `list_agents`, the line of every log
    entry (`describe_entry`, `describe_checkpoint`), `run_reason`, `read_results` and
    `read_run_summary` of Claude Code (`UserApi`), the items of the approval inbox. The run's
    size on disk is left out."""
    from arr.runs.inspect import describe_checkpoint, run_reason
    from arr.runs.launch import describe_entry
    from arr.runs.orchestrator import OrchestratorLog
    from arr.runs.prepare import load_run_meta
    from arr.service.user_api import UserApi

    run_dir = workfolder / "runs" / name
    entries = OrchestratorLog(run_dir).entries()
    users = UserApi(api)
    found: dict[str, Any] = {
        "get_run": (await api.get_run(name)).model_dump(mode="json"),
        "get_results": (await api.get_results(name)).model_dump(mode="json"),
        "get_log": [e.model_dump(mode="json") for e in await api.get_log(name)],
        "list_channels": [c.model_dump(mode="json") for c in await api.list_channels(name)],
        "list_agents": [a.model_dump(mode="json") for a in await api.list_agents(name)],
        "describe_entry": [describe_entry(e, run_dir) for e in entries],
        "describe_checkpoint": [
            describe_checkpoint(e["data"]) for e in entries if e.get("kind") == "checkpoint"
        ],
        "run_reason": run_reason(load_run_meta(run_dir), entries),
        "read_results": await users.read_results("demo", name),
        "read_run_summary": await users.read_run_summary("demo", name),
        "approvals": [
            i.model_dump(mode="json") for i in await api.list_approvals(run_id=name, status=None)
        ],
    }
    found["get_run"]["run"].pop("size", None)  # bytes of the room's SQLite files vary
    return normalized(found, workfolder)
