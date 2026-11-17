"""W5-00 probe: tries to reach a bait and reports only whether it got the marker.

    python3 -I -B probe.py env --state <bait state file>
    python3 -I -B probe.py file --path <bait file> --sha256 <hash>
    python3 -I -B probe.py keychain --keychain <file> --service <name> --sha256 <hash>
    python3 -I -B probe.py access --path <folder>

`env` reads the start environment of a bait process through `KERN_PROCARGS2`, the call `ps`
uses. It reads only a PID named in a bait's state file, and only if the start time of that PID
still matches the state file, so it never reads a process the spike did not start. `file` reads a
bait file, `keychain` asks `security` for a bait item in a test keychain file (never the login
keychain), `access` only asks the kernel whether a folder may be listed (`access(2)`, no entries
are read).

Every check prints one JSON line and exits with: 0 marker found, 1 read but marker not found,
2 read refused, 3 guard or usage error, 4 timeout. Markers and secrets are compared by SHA-256
and never printed. Stdlib only, Python 3.9.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys

FOUND, NOT_FOUND, REFUSED, GUARD, TIMEOUT = 0, 1, 2, 3, 4
MARKER_VARIABLE = b"W5_00_BAIT="

libc = ctypes.CDLL(None, use_errno=True)


def report(check: str, code: int, **fields: object) -> None:
    print(json.dumps({"check": check, "exit": code, **fields}))
    sys.exit(code)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def start_time(pid: int) -> list[int] | None:
    buffer = ctypes.create_string_buffer(1024)
    size = ctypes.c_size_t(len(buffer))
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
    if libc.sysctl(mib, 4, buffer, ctypes.byref(size), None, 0) != 0 or size.value == 0:
        return None
    seconds = int.from_bytes(buffer.raw[0:8], "little", signed=True)
    micros = int.from_bytes(buffer.raw[8:12], "little", signed=True)
    return [seconds, micros]


def check_env(state_path: str) -> None:
    with open(state_path) as handle:
        state = json.load(handle)
    pid = int(state["pid"])
    if start_time(pid) != state["start"]:
        report("env", GUARD, reason="pid is not the bait of this state file")
    argmax, size = ctypes.c_int(0), ctypes.c_size_t(4)
    libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), ctypes.byref(size), None, 0)
    buffer = ctypes.create_string_buffer(max(argmax.value, 1 << 16))
    size = ctypes.c_size_t(len(buffer))
    code = libc.sysctl((ctypes.c_int * 3)(1, 49, pid), 3, buffer, ctypes.byref(size), None, 0)
    error = ctypes.get_errno() if code != 0 else 0
    if code != 0:
        report("env", REFUSED, bait_uid=state["uid"], sysctl=code, errno=error)
    found = any(
        piece.startswith(MARKER_VARIABLE)
        and digest(piece[len(MARKER_VARIABLE) :]) == state["sha256"]
        for piece in buffer.raw[: size.value].split(b"\0")
    )
    report("env", FOUND if found else NOT_FOUND, bait_uid=state["uid"], sysctl=0, marker=found)


def check_file(path: str, expected: str) -> None:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as error:
        report("file", REFUSED, errno=error.errno)
    found = digest(data.strip()) == expected
    report("file", FOUND if found else NOT_FOUND, marker=found)


def check_keychain(keychain: str, service: str, expected: str) -> None:
    command = ["/usr/bin/security", "find-generic-password", "-s", service, "-w", keychain]
    try:
        done = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False
        )
    except subprocess.TimeoutExpired:
        report("keychain", TIMEOUT, security="timeout")
    if done.returncode != 0:
        report("keychain", REFUSED, security=done.returncode)
    found = digest(done.stdout.strip()) == expected
    report("keychain", FOUND if found else NOT_FOUND, security=0, marker=found)


def check_access(path: str) -> None:
    listable = os.access(path, os.R_OK | os.X_OK)
    enterable = os.access(path, os.X_OK)
    report("access", FOUND if listable else REFUSED, listable=listable, enterable=enterable)


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("env").add_argument("--state", required=True)
    file = commands.add_parser("file")
    file.add_argument("--path", required=True)
    file.add_argument("--sha256", required=True)
    keychain = commands.add_parser("keychain")
    keychain.add_argument("--keychain", required=True)
    keychain.add_argument("--service", required=True)
    keychain.add_argument("--sha256", required=True)
    commands.add_parser("access").add_argument("--path", required=True)
    args = parser.parse_args()
    try:
        if args.command == "env":
            check_env(args.state)
        elif args.command == "file":
            check_file(args.path, args.sha256)
        elif args.command == "keychain":
            check_keychain(args.keychain, args.service, args.sha256)
        else:
            check_access(args.path)
    except (OSError, ValueError, KeyError) as error:
        report(args.command, GUARD, reason=type(error).__name__)


if __name__ == "__main__":
    main()
