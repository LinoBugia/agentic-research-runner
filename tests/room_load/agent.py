"""Load test helper: one simulated agent (MCP client, no model) with its own room process.

Started by tests/test_room_load.py as a separate OS process. Starts `python -m
arr.room.server` over stdio, waits for the common start signal, probes foreign channels,
posts and reads, drains all channels at the end and writes a JSON report.
See docs/spikes/S4_room.md §1 (same setup as the spike, now against the real server).
"""

import argparse
import asyncio
import json
import random
import re
import sys
import time
from pathlib import Path

from mcp import Client, StdioServerParameters

HEADER = re.compile(r"^\[#(\d+) ", re.MULTILINE)
POSTED = re.compile(r"^Nachricht #(\d+) in #")
SEPARATOR = "\n\n---\nungelesen: "


def wait_for(path: Path) -> None:
    while not path.exists():
        time.sleep(0.005)


async def run(args: argparse.Namespace) -> dict:
    rng = random.Random(args.seed)
    channels = args.channels.split(",")
    signals = Path(args.signal_dir)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "arr.room.server"],
        env={"AGENT_ID": args.agent, "RUN_DIR": args.run_dir, "ARR_LOG_LEVEL": "INFO"},
    )
    latency: dict[str, list[float]] = {"post_message": [], "read_messages": []}
    errors: list[str] = []
    received: dict[str, list[int]] = {c: [] for c in channels}
    posted: list[int] = []
    membership: dict[str, dict[str, bool]] = {}
    footers_ok = True

    async with Client(params, cache=None) as client:

        async def call(name: str, arguments: dict) -> tuple[bool, str]:
            nonlocal footers_ok
            t0 = time.perf_counter()
            res = await client.call_tool(name, arguments)
            latency.setdefault(name, []).append(time.perf_counter() - t0)
            text = res.content[0].text if res.content else ""
            footers_ok = footers_ok and SEPARATOR in text
            return res.is_error, text.split(SEPARATOR)[0]

        async def read(channel: str) -> int:
            is_error, text = await call("read_messages", {"channel": channel, "limit": 50})
            if is_error:
                errors.append(f"read #{channel}: {text}")
                return 0
            ids = [int(i) for i in HEADER.findall(text)]
            received[channel].extend(ids)
            return len(ids)

        (signals / f"{args.agent}.ready").touch()
        await asyncio.to_thread(wait_for, signals / "go")

        for ch in args.foreign.split(","):
            post_error, _ = await call("post_message", {"channel": ch, "body": "Eindringling"})
            read_error, _ = await call("read_messages", {"channel": ch})
            membership[ch] = {"post_rejected": post_error, "read_rejected": read_error}

        t_first = time.time()
        for seq in range(args.count):
            ch = rng.choice(channels)
            body = f"{args.agent}/{seq} " + "x" * 200
            is_error, text = await call("post_message", {"channel": ch, "body": body})
            match = POSTED.match(text)
            if is_error or not match:
                errors.append(f"post #{ch}: {text}")
            else:
                posted.append(int(match.group(1)))
            if rng.random() < 0.5:
                await read(rng.choice(channels))
        t_last = time.time()
        (signals / f"{args.agent}.posted").touch()

        # Keep reading while the others write, then drain every channel.
        done = signals / "all.done"
        while not await asyncio.to_thread(done.exists):
            await read(rng.choice(channels))
            await asyncio.sleep(0.02)
        for ch in channels:
            while await read(ch):
                pass

    return {
        "agent": args.agent,
        "window": [t_first, t_last],
        "latency": latency,
        "errors": errors,
        "received": received,
        "posted": posted,
        "membership": membership,
        "footers_ok": footers_ok,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--agent", required=True)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--channels", required=True)
    p.add_argument("--foreign", required=True)
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--signal-dir", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    report = asyncio.run(run(args))
    Path(args.out).write_text(json.dumps(report), encoding="utf-8")


if __name__ == "__main__":
    main()
