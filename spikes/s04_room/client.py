"""Spike S4: one simulated agent (MCP client, no LLM) talking to its own room server.

Started by load.py as a separate process. Writes a JSON result file at the end.
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

SERVER = Path(__file__).with_name("server.py")
HEADER = re.compile(r"^\[#(\d+) (\S+) · (\S+)\]$", re.MULTILINE)


def parse_ids(text: str) -> list[int]:
    return [int(m.group(1)) for m in HEADER.finditer(text)]


async def run(args: argparse.Namespace) -> dict:
    rng = random.Random(args.seed)
    channels = args.channels.split(",")
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        env={"AGENT_ID": args.agent, "ROOM_DB": args.db, "ROOM_TXN": args.txn},
    )
    lat: dict[str, list[float]] = {"post_message": [], "read_messages": []}
    errors: list[str] = []
    received: dict[str, list[int]] = {c: [] for c in channels}
    membership: dict[str, dict] = {}

    async def call(name: str, arguments: dict) -> tuple[bool, str]:
        t0 = time.perf_counter()
        res = await client.call_tool(name, arguments)
        lat[name].append(time.perf_counter() - t0)
        text = res.content[0].text if res.content else ""
        return res.is_error, text

    async def read(channel: str) -> int:
        is_error, text = await call("read_messages", {"channel": channel, "limit": 50})
        if is_error:
            errors.append(f"read #{channel}: {text}")
            return 0
        ids = parse_ids(text)
        received[channel].extend(ids)
        return len(ids)

    t_start = time.perf_counter()
    async with Client(params, cache=None) as client:
        startup_s = time.perf_counter() - t_start

        # Membership: reading and writing in a foreign channel must be rejected.
        for ch in args.foreign.split(","):
            res_post = await client.call_tool(
                "post_message", {"channel": ch, "body": f"intrusion by {args.agent}"}
            )
            res_read = await client.call_tool("read_messages", {"channel": ch})
            membership[ch] = {
                "post_rejected": res_post.is_error,
                "post_text": res_post.content[0].text,
                "read_rejected": res_read.is_error,
                "read_text": res_read.content[0].text,
            }

        # Load: post N messages to random own channels, read in between.
        t_first = time.time()
        for seq in range(args.count):
            ch = rng.choice(channels)
            is_error, text = await call(
                "post_message", {"channel": ch, "body": f"{args.agent}/{seq} " + "x" * 200}
            )
            if is_error:
                errors.append(f"post #{ch}: {text}")
            if rng.random() < 0.5:
                await read(rng.choice(channels))
        t_last = time.time()
        await asyncio.to_thread(Path(args.signal_dir, f"{args.agent}.posted").touch)

        # Drain: once every writer is done, read all channels until nothing is new.
        done = Path(args.signal_dir, "all.done")
        while not await asyncio.to_thread(done.exists):
            await read(rng.choice(channels))
            await asyncio.sleep(0.02)
        for ch in channels:
            while await read(ch):
                pass

    return {
        "agent": args.agent,
        "startup_s": startup_s,
        "load_window": [t_first, t_last],
        "latency": lat,
        "errors": errors,
        "received": received,
        "membership": membership,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--agent", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--channels", required=True)
    p.add_argument("--foreign", required=True)
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--txn", default="immediate")
    p.add_argument("--signal-dir", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    result = asyncio.run(run(args))
    Path(args.out).write_text(json.dumps(result))


if __name__ == "__main__":
    main()
