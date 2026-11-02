"""W3-06.2 §1: the Not-Aus in a running run, one short run per backend with a real model.

The agent does nothing but wait in one shell command. While it waits, the Not-Aus blocks its
real profile; stop, finalizer and secret scan are then checked. `profiles.yaml` is restored byte
for byte in any case. The agent reads nothing: the probe of the blocks is `real_home_probe.py`.

Budget: at most 5 requests per backend (total 15 for Claude, 20 for Codex), `budget_usd` 0.30
where the backend knows it, 5 minutes. The run stops one request before the limit. `check` plans
the run without a model. Codex requests are also counted in the rollout, by token totals only.

    uv run python spikes/w3_06/live_probe.py check
    uv run python spikes/w3_06/live_probe.py claude
    uv run python spikes/w3_06/live_probe.py codex
"""

import asyncio
import hashlib
import json
import os
import pwd
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from arr.models import RunInput, RunStatus
from arr.resolve import validate_model
from arr.runs.eventlog import read_events
from arr.runs.launch import plan_run_input
from arr.runs.orchestrator import OrchestratorLog
from arr.runs.prepare import load_run_meta
from arr.runs.secret_scan import SCAN_KIND
from arr.service.real_api import RealApi
from arr.settings import app_dir

HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
WORKFOLDER = HOME / "arr-m1"
BACKENDS = {
    "claude": {"provider": "claude", "model": "claude-haiku-5-5", "auth": "max"},
    "codex": {"provider": "codex", "model": "gpt-6-luna", "auth": "chatgpt-w2-02a"},
}
OUT = Path(__file__).with_name("tmp")
MAX_REQUESTS = 5
TOTAL_LIMIT_S = 280
START_LIMIT_S = 120
STOP_LIMIT_S = 60
WAIT_COMMAND = "sleep 100"

TASK = f"""Dies ist ein Test von arr. Führe genau einmal den Befehl `{WAIT_COMMAND}` aus. Er darf
nicht im Hintergrund laufen. Mach sonst nichts und erkläre nichts.
"""


def keychain_list() -> str:
    done = subprocess.run(
        ["/usr/bin/security", "list-keychains"], capture_output=True, text=True, check=True
    )
    return hashlib.sha256(done.stdout.encode()).hexdigest()[:16]


def run_input(backend: str) -> dict[str, Any]:
    chosen = BACKENDS[backend]
    return {
        "version": 1,
        "name": f"W3-06.2 Not-Aus {backend}",
        "project": "collatz-mini",
        "task": TASK,
        "topology": {"kind": "solo"},
        "limits": {"wall_time": "5m", "budget_usd": 0.30, "turns_per_agent": 1},
        "defaults": {**chosen, "effort": "low"},
        "agents": [{"id": "wait"}],
        "groupchats": [{"name": "main", "members": ["wait"]}],
    }


def events_of(run_dir: Path) -> list[dict[str, Any]]:
    return list(read_events(run_dir / "agents" / "wait" / "transcript.jsonl"))


def requests_of(run_dir: Path) -> int:
    return sum(1 for event in events_of(run_dir) if event.get("kind") == "usage")


def waiting(run_dir: Path) -> bool:
    return any(event.get("kind") == "tool_call" for event in events_of(run_dir))


def rollout_requests(since: float) -> int | None:
    """Model requests of the newest rollout, by new token totals; nothing else is read."""
    home = app_dir() / "auth" / BACKENDS["codex"]["auth"] / "codex_home" / "sessions"
    files = [p for p in home.rglob("rollout-*.jsonl") if p.stat().st_mtime >= since]
    if not files:
        return None
    path = max(files, key=lambda p: p.stat().st_mtime)
    totals = []
    for line in path.read_text(errors="replace").splitlines():
        row = json.loads(line)
        payload = row.get("payload") or {}
        if row.get("type") == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            totals.append((info.get("total_token_usage") or {}).get("total_tokens"))
    return len({total for total in totals if total is not None})


async def until(check: Any, limit_s: float, what: str, guard: Any = None) -> None:
    end = time.monotonic() + limit_s
    while not check():
        if time.monotonic() > end:
            raise TimeoutError(what)
        if guard is not None:
            guard()
        await asyncio.sleep(0.25)


async def live(backend: str, report: dict[str, Any]) -> None:
    auth = BACKENDS[backend]["auth"]
    api = RealApi(WORKFOLDER, base_url="http://127.0.0.1:1")
    began = time.time()
    started = await api.start_run(run_input(backend))
    run_dir = WORKFOLDER / "runs" / started.run_id
    report["run"] = started.run_id

    def guard() -> None:
        if requests_of(run_dir) >= MAX_REQUESTS - 1:
            raise RuntimeError("the request limit is reached")

    def status() -> RunStatus:
        return load_run_meta(run_dir).status

    def scanned() -> bool:
        return any(e["kind"] == SCAN_KIND for e in OrchestratorLog(run_dir).entries())

    try:
        await until(lambda: waiting(run_dir), START_LIMIT_S, "the agent never called a tool", guard)
        await asyncio.sleep(3)
        report["running_before"] = status().value
        stop_began = time.monotonic()
        answer = await api.disable_profile(auth)
        report["not_aus"] = {"ok": answer.ok, "runs": len(answer.runs), "text": answer.message}
        await until(lambda: status().is_final, STOP_LIMIT_S, "the run did not stop")
        report["stop_s"] = round(time.monotonic() - stop_began, 1)
        await until(scanned, 60, "no secret scan")
        scan = [e for e in OrchestratorLog(run_dir).entries() if e["kind"] == SCAN_KIND][-1]
        report["scan_clean"] = scan["data"]["clean"]
        report["status"] = status().value
        report["stopped"] = status() is RunStatus.STOPPED
        report["summary"] = (run_dir / "output" / "summary.md").is_file()
        report["usage_json"] = (run_dir / "usage.json").is_file()
    finally:
        report["requests"] = requests_of(run_dir)
        report["seconds"] = round(time.time() - began)
        if backend == "codex":
            report["requests_rollout"] = rollout_requests(began)
        await api.runs.stop_all()
        await api.close()


def check() -> None:
    """Everything before the first model request: nothing is created, nothing is called."""
    for backend in BACKENDS:
        launch = plan_run_input(validate_model(RunInput, run_input(backend)), workfolder=WORKFOLDER)
        print("geplant:", [(a.id, a.auth, a.model) for a in launch.resolved.agents])
        print("Limits:", launch.resolved.limits.budget_usd, launch.resolved.limits.wall_time)


def main() -> None:
    backend = sys.argv[1]
    if backend == "check":
        check()
        return
    profiles = app_dir() / "profiles.yaml"
    original = profiles.read_bytes()
    keychains = keychain_list()
    report: dict[str, Any] = {"backend": backend}
    try:
        asyncio.run(asyncio.wait_for(live(backend, report), TOTAL_LIMIT_S))
    except Exception as exc:  # noqa: BLE001 - the report says what is known
        report["failed"] = f"{type(exc).__name__}: {exc}"
    finally:
        profiles.write_bytes(original)
    report["restored"] = profiles.read_bytes() == original
    report["keychain_list_same"] = keychain_list() == keychains
    report["left_behind"] = subprocess.run(
        ["/usr/bin/pgrep", "-f", WAIT_COMMAND], capture_output=True, text=True
    ).stdout.count("\n")
    OUT.mkdir(exist_ok=True)
    (OUT / f"{backend}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
