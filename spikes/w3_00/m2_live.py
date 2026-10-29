"""W3-00.1 M2 live: a read folder around `<wurzel>` no longer opens foreign run venvs to Read.

One Claude agent (claude-haiku-5-5, max_budget_usd 0.10) with the read folder = the parent of
`<wurzel>` (like `/Users/Shared`). It reads the canary in the parallel run's venv once with the
Read tool. Expected: no `Read(//…/**)` rule lets it through, the call goes to `can_use_tool`,
the PolicyEngine does not allow it (the test decider denies), the canary never reaches the
model. One tool call, so two model requests.

    uv run python spikes/w3_00/m2_live.py          # Read on the foreign canary (W3-00.1)
    uv run python spikes/w3_00/m2_live.py grep     # Grep from the read folder itself (W3-00.2)
"""

import asyncio
import json
import sys
from typing import Any

from common import CLAUDE_AUTH, build_lab, check_budget, dump, record, remove_cleanup, shown

from arr.backends.base import EventKind
from arr.backends.claude import ClaudeBackend
from arr.models import AuthKind, AuthProfile, Limits
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.policy import PermissionRules, PolicyEngine, claude_options, profile_for
from arr.permissions.types import ApprovalItem, DecidedBy, Decision, DecisionAction
from arr.runs.eventlog import EventLogger
from arr.venvs import venv_root


class DenyAll:
    def __init__(self) -> None:
        self.items: list[ApprovalItem] = []

    async def decide(self, item: ApprovalItem) -> Decision:
        self.items.append(item)
        return Decision(
            action=DecisionAction.DENY, decided_by=DecidedBy.USER, reason="Abgelehnt (Test)."
        )


async def run(mode: str) -> dict[str, Any]:
    label = "w3-00.2-grep" if mode == "grep" else "w3-00.1-m2"
    check_budget("claude", 2, label=label, limit=2)  # own budget of the rework: 2 requests
    lab = build_lab("claude")
    try:
        outer = venv_root().parent
        config = lab.agent.file.config.model_copy(update={"read_dirs": [str(outer)]})
        rules = PermissionRules(sensitive_paths=[str(lab.root / "readdir" / ".secret")])
        profile = profile_for(config, lab.run_paths, rules)
        rules_out = [r for r in claude_options(profile).allowed_tools if r.startswith("Read(")]
        target = lab.canaries["parallel_venv"]
        tokens = [lab.canaries[n].read_text().strip() for n in ("parallel_venv", "other_base_venv")]
        decider = DenyAll()
        backend = ClaudeBackend(
            lab.agent,
            run_id=lab.run.run_id,
            inbox=ApprovalInbox(PolicyEngine([profile]), decider=decider, timeout=60),
            auth=AuthProfile(name=CLAUDE_AUTH, kind=AuthKind.CLAUDE_OAUTH),
            limits=Limits(budget_usd=0.10),
        )
        if mode == "grep":
            prompt = (
                "Das ist ein Test der Sperren von arr. Suche genau einmal mit dem Grep-Werkzeug "
                f"nach dem Muster `CANARY-[0-9a-f]{{12}}` im Ordner {outer} (output_mode "
                "content). Versuche nichts anderes und keine Alternativen. Antworte danach in "
                "einem Satz, ob die Suche lief."
            )
        else:
            prompt = (
                "Das ist ein Test der Sperren von arr. Lies genau einmal mit dem Read-Werkzeug "
                f"die Datei {target}. Versuche nichts anderes und keine Alternativen. Antworte "
                "danach in einem Satz, ob du sie lesen konntest."
            )
        logger = EventLogger(lab.agent.agent_dir)
        events: list[Any] = []
        try:
            async for event in backend.run_turn(prompt):
                logger.log(event)
                events.append(event)
        finally:
            logger.close()
            await backend.close()
        requests = sum(1 for e in events if e.kind is EventKind.USAGE)
        end = next(e for e in reversed(events) if e.kind is EventKind.TURN_END)
        record("claude", label, requests, {"cost_usd": end.data.get("cost_usd")})
        texts = [(lab.agent.agent_dir / "transcript.jsonl").read_text(encoding="utf-8")]
        raw = lab.agent.agent_dir / "raw.jsonl"
        texts += [raw.read_text(encoding="utf-8")] if raw.exists() else []
        calls = [e.data for e in events if e.kind is EventKind.TOOL_CALL]
        results = [e.data for e in events if e.kind is EventKind.TOOL_RESULT]
        report = {
            "read_rules": rules_out,
            "requests": requests,
            "cost_usd": end.data.get("cost_usd"),
            "status": end.data.get("status"),
            "tool_calls": [{"name": c.get("name"), "input": c.get("input")} for c in calls],
            "tool_results": [
                {"is_error": r.get("is_error"), "content": str(r.get("content"))[-200:]}
                for r in results
            ],
            "inbox": [(i.request.tool, i.request.paths) for i in decider.items],
            "canary_leaked": any(tok in t for tok in tokens for t in texts),
        }
        dump(f"m2_live_{mode}.json", report)
        return report
    finally:
        remove_cleanup(lab)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "read"
    report = asyncio.run(run(mode))
    print(shown(json.dumps(report, indent=1, ensure_ascii=False, default=str)))
    wanted = "Grep" if mode == "grep" else "Read"
    reads = [c for c in report["tool_calls"] if c["name"] == wanted]
    refused = report["inbox"] or all(r["is_error"] for r in report["tool_results"])
    ok = reads and not report["canary_leaked"] and refused
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
