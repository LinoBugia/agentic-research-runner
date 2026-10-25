"""Summarise tmp/usage.jsonl as a Markdown table (per query and per script).

Run: uv run --group spikes python spikes/s01_claude/usage_report.py
"""

import json
from collections import defaultdict

from common import LEDGER, MAX_TOTAL_TURNS


def main() -> None:
    rows = [json.loads(line) for line in LEDGER.read_text().splitlines()]
    print(
        "| Skript | Lauf | Modell | Turns | Input | Cache-Write | Cache-Read | Output | Kosten $ |"
    )
    print("|---|---|---|---:|---:|---:|---:|---:|---:|")
    per_script = defaultdict(lambda: [0, 0.0])
    for r in rows:
        u = r.get("usage") or {}
        print(
            f"| {r['script']} | {r['label']} | {r['model']} | {r['num_turns']} "
            f"| {u.get('input_tokens', 0)} | {u.get('cache_creation_input_tokens', 0)} "
            f"| {u.get('cache_read_input_tokens', 0)} | {u.get('output_tokens', 0)} "
            f"| {r['cost_delta_usd']:.4f} |"
        )
        per_script[r["script"]][0] += r["num_turns"] or 0
        per_script[r["script"]][1] += r["cost_delta_usd"] or 0.0
    print("\n| Skript | Turns | Kosten $ |\n|---|---:|---:|")
    for script, (turns, cost) in per_script.items():
        print(f"| {script} | {turns} | {cost:.4f} |")
    total_turns = sum(t for t, _ in per_script.values())
    total_cost = sum(c for _, c in per_script.values())
    print(f"| **Summe** | **{total_turns}** / {MAX_TOTAL_TURNS} | **{total_cost:.4f}** |")


if __name__ == "__main__":
    main()
