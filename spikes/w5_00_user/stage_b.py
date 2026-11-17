"""W5-00 stage B, round 1: file rights between two users, closing a home, what arr-agent reaches.

    python3 -I -B stage_b.py prepare        # normal user: test run folder, test homes, baits
    python3 -I -B stage_b.py arr-side       # normal user, in the background: waits for the agent
    python3 -I -B stage_b.py agent 3<fd-bait <stdin-bait   # as arr-agent (sudo -u by the user)

The agent and the normal user (playing arr) take turns through marker files in `b/sync/`:

1. agent: writes into three test work folders (ACL variant twice, group variant once), locks one
   sub folder against everyone else, clones, keeps a working directory and folder handles open;
2. arr: reads, copies and deletes the agent's files, retires one work folder (move into a closed
   folder) and revokes another (ACL entry removed, mode 0700);
3. agent: tries the retired and revoked folders by path, by working directory and by handle,
   then the test homes, the access probes, inherited descriptors, its session, Seatbelt, and as
   the very last step `kill(-1, SIGKILL)` against its own baits while two baits of the normal
   user must survive.

Every check prints one JSON line with a label and yes/no or an errno. Markers are compared by
SHA-256 and never printed. Paths of the real installation appear only in `targets-b.json` in
the shared test folder, never in the output. Python 3.9 stdlib only.
"""

from __future__ import annotations

import argparse
import ctypes
import grp
import hashlib
import json
import os
import pwd
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

SHARED = Path("/Users/Shared/arr-w5-00")
B = SHARED / "b"
RUN = B / "run"
SYNC = B / "sync"
HOMES = B / "homes"
TARGETS = B / "targets-b.json"
AGENT = "arr-agent"
PYTHON = [sys.executable, "-I", "-B"]
RIGHTS = (
    "list,add_file,search,add_subdirectory,delete_child,read,write,append,execute,delete,"
    "readattr,writeattr,readextattr,writeextattr,readsecurity"
)
INHERIT = ",file_inherit,directory_inherit"

libc = ctypes.CDLL(None, use_errno=True)


def say(label: str, **fields: object) -> None:
    print(json.dumps({"label": label, "uid": os.getuid(), **fields}), flush=True)


def digest(data: bytes) -> str:
    return hashlib.sha256(data.strip()).hexdigest()


def bait_file(path: Path, mode: int | None = None) -> str:
    marker = secrets.token_hex(16)
    path.write_text(marker + "\n")
    if mode is not None:
        path.chmod(mode)
    return digest(marker.encode())


def read_check(label: str, path: str | Path, expected: str | None = None, **at: object) -> None:
    """Open and read; report ok/errno and, given a hash, whether the content matches."""
    try:
        if "dir_fd" in at:
            fd = os.open(str(path), os.O_RDONLY, dir_fd=at["dir_fd"])
        else:
            fd = os.open(str(path), os.O_RDONLY)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
    except OSError as error:
        say(label, ok=False, errno=error.errno)
        return
    fields: dict = {"ok": True}
    if expected is not None:
        fields["marker"] = digest(data) == expected
    say(label, **fields)


def act(label: str, function, *arguments) -> bool:
    try:
        function(*arguments)
    except OSError as error:
        say(label, ok=False, errno=error.errno)
        return False
    say(label, ok=True)
    return True


def run(*command: str) -> int:
    return subprocess.run(command, capture_output=True, check=False).returncode


def must(*command: str) -> None:
    """Run a command; raise OSError (as the file operations do) if it fails."""
    if run(*command) != 0:
        raise OSError(0, f"{Path(command[0]).name} failed")


def wait_for(name: str, seconds: int = 900) -> dict:
    path = SYNC / name
    for _ in range(seconds * 4):
        if path.exists():
            time.sleep(0.2)
            return json.loads(path.read_text())
        time.sleep(0.25)
    raise SystemExit(f"timeout waiting for {name}")


def signal_done(name: str, data: dict) -> None:
    temporary = SYNC / f".{name}.tmp"
    temporary.write_text(json.dumps(data))
    temporary.chmod(0o644)
    temporary.replace(SYNC / name)


