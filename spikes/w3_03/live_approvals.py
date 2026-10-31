"""Live probe of the approval inbox (W3-03 §3.8, A10): real models, decisions over the API.

Starts the service in this process on the test workfolder (default `~/arr-m1`, no browser) and
two runs at once, each with 1 × Claude `claude-haiku-5-5` (profile `max`) and 1 × Codex
`gpt-6-luna` effort low (profile `chatgpt-w2-02a`):

- Run 1 „Entscheiden“: both agents install a small package, then call `curl`. Each agent's
  first request is allowed through `decide_approval` after at least 5 minutes; the next one is
  denied. Expected: the agent goes on after both.
- Run 2 „Frist“: both agents call `curl` once and nobody decides. Expected: after 20 minutes
  the request expires, nothing is granted, the agent goes on without it.

Budget (W3-03 §7): 2 of 3 runs, ≤ 0,30 $ each, ≤ 30 min wall time, ≤ 20 model requests per
backend. A watchdog stops the runs when a backend reaches the limit. Real notifications appear
on this Mac; click on them does nothing yet. Decisions go through `decide_approval` with no
click: that is the probe's stand-in for the GUI, not a product path.

    uv run python spikes/w3_03/live_approvals.py [--workfolder ~/arr-m1] [--wait 305] [--port N]

Throwaway code (see spikes/README.md); never imported by `arr`. The token link of the service
is not printed.
"""

import argparse
import asyncio
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

from arr.permissions.types import ApprovalStatus, DecisionAction
from arr.service.api_models import ApprovalEvent, DecisionRequest, RunAction

AGENTS = ("claude-probe", "codex-probe")
MAX_REQUESTS = 20
WALL_LIMIT_S = 29 * 60
ALLOW = DecisionRequest(action=DecisionAction.ALLOW)
DENY = DecisionRequest(
    action=DecisionAction.DENY, comment="Nicht in diesem Test. Arbeite ohne weiter."
)
ASK_HINT = (
    "Du arbeitest in einer Sandbox ohne Netzwerk. Wenn dein Werkzeug es anbietet, bitte für den "
    "Befehl um erhöhte Rechte (escalated permissions) mit einer kurzen Begründung. Warte auf die "
    "Antwort. Wird etwas abgelehnt, versuche es nicht noch einmal und nimm keinen anderen Weg."
)
TASK_DECIDE = (
    "Test der Freigaben. Arbeite genau diese Schritte ab, je ein Befehl, nichts sonst.\n"
    "1. Führe aus: python3 -m pip install --quiet six\n"
    "2. Führe aus: curl -sS -m 10 -o /dev/null -w '%{http_code}' https://example.com\n"
    "3. Schreibe in einem Satz in den Chat main, was bei 1 und 2 herauskam, und melde done.\n"
    + ASK_HINT
)
TASK_EXPIRE = (
    "Test der Freigaben. Arbeite genau diese Schritte ab, je ein Befehl, nichts sonst.\n"
    "1. Führe aus: curl -sS -m 10 -o /dev/null -w '%{http_code}' https://example.org\n"
    "2. Schreibe in einem Satz in den Chat main, was herauskam, und melde done.\n" + ASK_HINT
)


def run_input(name: str, task: str) -> dict[str, Any]:
    return {
        "project": "collatz-mini",
        "name": name,
        "task": task,
        "topology": {"kind": "roundtable", "params": {"channel": "main"}},
        "limits": {"wall_time": "27m", "turns_per_agent": 2, "budget_usd": 0.3},
        "approvals": {"timeout": "20m"},
        "defaults": {
            "provider": "claude",
            "model": "claude-haiku-5-5",
            "auth": "max",
            "effort": "low",
        },
        "agents": [
            {"id": "claude-probe"},
            {
                "id": "codex-probe",
                "provider": "codex",
                "model": "gpt-6-luna",
                "auth": "chatgpt-w2-02a",
                "effort": "low",
            },
        ],
        "groupchats": [
            {
                "name": "main",
                "purpose": "Ergebnis des Freigabe-Tests",
                "members": list(AGENTS),
                "mode": "event",
                "debounce_s": 3,
                "quiet_after_s": 60,
                "max_wakeups_per_agent": 1,
            }
        ],
    }


