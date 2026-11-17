"""The unified diff of two versions, made in a process of its own (H2 of W4-03.1, docs/10 §4.8).

Moved here from `arr.runs.topologies.review` in W4b-01 (`review` imports it from here and
does the same as before), so the room can write `diff.patch` of a named submission the same
way. The texts go to the process as data on stdin; nothing is run from them, and `python -I`
loads nothing from the current folder or the environment. arr ends the process after a time
limit.
"""

import asyncio
import contextlib
import difflib
import json
import subprocess
import sys
from collections.abc import Callable, Sequence

__all__ = ["DIFF_PROGRAM", "PAIRS_PROGRAM", "diff_elsewhere", "diff_pairs", "unified_diff"]


def unified_diff(before: str, after: str, old_name: str, new_name: str) -> str:
    """The unified diff of two texts, in this process (`DIFF_PROGRAM` does the same apart)."""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=old_name,
        tofile=new_name,
    )
    return "".join(lines)


DIFF_PROGRAM = (
    "import difflib, json, sys\n"
    "d = json.load(sys.stdin)\n"
    "sys.stdout.write(''.join(difflib.unified_diff(d['a'].splitlines(True), "
    "d['b'].splitlines(True), fromfile=d['n'][0], tofile=d['n'][1])))\n"
)
"""The diff as a program of its own: the texts come as data on stdin, nothing is run from
them; `python -I` loads nothing from the current folder or the environment."""

PAIRS_PROGRAM = (
    "import difflib, json, sys\n"
    "for a, b, n, m in json.load(sys.stdin):\n"
    "    for line in difflib.unified_diff(a.splitlines(True), b.splitlines(True), "
    "fromfile=n, tofile=m, lineterm='\\n'):\n"
    "        if not line.endswith('\\n'):\n"
    "            line += '\\n\\\\ No newline at end of file\\n'\n"
    "        sys.stdout.write(line)\n"
)
"""`DIFF_PROGRAM` for several pairs `[before, after, old_name, new_name]` in one process; a
last line without line break is marked as `git diff` does."""


async def diff_elsewhere(
    before: str,
    after: str,
    names: tuple[str, str],
    *,
    timeout_s: float,
    stopped: Callable[[], bool],
) -> str | None:
    """The unified diff in a process of its own (H2): the event loop stays free, and arr ends
    the process (by its handle) after `timeout_s` or as soon as `stopped()` is true; then None."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        DIFF_PROGRAM,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    data = json.dumps({"a": before, "b": after, "n": list(names)}).encode("utf-8")
    talk = asyncio.ensure_future(process.communicate(data))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    try:
        while not talk.done():
            left = deadline - loop.time()
            if left <= 0 or stopped():
                return None
            await asyncio.wait({talk}, timeout=min(0.1, left))
        out, _ = talk.result()
        return out.decode("utf-8", errors="replace") if process.returncode == 0 else None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if not talk.done():
            talk.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await talk


def diff_pairs(pairs: Sequence[tuple[str, str, str, str]], *, timeout_s: float) -> str | None:
    """The unified diffs of `(before, after, old_name, new_name)` pairs, one after the other,
    in one process of its own (for the synchronous room server); None after `timeout_s` (the
    process is killed by its handle) or on any failure of the process."""
    data = json.dumps([list(p) for p in pairs]).encode("utf-8")
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", PAIRS_PROGRAM],
            input=data,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return done.stdout.decode("utf-8", errors="replace") if done.returncode == 0 else None