def start_time(pid: int) -> list[int] | None:
    buffer = ctypes.create_string_buffer(1024)
    size = ctypes.c_size_t(len(buffer))
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)
    if libc.sysctl(mib, 4, buffer, ctypes.byref(size), None, 0) != 0 or size.value == 0:
        return None
    return [
        int.from_bytes(buffer.raw[0:8], "little", signed=True),
        int.from_bytes(buffer.raw[8:12], "little", signed=True),
    ]


def processes_of(uid: int) -> int:
    """Number of processes of `uid` (`KERN_PROC_UID`; reads no environment)."""
    mib = (ctypes.c_int * 4)(1, 14, 5, uid)
    size = ctypes.c_size_t(0)
    libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0)
    buffer = ctypes.create_string_buffer(size.value + 64 * 648)
    size = ctypes.c_size_t(len(buffer))
    if libc.sysctl(mib, 4, buffer, ctypes.byref(size), None, 0) != 0:
        return -1
    return size.value // 648  # sizeof(struct kinfo_proc) on arm64


def start_bait(state: Path, ttl: int) -> None:
    subprocess.run(
        [*PYTHON, str(SHARED / "bin" / "bait.py"), "--state", str(state), "--ttl", str(ttl)],
        capture_output=True,
        timeout=30,
        check=False,
    )


def alive(state: Path) -> bool | None:
    try:
        record = json.loads(state.read_text())
    except (OSError, ValueError):
        return None
    return start_time(record["pid"]) == record["start"]


# --- prepare (normal user) -------------------------------------------------------------------


def acl(path: Path, entry: str) -> None:
    if run("/bin/chmod", "+a", entry, str(path)) != 0:
        raise SystemExit(f"chmod +a failed on {path.name}")


