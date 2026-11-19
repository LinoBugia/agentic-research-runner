"""Live proof of W4b-05b (A14): auto-compact and `compact_session` with real models.

One run per backend through arr's own start path (RunManager, `LiveFactory`, the profiles of
`~/.arr`), agent `solo` with `compact: {at: 1000}` (tokens), so the first turn is already over
the threshold. Five working turns, each in a stage of its own (so the package never repeats
an earlier prompt):

1. `eins`: a long reading text (bulk for the session) and a fantasy word to remember;
2. auto-compact before `zwei`: note turn, new session, package; `zwei` asks for the word: it
   must come from the agent's own note; new session ID, fill level lower than before;
3. `compact_session(note=…)` with a second word; `drei` asks for it: it knows it;
4. `compact_session()` without a note (counter-check); `vier` asks again: UNBEKANNT.

Haiku with the Mac's Claude login (profile `max`), Codex `gpt-6-luna` effort low (profile
`ARR_CODEX_LIVE_AUTH`, else `chatgpt-w2-02a`). Not part of the normal test run:
`uv run pytest -m live tests/test_compact_live.py -s`. About 5 to 6 model requests per
backend; the test prints the count (`usage` events) per run.
"""

import asyncio
import os
import secrets
import shutil
from pathlib import Path
from typing import Any

import pytest

from arr.backends.codex_config import codex_home_for
from arr.models import (
    AuthKind,
    AuthProfile,
    RunInput,
    RunStatus,
    Toolchain,
    WorkfolderSettings,
)
from arr.permissions.policy import RunPaths
from arr.permissions.types import DecidedBy, Decision, DecisionAction
from arr.resolve import resolve_run
from arr.runs.compact import PACKAGE_HEADING, PURPOSE
from arr.runs.context import RunContext
from arr.runs.eventlog import read_events
from arr.runs.launch import LiveFactory, load_profiles
from arr.runs.manager import RunManager
from arr.workfolder import init_workfolder

pytestmark = [pytest.mark.live, pytest.mark.macos]

CODEX_AUTH = os.environ.get("ARR_CODEX_LIVE_AUTH", "chatgpt-w2-02a")
TMP = Path(__file__).parent.parent / "spikes" / "w4b_05b" / "tmp"
NO_TOOLS = "Nutze keine Werkzeuge."
FILLER = " ".join(
    f"Absatz {n}: Die Folge a(n) = n² + n + 41 liefert für kleine n Primzahlen, für n = 40 "
    "aber 1681 = 41². Das ist nur Lesestoff und für die Aufgabe ohne Bedeutung."
    for n in range(1, 120)
)
REMEMBER = (
    "Lesetext (nur zum Lesen, ohne Bedeutung):\n\n{filler}\n\n"
    "Für ein Gedächtnisspiel: Merke dir das Fantasiewort {word}. Es ist kein Geheimnis. "
    "Antworte nur mit OK. " + NO_TOOLS
)
ASK = (
    "Gedächtnisspiel: Welches Fantasiewort hast du dir gemerkt? Antworte nur mit dem Wort. "
    "Kennst du keines, antworte genau UNBEKANNT. " + NO_TOOLS
)
ASK_PHASE = (
    "Welches Codewort für Phase 2 kennst du? Antworte nur mit dem Wort. Kennst du keines, "
    "antworte genau UNBEKANNT. " + NO_TOOLS
)


class DenyAll:
    """Decider: no approval in this test (the prompts ask for no tools)."""

    async def decide(self, item: Any) -> Decision:
        return Decision(action=DecisionAction.DENY, decided_by=DecidedBy.USER, reason="Nein.")


