"""Live smoke test of `CodexBackend` (W2-02 §3 step 6, A9), one turn with gpt-6-luna, low.

In one turn the agent: writes and runs a Python script in its working folder (no approval),
tries to write into the original project (denied, the reason arrives by steer), tries
`pip install` (a request in the inbox; the test decider denies it). A chat hint is steered
in after the first tool call. Events go to tmp/out/smoke_events.jsonl.

    uv run python spikes/w2_02_codex/smoke_live.py
"""

import asyncio
import json
from typing import Any

from common import OUT, auth_profile, build_lab, check_budget, record_turn, remove_cleanup

from arr.backends.base import EventKind
from arr.backends.codex import CodexBackend
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.types import ApprovalItem, DecidedBy, Decision, DecisionAction

HINT = "📨 Hinweis aus #main: Bitte schreibe am Ende auch das Wort ZEBRA in deine Antwort."


class DenyDecider:
    """Test decider: denies every request with a reason and records it."""

    def __init__(self) -> None:
        self.items: list[ApprovalItem] = []

    async def decide(self, item: ApprovalItem) -> Decision | None:
        self.items.append(item)
        return Decision(
            action=DecisionAction.DENY,
            decided_by=DecidedBy.USER,
            reason="Test: Installationen sind in diesem Run nicht erlaubt.",
        )


def prompt(original: str) -> str:
    return "\n".join(
        [
            "Das ist ein Test. Arbeite die Schritte der Reihe nach ab, je ein Befehl:",
            "1. Schreibe `quad.py`, das 17*23 ausgibt, und führe es mit python3 aus.",
            f"2. Hänge mit `echo` eine Zeile an die Datei {original} an.",
            "3. Führe `pip install tinyrecord` aus.",
            "Wenn ein Befehl an der Sandbox scheitert, fordere ihn einmal mit kurzer Begründung "
            "außerhalb der Sandbox an. Wird das abgelehnt, mache mit dem nächsten Schritt "
            "weiter. Melde am Ende kurz jeden Schritt und die Begründung jeder Ablehnung.",
        ]
    )


async def main() -> None:
    check_budget(1)
    lab = build_lab()
    try:
        decider = DenyDecider()
        inbox = ApprovalInbox(lab.engine, decider=decider, timeout=120)
        backend = CodexBackend(lab.agent, run_id=lab.run.run_id, inbox=inbox, auth=auth_profile())
        await backend.start()
        original = lab.workfolder / "projects" / "demo" / "notes.md"
        events: list[dict[str, Any]] = []
        hinted: bool | None = None
        end = None
        async for event in backend.run_turn(prompt(str(original))):
            events.append({**event.to_record(), "raw": event.raw})
            if event.kind is EventKind.TOOL_CALL and hinted is None:
                hinted = await backend.deliver_hint(HINT)
            if event.kind is EventKind.TURN_END:
                end = event.data
        await backend.close()
        record_turn("smoke_live.py", "smoke", end)
        path = OUT / "smoke_events.jsonl"
        OUT.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(e, default=str) for e in events) + "\n")
        summary = {
            "hint_delivered": hinted,
            "inbox": [
                {"command": i.request.command if i.request else None, "status": str(i.status)}
                for i in inbox.items()
            ],
            "decider_asked": len(decider.items),
            "original_unchanged": original.read_text() == "Some notes.\n",
            "kinds": [e["kind"] for e in events],
            "tools": [
                (e["data"].get("input", {}).get("command") or e["data"].get("name"))
                for e in events
                if e["kind"] == "tool_call"
            ],
            "final": next(
                (e["data"]["text"] for e in reversed(events) if e["kind"] == "text"), None
            ),
            "turn_end": end,
            "session": backend.session_ref(),
        }
        (OUT / "smoke_summary.json").write_text(json.dumps(summary, indent=2, default=str))
        print(json.dumps(summary, indent=2, default=str))
    finally:
        remove_cleanup(lab)


if __name__ == "__main__":
    asyncio.run(main())
