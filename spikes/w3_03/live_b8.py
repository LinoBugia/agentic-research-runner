"""Live probe B8 (W3-03.1 point 7): does an allowed Codex escalation run inside the sandbox?

Starts the service in this process on the test workfolder (default `~/arr-m1`, no browser) and
one solo run with 1 × Codex `gpt-6-luna` effort low (profile `chatgpt-w2-02a`). The agent runs
the same harmless check twice in its work folder: `getcwd`, then writing into an own empty
folder that is readable for the agent but outside its write roots.

- Step 1 inside the sandbox, as the comparison.
- Step 2 with escalated rights. Only this exact request is allowed, once, through
  `decide_approval` (the probe's stand-in for the click, not a product path); anything else is
  denied.

The outcome comes from files, not from what the agent says: `b8-*.txt` in its work folder and
the file in the probe folder. The probe folder is removed at the end.

Budget (W3-03.1): 1 run, ≤ 5 Codex requests. The watchdog stops the run at 4 seen requests, one
may still be under way. Claude is not used.

    uv run python spikes/w3_03/live_b8.py [--workfolder ~/arr-m1] [--port N]

Throwaway code (see spikes/README.md); never imported by `arr`. The token link of the service
is not printed.
"""

import argparse
import asyncio
import json
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any

from arr.permissions.types import ApprovalStatus, DecisionAction
from arr.service.api_models import DecisionRequest, RunAction

AGENT = "codex-probe"
STOP_AT = 4
WALL_LIMIT_S = 9 * 60
ALLOW = DecisionRequest(action=DecisionAction.ALLOW)
DENY = DecisionRequest(action=DecisionAction.DENY, comment="Nicht Teil dieser Probe.")


def check(probe: Path, tag: str) -> str:
    """The shell command of a step; its output lands in files of the work folder."""
    return (
        f"""python3 -c "import os; print(os.getcwd())" > b8-{tag}-cwd.txt 2>&1; """
        f"""python3 -c "open('{probe}/{tag}.txt','w').write('x'); print('geschrieben')" """
        f"""> b8-{tag}-write.txt 2>&1"""
    )


def task(probe: Path) -> str:
    return (
        "Test der Sandbox. Arbeite genau diese Schritte ab, je ein Befehl, nichts sonst, ändere "
        "die Befehle nicht und werte die Fehler nicht aus.\n"
        f"1. Führe ohne erhöhte Rechte aus: {check(probe, 'sandbox')}\n"
        "2. Führe denselben Test mit erhöhten Rechten (escalated permissions, "
        "sandbox_permissions: require_escalated) und kurzer Begründung aus: "
        f"{check(probe, 'escalated')}\n"
        "3. Melde done. Wird etwas abgelehnt, versuche es nicht noch einmal."
    )


def run_input(probe: Path) -> dict[str, Any]:
    return {
        "project": "collatz-mini",
        "name": "W3-03.1 Probe B8",
        "task": task(probe),
        "topology": {"kind": "solo"},
        "limits": {"wall_time": "9m", "turns_per_agent": 1, "budget_usd": 0.3},
        "approvals": {"timeout": "5m"},
        "defaults": {
            "provider": "codex",
            "model": "gpt-6-luna",
            "auth": "chatgpt-w2-02a",
            "effort": "low",
        },
        "agents": [{"id": AGENT, "read_dirs": [str(probe)]}],
    }


def say(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def requests(folder: Path) -> int:
    path = folder / "agents" / AGENT / "transcript.jsonl"
    count = 0
    if path.is_file():
        for line in path.read_text("utf-8", errors="replace").splitlines():
            try:
                count += json.loads(line).get("kind") == "usage"
            except ValueError:
                continue
    return count


async def follow(api: Any, run_id: str, folder: Path, probe: Path) -> tuple[str, list[dict]]:
    started = time.time()
    stopped = False
    handled: dict[str, dict[str, Any]] = {}
    while True:
        for item in await api.list_approvals(status=[ApprovalStatus.OPEN], run_id=run_id):
            if item.id in handled:
                continue
            wanted = item.no_sandbox and "escalated.txt" in (item.command or "")
            await api.decide_approval(item.id, ALLOW if wanted else DENY)
            handled[item.id] = {
                "command": item.command,
                "no_sandbox": item.no_sandbox,
                "risk": item.risk.level.value if item.risk else None,
                "scope_only_once": item.no_sandbox,
                "action": "allow" if wanted else "deny",
            }
            say(f"  entschieden: {json.dumps(handled[item.id], ensure_ascii=False)}")
        status = (await api.get_run(run_id)).run.status
        if status.is_final:
            return status.value, list(handled.values())
        if not stopped and (requests(folder) >= STOP_AT or time.time() - started > WALL_LIMIT_S):
            say(f"Wachhund: Stopp bei {requests(folder)} Anfragen")
            await api.run_action(run_id, RunAction.STOP)
            stopped = True
        await asyncio.sleep(0.5)


def report(folder: Path, probe: Path, status: str, decided: list[dict]) -> dict[str, Any]:
    work = folder / "agents" / AGENT / "work"
    files = {}
    for name in sorted(work.glob("b8-*.txt")):
        files[name.name] = name.read_text("utf-8", errors="replace")[-400:]
    raw = folder / "agents" / AGENT / "raw.jsonl"
    scan = None
    for line in (folder / "orchestrator.jsonl").read_text("utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("kind") == "secret_scan":
            scan = entry.get("data")
    return {
        "run": folder.name,
        "status": status,
        "codex_requests": requests(folder),
        "decisions": decided,
        "probe_folder": sorted(p.name for p in probe.iterdir()),
        "files": files,
        "raw_mentions_command": raw.read_text("utf-8", errors="replace").count("b8-escalated-write")
        if raw.is_file()
        else None,
        "scan_clean": None if scan is None else bool(scan.get("clean")),
    }


async def probe_run(api: Any, workfolder: Path) -> int:
    probe = workfolder / f"b8-probe-{int(time.time())}"
    probe.mkdir()
    try:
        started = await api.start_run(run_input(probe))
        folder = workfolder / "runs" / started.run_id
        say(f"Lauf: {started.run_id}")
        status, decided = await follow(api, started.run_id, folder, probe)
        await asyncio.sleep(3.0)  # the secret scan runs after the final status
        say(json.dumps(report(folder, probe, status, decided), ensure_ascii=False, indent=2))
    finally:
        shutil.rmtree(probe)
        say(f"Probe-Ordner entfernt: {not probe.exists()}")
    return 0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser(description="Live-Probe B8 (W3-03.1)")
    parser.add_argument("--workfolder", default="~/arr-m1")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    workfolder = Path(args.workfolder).expanduser()

    from nicegui import app

    from arr.log import setup_logging
    from arr.service.gui.app import get_api
    from arr.service.serve import serve

    setup_logging("INFO")
    outcome = {"code": 1}

    async def start() -> None:
        async def go() -> None:
            await asyncio.sleep(2.0)  # the watcher must be running before the first run
            try:
                outcome["code"] = await probe_run(get_api(), workfolder)
            except Exception as exc:  # noqa: BLE001 - a spike reports and ends
                print(f"Sonde fehlgeschlagen: {type(exc).__name__}: {exc}", flush=True)
            finally:
                app.shutdown()

        asyncio.create_task(go())

    app.on_startup(start)

    def show(line: str) -> None:
        if "token=" not in line:
            print(line, flush=True)

    serve(workfolder=workfolder, port=args.port or _free_port(), open_browser=False, report=show)
    return outcome["code"]


if __name__ == "__main__":
    sys.exit(main())
