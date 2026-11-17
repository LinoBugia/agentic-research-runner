"""W5-00 stage B, round 2: a launchd service as arr-agent, outside the user's login session.

    python3 -I -B stage_b2.py prepare   # normal user: folder b2/, plist, unlock test keychain K2
    python3 -I -B stage_b2.py daemon    # started by launchd as arr-agent (UserName in the plist)
    python3 -I -B stage_b2.py client    # normal user: hands a secret to the service over a socket

The service records which session it lives in and which services of the login session it can
look up, asks `security` for the bait item in the test keychain K2 (the question is whether a
password window appears on the user's screen, as it did under sudo), and then serves a Unix
socket for a few minutes. It accepts only connections whose peer is the normal user
(`getpeereid`), reads the secret from a descriptor sent with `SCM_RIGHTS` and answers yes/no by
hash. A child of its own (arr-agent) tries the same socket as the counter-test and must be turned
away. Nothing secret is printed; results go to `b2/daemon.txt`. Python 3.9 stdlib only.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path

SHARED = Path("/Users/Shared/arr-w5-00")
B2 = SHARED / "b2"
SOCKET = B2 / "sock" / "arr.sock"
RESULTS = B2 / "daemon.txt"
LABEL = "local.arr.w5-00-probe"
PYTHON = "/Library/Developer/CommandLineTools/usr/bin/python3"
SERVE_SECONDS = 900

libc = ctypes.CDLL(None, use_errno=True)


def digest(data: bytes) -> str:
    return hashlib.sha256(data.strip()).hexdigest()


def say(label: str, **fields: object) -> None:
    line = json.dumps({"label": label, "uid": os.getuid(), **fields})
    print(line, flush=True)
    if os.getuid() != json.loads((SHARED / "targets.json").read_text()).get("owner_uid", -1):
        with open(RESULTS, "a") as handle:
            handle.write(line + "\n")


def peer_uid(connection: socket.socket) -> int:
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    if libc.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        return -1
    return uid.value


# --- prepare (normal user) -------------------------------------------------------------------


def prepare() -> None:
    B2.mkdir(exist_ok=True)
    B2.chmod(0o1777)
    for name in ("bait.py", "probe.py", "stage_a.py", "stage_b.py", "stage_b2.py"):
        source = Path(__file__).resolve().parent / name
        (SHARED / "bin" / name).write_bytes(source.read_bytes())
        (SHARED / "bin" / name).chmod(0o644)
    targets = json.loads((SHARED / "targets.json").read_text())
    targets["owner_uid"] = os.getuid()
    (SHARED / "targets.json").write_text(json.dumps(targets))
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>UserName</key><string>arr-agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>{PYTHON}</string><string>-I</string><string>-B</string>
    <string>{SHARED / "bin" / "stage_b2.py"}</string><string>daemon</string>
  </array>
  <key>WorkingDirectory</key><string>{SHARED}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
</dict>
</plist>
"""
    (SHARED / "bin" / f"{LABEL}.plist").write_text(plist)
    (SHARED / "bin" / f"{LABEL}.plist").chmod(0o644)
    # Unlock the test keychains again, so K2 is in the same state as in stage A.
    subprocess.run(
        [PYTHON, "-I", "-B", str(Path(__file__).resolve().parent / "stage_a.py"), "counter"],
        capture_output=True,
        check=False,
    )
    print(json.dumps({"prepared": str(B2.name), "plist": f"{LABEL}.plist"}))


# --- daemon (arr-agent, started by launchd) --------------------------------------------------


