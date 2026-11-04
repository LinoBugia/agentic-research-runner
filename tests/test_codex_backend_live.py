"""Live smoke test of the Codex backend (W2-02 A9, W2-02.1 G4) with a real model.

gpt-6-luna, effort low, one agent, one turn. Not part of the normal test run:
`uv run pytest -m live tests/test_codex_backend_live.py -s`. Needs a logged-in profile
(`uv run arr auth codex-login <profil>`, default `chatgpt-w2-02a`, else `ARR_CODEX_LIVE_AUTH`).

- a Python script in the work folder is written and run without approval
- writing into the original project is refused (sandbox, then a fixed rule)
- `pip install` installs nothing: the sandbox has no network and the test decider denies an
  escalation. Whether the model asks for one is up to the model, so the test does not need it.
- a hint mid-turn arrives by `steer`
"""

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from arr.backends.base import EventKind
from arr.backends.codex import CodexBackend
from arr.backends.codex_config import codex_home_for
from arr.models import AuthKind, AuthProfile, RunInput, Toolchain, WorkfolderSettings
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.policy import PolicyEngine, RunPaths, profile_for
from arr.permissions.types import (
    ApprovalItem,
    DecidedBy,
    Decision,
    DecisionAction,
    RequestCategory,
)
from arr.resolve import resolve_run
from arr.room.store import RoomStore
from arr.runs.manager import create_run_venv
from arr.runs.prepare import prepare_run
from arr.workfolder import init_workfolder

pytestmark = [pytest.mark.live, pytest.mark.macos]

MODEL = "gpt-6-luna"
AUTH = os.environ.get("ARR_CODEX_LIVE_AUTH", "chatgpt-w2-02a")
SPIKE_TMP = Path(__file__).parent.parent / "spikes" / "w2_02_codex" / "tmp"
HINT = "📨 Hinweis aus #main: Schreibe am Ende auch das Wort ZEBRA in deine Antwort."


class DenyAll:
    def __init__(self) -> None:
        self.items: list[ApprovalItem] = []

    async def decide(self, item: ApprovalItem) -> Decision:
        self.items.append(item)
        return Decision(
            action=DecisionAction.DENY,
            decided_by=DecidedBy.USER,
            reason="Abgelehnt (Test): keine Installation in diesem Test.",
        )


def test_codex_live_smoke(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    auth = AuthProfile(name=AUTH, kind=AuthKind.CODEX_CHATGPT)
    # The real venv root: below the user's temp folder (the test default) Python would not
    # start, a denied ancestor of the run venv stops its `realpath` (W3-00).
    monkeypatch.delenv("ARR_VENV_ROOT")
    if not (codex_home_for(auth) / "auth.json").is_file():
        pytest.skip(f"Profil {AUTH} ist nicht angemeldet (uv run arr auth codex-login {AUTH})")
    # Not below tmp_path: the user's temp folder is denied and would become a passage.
    root = SPIKE_TMP / "live_test"
    shutil.rmtree(root, ignore_errors=True)
    wf = init_workfolder(root / "wf")
    project = wf / "projects" / "smoke"
    project.mkdir(parents=True)
    (project / "notes.md").write_text("Original\n")
    settings = WorkfolderSettings(toolchain=Toolchain())
    run_input = RunInput.model_validate(
        {
            "project": "smoke",
            "task": "Smoke",
            "topology": {"kind": "solo"},
            "defaults": {"provider": "codex", "model": MODEL, "auth": AUTH, "effort": "low"},
            "agents": [{"id": "gpt-1"}],
            "groupchats": [{"name": "main", "members": ["gpt-1"]}],
        }
    )
    run = prepare_run(wf, run_input, resolve_run(run_input, None, settings), settings)
    paths = RunPaths(wf, run.run_dir, settings.toolchain)
    create_run_venv(paths, None)
    request.addfinalizer(lambda: shutil.rmtree(paths.venv_dir, ignore_errors=True))
    RoomStore.create(run.run_dir).setup_channels(run.resolved.groupchats, {})
    (run.run_dir / "tmp").mkdir(exist_ok=True)
    agent = run.agents["gpt-1"]
    profile = profile_for(agent.file.config, RunPaths(wf, run.run_dir, settings.toolchain))
    decider = DenyAll()
    inbox = ApprovalInbox(PolicyEngine([profile]), decider=decider, timeout=120)
    original = project / "notes.md"
    prompt = "\n".join(
        [
            "Das ist ein Test. Arbeite die Schritte der Reihe nach ab, je ein Befehl:",
            "1. Schreibe `quad.py`, das 17*23 ausgibt, und führe es mit python3 aus.",
            f"2. Hänge mit `echo` eine Zeile an die Datei {original} an.",
            "3. Führe `pip install tinyrecord` aus.",
            "Scheitert ein Befehl an der Sandbox, fordere ihn einmal mit kurzer Begründung "
            "außerhalb der Sandbox an. Wird das abgelehnt, mache weiter. Melde am Ende kurz "
            "jeden Schritt.",
        ]
    )

    async def play() -> tuple[list, bool | None]:
        backend = CodexBackend(agent, run_id=run.run_id, inbox=inbox, auth=auth)
        await backend.start()
        events, hinted = [], None
        async for event in backend.run_turn(prompt):
            events.append(event)
            if event.kind is EventKind.TOOL_CALL and hinted is None:
                hinted = await backend.deliver_hint(HINT)
        await backend.close()
        return events, hinted

    events, hinted = asyncio.run(play())
    end = events[-1].data
    with (SPIKE_TMP / "turns.jsonl").open("a", encoding="utf-8") as fh:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "script": "test_codex_backend_live"}
        fh.write(json.dumps({**entry, "label": "live", "status": end.get("status")}) + "\n")

    assert events[-1].kind is EventKind.TURN_END and end["status"] == "ok"
    results = [e.data.get("content") or "" for e in events if e.kind is EventKind.TOOL_RESULT]
    assert any("391" in text for text in results)  # script without approval
    assert original.read_text() == "Original\n"
    assert not list(paths.venv_dir.rglob("tinyrecord*")), "installed without an approval"
    asked = [i for i in decider.items if "pip install" in (i.request.command or "")]
    assert all(RequestCategory.INSTALL in i.request.categories for i in asked)
    assert hinted is True
    assert any(e.kind is EventKind.HINT and e.data["method"] == "steer" for e in events)
    final = [e.data["text"] for e in events if e.kind is EventKind.TEXT][-1]
    print("\nAntwort:", final, "\nUsage:", end["usage"])
