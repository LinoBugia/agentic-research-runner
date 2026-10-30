"""Fake `claude setup-token` for the login tests: a small script run in a real pty.

`fake_cli` writes the executable; `facts` reads what it saw (pid, terminal, environment).
`MARKER` is the token of all tests, a value from no real source.
"""

import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

MARKER = "sk-ant-oat01-MARKER" + "x7Q_" * 20

HEADER = """#!{python}
import fcntl, json, os, signal, struct, sys, termios, time
rows, cols = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8)))[:2]
facts = dict(
    pid=os.getpid(),
    args=sys.argv[1:],
    tty=os.isatty(0) and os.isatty(1),
    cols=cols,
    token_in_env="CLAUDE_CODE_OAUTH_TOKEN" in os.environ,
    config_dir=os.environ.get("CLAUDE_CONFIG_DIR"),
    home_exists=os.path.isdir(os.environ.get("CLAUDE_CONFIG_DIR") or "/nonexistent"),
)
with open({facts!r}, "w") as file:
    json.dump(facts, file)
"""

PRINT_TOKEN = f"""
print("\\x1b[2J\\x1b[?25lOpening browser to sign in", flush=True)
time.sleep(0.05)
text = {MARKER!r}
print("\\x1b[1;33mYour OAuth token:\\x1b[0m\\r\\n\\x1b[33m" + text[:30], end="", flush=True)
time.sleep(0.05)
print("\\x1b[0m\\x1b[33m" + text[30:] + "\\x1b[39m\\r\\nStore this token securely.", flush=True)
time.sleep(30)
"""
FLOOD = PRINT_TOKEN.replace(
    "time.sleep(30)", 'while True:\n    os.write(1, b"x" * 4096 + b"\\r\\n")'
)
"""Output the service no longer reads after the token: fills the terminal buffer."""
HANG = "time.sleep(60)\n"
FAIL = 'print("Failed to exchange authorization code", flush=True)\nsys.exit(1)\n'
IGNORE_TERM = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n"
IGNORE_HUP = "signal.signal(signal.SIGHUP, signal.SIG_IGN)\ntime.sleep(60)\n"


def fake_cli(folder: Path, body: str) -> tuple[Path, Path]:
    """The executable and the file it writes its facts to."""
    folder.mkdir(parents=True, exist_ok=True)
    facts = folder / "facts.json"
    cli = folder / "claude"
    cli.write_text(HEADER.format(python=sys.executable, facts=str(facts)) + body, "utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli, facts


def facts_of(path: Path) -> dict:
    return json.loads(path.read_text("utf-8"))


def gone(path: str) -> bool:
    return not Path(path).exists()


def alive(pid: int) -> bool:
    """True while the process exists and is no zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(state.stdout.strip()) and not state.stdout.strip().startswith("Z")


def wait_until(check: Callable[[], bool], timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()