def daemon(dry: bool = False) -> None:
    """The service; `dry` lets the owner rehearse it without the keychain probe."""
    targets = json.loads((SHARED / "targets.json").read_text())
    normal_uid = targets["owner_uid"]
    if os.getuid() == normal_uid and not dry:
        raise SystemExit("daemon must run as arr-agent")
    if not dry:
        RESULTS.write_text("")
    say("whoami", groups=os.getgroups(), home=os.environ.get("HOME", "") == "/Users/arr-agent")
    done = subprocess.run(["/bin/launchctl", "managername"], capture_output=True, text=True)
    say("launchctl managername", value=done.stdout.strip())
    port = ctypes.c_uint.in_dll(libc, "bootstrap_port")
    libc.bootstrap_look_up.argtypes = [
        ctypes.c_uint,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint),
    ]
    for service in (
        "com.apple.pasteboard.1",
        "com.apple.windowserver.active",
        "com.apple.coreservices.launchservicesd",
        "com.apple.tccd",
    ):
        found = ctypes.c_uint(0)
        code = libc.bootstrap_look_up(port, service.encode(), ctypes.byref(found))
        say(f"mach lookup {service}", reachable=code == 0, code=code)

    # The window question: the same keychain probe that showed a window under sudo.
    for label, arguments in targets["targets"]:
        if label == "keychain:file-in-shared-0644" and not dry:
            started = time.monotonic()
            done = subprocess.run(
                [PYTHON, "-I", "-B", str(SHARED / "bin" / "probe.py"), *arguments],
                capture_output=True,
                text=True,
                check=False,
            )
            result = json.loads(done.stdout.strip().splitlines()[-1])
            say("keychain K2 from launchd", seconds=round(time.monotonic() - started, 1), **result)

    # Point 7: the secret over a socket, only from the normal user.
    SOCKET.parent.mkdir(exist_ok=True)
    SOCKET.parent.chmod(0o755)
    if SOCKET.exists():
        SOCKET.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET))
    SOCKET.chmod(0o666)
    server.listen(4)
    server.settimeout(5)
    say("socket ready")
    own_client = subprocess.Popen(
        [PYTHON, "-I", "-B", str(SHARED / "bin" / "stage_b2.py"), "client", "--quiet"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    deadline = time.monotonic() + SERVE_SECONDS
    served_normal = False
    while time.monotonic() < deadline and not served_normal:
        try:
            connection, _ = server.accept()
        except OSError:  # socket.timeout is an OSError in 3.9, not yet TimeoutError
            continue
        with connection:
            peer = peer_uid(connection)
            if peer != normal_uid:
                say("connection refused by peer check", peer_is_agent=peer == os.getuid())
                continue
            message, fds, _, _ = socket.recv_fds(connection, 1024, 4)
            expected = json.loads(message.decode())["sha256"]
            data = b""
            for fd in fds:
                with os.fdopen(fd, "rb") as handle:
                    data = handle.read()
            found = bool(fds) and digest(data) == expected
            say("secret received over socket from the normal user", fds=len(fds), marker=found)
            connection.sendall(json.dumps({"marker": found}).encode())
            served_normal = True
    own_client.wait(timeout=30)
    say("own counter client (arr-agent)", answer=(own_client.stdout.read() or "").strip())
    server.close()
    SOCKET.unlink()
    say("daemon done")


# --- client ----------------------------------------------------------------------------------


def client(quiet: bool) -> None:
    secret = secrets.token_hex(16)
    read_end, write_end = os.pipe()
    os.write(write_end, secret.encode())
    os.close(write_end)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(20)
            connection.connect(str(SOCKET))
            payload = json.dumps({"sha256": digest(secret.encode())}).encode()
            socket.send_fds(connection, [payload], [read_end])
            answer = connection.recv(1024).decode() or "connection closed without answer"
    except OSError as error:
        answer = f"error errno {error.errno}"
    finally:
        os.close(read_end)
    if quiet:
        print(answer)
    else:
        print(json.dumps({"label": "client (normal user)", "uid": os.getuid(), "answer": answer}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "daemon", "daemon-dry", "client"])
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command in ("daemon", "daemon-dry"):
        daemon(dry=args.command == "daemon-dry")
    else:
        client(args.quiet)


if __name__ == "__main__":
    main()
