"""Checks that the tests of the old topologies held and that hold for every run (W4b-06b §6.7):
moved here unchanged in what they assert, when `test_review_exploration.py` and
`test_review_exploration_w403_2.py` were deleted with the old topologies.

- S2 (`RUN_HIDDEN_PATHS`): the sealed folder, staging and the room database are never readable,
  not even with an approval; `output/` stays readable (was `test_review_exploration.py:401`).
- N2: the RunManager makes a run tmp for every agent (was `test_review_exploration_w403_2.py:237`).
"""

import asyncio
from pathlib import Path
from typing import Any

from run_kit import agent, make_manager, make_run_input, make_settings, make_workfolder, say, turns

from arr.models import RunStatus
from arr.permissions.policy import RUN_HIDDEN_PATHS, PermissionRules, RunPaths, profile_for
from arr.resolve import resolve_run
from arr.room import store as room_store

J1 = "judge-1"


def test_a3_policy_hides_sealed_results_and_the_room_database(tmp_path: Path) -> None:
    """F4 as a fixed rule: the sealed folder and the database are sensitive (never readable,
    not even with an approval); `output/results` stays readable. The names match the store."""
    store_paths = {room_store.SEALED_DIR, room_store.STAGING_DIR}
    store_paths |= {f"{room_store.ROOM_DIR}/{n}" for n in room_store.ROOM_DB_FILES}
    assert set(RUN_HIDDEN_PATHS) == store_paths
    resolved = resolve_run(make_run_input([agent(J1)]), None, make_settings())
    run_dir = tmp_path / "wf" / "runs" / "r1"
    profile = profile_for(
        resolved.agents[0], RunPaths(tmp_path / "wf", run_dir), PermissionRules(), home=tmp_path
    )
    sealed = run_dir / room_store.SEALED_DIR / "007-mitglied-b-review.md"
    for path in (sealed, run_dir / "groupchats/room.sqlite", run_dir / "groupchats/staging/x"):
        assert profile.is_sensitive(path) and not profile.can_read(path), path
    assert profile.can_read(run_dir / "output/results/007-mitglied-b-review.md")
    submission = run_dir / room_store.SEALED_DIR / "submission-001" / "body.md"
    assert profile.is_sensitive(submission) and not profile.can_read(submission)
    assert profile.can_read(run_dir / "output/submissions/001-p-a/body.md")


async def test_n2_the_run_manager_makes_a_run_tmp_for_every_agent(tmp_path: Path) -> None:
    """Every agent has its `tmp/<id>/` from the start, also one whose backend makes none."""
    seen: dict[str, bool] = {}

    async def topology(ctx: Any) -> None:
        for agent_id in ctx.agents:
            seen[agent_id] = (ctx.run_folder / "tmp" / agent_id).is_dir()

    manager = make_manager(
        make_workfolder(tmp_path), topology, {"a": turns([say("x")]), "b": turns([say("y")])}
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    assert seen == {"a": True, "b": True}
