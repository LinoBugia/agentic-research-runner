"""Live probe of the service (W3-01 §3.8, A10): real models through `RealApi`, no clicks.

Starts the service in this process on the test workfolder (default `~/arr-m1`), then, once
the watcher runs, starts a small mixed run through `RealApi.start_run` (1 × Claude
`claude-haiku-5-5` via profile `max`, 1 × Codex `gpt-6-luna` effort low via profile
`chatgpt-w2-02a`, topology `roundtable` in `event` mode with the roles of the M1 wake-up
probe), follows it over `subscribe` until it is over and measures the live latency of the
first five transcript and channel events (time of arrival minus the time stamp the backend
wrote). A second, identical run is stopped through `run_action(stop)` as soon as its first
agent works. At the end both run folders' secret scans are checked and a German summary is
printed. Budget per run: ≤ 0,30 $, ≤ 10 min, 3 turns per agent, 2 wake-ups.

    uv run python spikes/w3_01/live_service.py [--workfolder ~/arr-m1] [--port N]

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

from arr.service.api_models import (
    AgentTranscriptEvent,
    ChannelMessageEvent,
    LiveEvent,
    RunAction,
    RunStatusEvent,
)

LATENCY_SAMPLES = 5
TASK = (
    "Kurze Diskussion: Passt die Ausgabe von `python3 code/collatz.py` zur Vermutung "
    "s(2^k) = k aus notes.md? Der frager fragt, der antworter antwortet, beide melden danach "
    "done. Keine Installationen."
)


def run_input(name: str) -> dict[str, Any]:
    return {
        "project": "collatz-mini",
        "name": name,
        "task": TASK,
        "topology": {"kind": "roundtable", "params": {"channel": "main"}},
        "limits": {"wall_time": "10m", "turns_per_agent": 3, "budget_usd": 0.3},
        "defaults": {
            "provider": "claude",
            "model": "claude-haiku-5-5",
            "auth": "max",
            "effort": "low",
        },
        "agents": [
            {"id": "frager", "role": "roles/frager.md"},
            {
                "id": "antworter",
                "provider": "codex",
                "model": "gpt-6-luna",
                "auth": "chatgpt-w2-02a",
                "effort": "low",
                "role": "roles/antworter.md",
            },
        ],
        "groupchats": [
            {
                "name": "main",
                "purpose": "Frage und Antwort zur Collatz-Vermutung für Zweierpotenzen",
                "members": ["frager", "antworter"],
                "mode": "event",
                "debounce_s": 3,
                "quiet_after_s": 90,
                "max_wakeups_per_agent": 2,
            }
        ],
    }


class Probe:
    def __init__(self, api: Any, workfolder: Path) -> None:
        self.api = api
        self.workfolder = workfolder

    @staticmethod
    def say(text: str) -> None:
        print(text, flush=True)

    async def follow(
        self, run_id: str, *, stop_at_first_event: bool = False
    ) -> tuple[str, list[float], float]:
        """Follow a run until it is over; returns status, latencies and the duration."""
        sub = self.api.subscribe(run_id, agents={"frager", "antworter"})
        started = time.time()
        latencies: list[float] = []
        stopped = False
        status = "?"
        try:
            while True:
                events: list[LiveEvent] = sub.drain()
                now = time.time()
                for event in events:
                    if isinstance(event, AgentTranscriptEvent):
                        ts = event.event.ts
                        if ts >= started and len(latencies) < LATENCY_SAMPLES:
                            latencies.append(now - ts)
                        if stop_at_first_event and not stopped:
                            stopped = True
                            result = await self.api.run_action(run_id, RunAction.STOP)
                            self.say(f"  Stopp mitten im Lauf: {result.message}")
                    elif isinstance(event, ChannelMessageEvent):
                        ts = event.message.ts
                        if ts >= started and len(latencies) < LATENCY_SAMPLES:
                            latencies.append(now - ts)
                    elif isinstance(event, RunStatusEvent):
                        status = event.run.status.value
                        if event.run.status.is_final:
                            return status, latencies, time.time() - started
                detail = await self.api.get_run(run_id)
                if detail.run.status.is_final:
                    return detail.run.status.value, latencies, time.time() - started
                if time.time() - started > 12 * 60:
                    self.say("  Zeitlimit der Sonde erreicht, Run wird gestoppt.")
                    await self.api.run_action(run_id, RunAction.STOP)
                await asyncio.sleep(0.2)
        finally:
            sub.close()

    def counts(self, run_id: str) -> dict[str, Any]:
        """Requests (usage events), turns, cost and the secret scan of a run folder."""
        run_dir = self.workfolder / "runs" / run_id
        requests = 0
        turns = 0
        for transcript in run_dir.glob("agents/*/transcript.jsonl"):
            for line in transcript.read_text("utf-8", errors="replace").splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("kind") == "usage":
                    requests += 1
                elif record.get("kind") == "turn_end":
                    turns += 1
        cost = kind = None
        usage_file = run_dir / "usage.json"
        if usage_file.is_file():
            total = json.loads(usage_file.read_text("utf-8")).get("total", {}).get("usage", {})
            cost, kind = total.get("cost_usd"), total.get("cost_kind")
        scan = None
        log = run_dir / "orchestrator.jsonl"
        if log.is_file():
            for line in log.read_text("utf-8", errors="replace").splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("kind") == "secret_scan":
                    scan = entry.get("data")
        return {
            "requests": requests,
            "turns": turns,
            "cost_usd": cost,
            "cost_kind": kind,
            "scan_clean": None if scan is None else bool(scan.get("clean")),
            "scan_files": None if scan is None else scan.get("files"),
        }

    async def run(self) -> int:
        self.say("Live-Probe W3-01: Service läuft, starte Lauf 1 (roundtable, Claude + Codex) …")
        first = await self.api.start_run(run_input("W3-01 Live-Probe 1"))
        self.say(f"  Lauf 1: {first.run_id}")
        status1, latencies1, duration1 = await self.follow(first.run_id)
        self.say(f"  Lauf 1 beendet: {status1} nach {duration1:.0f} s")
        self.say("Starte Lauf 2 und stoppe ihn mitten im Lauf …")
        second = await self.api.start_run(run_input("W3-01 Live-Probe 2 (Stopp)"))
        self.say(f"  Lauf 2: {second.run_id}")
        status2, latencies2, duration2 = await self.follow(second.run_id, stop_at_first_event=True)
        self.say(f"  Lauf 2 beendet: {status2} nach {duration2:.0f} s")
        await asyncio.sleep(2.0)  # the secret scan runs after the final status
        c1, c2 = self.counts(first.run_id), self.counts(second.run_id)
        self.say("")
        self.say("Zusammenfassung:")
        for label, run_id, status, lat, dur, counts in [
            ("Lauf 1", first.run_id, status1, latencies1, duration1, c1),
            ("Lauf 2", second.run_id, status2, latencies2, duration2, c2),
        ]:
            self.say(
                f"  {label} {run_id}: Status {status}, Dauer {dur:.0f} s, "
                f"Modell-Anfragen (usage-Events) {counts['requests']}, Turns {counts['turns']}, "
                f"Kosten {counts['cost_usd']} $ ({counts['cost_kind']}), "
                f"Secret-Scan sauber: {counts['scan_clean']} ({counts['scan_files']} Dateien)"
            )
            shown = ", ".join(f"{v:.2f}" for v in lat) or "keine Stichprobe"
            self.say(f"    Live-Latenzen (s): {shown}")
        ok = (
            status1 == "done"
            and status2 == "stopped"
            and len(latencies1) >= LATENCY_SAMPLES
            and max(latencies1) <= 1.0
            and c1["scan_clean"] is True
            and c2["scan_clean"] is True
        )
        self.say(f"Ergebnis: {'A10 erfüllt' if ok else 'A10 NICHT erfüllt'}")
        return 0 if ok else 1


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser(description="Live-Probe des Service (W3-01 A10)")
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
                outcome["code"] = await Probe(get_api(), workfolder).run()
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
