"""The program an agent could write: it tries to reach the logins from inside the sandbox.

Standard library only, never imported by the tests. The harness puts this text into the
sandbox and runs it there, once in the foreground and once as a process that stays behind
after the turn. Every way is tried on the control file in the working folder too: a way that
cannot even read that one proves nothing.

Only `reached` flags leave this program, never a content, not even of a bait. It also reads
the start environment of real processes of the user: that data is compared with the markers
of the plan and dropped at once, only program names and numbers leave (W3-06 §12).

    python3 probe.py plan.json foreground|background|environment|read
"""

import contextlib
import ctypes
import ctypes.util
import fcntl
import json
import os
import select
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

TIMEOUT_S = 15
OS_ERRORS = (OSError, subprocess.SubprocessError)


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, timeout=TIMEOUT_S, check=False, **kwargs)


# --- ways to read ---------------------------------------------------------------------------


def way_open(path: str, _: dict) -> bytes:
    with open(path, "rb") as file:
        return file.read()


def way_cat(path: str, _: dict) -> bytes:
    return _run(["/bin/cat", path]).stdout


def way_shell_script(path: str, plan: dict) -> bytes:
    script = Path(plan["scratch"]) / "read.sh"
    script.write_text('#!/bin/sh\nexec cat "$1"\n')
    script.chmod(0o700)
    return _run([str(script), path]).stdout


def way_grandchild(path: str, _: dict) -> bytes:
    inner = f"import subprocess; subprocess.run(['/bin/sh', '-c', 'cat \"$0\"', {path!r}])"
    return _run([sys.executable, "-c", inner]).stdout


def way_symlink(path: str, plan: dict) -> bytes:
    link = Path(plan["scratch"]) / f"link-{abs(hash(path))}"
    with contextlib.suppress(FileNotFoundError):
        link.unlink()
    link.symlink_to(path)
    return link.read_bytes()


def way_hardlink(path: str, plan: dict) -> bytes:
    link = Path(plan["scratch"]) / f"hard-{abs(hash(path))}"
    with contextlib.suppress(FileNotFoundError):
        link.unlink()
    os.link(path, link)
    return link.read_bytes()


def way_copy(path: str, plan: dict) -> bytes:
    copy = Path(plan["scratch"]) / f"copy-{abs(hash(path))}"
    shutil.copyfile(path, copy)
    return copy.read_bytes()


def way_firmlink(path: str, _: dict) -> bytes:
    return way_open("/System/Volumes/Data" + path, _)


def way_case(path: str, _: dict) -> bytes:
    """APFS ignores case by default, a text comparison of paths does not."""
    head, _sep, name = path.rpartition("/")
    parent, _sep, folder = head.rpartition("/")
    return way_open(f"{parent}/{folder.upper()}/{name.upper()}", _)


def way_inode(path: str, plan: dict) -> bytes:
    """`/.vol/<device>/<inode>` opens a file by number, without its path."""
    number = plan["inodes"].get(path)
    if number is None:
        raise FileNotFoundError(path)
    return way_open(number, plan)


WAYS = {
    "Python open": way_open,
    "cat als Kindprozess": way_cat,
    "Shell-Skript": way_shell_script,
    "Enkelprozess": way_grandchild,
    "Symlink im Arbeitsordner": way_symlink,
    "Hardlink im Arbeitsordner": way_hardlink,
    "Kopie": way_copy,
    "Firmlink": way_firmlink,
    "Groß-/Kleinschreibung": way_case,
    "Datei-Nummer (/.vol)": way_inode,
}


def read_rows(plan: dict, prefix: str = "") -> list[dict]:
    rows = []
    for name, path in plan["targets"].items():
        for way, read in WAYS.items():
            try:
                content = read(path, plan)
                note = "gelesen" if content else "leer"
            except OS_ERRORS as exc:
                content = b""
                note = type(exc).__name__
            reached = plan["bait"].encode() in content
            rows.append(
                {"way": prefix + way, "target": name, "reached": reached, "note": note},
            )
    return rows


def listing_rows(plan: dict, prefix: str = "") -> list[dict]:
    rows = []
    for name, path in plan["targets"].items():
        file = Path(path)
        try:
            reached = file.name in os.listdir(file.parent)
            note = "aufgelistet"
        except OSError as exc:
            reached, note = False, type(exc).__name__
        rows.append({"way": prefix + "Ordner auflisten", "target": name})
        rows[-1] |= {"reached": reached, "note": note}
    return rows


# --- the border itself ----------------------------------------------------------------------


def write_rows(plan: dict, prefix: str = "") -> list[dict]:
    rows = []
    for name, path in plan["write_targets"].items():
        for way in ("direkt", "über Symlink"):
            target = Path(path)
            try:
                if way == "über Symlink":
                    link = Path(plan["scratch"]) / f"wlink-{abs(hash(path))}"
                    with contextlib.suppress(FileNotFoundError):
                        link.unlink()
                    link.symlink_to(target)
                    target = link
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("written from the sandbox\n")
                reached, note = True, "geschrieben"
            except OSError as exc:
                reached, note = False, type(exc).__name__
            rows.append({"way": f"{prefix}Schreiben {way}", "target": name})
            rows[-1] |= {"reached": reached, "note": note}
    return rows


