"""Load test helper: external writer (orchestrator and user) directly on `room.sqlite`.

Started by tests/test_room_load.py as a separate OS process. Waits for the common start
signal and then writes through `RoomStore.post` (no MCP) until every agent has finished
posting, so its write phase overlaps the agents' one. See docs/spikes/S4_room.md §1.
"""

import argparse
import json
import random
import time
from pathlib import Path

from arr.room.store import RoomStore
from arr.room.types import MessageVia


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--channels", required=True)
    p.add_argument("--agents", required=True)
    p.add_argument("--min-count", type=int, default=50)
    p.add_argument("--signal-dir", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()

    rng = random.Random(99)
    store = RoomStore(Path(args.run_dir))
    channels = args.channels.split(",")
    signals = Path(args.signal_dir)
    agents = args.agents.split(",")
    while not (signals / "go").exists():
        time.sleep(0.005)

    written: list[int] = []
    latency: list[float] = []
    errors: list[str] = []
    seq = 0
    while seq < args.min_count or not all((signals / f"{a}.posted").exists() for a in agents):
        channel = rng.choice(channels)
        t0 = time.perf_counter()
        try:
            if seq % 2:
                msg = store.post(channel, f"user/{seq}", sender="user", via=MessageVia.GUI)
            else:
                msg = store.post(channel, f"orchestrator/{seq}")
            written.append(msg.id)
        except Exception as exc:  # noqa: BLE001 - the test counts every failure
            errors.append(repr(exc))
        latency.append(time.perf_counter() - t0)
        seq += 1
        time.sleep(rng.uniform(0.002, 0.008))

    report = {"written": written, "errors": errors, "latency": latency}
    Path(args.out).write_text(json.dumps(report), encoding="utf-8")


if __name__ == "__main__":
    main()
