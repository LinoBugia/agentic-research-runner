"""W5-00 bait process: carries a random marker in its environment and waits.

    python3 -I -B bait.py --state <file> [--ttl <seconds>]

The program generates the marker, then replaces itself with a copy that has a clean environment
holding only the marker and `PATH`, so the bait never carries anything else. That copy runs in
the background in a session of its own, writes a state file and sleeps until `--ttl` runs out
or it gets SIGTERM. The state file holds the PID, the start time (to guard against PID reuse)
and the SHA-256 of the marker, never the marker itself. It removes its state file at the end.

Prints one JSON line with the PID. Stdlib only; runs on the system Python 3.9 of the command line
tools, so another user can start it without access to the repo.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import secrets
import signal
import sys
import time

MARKER_VARIABLE = "W5_00_BAIT"
HOLD_FLAG = "--hold"


def start_time(pid: int) -> list[int]:
    """Return `[seconds, microseconds]` of the start of `pid` (`KERN_PROC_PID`, no environment)."""
    libc = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(1024)
    size = ctypes.c_size_t(len(buffer))
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
    if libc.sysctl(mib, 4, buffer, ctypes.byref(size), None, 0) != 0 or size.value == 0:
        raise OSError(ctypes.get_errno(), "kinfo_proc")
    # kinfo_proc starts with extern_proc, whose first field is the start time (struct timeval).
    seconds = int.from_bytes(buffer.raw[0:8], "little", signed=True)
    micros = int.from_bytes(buffer.raw[8:12], "little", signed=True)
    return [seconds, micros]


def hold(state: str, ttl: int) -> None:
    marker = os.environ[MARKER_VARIABLE]
    record = {
        "pid": os.getpid(),
        "uid": os.getuid(),
        "start": start_time(os.getpid()),
        "sha256": hashlib.sha256(marker.encode()).hexdigest(),
    }
    del marker
    temporary = f"{state}.{os.getpid()}.tmp"
    with open(temporary, "w") as handle:
        json.dump(record, handle)
    os.chmod(temporary, 0o644)
    os.replace(temporary, state)

    def finish(*_: object) -> None:
        try:
            with open(state) as handle:
                if json.load(handle).get("pid") == os.getpid():
                    os.unlink(state)
        except (OSError, ValueError):
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    time.sleep(ttl)
    finish()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--ttl", type=int, default=1800)
    parser.add_argument(HOLD_FLAG, action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    state = os.path.abspath(args.state)
    if args.hold:
        hold(state, args.ttl)
        return
    if os.path.exists(state):
        os.unlink(state)
    environment = {MARKER_VARIABLE: secrets.token_hex(16), "PATH": "/usr/bin:/bin"}
    if os.fork() == 0:
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        os.execve(
            sys.executable,
            [sys.executable, "-I", "-B", os.path.abspath(__file__), "--state", state]
            + ["--ttl", str(args.ttl), HOLD_FLAG],
            environment,
        )
    for _ in range(100):
        if os.path.exists(state):
            with open(state) as handle:
                record = json.load(handle)
            print(json.dumps({"bait": "env", "pid": record["pid"], "uid": record["uid"]}))
            return
        time.sleep(0.05)
    print(json.dumps({"bait": "env", "error": "no state file"}))
    sys.exit(3)


if __name__ == "__main__":
    main()
