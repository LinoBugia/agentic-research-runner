"""W4b-03: the topology `chat` (§6.5) with FakeBackend agents and the real orchestrator."""

from pathlib import Path
from typing import Any

import pytest
from phases_kit import (
    TurnView,
    answer,
    errors,
    fixture_manager,
    hand_in,
    run,
)
from run_kit import make_workfolder, room

from arr.models import RunStatus


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


def talk_then_submit(view: TurnView) -> list[dict[str, Any]]:
    if view.turn == 1:
        return [room("post_message", channel="runde", body=f"Beitrag von {view.agent}")]
    return hand_in(view)


async def test_roundtable_runs(workfolder: Path) -> None:
    pol = {"diskutant-1": talk_then_submit, "diskutant-2": talk_then_submit, "summarizer": answer()}
    mgr, log = fixture_manager(workfolder, "roundtable", pol)
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    print(log.turns)
