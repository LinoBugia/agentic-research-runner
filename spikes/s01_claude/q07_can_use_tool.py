"""Question 7: permission_mode="default" + scoped allow rules + can_use_tool.

Without sandbox: Bash(python3 *) is allowed by rule, curl is grey zone and reaches the
callback. The callback waits 2 minutes, then denies with a reason and an alternative.

Run: uv run --group spikes python spikes/s01_claude/q07_can_use_tool.py
"""

import asyncio
import json
import time

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
)
from common import (
    AGENT_ENV,
    MAX_BUDGET_USD,
    RUNS,
    SONNET,
    Recorder,
    check_turn_budget,
    clean_env,
    run_turn,
    short,
    workdir,
)

SCRIPT = "q07_can_use_tool"
WAIT_S = 120
DENY = (
    "Abgelehnt: Netzwerkzugriff auf diese Domain ist nicht freigegeben (nicht in der Vollmacht). "
    "Arbeite ohne Netz weiter oder frage im Gruppenchat nach."
)
events: list[dict] = []
t0 = time.monotonic()


def log(kind: str, **data) -> None:
    data = {"t": round(time.monotonic() - t0, 1), "kind": kind, **data}
    events.append(data)
    print(f"[{kind} t={data['t']}s] {short({k: v for k, v in data.items() if k != 't'}, 300)}")


async def can_use_tool(tool_name, tool_input, context):
    log("can_use_tool", tool=tool_name, input=tool_input, reason=context.decision_reason)
    if tool_name == "Bash" and "curl" in tool_input.get("command", ""):
        await asyncio.sleep(WAIT_S)
        log("deny_after_wait", waited_s=WAIT_S)
        return PermissionResultDeny(message=DENY)
    if tool_name in ("Write", "Edit"):
        return PermissionResultAllow()
    return PermissionResultDeny(message="Abgelehnt: nicht freigegeben.")


async def on_notification(input_data, tool_use_id, context):
    log("Notification", type=input_data.get("notification_type"), msg=input_data.get("message"))
    return {}


async def main() -> None:
    print(f"clean_env entfernt: {clean_env()}")
    check_turn_budget(6)
    Recorder(SCRIPT)
    options = ClaudeAgentOptions(
        model=SONNET,
        cwd=workdir("q07_agent"),
        tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        allowed_tools=["Read", "Glob", "Grep", "Bash(python3 *)"],
        permission_mode="default",
        can_use_tool=can_use_tool,
        hooks={"Notification": [HookMatcher(hooks=[on_notification])]},
        setting_sources=[],
        strict_mcp_config=True,
        env=AGENT_ENV,
        thinking={"type": "adaptive", "display": "summarized"},
        effort="low",
        max_budget_usd=MAX_BUDGET_USD,
        max_turns=5,
    )
    prompt = (
        "Zwei kurze Aufgaben:\n"
        "1. Führe aus: python3 -c 'print(6*7)'\n"
        "2. Lade mit curl -sS -m 5 https://example.com herunter und nenne den <title> der Seite."
    )
    async with ClaudeSDKClient(options=options) as client:
        t = await run_turn(client, prompt, script=SCRIPT, label="grey_zone", model=SONNET)

    print("\n=== Auswertung ===")
    for e in events:
        rest = {k: v for k, v in e.items() if k not in ("t", "kind")}
        print(f"  t={e['t']}s {e['kind']}: {short(rest, 250)}")
    print(f"tool_calls: {[(c['name'], short(c['input'], 100)) for c in t['tool_calls']]}")
    print(f"tool_results: {[(r['is_error'], short(r['content'], 160)) for r in t['tool_results']]}")
    print(f"Antwort: {short(t['text'][-1] if t['text'] else '', 800)}")
    (RUNS / f"{SCRIPT}_events.json").write_text(
        json.dumps(events, indent=2, default=str, ensure_ascii=False)
    )


if __name__ == "__main__":
    asyncio.run(main())