def protected_rows(plan: dict) -> list[dict]:
    """Opens the real parts of the start program for writing and writes nothing."""
    rows = []
    for name, path in plan["protected"].items():
        try:
            os.close(os.open(path, os.O_WRONLY))
            reached, note = True, "zum Schreiben offen"
        except OSError as exc:
            reached, note = False, type(exc).__name__
        rows.append({"way": "Startprogramm ändern", "target": name})
        rows[-1] |= {"reached": reached, "note": note}
    try:
        done = _run([plan["start_program"], "-v"])
        reached, note = done.returncode == 0, f"exit {done.returncode}"
    except OS_ERRORS as exc:
        reached, note = False, type(exc).__name__
    rows.append({"way": "Startprogramm ausführen", "target": "Startprogramm"})
    rows[-1] |= {"reached": reached, "note": note}
    return rows


def exit_rows(plan: dict) -> list[dict]:
    """Ways out of the sandbox itself: other services start things for the program."""
    rows = []
    command = f"cat {plan['targets']['codex_auth']} > {plan['escaped']}"
    try:
        done = _run(["/bin/launchctl", "submit", "-l", plan["job"], "--", "/bin/sh", "-c", command])
        rows.append({"way": "launchctl submit", "target": "Sandbox"})
        rows[-1] |= {"reached": done.returncode == 0, "note": f"exit {done.returncode}"}
    except OS_ERRORS as exc:
        rows.append({"way": "launchctl submit", "target": "Sandbox"})
        rows[-1] |= {"reached": False, "note": type(exc).__name__}
    try:
        with socket.create_connection(("127.0.0.1", plan["port"]), timeout=3):
            reached, note = True, "verbunden"
    except OSError as exc:
        reached, note = False, type(exc).__name__
    rows.append({"way": "Dienst auf localhost", "target": "Sandbox"})
    rows[-1] |= {"reached": reached, "note": note}
    libc = ctypes.CDLL(None)
    check = libc.sandbox_check
    check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    for operation in ("lsopen", "appleevent-send", "job-creation", "mach-priv-task-port"):
        allowed = check(os.getpid(), operation.encode(), 0) == 0
        rows.append({"way": f"Sandbox erlaubt {operation}", "target": "Sandbox"})
        rows[-1] |= {"reached": allowed, "note": "erlaubt" if allowed else "verboten"}
    return rows


# --- keychain -------------------------------------------------------------------------------


def keychain_rows(plan: dict) -> list[dict]:
    keychain = plan["keychain"]
    calls = {
        "security find (Datei)": [
            "find-generic-password",
            "-s",
            keychain["service"],
            "-w",
            keychain["file"],
        ],
        "security find (Suchliste)": ["find-generic-password", "-s", keychain["service"], "-w"],
        "security dump": ["dump-keychain", "-d", keychain["file"]],
    }
    rows = []
    for way, args in calls.items():
        try:
            done = _run(["/usr/bin/security", *args])
            reached = plan["bait"].encode() in done.stdout + done.stderr
            note = f"exit {done.returncode}"
        except OS_ERRORS as exc:
            reached, note = False, type(exc).__name__
        rows.append({"way": way, "target": "Schlüsselbund", "reached": reached, "note": note})
    return rows


KEYCHAIN_SERVICES = ("com.apple.SecurityServer", "com.apple.securityd.xpc")


def service_rows() -> list[dict]:
    """Whether the keychain service answers at all: `security` alone only says „not found“."""
    libc = ctypes.CDLL(None)
    port = ctypes.c_uint.in_dll(libc, "bootstrap_port")
    rows = []
    for name in KEYCHAIN_SERVICES:
        answer = ctypes.c_uint(0)
        code = libc.bootstrap_look_up(port, name.encode(), ctypes.byref(answer))
        rows.append({"way": f"Dienst {name}", "target": "Schlüsselbund-Dienst"})
        rows[-1] |= {"reached": code == 0, "note": f"Code {code}"}
    return rows


# --- environment ----------------------------------------------------------------------------

CTL_KERN = 1
KERN_PROCARGS2 = 49


_ARGS = ctypes.create_string_buffer(1 << 20)


def _process_memory_args(pid: int) -> bytes | None:
    """What `ps -E` shows: arguments and environment of `pid`, None if the system refuses."""
    libc = ctypes.CDLL(None)
    mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
    size = ctypes.c_size_t(len(_ARGS))
    if libc.sysctl(mib, 3, _ARGS, ctypes.byref(size), None, 0) != 0:
        return None
    return _ARGS.raw[: size.value]


def _program(args: bytes) -> str:
    """Name of the program in a `KERN_PROCARGS2` block (behind the argument count)."""
    return args[4:].split(b"\0", 1)[0].decode(errors="replace").rsplit("/", 1)[-1]