def real_logins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Like a real run: the real `~/.arr` (profiles, start program, Codex login folders) and
    the real venv root (below the user's temp folder Python would not start, W3-00)."""
    monkeypatch.delenv("ARR_HOME")
    monkeypatch.delenv("ARR_VENV_ROOT")


def manager_for(provider: str, model: str, auth: str, topology: Any) -> RunManager:
    root = TMP / provider
    shutil.rmtree(root, ignore_errors=True)
    workfolder = init_workfolder(root / "wf")
    (workfolder / "projects" / "probe").mkdir(parents=True)
    (workfolder / "projects" / "probe" / "notes.md").write_text("Probe\n")
    settings = WorkfolderSettings(toolchain=Toolchain())
    run_input = RunInput.model_validate(
        {
            "project": "probe",
            "task": "Probe",
            "topology": {"kind": "solo"},
            "defaults": {"provider": provider, "model": model, "auth": auth, "effort": "low"},
            "agents": [{"id": "solo", "compact": {"at": 1000}}],
            "limits": {"budget_usd": 0.5, "turns_per_agent": 8},
            "approvals": {"timeout": "1m"},
        }
    )
    resolved = resolve_run(run_input, None, settings)
    profiles = load_profiles()
    factory = LiveFactory(profiles, resolved.limits)
    manager = RunManager(
        workfolder,
        run_input,
        resolved,
        topology=topology,
        backend_factory=factory,
        settings=settings,
        decider=DenyAll(),
        profiles=profiles,
    )
    factory.manager = manager
    return manager


def live_run(provider: str, model: str, auth: str) -> dict[str, Any]:
    word, phase_word = (f"KW{secrets.token_hex(3).upper()}" for _ in range(2))
    answers: dict[str, str | None] = {}

    async def topology(ctx: RunContext) -> None:
        await ctx.turn("solo", REMEMBER.format(filler=FILLER, word=word), stage="eins")
        answers["zwei"] = (await ctx.turn("solo", ASK, stage="zwei")).text
        await ctx.compact_session("solo", note=f"Das Codewort für Phase 2 ist {phase_word}.")
        answers["drei"] = (await ctx.turn("solo", ASK_PHASE, stage="drei")).text
        await ctx.compact_session("solo")
        answers["vier"] = (await ctx.turn("solo", ASK_PHASE, stage="vier")).text

    manager = manager_for(provider, model, auth, topology)
    try:
        status = asyncio.run(manager.run())
    finally:  # the run venv lies outside the workfolder (venv root): remove it, like F5
        if manager.prepared is not None:
            paths = RunPaths(manager.workfolder, manager.run_dir, manager.settings.toolchain)
            shutil.rmtree(paths.venv_dir, ignore_errors=True)
    run_dir = manager.run_dir
    log = read_events(run_dir / "agents" / "solo" / "transcript.jsonl")
    starts = [e for e in log if e["kind"] == "turn_start"]
    ends = [e["data"] for e in log if e["kind"] == "turn_end"]
    requests = sum(e["kind"] == "usage" for e in log)
    print(f"\n{provider}: status {status}, {requests} Modell-Anfragen (usage), Antworten {answers}")
    for start, end in zip(starts, ends, strict=False):
        purpose = start["data"].get("purpose") or "-"
        print(
            f"  Turn {start['turn']} ({purpose}): {end['status']}, Sitzung {end['session']}, "
            f"Füllstand {end.get('context_tokens')}, Fenster {end.get('context_window')}"
        )
    return {
        "status": status,
        "word": word,
        "phase_word": phase_word,
        "answers": answers,
        "starts": starts,
        "ends": ends,
        "requests": requests,
        "run_dir": run_dir,
    }


def check(result: dict[str, Any]) -> None:
    assert result["status"] is RunStatus.DONE
    starts, ends, answers = result["starts"], result["ends"], result["answers"]
    purposes = [s["data"].get("purpose") for s in starts]
    assert purposes == [None, PURPOSE, None, None, None], purposes
    one, note, two, three, four = ends
    assert all(e["status"] == "ok" for e in ends), ends
    # auto-compact: a new session, a lower fill level, the word from its own note
    assert note["session"] == one["session"] and two["session"] not in (None, one["session"])
    assert two["context_tokens"] < note["context_tokens"], (two, note)
    prompt = starts[2]["data"]["prompt"]  # Claude puts `context.md` in front of a new session
    assert prompt.startswith(PACKAGE_HEADING) or f"## Auftrag\n\n{PACKAGE_HEADING}" in prompt
    assert result["word"] in (answers["zwei"] or ""), answers
    # compact_session with a note, then without one (counter-check)
    assert three["session"] not in (None, two["session"])
    assert result["phase_word"] in (answers["drei"] or ""), answers
    assert four["session"] not in (None, three["session"])
    assert result["phase_word"] not in (answers["vier"] or "")
    assert "UNBEKANNT" in (answers["vier"] or "").upper(), answers
    assert result["requests"] <= 30


def test_a14_claude_auto_compact_and_compact_session(monkeypatch: pytest.MonkeyPatch) -> None:
    real_logins(monkeypatch)
    check(live_run("claude", "claude-haiku-5-5", "max"))


def test_a14_codex_auto_compact_and_compact_session(monkeypatch: pytest.MonkeyPatch) -> None:
    real_logins(monkeypatch)
    auth = AuthProfile(name=CODEX_AUTH, kind=AuthKind.CODEX_CHATGPT)
    if not (codex_home_for(auth) / "auth.json").is_file():
        pytest.skip(f"Profil {CODEX_AUTH} ist nicht angemeldet")
    check(live_run("codex", "gpt-6-luna", CODEX_AUTH))
