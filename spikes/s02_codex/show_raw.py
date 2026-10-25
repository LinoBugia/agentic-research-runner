"""Print a RawTap log, sanitized and shortened.

Usage: uv run --group spikes python spikes/s02_codex/show_raw.py tmp/out/q03_raw.jsonl \
           [--skip item/agentMessage/delta,...] [--only method,...] [--width 700]
"""

import argparse
import json
from pathlib import Path

from common import SPIKE_DIR, sanitize

DEFAULT_SKIP = (
    "item/agentMessage/delta,item/reasoning/summaryTextDelta,item/commandExecution/outputDelta"
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--skip", default=DEFAULT_SKIP)
    ap.add_argument("--only", default="")
    ap.add_argument("--width", type=int, default=700)
    args = ap.parse_args()
    path = Path(args.path)
    if not path.is_absolute():
        path = SPIKE_DIR / path
    skip = set(filter(None, args.skip.split(",")))
    only = set(filter(None, args.only.split(",")))
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = sanitize(json.loads(line))
        msg = entry["msg"]
        method = msg.get("method", "<response>")
        if method in skip or (only and method not in only):
            continue
        text = json.dumps(msg, ensure_ascii=False)
        print(f"{entry['dir']:3} {entry['t']:8.3f} {text[: args.width]}\n")


if __name__ == "__main__":
    main()
