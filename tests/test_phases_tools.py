"""W4b-03 §6.7 (A15): the tools of the room server per kind of run. Since W4b-03 no run offers
`vote`, `call_vote`, `give_floor`, `pin` or `close_discussion`; the old runs are in
`tests/test_room_server.py` (also for the moderator of a channel), here a `phases` run (it
has no moderated channels: `resolve` refuses them)."""

from pathlib import Path

import pytest
from mcp.client.client import Client
from phases_kit import errors, hand_in, manager, run, solo
from run_kit import make_workfolder

from arr.models import RunStatus
from arr.room.server import build_server

PHASES_TOOLS = {
    "inbox",
    "list_channels",
    "read_messages",
    "post_message",
    "get_attachment",
    "set_status",
    "submit",
    "assign",  # W4b-04
    "get_assignment",
    "wait_for_messages",
    "wait_until",
}
REMOVED = {"vote", "call_vote", "give_floor", "pin", "close_discussion"}


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


async def test_a15_the_tools_of_a_phases_run(workfolder: Path) -> None:
    group = {
        "agents": [{"id": "a"}, {"id": "b"}],
        "channels": [{"name": "k", "members": ["a", "b"]}],
        "phases": [{**solo("p", "a"), "channels": ["k"]}],
    }
    mgr, log = manager(workfolder, group, {"a": lambda v: hand_in(v)})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    for agent in ("a", "b"):
        async with Client(build_server(mgr.room.for_agent(agent))) as client:
            names = {t.name for t in (await client.list_tools()).tools}
        assert names == PHASES_TOOLS, agent
        assert not names & REMOVED
