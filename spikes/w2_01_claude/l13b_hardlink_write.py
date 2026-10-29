"""W2-01 L13b: hard link to a readable but read-only file (read dir, base venv) and write
through it. Same fail-safe setup as e19_read_allowlist.py (callback denies everything).

Run (live): uv run python spikes/w2_01_claude/l13b_hardlink_write.py
"""

from __future__ import annotations

import asyncio
import json

from claude_agent_sdk import ClaudeSDKClient, PermissionResultDeny, ResultMessage
from setup_run import (
    BASE_VENV,
    MAX_BUDGET_USD,
    TMP,
    Recorder,
    check_turn_budget,
    ledger,
    make_run,
    profile,
)

from arr.auth import CLAUDE_SWITCHES, clean_process_env
from arr.backends.claude_options import build_options, remove_tmp_link, tmp_link

SCRIPT = "l13b_hardlink_write"


async def deny_all(tool_name, tool_input, context):
    return PermissionResultDeny(message="Abgelehnt (Test).")


async def main() -> None:
    clean_process_env()
    layout = make_run("l13b", skills=False)
    victim_lit = layout.literature / "paper.tex"
    victim_venv = BASE_VENV / "arr_l13b_victim.txt"
    victim_venv.write_text("unchanged\n")
    lines = [
        'r() { printf \'%s|%s\\n\' "$1" "$2"; }',
        f"ln {victim_lit} hl_lit.tex 2>/dev/null; r 'ln read-dir file' $?",
        "echo x >> hl_lit.tex 2>/dev/null; r 'append via link' $?",
        f"ln {victim_venv} hl_venv.txt 2>/dev/null; r 'ln base-venv file' $?",
        "echo x >> hl_venv.txt 2>/dev/null; r 'append via link' $?",
        f"python3 -c \"import os; os.link('{victim_lit}', 'hl_py.tex')\" 2>/dev/null; "
        "r 'os.link read-dir file' $?",
        f"cp -l {victim_lit} hl_cp.tex 2>/dev/null; r 'cp -l read-dir file' $?",
    ]
    (layout.work / "probe.sh").write_text("\n".join(lines) + "\n")
    prof = profile(layout)
    link = tmp_link(prof.tmp_dir, prof.agent_id)
    options = build_options(
        layout.agent,
        prof,
        env=dict(CLAUDE_SWITCHES),
        can_use_tool=deny_all,
        hooks=None,
        max_budget_usd=MAX_BUDGET_USD,
        tmpdir=link,
    )
    options.max_turns = 3
    check_turn_budget(3)
    record = TMP / "runs" / f"{SCRIPT}.jsonl"
    Recorder(record)
    result = None
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(
                "Führe genau einen Bash-Aufruf aus: `bash probe.sh`. "
                "Antworte danach nur mit der Ausgabe."
            )
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    result = msg
                    print(msg.result)
    finally:
        remove_tmp_link(link, prof.tmp_dir)
    rows = [json.loads(x) for x in record.read_text().splitlines()]
    ids = {
        r["msg"]["message"]["id"]
        for r in rows
        if r["dir"] == "in" and r["msg"].get("type") == "assistant"
    }
    ledger(SCRIPT, "l13b", result.num_turns, result.total_cost_usd, result.usage, len(ids))
    print(f"[requests] {len(ids)} cost={result.total_cost_usd}")
    print(
        "read-dir file:",
        victim_lit.read_text().strip(),
        "| base-venv file:",
        victim_venv.read_text().strip(),
    )
    print("links in work:", sorted(p.name for p in layout.work.glob("hl_*")))
    victim_venv.unlink()


if __name__ == "__main__":
    asyncio.run(main())