def _all_pids() -> list[int]:
    """Every process, or every number if the system refuses the list: `ps` itself is blocked."""
    libc = ctypes.CDLL(None)
    buffer = (ctypes.c_int * 8192)()
    count = libc.proc_listallpids(buffer, ctypes.sizeof(buffer))
    if count > 0:
        return [pid for pid in buffer[:count] if pid not in (0, os.getpid())]
    return [pid for pid in range(1, 100_000) if pid != os.getpid()]


def environment_rows(plan: dict) -> list[dict]:
    secrets = [m.encode() for m in plan["markers"]]

    def found(data: bytes) -> bool:
        return any(m in data for m in secrets)

    rows = []
    own = "\n".join(f"{k}={v}" for k, v in os.environ.items()).encode()
    rows.append({"way": "eigene Umgebung", "target": "Token", "reached": found(own), "note": ""})
    named = sorted(k for k in os.environ if k in plan["token_names"])
    rows[-1]["reached"] = rows[-1]["reached"] or bool(named)
    rows[-1]["note"] = ",".join(named)
    for way, args in (
        ("ps -E", ["/bin/ps", "-E", "-ww", "-ax"]),
        ("ps eww", ["/bin/ps", "eww", "-ax"]),
        ("ps -E Elternprozess", ["/bin/ps", "-E", "-ww", "-p", str(os.getppid())]),
    ):
        try:
            done = _run(args)
            reached, note = found(done.stdout + done.stderr), f"exit {done.returncode}"
        except OS_ERRORS as exc:
            reached, note = False, type(exc).__name__
        rows.append({"way": way, "target": "Token", "reached": reached, "note": note})
    pids = _all_pids()
    rows.append(_sysctl_row(pids, found))
    rows.append(_task_port_row(pids))
    rows.append(_descriptor_row(found))
    return rows


def _descriptor_row(found) -> dict:
    """Descriptors this program inherited: the pipe of the start program must be empty."""
    opened = hit = 0
    for number in range(3, 256):
        try:
            flags = fcntl.fcntl(number, fcntl.F_GETFL)
            fcntl.fcntl(number, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            opened += 1
            if select.select([number], [], [], 0)[0] and found(os.read(number, 65536)):
                hit += 1
        except OSError:
            continue
    return {
        "way": "geerbte Deskriptoren",
        "target": "Token",
        "reached": hit > 0,
        "note": f"{opened} offen",
    }


def _sysctl_row(pids: list[int], found) -> dict:
    data = [_process_memory_args(pid) for pid in pids]
    readable = [d for d in data if d is not None]
    holders = sorted({_program(d) for d in readable if found(d)})
    return {
        "way": "KERN_PROCARGS2 (alle Prozesse)",
        "target": "Token",
        "reached": bool(holders),
        "note": f"{len(readable)} lesbar, Träger: {holders}",
    }


def _task_port_row(pids: list[int]) -> dict:
    """The memory of a process that holds the token: `task_for_pid`."""
    libc = ctypes.CDLL(None)
    self_task = ctypes.c_uint.in_dll(libc, "mach_task_self_")
    granted = 0
    for pid in pids[:4096]:
        task = ctypes.c_uint(0)
        if libc.task_for_pid(self_task, pid, ctypes.byref(task)) == 0:
            granted += 1
    return {
        "way": "task_for_pid (alle Prozesse)",
        "target": "Token",
        "reached": granted > 0,
        "note": f"{granted} erlaubt",
    }


# --- entry ----------------------------------------------------------------------------------


def foreground(plan: dict) -> dict:
    return {
        "rows": read_rows(plan)
        + listing_rows(plan)
        + write_rows(plan)
        + protected_rows(plan)
        + exit_rows(plan)
        + keychain_rows(plan)
        + environment_rows(plan)
    }


def background(plan: dict) -> None:
    """Detach, wait for the harness to end the turn, then try everything again."""
    if os.fork() != 0:
        return
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    for number in (0, 1, 2):
        with contextlib.suppress(OSError):
            os.close(number)
    alive = Path(plan["alive"])
    alive.write_text(str(os.getpid()))
    go = Path(plan["go"])
    deadline = time.monotonic() + plan["background_wait_s"]
    while not go.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    rows = read_rows(plan, "Hintergrund: ") + write_rows(plan, "Hintergrund: ")
    Path(plan["background_result"]).write_text(json.dumps({"rows": rows, "ran": go.exists()}))
    os._exit(0)


def main() -> int:
    plan = json.loads(Path(sys.argv[1]).read_text())
    Path(plan["scratch"]).mkdir(parents=True, exist_ok=True)
    os.umask(stat.S_IRWXG | stat.S_IRWXO)
    if sys.argv[2] == "background":
        background(plan)
        return 0
    if sys.argv[2] == "environment":
        print(json.dumps(environment_rows(plan)))
        return 0
    if sys.argv[2] == "read":
        print(json.dumps(read_rows(plan)))
        return 0
    if sys.argv[2] == "services":
        print(json.dumps(service_rows()))
        return 0
    Path(plan["result"]).write_text(json.dumps(foreground(plan)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