def prepare() -> None:
    me = pwd.getpwuid(os.getuid()).pw_name
    if B.exists():
        shutil.rmtree(B)
    for folder in (B, RUN, HOMES):
        folder.mkdir(mode=0o755)
    SYNC.mkdir()
    SYNC.chmod(0o1777)
    for name in ("bait.py", "probe.py", "stage_a.py", "stage_b.py"):
        shutil.copyfile(Path(__file__).resolve().parent / name, SHARED / "bin" / name)
        (SHARED / "bin" / name).chmod(0o644)
    hashes: dict[str, str] = {}

    # Point 3: folders standing in for the home, closed in different ways.
    other_group = grp.getgrnam("admin").gr_gid  # a group of the normal user, not of arr-agent
    homes = {
        "h-0755": (0o755, None, None),
        "h-0750-staff": (0o750, grp.getgrnam("staff").gr_gid, None),  # folders inherit wheel here
        "h-0750-other-group": (0o750, other_group, None),
        "h-0700": (0o700, None, None),
        "h-0755-acl-deny": (0o755, None, "list,search,readattr,readextattr,readsecurity"),
        "h-0755-acl-deny-list-only": (0o755, None, "list"),
    }
    for name, (mode, group, deny) in homes.items():
        home = HOMES / name
        (home / "sub").mkdir(parents=True)
        hashes[f"home:{name}"] = bait_file(home / "sub" / "bait.txt")
        if group is not None:
            os.chown(home, -1, group)
        home.chmod(mode)
        if deny:
            acl(home, f"user:{AGENT} deny {deny}")
    # A run folder inside a home closed by ACL, with an allow entry of its own.
    runs = HOMES / "h-0755-acl-deny" / "runs"
    runs.mkdir()
    acl(runs, f"user:{AGENT} allow {RIGHTS}")
    hashes["home:h-0755-acl-deny/runs"] = bait_file(runs / "bait.txt", 0o644)

    # Point 8: a test run folder shared by arr (normal user) and arr-agent.
    base = RUN / "base"
    base.mkdir(mode=0o755)
    hashes["base"] = bait_file(base / "file.txt", 0o644)
    for agent in ("a1", "a2", "a3"):
        (RUN / "agents" / agent).mkdir(parents=True, mode=0o755)
    (RUN / "agents").chmod(0o755)
    for agent in ("a1", "a2"):
        work = RUN / "agents" / agent / "work"
        work.mkdir(mode=0o700)
        acl(work, f"user:{AGENT} allow {RIGHTS}{INHERIT}")
        acl(work, f"user:{me} allow {RIGHTS}{INHERIT}")
        run("/bin/cp", "-c", str(base / "file.txt"), str(work / "project.txt"))  # copy mode
    work3 = RUN / "agents" / "a3" / "work"
    work3.mkdir()
    os.chown(work3, -1, grp.getgrnam("staff").gr_gid)
    work3.chmod(0o2770)
    hashes["transcript"] = bait_file(RUN / "agents" / "a1" / "transcript.jsonl", 0o600)
    (RUN / "retired").mkdir(mode=0o700)
    (RUN / "output").mkdir(mode=0o755)
    hashes["output"] = bait_file(RUN / "output" / "result.txt", 0o644)

    # Point 7: descriptors the user's shell hands across sudo (files only the normal user reads).
    hashes["fd3"] = bait_file(B / "fd-bait", 0o600)
    hashes["stdin"] = bait_file(B / "stdin-bait", 0o600)

    # Point 10: two baits of the normal user that the agent's kill must not hit.
    for number in (1, 2):
        start_bait(SHARED / "state" / f"user-kill-{number}.json", 7200)

    home = Path.home()
    probe_paths = {
        "~ (home)": home,
        "~/.arr": home / ".arr",
        "~/.arr/profiles.yaml": home / ".arr" / "profiles.yaml",
        "~/.arr/bin (start program)": home / ".arr" / "bin",
        "~/.arr/auth": home / ".arr" / "auth",
        "~/.arr/logs": home / ".arr" / "logs",
        "~/arr-m1 (workfolder)": home / "arr-m1",
        "~/.venvs/mathe/bin/python": home / ".venvs" / "mathe" / "bin" / "python",
        "/Users/Shared/arr-<user> (venv root)": Path("/Users/Shared") / f"arr-{me}",
        "repo .venv (arr, CLI, Codex)": home / "Documents" / "python" / "agentic-research-runner",
        "/Library/Frameworks/Python 3.12": Path(
            "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12"
        ),
        "/usr/bin/sandbox-exec": Path("/usr/bin/sandbox-exec"),
        "/usr/bin/git": Path("/usr/bin/git"),
    }
    for path in sorted((home / ".arr" / "auth").glob("*/codex_home"))[:1]:
        probe_paths["~/.arr/auth/<profile>/codex_home"] = path
    TARGETS.write_text(
        json.dumps(
            {
                "hashes": hashes,
                "probe_paths": {label: str(path) for label, path in probe_paths.items()},
                "normal_uid": os.getuid(),
            }
        )
    )
    TARGETS.chmod(0o644)
    say("prepared", homes=len(homes), probe_paths=len(probe_paths))
    for name in homes:
        read_check(
            f"counter home:{name}", HOMES / name / "sub" / "bait.txt", hashes[f"home:{name}"]
        )
    for label in ("user-kill-1", "user-kill-2"):
        say(f"counter bait {label} alive", alive=alive(SHARED / "state" / f"{label}.json"))


# --- arr side (normal user) ------------------------------------------------------------------