def say(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


class Probe:
    def __init__(self, api: Any, workfolder: Path, wait_s: float) -> None:
        self.api = api
        self.workfolder = workfolder
        self.wait_s = wait_s
        self.started = time.time()
        self.handled: dict[str, str] = {}
        self.first_done: set[str] = set()
        self.results: list[dict[str, Any]] = []

    def run_dir(self, run_id: str) -> Path:
        return self.workfolder / "runs" / run_id

    def requests(self, run_id: str) -> dict[str, int]:
        """Model requests (usage events) per agent."""
        out = dict.fromkeys(AGENTS, 0)
        for agent in AGENTS:
            path = self.run_dir(run_id) / "agents" / agent / "transcript.jsonl"
            if not path.is_file():
                continue
            for line in path.read_text("utf-8", errors="replace").splitlines():
                try:
                    out[agent] += json.loads(line).get("kind") == "usage"
                except ValueError:
                    continue
        return out

    def turn_ended_ok(self, run_id: str, agent: str) -> list[str]:
        path = self.run_dir(run_id) / "agents" / agent / "transcript.jsonl"
        ends = []
        if path.is_file():
            for line in path.read_text("utf-8", errors="replace").splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("kind") == "turn_end":
                    ends.append(str((record.get("data") or {}).get("status")))
        return ends

    async def decide_run_one(self, run_id: str) -> None:
        for item in await self.api.list_approvals(status=[ApprovalStatus.OPEN], run_id=run_id):
            if item.id in self.handled:
                continue
            agent = item.agent.id
            waited = time.time() - item.created_at
            if agent not in self.first_done:
                if waited < self.wait_s:
                    continue
                request = ALLOW
            else:
                request = DENY
            response = await self.api.decide_approval(item.id, request)
            self.first_done.add(agent)
            verb = request.action.value
            self.handled[item.id] = verb
            line = {
                "run": "1",
                "agent": agent,
                "item": item.id,
                "title": item.title,
                "command": item.command,
                "no_sandbox": item.no_sandbox,
                "count": item.count,
                "action": verb,
                "waited_s": round(waited, 1),
                "decided_by": response.item.decision.decided_by.value,
            }
            self.results.append(line)
            say(f"  entschieden: {json.dumps(line, ensure_ascii=False)}")

    async def follow(self, run_ids: list[str]) -> dict[str, str]:
        sub = self.api.subscribe()
        status: dict[str, str] = {}
        try:
            while True:
                for event in sub.drain():
                    if isinstance(event, ApprovalEvent):
                        item = event.item
                        say(
                            f"  {event.type} {item.id} {item.agent.id if item.agent else ''} "
                            f"({item.status.value}) nach {time.time() - item.created_at:.0f} s: "
                            f"{item.title}"
                        )
                        if event.type == "approval.expired" and item.status.value == "expired":
                            self.results.append(
                                {
                                    "run": "2",
                                    "agent": item.agent.id if item.agent else None,
                                    "item": item.id,
                                    "action": "expired",
                                    "waited_s": round(time.time() - item.created_at, 1),
                                }
                            )
                await self.decide_run_one(run_ids[0])
                for run_id in run_ids:
                    detail = await self.api.get_run(run_id)
                    status[run_id] = detail.run.status.value
                if all(s in ("done", "stopped", "failed", "interrupted") for s in status.values()):
                    return status
                totals = dict.fromkeys(AGENTS, 0)
                for run_id in run_ids:
                    for agent, count in self.requests(run_id).items():
                        totals[agent] += count
                over = max(totals.values()) >= MAX_REQUESTS
                late = time.time() - self.started > WALL_LIMIT_S
                if over or late:
                    say(f"Wachhund: Stopp, Anfragen {totals}")
                    for run_id in run_ids:
                        if not (await self.api.get_run(run_id)).run.status.is_final:
                            await self.api.run_action(run_id, RunAction.STOP)
                    self.started = time.time() + 3600
                await asyncio.sleep(0.5)
        finally:
            sub.close()

    def report(self, run_id: str, label: str, status: str) -> dict[str, Any]:
        folder = self.run_dir(run_id)
        scan = None
        for line in (folder / "orchestrator.jsonl").read_text("utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("kind") == "secret_scan":
                scan = entry.get("data")
        events: list[str] = []
        path = folder / "approvals.jsonl"
        if path.is_file():
            for line in path.read_text("utf-8").splitlines():
                entry = json.loads(line)
                if entry.get("item_id"):
                    events.append(f"{entry['event']}:{str(entry['item_id'])[-4:]}")
        cost = None
        usage = folder / "usage.json"
        if usage.is_file():
            cost = json.loads(usage.read_text("utf-8")).get("total", {}).get("usage", {})
            cost = cost.get("cost_usd")
        info = {
            "label": label,
            "run": run_id,
            "status": status,
            "requests": self.requests(run_id),
            "turn_ends": {a: self.turn_ended_ok(run_id, a) for a in AGENTS},
            "approvals": events,
            "cost_usd": cost,
            "scan_clean": None if scan is None else bool(scan.get("clean")),
            "scan_files": None if scan is None else scan.get("files"),
        }
        say(json.dumps(info, ensure_ascii=False))
        return info

    async def run(self) -> int:
        say("Live-Probe W3-03: starte zwei Läufe parallel …")
        one = await self.api.start_run(run_input("W3-03 Live 1 Entscheiden", TASK_DECIDE))
        two = await self.api.start_run(run_input("W3-03 Live 2 Frist", TASK_EXPIRE))
        say(f"  Lauf 1: {one.run_id}")
        say(f"  Lauf 2: {two.run_id}")
        status = await self.follow([one.run_id, two.run_id])
        await asyncio.sleep(3.0)  # the secret scan runs after the final status
        infos = [
            self.report(one.run_id, "Lauf 1", status[one.run_id]),
            self.report(two.run_id, "Lauf 2", status[two.run_id]),
        ]
        say("Entscheidungen: " + json.dumps(self.results, ensure_ascii=False))
        ok = all(i["scan_clean"] is True for i in infos)
        say(f"Secret-Scans sauber: {ok}")
        return 0 if ok else 1


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser(description="Live-Probe der Freigaben (W3-03 A10)")
    parser.add_argument("--workfolder", default="~/arr-m1")
    parser.add_argument("--wait", type=float, default=305.0)
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
                outcome["code"] = await Probe(get_api(), workfolder, args.wait).run()
            except Exception as exc:  # noqa: BLE001 - a spike reports and ends
                print(f"Sonde fehlgeschlagen: {type(exc).__name__}: {exc}", flush=True)
            finally:
                app.shutdown()

        asyncio.create_task(go())

    app.on_startup(start)

    def report(line: str) -> None:
        if "token=" not in line:
            print(line, flush=True)

    serve(workfolder=workfolder, port=args.port or _free_port(), open_browser=False, report=report)
    return outcome["code"]


if __name__ == "__main__":
    sys.exit(main())
