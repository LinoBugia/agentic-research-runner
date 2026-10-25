"""Summarize tmp/usage.jsonl (one line per turn) as a Markdown table.

ThreadTokenUsage.total is cumulative per thread, also across thread_resume, so the
resumed q09 turn is reported as the difference to its start turn.
"""

import json

from common import OUT, USAGE_LOG

FIELDS = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens")


def main() -> None:
    rows = [json.loads(line) for line in USAGE_LOG.read_text().splitlines() if line.strip()]
    print("| # | Skript | Lauf | Modell | Effort | Input | davon Cache | Output | Reasoning |")
    print("|---|---|---|---|---|---:|---:|---:|---:|")
    sums = dict.fromkeys(FIELDS, 0)
    previous = None
    for n, row in enumerate(rows, 1):
        total = dict(row["total"] or {})
        if row["label"].startswith("resume:") and previous is not None:
            total = {k: total[k] - previous["total"][k] for k in FIELDS}
        for k in FIELDS:
            sums[k] += total.get(k, 0)
        cells = " | ".join(f"{total.get(k, 0):,}".replace(",", ".") for k in FIELDS)
        effort = "medium" if "effort=medium" in row["label"] else row["effort"]
        print(f"| {n} | {row['script']} | {row['label']} | {row['model']} | {effort} | {cells} |")
        previous = row
    cells = " | ".join(f"**{sums[k]:,}**".replace(",", ".") for k in FIELDS)
    print(f"| | | **Summe {len(rows)} Turns** | | | {cells} |")

    last_limits = None
    for raw in sorted(OUT.glob("*_raw.jsonl")):
        for line in raw.read_text(encoding="utf-8").splitlines():
            msg = json.loads(line)["msg"]
            if msg.get("method") == "account/rateLimits/updated":
                last_limits = msg["params"]["rateLimits"]["primary"]
    print("\nletztes account/rateLimits/updated (primary):", last_limits)


if __name__ == "__main__":
    main()