def arr_side() -> None:
    files = wait_for("agent-p1.json", 3600)["hashes"]
    a1, a2, a3 = (RUN / "agents" / agent / "work" for agent in ("a1", "a2", "a3"))
    read_check("arr reads agent file (ACL variant)", a1 / "notes.txt", files["a1"])
    read_check("arr reads agent file (group variant)", a3 / "notes.txt", files["a3"])
    read_check("arr reads agent file in locked sub folder", a1 / "locked" / "f.txt")
    copy = RUN / "output" / "notes-copy.txt"
    act(
        "arr clones agent file into output (cp -c)",
        must,
        "/bin/cp",
        "-c",
        str(a1 / "notes.txt"),
        str(copy),
    )
    if copy.exists():
        say("owner of arr's clone is arr", ok=copy.stat().st_uid == os.getuid())
    act("arr deletes agent file in agent sub folder (ACL variant)", os.unlink, a1 / "sub" / "f.txt")
    act("arr deletes agent file in agent sub folder (group)", os.unlink, a3 / "sub" / "f.txt")
    act("arr deletes agent's locked sub folder", shutil.rmtree, a1 / "locked")
    act("arr retires a1/work (move into closed retired/)", os.rename, a1, RUN / "retired" / "a1-1")
    act("arr removes the ACL of a2/work", must, "/bin/chmod", "-N", str(a2))
    act("arr sets a2/work to 0700", os.chmod, a2, 0o700)
    signal_done("arr-done.json", {})
    wait_for("agent-done.json", 1800)
    act("arr deletes the retired folder afterwards", shutil.rmtree, RUN / "retired" / "a1-1")
    act("arr deletes a3/work (group variant)", shutil.rmtree, a3)
    say("arr side done")


# --- agent side (arr-agent) ------------------------------------------------------------------


