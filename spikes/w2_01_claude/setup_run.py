"""Throwaway helpers for the W2-01 live tests: a real run folder below the home directory.

The layout is built with `prepare_run`, so `agent.json`, role, context and the generated
plugin are exactly what the adapter sees later. Everything lives in `spikes/w2_01_claude/tmp/`
(ignored by git). Canary files carry random content; real sensitive paths are only checked
by exit code.

Never imported by arr.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from arr.models import GroupChatConfig, RunInput, Toolchain, WorkfolderSettings
from arr.permissions.policy import PermissionRules, RunPaths, profile_for
from arr.resolve import resolve_run
from arr.room.store import RoomStore
from arr.runs.prepare import PreparedAgent, PreparedRun, prepare_run
from arr.workfolder import init_workfolder

SPIKE = Path(__file__).resolve().parent
TMP = SPIKE / "tmp"
BASE_VENV = TMP / "basevenv"
LEDGER = TMP / "usage.jsonl"
HAIKU = "claude-haiku-5-5"
MAX_TOTAL_TURNS = 30
MAX_BUDGET_USD = 0.5


@dataclass
class Layout:
    root: Path
    run: PreparedRun
    agent: PreparedAgent
    literature: Path
    outside: Path
    tokens: dict[str, str] = field(default_factory=dict)

    @property
    def run_dir(self) -> Path:
        return self.run.run_dir

    @property
    def work(self) -> Path:
        return self.agent.work_dir


def token(prefix: str = "CANARY") -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_run(name: str, *, skills: bool = True) -> Layout:
    """Fresh workfolder + run with agent `e1` (Haiku) in channels `main` and `beweis`."""
    root = TMP / name
    if root.exists():
        shutil.rmtree(root)
    wf = init_workfolder(root / "wf")
    literature = root / "literatur"
    outside = root / "outside"
    tokens: dict[str, str] = {}

    def canary(key: str, path: Path) -> Path:
        tokens[key] = token()
        return _write(path, tokens[key] + "\n")

    _write(wf / "projects" / "e19" / "CLAUDE.md", "# Projekt e19\n\nTestprojekt für W2-01.\n")
    _write(wf / "projects" / "e19" / "notes.md", "PROBE-OK-notes\n")
    _write(
        wf / "skills" / "probe" / "SKILL.md",
        "---\nname: probe\ndescription: Liefert das Probe-Codewort für W2-01.\n"
        "metadata:\n  arr-audience: research\n---\n\n# Probe\n\n"
        "Das Codewort lautet PROBE-OK-skill. Prüfskript: `scripts/check.py`.\n",
    )
    _write(wf / "skills" / "probe" / "scripts" / "check.py", "print('PROBE-OK-script')\n")
    canary("other_project", wf / "projects" / "other" / "canary.txt")
    canary("wf_arr", wf / ".arr" / "canary.txt")
    _write(literature / "paper.tex", "PROBE-OK-paper\n")
    canary("read_dir_secret", literature / "geheim" / "canary.txt")
    for key in ("outside_cat", "outside_hl_bash", "outside_hl_py", "outside_symlink"):
        canary(key, outside / f"{key}.txt")
    _write(outside / "sed_target.txt", "unchanged\n")

    settings = WorkfolderSettings(
        toolchain=Toolchain(python=str(BASE_VENV)),
        permissions={"sensitive_paths": [str(literature / "geheim")]},
    )
    run_input = RunInput.model_validate(
        {
            "project": "e19",
            "task": "W2-01 Live-Test",
            "topology": {"kind": "solo"},
            "defaults": {"provider": "claude", "model": HAIKU, "auth": "max", "effort": "low"},
            "agents": [
                {
                    "id": "e1",
                    "skills": ["probe"] if skills else [],
                    "read_dirs": [str(literature)],
                },
                {"id": "e2"},
            ],
            "groupchats": [
                {"name": "main", "members": ["e1", "e2"]},
                {"name": "beweis", "members": ["e1"]},
                {"name": "kritik", "members": ["e2"]},
            ],
        }
    )
    resolved = resolve_run(run_input, None, settings)
    run = prepare_run(wf, run_input, resolved, settings)
    agent = run.agents["e1"]
    run_dir = run.run_dir

    # What the RunManager (W2-03) does before the agents start: run venv over the base venv,
    # room with channels; and some content in output/ and the attachments (layout docs/06 §6).
    subprocess.run(
        [str(BASE_VENV / "bin" / "python"), "-m", "venv", str(run_dir / ".venv")], check=True
    )
    site = next((run_dir / ".venv" / "lib").glob("python3*/site-packages"))
    base_site = next((BASE_VENV / "lib").glob("python3*/site-packages"))
    _write(site / "_arr_base.pth", f"{base_site}\n")
    store = RoomStore.create(run_dir)
    store.setup_channels([GroupChatConfig(**gc.model_dump()) for gc in resolved.groupchats])
    _write(run_dir / "output" / "summary.md", "PROBE-OK-output\n")
    attachments = run_dir / "groupchats" / "attachments"
    _write(attachments / "beweis" / "7" / "lemma.txt", "PROBE-OK-attachment\n")
    canary("foreign_attachment", attachments / "kritik" / "8" / "notiz.txt")
    return Layout(root, run, agent, literature, outside, tokens)


def profile(layout: Layout):
    settings_rules = PermissionRules(sensitive_paths=[str(layout.literature / "geheim")])
    paths = RunPaths(
        layout.run.run_dir.parent.parent, layout.run_dir, Toolchain(python=str(BASE_VENV))
    )
    return profile_for(layout.agent.file.config, paths, settings_rules)


# --------------------------------------------------------------------------------------------
# Recording and budget
# --------------------------------------------------------------------------------------------


class Recorder:
    """Tees the raw JSON lines between SDK and CLI into a JSONL file (as in spike S1)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        self.t0 = time.time()
        recorder = self
        original_read = SubprocessCLITransport.read_messages
        original_write = SubprocessCLITransport.write

        async def read_messages(self):  # type: ignore[no-untyped-def]
            async for data in original_read(self):
                recorder.log("in", data)
                yield data

        async def write(self, data: str) -> None:  # type: ignore[no-untyped-def]
            for line in data.splitlines():
                try:
                    recorder.log("out", json.loads(line))
                except json.JSONDecodeError:
                    recorder.log("out", line)
            await original_write(self, data)

        SubprocessCLITransport.read_messages = read_messages  # type: ignore[method-assign]
        SubprocessCLITransport.write = write  # type: ignore[method-assign]

    def log(self, direction: str, payload: Any) -> None:
        row = {"dir": direction, "t": round(time.time() - self.t0, 3), "msg": payload}
        with self.path.open("a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def turns_used() -> int:
    """Live turns used so far: model requests (Lead decision 28.10.: not `num_turns`)."""
    if not LEDGER.exists():
        return 0
    return sum(json.loads(line)["requests"] for line in LEDGER.read_text().splitlines())


def check_turn_budget(planned: int) -> None:
    used = turns_used()
    if used + planned > MAX_TOTAL_TURNS:
        raise SystemExit(f"Turn-Limit: {used} verbraucht + {planned} geplant > {MAX_TOTAL_TURNS}.")


def ledger(
    script: str, label: str, num_turns: int, cost: float | None, usage: Any, requests: int
) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    row = {"script": script, "label": label, "requests": requests, "num_turns": num_turns}
    row |= {"cost_usd": cost, "usage": usage}
    with LEDGER.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


HOME = str(Path.home())


def scrub_text(text: str) -> str:
    return text.replace(HOME, "~").replace(HOME.replace("/", "-"), "-~")


def env_names() -> list[str]:
    return sorted(os.environ)
