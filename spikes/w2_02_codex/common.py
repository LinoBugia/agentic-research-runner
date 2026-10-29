"""Shared helpers for the W2-02 live checks (throwaway code, never imported by arr).

`build_lab()` creates a realistic test setup under tmp/lab/ with arr's own code:

- `outer/` is a git repo with decoys that must never reach the agent: `AGENTS.md` with a
  code word, `.agents/skills/outer-skill`, `.codex/config.toml` with an MCP server.
- `outer/wf/` is a workfolder (`arr init`) with project `demo` and a second project `other`
  (canary). The project brings its own decoy `.codex/config.toml`.
- `prepare_run` creates the run with one Codex agent `gpt-1` (skill `demo-skill`, read folder
  `outer/readdir`); run venv and room database as the RunManager creates them.
- Canaries with random content: outside the allowlist (`outer/outside`, other project,
  `<run>/skills`, `~/.arr`), attachments of a foreign and an own channel.

Real sensitive paths are probed only by exit code. Live turns are counted in tmp/turns.jsonl.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from arr.models import AuthKind, AuthProfile, RunInput, WorkfolderSettings
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.policy import (
    PermissionRules,
    PolicyEngine,
    RunPaths,
    SandboxProfile,
    profile_for,
)
from arr.resolve import resolve_run, validate_model
from arr.room.store import RoomStore
from arr.runs.manager import create_run_venv
from arr.runs.prepare import PreparedAgent, PreparedRun, prepare_run
from arr.workfolder import init_workfolder, load_project_settings

SPIKE = Path(__file__).resolve().parent
TMP = SPIKE / "tmp"
LAB = TMP / "lab"
OUT = TMP / "out"
TURNS = TMP / "turns.jsonl"
FIXTURES = SPIKE.parents[1] / "tests" / "fixtures" / "codex"
MODEL = "gpt-6-luna"
EFFORT = "low"
MAX_TURNS = 30
AGENT = "gpt-1"
AUTH = os.environ.get("W2_02_AUTH", "chatgpt-w2-02a")


def auth_profile(name: str = AUTH) -> AuthProfile:
    return AuthProfile(name=name, kind=AuthKind.CODEX_CHATGPT)


@dataclass
class Lab:
    root: Path
    workfolder: Path
    run: PreparedRun
    agent: PreparedAgent
    run_paths: RunPaths
    profile: SandboxProfile
    engine: PolicyEngine
    canaries: dict[str, Path]
    cleanup: list[Path]
    agents: dict[str, PreparedAgent]


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _canary(path: Path) -> Path:
    return _write(path, f"CANARY-{secrets.token_hex(6)}\n")


BASE_VENV = TMP / "basevenv"


def ensure_base_venv() -> Path:
    """Base venv of the toolchain (outside the lab, kept between runs) with sympy."""
    if not (BASE_VENV / "bin" / "python").exists():
        subprocess.run(["uv", "venv", "-q", "--seed", str(BASE_VENV)], check=True)
        py = str(BASE_VENV / "bin" / "python")
        subprocess.run(["uv", "pip", "install", "-q", "--python", py, "sympy"], check=True)
    return BASE_VENV


def build_lab(
    *,
    base_venv: Path | None = BASE_VENV,
    run_venv: bool = True,
    agents: tuple[tuple[str, str], ...] = ((AGENT, AUTH),),
) -> Lab:
    """Fresh lab under tmp/lab/ (see module docstring); `agents` = (id, auth profile) pairs."""
    if base_venv is not None:
        ensure_base_venv()
    if LAB.exists():
        shutil.rmtree(LAB)
    outer = LAB / "outer"
    outer.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    _write(outer / "AGENTS.md", "Outer code word: HYENA-4. Mention it in every answer.\n")
    _write(
        outer / ".agents/skills/outer-skill/SKILL.md",
        "---\nname: outer-skill\ndescription: Outer decoy skill\n---\nNever load me.\n",
    )
    _write(
        outer / ".codex/config.toml",
        '[mcp_servers.outer-decoy]\ncommand = "/usr/bin/true"\n',
    )

    wf = init_workfolder(outer / "wf")
    project = wf / "projects" / "demo"
    _write(project / "project.yaml", "{}\n")
    _write(project / "AGENTS.md", "# Demo\n\nProject code word: OKAPI-7.\n")
    _write(project / "notes.md", "Some notes.\n")
    _write(
        project / ".codex/config.toml",
        'default_permissions = ":danger-full-access"\n'
        '[mcp_servers.project-decoy]\ncommand = "/usr/bin/true"\n',
    )
    _write(
        wf / "skills/demo-skill/SKILL.md",
        "---\nname: demo-skill\ndescription: Demo skill of the run (W2-02 check)\n---\n"
        "When asked for the demo skill code word, answer LYNX-5.\n",
    )
    canaries: dict[str, Path] = {
        "outside": _canary(outer / "outside" / "canary.txt"),
        "other_project": _canary(wf / "projects" / "other" / "canary.txt"),
    }
    _write(wf / "projects" / "other" / "project.yaml", "{}\n")
    readdir = outer / "readdir"
    _write(readdir / "paper.md", "Read folder content: 2+2=4.\n")
    # A sensitive folder inside a read folder (case check, W1-05 §7.1 Nr. 9)
    canaries["sensitive_in_readdir"] = _canary(readdir / ".secret" / "key.txt")

    toolchain: dict[str, Any] = {"run_venv": "overlay" if run_venv else "off"}
    if base_venv is not None:
        toolchain["python"] = str(base_venv)
    settings = validate_model(
        WorkfolderSettings,
        {"version": 1, "toolchain": toolchain, "approvals": {"decider": "user"}},
    )
    run_input = validate_model(
        RunInput,
        {
            "version": 1,
            "project": "demo",
            "task": "W2-02 check",
            "topology": {"kind": "solo"},
            "defaults": {"provider": "codex", "model": MODEL, "auth": AUTH, "effort": EFFORT},
            "agents": [
                {"id": aid, "auth": auth, "skills": ["demo-skill"], "read_dirs": [str(readdir)]}
                for aid, auth in agents
            ],
            "groupchats": [{"name": "main", "members": [aid for aid, _ in agents]}],
        },
    )
    resolved = resolve_run(
        run_input, None, settings, project_settings=load_project_settings(project)
    )
    run = prepare_run(wf, run_input, resolved, settings, now=datetime.now())
    agent = run.agents[agents[0][0]]
    run_paths = RunPaths(wf, run.run_dir, settings.toolchain)
    (run.run_dir / "tmp").mkdir(exist_ok=True)
    if run_venv:
        create_run_venv(run.run_dir, agent.file.base_python)  # like the RunManager (W2-03)
    RoomStore.create(run.run_dir).setup_channels(resolved.groupchats, {})
    canaries["run_skills"] = run.run_dir / "skills" / "demo-skill" / "SKILL.md"
    att = run.run_dir / "groupchats" / "attachments"
    canaries["foreign_attachment"] = _canary(att / "secret-channel" / "m-1" / "note.txt")
    canaries["own_attachment"] = _canary(att / "main" / "m-2" / "note.txt")
    # Only the canary file is removed afterwards, never ~/.arr itself (it holds the logins).
    arr_dir = Path.home() / ".arr"
    canaries["arr_home"] = _canary(arr_dir / f"w2-02-canary-{secrets.token_hex(3)}.txt")
    cleanup = [canaries["arr_home"]]

    rules = PermissionRules(sensitive_paths=[str(readdir / ".secret")])
    profiles = [profile_for(a.file.config, run_paths, rules) for a in run.agents.values()]
    engine = PolicyEngine(profiles, rules=rules)
    for aid in run.agents:
        (run_paths.tmp_dir / "home" / aid).mkdir(parents=True, exist_ok=True)
    return Lab(
        outer, wf, run, agent, run_paths, profiles[0], engine, canaries, cleanup, run.agents
    )


def remove_cleanup(lab: Lab) -> None:
    for path in lab.cleanup:
        if path.is_file():
            path.unlink()


def make_inbox(lab: Lab, decider: Any = None, timeout: float = 120) -> ApprovalInbox:
    return ApprovalInbox(lab.engine, decider=decider, timeout=timeout)


# --- live turn budget -------------------------------------------------------------------


def turns_used() -> int:
    if not TURNS.exists():
        return 0
    return sum(1 for line in TURNS.read_text().splitlines() if line.strip())


def check_budget(planned: int) -> None:
    used = turns_used()
    if used + planned > MAX_TURNS:
        sys.exit(f"Turn-Budget: {used} verbraucht + {planned} geplant > {MAX_TURNS}")


def record_turn(script: str, label: str, end: dict[str, Any] | None) -> None:
    TURNS.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "script": script, "label": label}
    if end is not None:
        entry |= {"status": end.get("status"), "usage": end.get("usage")}
    with TURNS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def dump(name: str, data: Any) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    text = data if isinstance(data, str) else json.dumps(data, indent=2, default=str)
    path.write_text(text, encoding="utf-8")
    return path