def agent(dry: bool = False) -> None:
    """The agent side; `dry` lets the owner rehearse it and stops before the kill."""
    targets = json.loads(TARGETS.read_text())
    if os.getuid() == targets["normal_uid"] and not dry:
        raise SystemExit("agent must run as arr-agent")
    hashes = targets["hashes"]
    old_umask = os.umask(0o022)
    os.umask(old_umask)
    say("whoami", groups=os.getgroups(), umask=oct(old_umask))

    # Phase 1: work in the three work folders.
    written: dict[str, str] = {}
    a1, a2, a3 = (RUN / "agents" / agent / "work" for agent in ("a1", "a2", "a3"))
    for name, work in (("a1", a1), ("a2", a2), ("a3", a3)):
        written[name] = bait_file(work / "notes.txt")
        (work / "sub").mkdir()
        bait_file(work / "sub" / "f.txt")
        bait_file(work / "sub" / "g.txt")
    written["a1-sub-g"] = digest((a1 / "sub" / "g.txt").read_bytes())
    written["a2-sub-g"] = digest((a2 / "sub" / "g.txt").read_bytes())
    locked = a1 / "locked"
    locked.mkdir()
    bait_file(locked / "f.txt")
    act("agent strips inherited ACL of its own sub folder", must, "/bin/chmod", "-N", str(locked))
    act(
        "agent strips inherited ACL of its file there",
        must,
        "/bin/chmod",
        "-N",
        str(locked / "f.txt"),
    )
    act("agent sets its sub folder to 0700", os.chmod, locked, 0o700)

    def edit(path: Path) -> None:
        with open(path, "a") as handle:
            handle.write("edited by the agent\n")

    act("agent edits arr's clone of the project (copy mode)", edit, a1 / "project.txt")
    base_clone = str(a1 / "base-clone.txt")
    act(
        "agent clones base file (cp -c)",
        must,
        "/bin/cp",
        "-c",
        str(RUN / "base" / "file.txt"),
        base_clone,
    )
    read_check("agent a1 reads file of agent a2 (same user)", a2 / "notes.txt", written["a2"])
    os.chdir(a1 / "sub")
    handles = {
        "a1": os.open(str(a1), os.O_RDONLY | os.O_DIRECTORY),
        "a2": os.open(str(a2), os.O_RDONLY | os.O_DIRECTORY),
        "a2/sub": os.open(str(a2 / "sub"), os.O_RDONLY | os.O_DIRECTORY),
    }
    signal_done("agent-p1.json", {"hashes": written})
    wait_for("arr-done.json")

    # Phase 2: what is left of a retired or revoked folder.
    read_check("retired a1: old path", a1 / "notes.txt", written["a1"])
    read_check("retired a1: new path", RUN / "retired" / "a1-1" / "notes.txt", written["a1"])
    read_check("retired a1: via working directory", "g.txt", written["a1-sub-g"])
    read_check(
        "retired a1: via held folder handle", "notes.txt", written["a1"], dir_fd=handles["a1"]
    )
    read_check("revoked a2: path", a2 / "notes.txt", written["a2"])
    read_check(
        "revoked a2: via held folder handle", "notes.txt", written["a2"], dir_fd=handles["a2"]
    )
    read_check(
        "revoked a2: via held handle of own sub folder",
        "g.txt",
        written["a2-sub-g"],
        dir_fd=handles["a2/sub"],
    )
    read_check("own transcript (0600 of arr)", RUN / "agents" / "a1" / "transcript.jsonl")
    read_check("output (0644 of arr)", RUN / "output" / "result.txt", hashes["output"])
    os.chdir(SHARED)
    for handle in handles.values():
        os.close(handle)

    # Point 3: test homes.
    for home in sorted(HOMES.iterdir()):
        read_check(
            f"home {home.name}: bait in sub/",
            home / "sub" / "bait.txt",
            hashes.get(f"home:{home.name}"),
        )
        say(f"home {home.name}: listable", ok=os.access(home, os.R_OK | os.X_OK))
    read_check(
        "home h-0755-acl-deny: run folder inside with allow entry",
        HOMES / "h-0755-acl-deny" / "runs" / "bait.txt",
        hashes["home:h-0755-acl-deny/runs"],
    )

    # Point 4: what arr-agent reaches of the real installation (access(2) only).
    for label, path in targets["probe_paths"].items():
        say(
            f"access {label}",
            exists=os.path.lexists(path),
            read=os.access(path, os.R_OK),
            write=os.access(path, os.W_OK),
            execute=os.access(path, os.X_OK),
        )

    # Point 7: descriptors across sudo.
    for label, fd, key, name in (
        ("fd 3", 3, "fd3", "fd-bait"),
        ("stdin", 0, "stdin", "stdin-bait"),
    ):
        try:
            data = os.read(fd, 4096)
            say(f"sudo passes {label}", ok=True, marker=digest(data) == hashes[key])
        except OSError as error:
            say(f"sudo passes {label}", ok=False, errno=error.errno)
        read_check(f"counter: {name} by path", B / name, hashes[key])

    # Point 6: which session the process lives in.
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

    # Point 9: Seatbelt holds under arr-agent.
    counter = str(SHARED / "counter" / "bait.txt")
    for label, rule in (
        ("seatbelt allow default (counter)", "(version 1)(allow default)"),
        (
            "seatbelt deny read of the bait folder",
            f'(version 1)(allow default)(deny file-read* (subpath "{SHARED / "counter"}"))',
        ),
    ):
        done = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", rule, "/bin/cat", counter], capture_output=True
        )
        say(label, exit=done.returncode, read=bool(done.stdout))

    # Point 10, last: end every process of arr-agent, none of the normal user.
    if dry or os.getuid() == targets["normal_uid"]:
        signal_done("agent-done.json", {})
        say("dry run: kill skipped")
        return
    states = [SHARED / "drop" / f"kill-{number}.json" for number in (1, 2, 3)]
    for state in states:
        start_bait(state, 600)
    users = [SHARED / "state" / f"user-kill-{number}.json" for number in (1, 2)]
    say(
        "before kill",
        agent_baits_alive=[alive(state) for state in states],
        user_baits_alive=[alive(state) for state in users],
        processes_of_agent=processes_of(os.getuid()),
    )
    signal_done("agent-done.json", {})
    try:
        os.kill(-1, signal.SIGKILL)
        say("kill(-1, SIGKILL) as arr-agent", ok=True)
    except OSError as error:
        say("kill(-1, SIGKILL) as arr-agent", ok=False, errno=error.errno)
    time.sleep(1)
    say(
        "after kill",
        agent_baits_alive=[alive(state) for state in states],
        user_baits_alive=[alive(state) for state in users],
        processes_of_agent=processes_of(os.getuid()),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "arr-side", "agent", "agent-dry"])
    command = parser.parse_args().command
    if command == "agent-dry":
        agent(dry=True)
        return
    {"prepare": prepare, "arr-side": arr_side, "agent": agent}[command]()


if __name__ == "__main__":
    main()
