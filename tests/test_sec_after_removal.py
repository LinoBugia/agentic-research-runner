"""Security properties that only tests of the old topologies held, now on phases runs
(W4b-06b §6.7, table S1–S14; Entscheidung 57). Same statements, the way changed: a phases run
instead of an old topology, `submit` instead of `submit_result`.

- S4 (error): a turn of an isolated phase fails after another agent handed in; the run fails,
  and still nothing stays sealed: the submissions are released to `output/` (was
  `test_review_exploration_w403_1.py::test_m31_an_error_in_the_review_still_releases_…`).

FakeBackend agents with the real RunManager, Orchestrator and room; no model.
"""

from pathlib import Path
from typing import Any

import pytest
from phases_kit import answer, errors, manager, parallel, run
from run_kit import eventually, make_workfolder

from arr.models import RunStatus
from arr.room import store as room_store
from arr.room.store import RoomStore
from arr.runs.orchestrator import Orchestrator

AGENTS = {"agents": [{"id": a} for a in ("a", "b")]}


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


async def test_s4_an_error_in_an_isolated_phase_still_releases_the_sealed_submissions(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entscheidung 57: a stop ends the turns regularly; the release matters as well when the
    phase fails: here a turn of `b` raises after `a` handed in."""
    real = Orchestrator.turn

    async def failing(self: Orchestrator, agent_id: str, prompt: str, **kwargs: Any) -> Any:
        if agent_id == "b" and kwargs.get("stage") == "x":
            await eventually(lambda: bool(self.room.submissions(agent="a")), within=10)
            raise RuntimeError("Backend weg")
        return await real(self, agent_id, prompt, **kwargs)

    monkeypatch.setattr(Orchestrator, "turn", failing)
    group = {**AGENTS, "phases": [{**parallel("x", ["a", "b"]), "isolated": True}]}
    mgr, _log = manager(workfolder, group, {"a": answer(), "b": answer()})
    assert await run(mgr) is RunStatus.FAILED
    assert any("Backend weg" in e for e in errors(mgr)), errors(mgr)
    store = RoomStore(mgr.run_dir)
    assert store.sealed_stages() == {}, "nothing stays sealed after the error"
    [sub] = store.submissions()
    assert sub.agent == "a" and not sub.sealed
    assert sub.path.startswith("output/") and (mgr.run_dir / sub.path).is_dir()
    sealed = mgr.run_dir / room_store.SEALED_DIR
    assert not sealed.exists() or list(sealed.iterdir()) == []
