"""Live probe of W3-02 (§3.7, A8): a small mixed run through `RealApi`, no clicks.

Starts the service in this process on the test workfolder (default `~/arr-m1`), checks that the
preview of `resolve_run` is what lands in `run.yaml`, and starts a run in `event` mode with one
Claude agent (`claude-haiku-5-5`, profile `max`) and one Codex agent (`gpt-6-luna`, effort low,
profile `chatgpt-w2-02a`). After the first message in the channel it sends a question of the
user to the Codex agent, follows the delivery state and waits for the answer. When the run is
`done` and its secret scan is clean it slims the run (first `dry_run`) and checks that the
working copies and the run venv are gone while transcripts and the chat stay readable.

    uv run python spikes/w3_02/live_probe.py [--workfolder ~/arr-m1] [--port N]

Budget: one run, ≤ 0,30 $, ≤ 10 min, 3 turns per agent, 2 wake-ups. Throwaway code (see
spikes/README.md); never imported by `arr`. The token link of the service is not printed.
"""

import argparse
import asyncio
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

from arr.room.types import DeliveryStatus, MessageKind
from arr.runs.prepare import run_venv_location
from arr.service.api_models import (
    ChannelMessageEvent,
    PostMessageRequest,
    RunAction,
)

CHANNEL = "main"
QUESTION = "Kurze Frage vom User: Wie groß ist s(8)? Antworte in einem Satz in #main."
TASK = (
    "Kurze Diskussion: Passt die Ausgabe von `python3 code/collatz.py` zur Vermutung "
    "s(2^k) = k aus notes.md? Der frager fragt, der antworter antwortet, beide melden danach "
    "done. Keine Installationen."
)
RUN_INPUT: dict[str, Any] = {
    "project": "collatz-mini",
    "name": "W3-02 Live-Probe",
    "task": TASK,
    "topology": {"kind": "roundtable", "params": {"channel": CHANNEL}},
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
            "name": CHANNEL,
            "purpose": "Frage und Antwort zur Collatz-Vermutung für Zweierpotenzen",
            "members": ["frager", "antworter"],
            "mode": "event",
            "debounce_s": 3,
            "quiet_after_s": 90,
            "max_wakeups_per_agent": 2,
        }
    ],
}


def say(text: str) -> None:
    print(text, flush=True)


