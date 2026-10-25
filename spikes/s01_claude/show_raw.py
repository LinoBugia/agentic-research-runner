"""Print a condensed view of a raw recording (tmp/runs/<name>.jsonl).

Run: uv run --group spikes python spikes/s01_claude/show_raw.py q01_solo [--full TYPE]
"""

import json
import sys
from pathlib import Path

RUNS = Path(__file__).resolve().parent / "tmp" / "runs"


def main() -> None:
    name = sys.argv[1]
    full = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--full" else None
    for line in (RUNS / f"{name}.jsonl").read_text().splitlines():
        row = json.loads(line)
        msg = row["msg"]
        if not isinstance(msg, dict):
            print(row["dir"], "RAW", str(msg)[:120])
            continue
        kind = msg.get("type")
        sub = msg.get("subtype") or (msg.get("request") or {}).get("subtype") or ""
        if kind == "system" and sub == "thinking_tokens":
            continue
        tag = f"{row['dir']:3} {kind}/{sub}"
        if full and (kind == full or sub == full):
            print(tag, json.dumps(msg, ensure_ascii=False, indent=1)[:4000])
        else:
            print(tag, json.dumps(msg, ensure_ascii=False)[:220])


if __name__ == "__main__":
    main()
