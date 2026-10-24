"""Spike S6: append transcript-like events to a JSONL file at a fixed rate.

uv run python spikes/s06_service/writer.py <file> [--rate 50] [--seconds 60]
"""

import argparse
import json
import random
import time
from pathlib import Path

KINDS = ["assistant_text", "thinking", "tool_call", "tool_result", "room_message"]
AGENTS = ["explorer-1", "explorer-2", "critic", "judge"]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("file")
    p.add_argument("--rate", type=float, default=50)
    p.add_argument("--seconds", type=float, default=60)
    args = p.parse_args()

    rng = random.Random(0)
    path = Path(args.file)
    total = int(args.rate * args.seconds)
    t0 = time.perf_counter()
    with path.open("a", encoding="utf-8") as fh:
        for seq in range(total):
            event = {
                "seq": seq,
                "ts": time.time(),
                "agent": rng.choice(AGENTS),
                "type": rng.choice(KINDS),
                "text": "Lemma: "
                + " ".join(rng.choice(["n", "k", "≤", "2^n", "mod 3"]) for _ in range(30)),
            }
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            fh.flush()  # like the transcript writer: flushed after every event
            next_at = t0 + (seq + 1) / args.rate
            time.sleep(max(0.0, next_at - time.perf_counter()))
    print(json.dumps({"events": total, "seconds": round(time.perf_counter() - t0, 2)}))


if __name__ == "__main__":
    main()