def counts(run_dir: Path) -> dict[str, Any]:
    """Requests (usage events), cost and the secret scan of a run folder."""
    requests = 0
    for transcript in run_dir.glob("agents/*/transcript.jsonl"):
        for line in transcript.read_text("utf-8", errors="replace").splitlines():
            try:
                requests += json.loads(line).get("kind") == "usage"
            except ValueError:
                continue
    usage_file = run_dir / "usage.json"
    total = {}
    if usage_file.is_file():
        total = json.loads(usage_file.read_text("utf-8")).get("total", {}).get("usage", {})
    scan = None
    for line in (run_dir / "orchestrator.jsonl").read_text("utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("kind") == "secret_scan":
            scan = entry.get("data")
    return {
        "requests": requests,
        "cost_usd": total.get("cost_usd"),
        "scan_clean": None if scan is None else bool(scan.get("clean")),
        "scan_files": None if scan is None else scan.get("files"),
    }


async def probe(api: Any, workfolder: Path) -> int:
    resolved = await api.resolve_run(RUN_INPUT)
    if resolved.errors:
        say(f"Prüfung fehlgeschlagen: {resolved.errors}")
        return 1
    started = await api.start_run(RUN_INPUT)
    run_id = started.run_id
    run_dir = workfolder / "runs" / run_id
    say(f"Run gestartet: {run_id}")
    sub = api.subscribe(run_id)
    begin = time.time()
    asked_id: int | None = None
    asked_at = 0.0
    states: list[tuple[str, float]] = []
    answer_after: float | None = None
    status = "?"
    while True:
        for event in sub.drain():
            if not isinstance(event, ChannelMessageEvent):
                continue
            msg = event.message
            if asked_id is None and msg.sender != "orchestrator":
                request = PostMessageRequest(body=QUESTION, kind=MessageKind.QUESTION)
                request.to = ["antworter"]
                asked = await api.post_message(run_id, CHANNEL, request)
                asked_id, asked_at = asked.id, time.time()
                say(f"  Frage #{asked_id} an antworter gesendet ({asked_at - begin:.0f} s)")
            elif msg.id == asked_id and msg.delivery is not None:
                state = msg.delivery.status
                if not states or states[-1][0] != state.value:
                    states.append((state.value, time.time() - asked_at))
                    say(f"  Zustellung: {state.value} ({states[-1][1]:.1f} s)")
            elif asked_id is not None and msg.id > asked_id and msg.sender == "antworter":
                if answer_after is None:
                    answer_after = time.time() - asked_at
                    say(f"  Antwort #{msg.id} nach {answer_after:.1f} s: {msg.body[:120]}")
        detail = await api.get_run(run_id)
        status = detail.run.status.value
        if detail.run.status.is_final:
            break
        if time.time() - begin > 11 * 60:
            say("  Zeitlimit der Sonde erreicht, Run wird gestoppt.")
            await api.run_action(run_id, RunAction.STOP)
        await asyncio.sleep(0.2)
    sub.close()
    say(f"Run beendet: {status} nach {time.time() - begin:.0f} s")
    for _ in range(100):  # the secret scan runs after the final status, under the lock
        try:
            preview = await api.run_action(run_id, RunAction.SLIM, dry_run=True)
            break
        except Exception:  # noqa: BLE001 - a spike waits for the lock to fall
            await asyncio.sleep(0.2)
    same_yaml = (run_dir / "run.yaml").read_text("utf-8") == resolved.resolved_run_yaml
    numbers = counts(run_dir)
    venv = run_venv_location(run_dir)
    work = sorted(p.relative_to(run_dir).as_posix() for p in run_dir.glob("agents/*/work"))
    say(f"Vorschau gleich run.yaml: {same_yaml}")
    say(f"Vorschau Verschlanken: {preview.message!r}, {preview.freed_bytes} Bytes")
    say(f"Vorher: {work}, base {(run_dir / 'base').is_dir()}, venv {venv and venv.is_dir()}")
    result = await api.run_action(run_id, RunAction.SLIM)
    say(f"Verschlankt: {result.message}")
    gone = not list(run_dir.glob("agents/*/work")) and not (run_dir / "base").exists()
    venv_gone = venv is not None and not venv.exists()
    agents = await api.list_agents(run_id)
    lines = sum([(await api.get_transcript(run_id, a.id)).total for a in agents])
    messages = (await api.get_messages(run_id, CHANNEL, limit=200)).items
    summary = (await api.get_run(run_id)).run
    say(f"Nachher: Kopien weg {gone}, venv weg {venv_gone}, verschlankt {summary.slimmed}")
    say(f"Lesbar: {lines} Transkript-Einträge, {len(messages)} Nachrichten")
    say(f"Zählung: {numbers}")
    delivered = any(s in (DeliveryStatus.HINT_MID_TURN, DeliveryStatus.WOKEN) for s, _t in states)
    ok = (
        status == "done"
        and same_yaml
        and delivered
        and answer_after is not None
        and gone
        and venv_gone
        and summary.slimmed
        and lines > 0
        and numbers["scan_clean"] is True
    )
    say(f"Ergebnis: {'A8 erfüllt' if ok else 'A8 NICHT erfüllt'}")
    return 0 if ok else 1


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser(description="Live-Probe W3-02 (A8)")
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
            await asyncio.sleep(2.0)  # the watcher must be running before the run
            try:
                outcome["code"] = await probe(get_api(), workfolder)
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
