"""Live proof of W4-03 F5 (A4): after `reset` a real model does not know its earlier turn.

Three turns per backend: (1) remember a random code word, (2) the same session asks for it
(counter-check: it knows it), `reset`, (3) the fresh session asks for it: it must not know it.
Haiku with the Mac's Claude login (profile `max`), Codex `gpt-6-luna` effort low (profile
`chatgpt-w2-02a`, else `ARR_CODEX_LIVE_AUTH`). Not part of the normal test run:
`uv run pytest -m live tests/test_session_reset_live.py -s`. About 3 requests per backend.
"""

import asyncio
import os
import secrets
import shutil
from pathlib import Path
from typing import Any

import pytest

from arr.backends.base import EventKind
from arr.backends.claude import ClaudeBackend
from arr.backends.codex import CodexBackend
from arr.backends.codex_config import codex_home_for
from arr.models import AuthKind, AuthProfile, Limits, RunInput, Toolchain, WorkfolderSettings
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.policy import PolicyEngine, RunPaths, profile_for
from arr.resolve import resolve_run
from arr.room.store import RoomStore
from arr.runs.manager import create_run_venv
from arr.runs.prepare import prepare_run
from arr.workfolder import init_workfolder

pytestmark = [pytest.mark.live, pytest.mark.macos]

CODEX_AUTH = os.environ.get("ARR_CODEX_LIVE_AUTH", "chatgpt-w2-02a")
TMP = Path(__file__).parent.parent / "spikes" / "w4_03" / "tmp"
REMEMBER = (
    "Für ein Gedächtnisspiel: Merke dir das Fantasiewort {word}. Es ist kein Geheimnis. "
    "Antworte nur mit OK, nutze keine Werkzeuge."
)
ASK = (
    "Gedächtnisspiel: Welches Fantasiewort hast du dir früher in unserem Gespräch gemerkt? "
    "Antworte nur mit dem Wort. Kennst du keines, antworte genau UNBEKANNT. Nutze keine "
    "Werkzeuge."
)
"""Not „Kennwort“: Codex keeps such a word in the resumed thread but will not repeat it
(rollout of the first try, W4-03), so the counter-check would fail for the wrong reason."""


def real_logins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Like a real run: the real `~/.arr` (profiles, start program, Codex login folders) and
    the real venv root (below the user's temp folder Python would not start, W3-00)."""
    monkeypatch.delenv("ARR_HOME")
    monkeypatch.delenv("ARR_VENV_ROOT")


def prepared(provider: str, model: str, auth: str, request: pytest.FixtureRequest) -> Any:
    root = TMP / provider
    shutil.rmtree(root, ignore_errors=True)
    wf = init_workfolder(root / "wf")
    (wf / "projects" / "probe").mkdir(parents=True)
    (wf / "projects" / "probe" / "notes.md").write_text("Probe\n")
    settings = WorkfolderSettings(toolchain=Toolchain())
    run_input = RunInput.model_validate(
        {
            "project": "probe",
            "task": "Probe",
            "topology": {"kind": "solo"},
            "defaults": {"provider": provider, "model": model, "auth": auth, "effort": "low"},
            "agents": [{"id": "judge-1"}],
        }
    )
    run = prepare_run(wf, run_input, resolve_run(run_input, None, settings), settings)
    paths = RunPaths(wf, run.run_dir, settings.toolchain)
    create_run_venv(paths, None)
    request.addfinalizer(lambda: shutil.rmtree(paths.venv_dir, ignore_errors=True))
    RoomStore.create(run.run_dir).setup_channels(run.resolved.groupchats, {})
    (run.run_dir / "tmp").mkdir(exist_ok=True)
    agent = run.agents["judge-1"]
    inbox = ApprovalInbox(PolicyEngine([profile_for(agent.file.config, paths)]), timeout=30)
    return run, agent, inbox


async def three_turns(backend: Any, word: str) -> list[tuple[str, str | None]]:
    """Text and session of: remember, ask in the same session, ask after `reset`."""
    answers = []
    await backend.start()
    try:
        for prompt, reset in ((REMEMBER.format(word=word), False), (ASK, False), (ASK, True)):
            if reset:
                await backend.reset()
            texts, session = [], None
            async for event in backend.run_turn(prompt):
                if event.kind is EventKind.TEXT:
                    texts.append(event.data["text"])
                if event.kind is EventKind.TURN_END:
                    session = event.data.get("session")
            answers.append(("".join(texts[-1:]), session))
    finally:
        await backend.close()
    print(f"\n{type(backend).__name__}: {answers}")
    return answers


def check(answers: list[tuple[str, str | None]], word: str) -> None:
    (_, first), (same, second), (fresh, third) = answers
    assert word in same, "counter-check: the same session knows the word"
    assert word not in fresh and "UNBEKANNT" in fresh.upper(), fresh
    assert first == second and third not in (None, first), "a new session after reset"


def test_f5_claude_fresh_session_forgets(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    real_logins(monkeypatch)
    run, agent, inbox = prepared("claude", "claude-haiku-5-5", "max", request)
    backend = ClaudeBackend(
        agent,
        run_id=run.run_id,
        inbox=inbox,
        auth=AuthProfile(name="max", kind=AuthKind.CLAUDE_OAUTH),
        limits=Limits(budget_usd=0.2),
    )
    word = f"KW{secrets.token_hex(3).upper()}"
    check(asyncio.run(three_turns(backend, word)), word)


def test_f5_codex_fresh_thread_forgets(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    real_logins(monkeypatch)
    auth = AuthProfile(name=CODEX_AUTH, kind=AuthKind.CODEX_CHATGPT)
    if not (codex_home_for(auth) / "auth.json").is_file():
        pytest.skip(f"Profil {CODEX_AUTH} ist nicht angemeldet")
    run, agent, inbox = prepared("codex", "gpt-6-luna", CODEX_AUTH, request)
    backend = CodexBackend(agent, run_id=run.run_id, inbox=inbox, auth=auth)
    word = f"KW{secrets.token_hex(3).upper()}"
    check(asyncio.run(three_turns(backend, word)), word)
