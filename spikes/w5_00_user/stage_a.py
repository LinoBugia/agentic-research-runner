"""W5-00 stage A: lays out the baits, runs the probe as either user, tidies up.

    python3 -I -B stage_a.py prepare     # normal user: baits, test keychains, counter-tests
    python3 -I -B stage_a.py agent       # as arr-agent (the user starts it with sudo -u)
    python3 -I -B stage_a.py reverse     # normal user: probe against the bait of arr-agent
    python3 -I -B stage_a.py cleanup     # normal user: end own baits, remove baits and keychains

`prepare` copies the three scripts to a shared folder (`SHARED`) that a second account can read,
because the repo lies below a folder only its owner may enter. All baits are random and created
here; the scripts report labels, exit codes and yes/no only, never a marker, secret or path.

Python 3.9 stdlib only (system Python of the command line tools).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

SHARED = Path("/Users/Shared/arr-w5-00")
TARGETS = SHARED / "targets.json"
HOME_BAIT = "arr-w5-00-bait"
SERVICE = "arr-w5-00-bait"
SCRIPTS = ("bait.py", "probe.py", "stage_a.py")
HERE = Path(__file__).resolve().parent
PYTHON = [sys.executable, "-I", "-B"]


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def probe(label: str, arguments: list[str]) -> dict:
    done = subprocess.run(
        [*PYTHON, str(SHARED / "bin" / "probe.py"), *arguments],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=SHARED,
    )
    try:
        result = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        result = {"exit": done.returncode, "output": "unreadable"}
    result["label"] = label
    result["uid"] = os.getuid()
    print(json.dumps(result))
    return result


def start_bait(state: Path, ttl: int) -> dict:
    done = subprocess.run(
        [*PYTHON, str(SHARED / "bin" / "bait.py"), "--state", str(state), "--ttl", str(ttl)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=SHARED,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def write_bait_file(path: Path, mode: int | None) -> str:
    marker = secrets.token_hex(16)
    path.write_text(marker + "\n")
    if mode is not None:
        path.chmod(mode)
    return sha256(marker)


def security(*arguments: str) -> int:
    done = subprocess.run(
        ["/usr/bin/security", *arguments], capture_output=True, stdin=subprocess.DEVNULL
    )
    return done.returncode


def make_keychain(path: Path, secrets_file: dict, mode: int | None) -> str:
    if path.exists():
        security("delete-keychain", str(path))
    password, value = secrets.token_hex(16), secrets.token_hex(16)
    if security("create-keychain", "-p", password, str(path)) != 0:
        raise SystemExit(f"create-keychain failed for {path.name}")
    # -A: any program may read the item, so the only wall measured is the account boundary.
    if security("add-generic-password", "-A", "-s", SERVICE, "-a", "bait", "-w", value, str(path)):
        raise SystemExit(f"add-generic-password failed for {path.name}")
    if mode is not None:
        path.chmod(mode)
    secrets_file[str(path)] = password
    return sha256(value)


def prepare() -> None:
    home = Path.home()
    tmp = HERE / "tmp"
    tmp.mkdir(exist_ok=True)
    tmp.chmod(0o700)
    for folder, mode in ((SHARED, 0o755), (SHARED / "bin", 0o755), (SHARED / "state", 0o755)):
        folder.mkdir(exist_ok=True)
        folder.chmod(mode)
    for folder in ("kc", "counter"):
        (SHARED / folder).mkdir(exist_ok=True)
        (SHARED / folder).chmod(0o755)
    drop = SHARED / "drop"
    drop.mkdir(exist_ok=True)
    drop.chmod(0o1777)
    for name in SCRIPTS:
        shutil.copyfile(HERE / name, SHARED / "bin" / name)
        (SHARED / "bin" / name).chmod(0o644)

    plain = home / HOME_BAIT
    plain.mkdir(exist_ok=True)  # default mode from the umask, as most tools create folders
    private = plain / "private"
    private.mkdir(exist_ok=True)
    private.chmod(0o700)
    files = {
        "file:home-default-mode": (plain / "bait.txt", None),
        "file:home-private-0700": (private / "bait.txt", 0o600),
        "file:documents-default-mode": (tmp / "bait.txt", None),
        "file:shared-0644 (counter)": (SHARED / "counter" / "bait.txt", 0o644),
    }
    targets: list[list] = []
    for label, (path, mode) in files.items():
        targets.append(
            [label, ["file", "--path", str(path), "--sha256", write_bait_file(path, mode)]]
        )

    keychain_passwords: dict[str, str] = {}
    keychains = {
        "keychain:file-in-documents": (tmp / "w5-00-k1.keychain", None),
        "keychain:file-in-shared-0644": (SHARED / "kc" / "w5-00-k2.keychain", 0o644),
    }
    for label, (path, mode) in keychains.items():
        digest = make_keychain(path, keychain_passwords, mode)
        targets.append(
            [label, ["keychain", "--keychain", str(path), "--service", SERVICE, "--sha256", digest]]
        )
    secrets_path = tmp / "keychains.json"
    secrets_path.write_text(json.dumps(keychain_passwords))
    secrets_path.chmod(0o600)

    for label, path in (
        ("access:home", home),
        ("access:home/Documents", home / "Documents"),
        ("access:home/Library", home / "Library"),
        ("access:home/" + HOME_BAIT, plain),
        ("access:shared-counter (counter)", SHARED / "counter"),
    ):
        targets.append([label, ["access", "--path", str(path)]])

    state = SHARED / "state" / "user-env.json"
    bait = start_bait(state, 3600)
    targets.insert(0, ["env:bait-of-normal-user", ["env", "--state", str(state)]])
    TARGETS.write_text(json.dumps({"targets": targets, "user_bait_pid": bait.get("pid")}))
    TARGETS.chmod(0o644)
    print(json.dumps({"prepared": len(targets), "user_bait_pid": bait.get("pid")}))
    counter()


def unlock_test_keychains() -> None:
    secrets_path = HERE / "tmp" / "keychains.json"
    for path, password in json.loads(secrets_path.read_text()).items():
        security("unlock-keychain", "-p", password, path)


def counter() -> None:
    """Counter-tests as the normal user: the probe must reach every bait of its own account."""
    unlock_test_keychains()
    for label, arguments in json.loads(TARGETS.read_text())["targets"]:
        probe(label, arguments)


def agent() -> None:
    if os.getuid() == TARGETS.stat().st_uid:
        raise SystemExit("agent must run as arr-agent, not as the owner of the baits")
    print(json.dumps({"whoami": os.getuid(), "groups": os.getgroups()}))
    for label, arguments in json.loads(TARGETS.read_text())["targets"]:
        probe(label, arguments)
    own = SHARED / "drop" / f"agent-own-{os.getuid()}.json"
    start_bait(own, 120)
    probe("env:own bait of arr-agent (counter)", ["env", "--state", str(own)])
    stop_bait(own)
    reverse_state = SHARED / "drop" / "agent-env.json"
    bait = start_bait(reverse_state, 1800)
    print(json.dumps({"reverse_bait_pid": bait.get("pid"), "ttl": 1800}))


def reverse() -> None:
    probe("env:bait of arr-agent", ["env", "--state", str(SHARED / "drop" / "agent-env.json")])
    probe(
        "env:bait-of-normal-user (counter)",
        ["env", "--state", str(SHARED / "state" / "user-env.json")],
    )


def stop_bait(state: Path) -> None:
    """End a bait by the PID in its state file, only if it is ours and its start time matches."""
    sys.path.insert(0, str(SHARED / "bin"))
    from probe import start_time

    try:
        record = json.loads(state.read_text())
    except (OSError, ValueError):
        return
    if record["uid"] == os.getuid() and start_time(record["pid"]) == record["start"]:
        os.kill(record["pid"], signal.SIGTERM)


def cleanup() -> None:
    stop_bait(SHARED / "state" / "user-env.json")
    secrets_path = HERE / "tmp" / "keychains.json"
    if secrets_path.exists():
        for path in json.loads(secrets_path.read_text()):
            if Path(path).exists():
                security("delete-keychain", path)
        secrets_path.unlink()
    for lock in (HERE / "tmp").glob(".fl*"):  # lock files `security` leaves next to a keychain
        lock.unlink()
    shutil.rmtree(Path.home() / HOME_BAIT, ignore_errors=True)
    (HERE / "tmp" / "bait.txt").unlink(missing_ok=True)
    left = []
    for path in sorted(SHARED.rglob("*"), reverse=True):
        try:
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()
        except OSError:
            owner = path.lstat().st_uid
            left.append([str(path.relative_to(SHARED)), owner, stat.filemode(path.lstat().st_mode)])
    try:
        SHARED.rmdir()
    except OSError:
        left.append([".", SHARED.stat().st_uid, "folder not empty"])
    print(json.dumps({"cleanup": "done", "left_in_shared": left}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "counter", "agent", "reverse", "cleanup"])
    command = parser.parse_args().command
    {
        "prepare": prepare,
        "counter": counter,
        "agent": agent,
        "reverse": reverse,
        "cleanup": cleanup,
    }[command]()


if __name__ == "__main__":
    main()
