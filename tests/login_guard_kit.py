"""Test lab of the login guard: a test `HOME` with baits and arr's real profiles.

Everything lies below a new folder in `/private/tmp`, not in pytest's temp folder: on macOS
that one lies below the user's temp folder, which is a read block of the profile itself, and
a blocked ancestor of the run venv stops Python (W3-00). No path of the user occurs.

The baits are random text in the places where logins lie (`BAITS`). `run_codex` runs the
probe through `codex sandbox -P arr` with the entries of `build_launch`, `run_claude` through
the real CLI with the options of `build_options`, against `FakeMessagesApi`. Neither needs a
model or a real login.
"""

import asyncio
import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import PermissionResultAllow
from codex_cli_bin import bundled_codex_path
from fake_messages_api import FakeMessagesApi, ToolCall

import arr
from arr.auth import (
    START_PROFILE_VARIABLE,
    claude_env,
    clean_process_env,
)
from arr.backends.claude import ClaudeBackend
from arr.backends.claude_options import build_options, remove_tmp_link, tmp_link
from arr.backends.claude_start import bundled_cli, shim_text
from arr.backends.codex_config import PERMISSION_PROFILE, build_launch, codex_home_for, toml_value
from arr.models import AuthKind, AuthProfile, RunInput, WorkfolderSettings
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.policy import (
    ApprovalLog,
    PermissionRules,
    PolicyEngine,
    RunPaths,
    SandboxProfile,
    profile_for,
)
from arr.resolve import resolve_run, validate_model
from arr.room.store import RoomStore
from arr.runs.manager import create_run_venv
from arr.runs.prepare import PreparedAgent, PreparedRun, prepare_run
from arr.settings import app_dir
from arr.workfolder import init_workfolder, load_project_settings

PROBE = Path(__file__).with_name("login_guard_probe.py")
CHATGPT_PROFILE = "guard-chatgpt"
CLAUDE_PROFILE = "guard-claude"
BACKGROUND_WAIT_S = 45
TOKEN_NAMES = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

BAITS = {
    "codex_auth": ".arr/auth/{profile}/codex_home/auth.json",
    "claude_folder": ".claude/.credentials.json",
    "codex_folder": ".codex/auth.json",
    "ssh": ".ssh/id_ed25519",
    "keychains": "Library/Keychains/login.keychain-db",
}
"""Name → place below the test `HOME` (Auftrag §3.1)."""


@dataclass
class Lab:
    base: Path
    home: Path
    workfolder: Path
    run: PreparedRun
    agent: PreparedAgent
    run_paths: RunPaths
    profile: SandboxProfile
    auth: AuthProfile
    bait: str
    baits: dict[str, Path]
    keychain_file: Path
    service: str
    token: str
    job: str
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def start_folder(self) -> Path:
        return self.home / ".arr" / "bin"


@contextmanager
def lab_env(base: Path, home: Path) -> Iterator[None]:
    """`HOME`, `ARR_HOME` and the venv root of the lab, and no credential of this process."""
    saved = dict(os.environ)
    clean_process_env()
    for name in list(os.environ):
        if name.startswith(("CODEX_", "OPENAI_")):
            del os.environ[name]
    os.environ |= {
        "HOME": str(home),
        "ARR_HOME": str(home / ".arr"),
        "ARR_VENV_ROOT": str(base / "venv-root"),
    }
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _settings() -> WorkfolderSettings:
    return validate_model(
        WorkfolderSettings,
        {
            "version": 1,
            "toolchain": {"run_venv": "overlay"},
            "approvals": {"decider": "user"},
        },
    )


def _run_input(provider: str, model: str, auth: str, agent: str) -> RunInput:
    return validate_model(
        RunInput,
        {
            "version": 1,
            "project": "demo",
            "task": "W3-06 check",
            "topology": {"kind": "solo"},
            "defaults": {"provider": provider, "model": model, "auth": auth, "effort": "low"},
            "agents": [{"id": agent}],
            "groupchats": [{"name": "main", "members": [agent]}],
        },
    )


def build_lab(provider: str, kind: AuthKind = AuthKind.CLAUDE_OAUTH) -> Lab:
    """A prepared run of one agent (`codex` | `claude`) in a fresh test `HOME`. Call it inside
    `lab_env`, see `open_lab`. `kind` is the login of a Claude profile."""
    base = Path(os.environ["ARR_VENV_ROOT"]).parent
    home = Path(os.environ["HOME"])
    settings = _settings()
    wf = init_workfolder(home / "wf")
    project = wf / "projects" / "demo"
    project.mkdir(parents=True, exist_ok=True)
    (project / "project.yaml").write_text("{}\n")
    if provider == "codex":
        model, agent_id = "gpt-6-luna", "gpt-1"
        auth = AuthProfile(name=CHATGPT_PROFILE, kind=AuthKind.CODEX_CHATGPT)
    else:
        model, agent_id = "claude-haiku-5-5", "claude-1"
        auth = AuthProfile(name=CLAUDE_PROFILE, kind=kind)
    app_dir().mkdir(parents=True, exist_ok=True)
    (app_dir() / "profiles.yaml").write_text(
        f"version: 1\nprofiles:\n  - {{name: {auth.name}, kind: {auth.kind.value}}}\n"
    )
    run_input = _run_input(provider, model, auth.name, agent_id)
    resolved = resolve_run(
        run_input, None, settings, project_settings=load_project_settings(project)
    )
    run = prepare_run(wf, run_input, resolved, settings)
    agent = run.agents[agent_id]
    run_paths = RunPaths(wf, run.run_dir, settings.toolchain)
    (run_paths.tmp_dir / "home" / agent_id).mkdir(parents=True, exist_ok=True)
    create_run_venv(run_paths, agent.file.base_python, with_pip=False)
    RoomStore.create(run.run_dir).setup_channels(resolved.groupchats, {})
    subprocess.run(["git", "init", "-q", str(agent.work_dir)], check=True)
    profile = profile_for(agent.file.config, run_paths, PermissionRules(), home=home)

    bait = f"ARR-GUARD-BAIT-{secrets.token_hex(12)}"
    baits: dict[str, Path] = {}
    for name, place in BAITS.items():
        path = home / place.format(profile=CHATGPT_PROFILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{bait}\n")
        path.chmod(0o600)
        baits[name] = path
    return Lab(
        base=base,
        home=home,
        workfolder=wf,
        run=run,
        agent=agent,
        run_paths=run_paths,
        profile=profile,
        auth=auth,
        bait=bait,
        baits=baits,
        keychain_file=baits["keychains"],
        service=f"arr-guard-{secrets.token_hex(6)}",
        token=f"sk-ant-oat01-GUARD{secrets.token_hex(16)}",
        job=f"arr.guard.{secrets.token_hex(6)}",
    )


@contextmanager
def open_lab(provider: str, kind: AuthKind = AuthKind.CLAUDE_OAUTH) -> Iterator[Lab]:
    base = Path(tempfile.mkdtemp(prefix="arr-guard-", dir="/private/tmp")).resolve()
    base.chmod(0o700)
    home = base / "home"
    home.mkdir()
    with lab_env(base, home):
        lab = build_lab(provider, kind)
        try:
            yield lab
        finally:
            subprocess.run(["/bin/launchctl", "remove", lab.job], capture_output=True, timeout=10)
            _remove_lab(base)


def _remove_lab(base: Path) -> None:
    """A process that stayed behind may still add a file while the folder goes."""
    for _ in range(50):
        shutil.rmtree(base, ignore_errors=True)
        if not base.exists():
            return
        _sleep()


def _free_listener() -> socket.socket:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener


def write_targets(lab: Lab) -> dict[str, str]:
    """Places outside the write roots, some of them lead to a login when changed."""
    tag = secrets.token_hex(4)
    parallel = lab.run_paths.run_dir.parent / "other-run"
    return {
        "HOME": str(lab.home / f"guard-{tag}.txt"),
        ".ssh/authorized_keys": str(lab.home / ".ssh" / "authorized_keys"),
        ".arr/profiles.yaml": str(lab.home / ".arr" / "profiles.yaml"),
        ".git/config": str(lab.agent.work_dir / ".git" / "config"),
        ".git/hooks": str(lab.agent.work_dir / ".git" / "hooks" / "post-commit"),
        "Login-Ordner": str(lab.baits["codex_auth"].parent / "config.toml"),
        ".claude/settings.json": str(lab.home / ".claude" / "settings.json"),
        ".codex/config.toml": str(lab.home / ".codex" / "config.toml"),
        "Workfolder": str(lab.workfolder / f"guard-{tag}.txt"),
        "anderer Run": str(parallel / "x.txt"),
        "/tmp": f"/private/tmp/arr-guard-{tag}.txt",
        "venv-Wurzel": str(lab.base / "venv-root" / f"guard-{tag}.txt"),
        "Startprogramm": str(lab.start_folder / "claude-start-guard"),
        "Startprogramm-Ordner": str(lab.start_folder / f"guard-{tag}"),
    }


def protected_parts() -> dict[str, str]:
    """Real files of the start program. The probe only opens them for writing, it never writes."""
    package = Path(arr.__file__).parent
    return {
        "Startmodul": str(package / "backends" / "claude_start.py"),
        "arr.auth": str(package / "auth.py"),
        "Interpreter": os.path.realpath(sys.executable),
        "Claude-CLI": str(bundled_cli()),
    }


def plan_for(lab: Lab, port: int) -> dict[str, Any]:
    work = lab.agent.work_dir
    control = work / "control.txt"
    control.write_text(f"{lab.bait}\n")
    targets = {"Kontrolle": str(control), **{n: str(p) for n, p in lab.baits.items()}}
    inodes = {}
    for path in targets.values():
        info = os.stat(path)
        inodes[path] = f"/.vol/{info.st_dev}/{info.st_ino}"
    return {
        "targets": targets,
        "bait": lab.bait,
        "markers": [lab.token],
        "token_names": list(TOKEN_NAMES),
        "write_targets": write_targets(lab),
        "protected": protected_parts(),
        "start_program": str(lab.start_folder / "claude-start-guard"),
        "inodes": inodes,
        "scratch": str(work / "scratch"),
        "keychain": {"file": str(lab.keychain_file), "service": lab.service},
        "port": port,
        "job": lab.job,
        "escaped": str(work / "escaped.txt"),
        "result": str(work / "result.json"),
        "alive": str(work / "background-alive"),
        "go": str(work / "background-go"),
        "background_result": str(work / "background.json"),
        "background_wait_s": BACKGROUND_WAIT_S,
    }


def _sandbox_folder(lab: Lab, entries: Mapping[str, str]) -> Path:
    folder = lab.base / "codex-config"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir()
    lines = [f'default_permissions = "{PERMISSION_PROFILE}"']
    lines += [f"[permissions.{PERMISSION_PROFILE}.filesystem]"]
    lines += [f"{toml_value(k)} = {toml_value(v)}" for k, v in entries.items()]
    (folder / "config.toml").write_text("\n".join(lines) + "\n")
    return folder


def codex_entries(lab: Lab) -> tuple[dict[str, str], dict[str, str], str]:
    """The agent's real permission map, the environment of its commands and its folder."""
    launch = build_launch(
        lab.agent,
        lab.profile,
        lab.run_paths,
        codex_home=codex_home_for(lab.auth),
        environ={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        run_channels=["main"],
    )
    return dict(launch.filesystem), dict(launch.shell_env), launch.cwd


def in_codex_sandbox(
    lab: Lab, entries: Mapping[str, str], env: Mapping[str, str], cwd: str, shell: str
) -> subprocess.CompletedProcess:
    full = {
        **env,
        "CODEX_HOME": str(_sandbox_folder(lab, entries)),
        "USER": os.environ.get("USER", "guard"),
        "SHELL": "/bin/zsh",
    }
    command = [str(bundled_codex_path()), "sandbox", "-P", PERMISSION_PROFILE, "-C", cwd, "--"]
    return subprocess.run(
        [*command, "/bin/zsh", "-c", shell],
        env=full,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=180,
    )


def _setup_command(plan: Mapping[str, Any]) -> str:
    """The shell line that writes plan and program inside the sandbox."""
    source = base64.b64encode(PROBE.read_bytes()).decode()
    data = base64.b64encode(json.dumps(plan).encode()).decode()
    return f"printf %s {source} | base64 -d > probe.py && printf %s {data} | base64 -d > plan.json"


def _read_result(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def _finish_background(plan: Mapping[str, Any]) -> dict[str, Any]:
    """End of the turn: release the process that stayed behind and collect its answers."""
    alive = Path(plan["alive"])
    Path(plan["go"]).write_text("")
    done = Path(plan["background_result"])
    for _ in range(600):
        if done.exists():
            try:
                return json.loads(done.read_text())
            except ValueError:
                pass  # still being written
        if not alive.exists():
            break
        _sleep()
    return {"rows": [], "ran": False, "alive_seen": alive.exists()}


def _sleep() -> None:
    subprocess.run(["/bin/sleep", "0.1"], check=False)


def in_codex(lab: Lab, plan: Mapping[str, Any], mode: str, entries=None) -> list[dict[str, Any]]:
    """One mode of the probe in the Codex sandbox; `entries` replaces the permission map."""
    real, env, cwd = codex_entries(lab)
    entries = real if entries is None else entries
    in_codex_sandbox(lab, entries, env, cwd, _setup_command(plan))
    done = in_codex_sandbox(lab, entries, env, cwd, f"python3 probe.py plan.json {mode}")
    assert done.returncode == 0, done.stderr[-300:]
    return json.loads(done.stdout)


def run_codex(lab: Lab, entries: dict[str, str] | None = None) -> dict[str, Any]:
    """The whole probe in the Codex sandbox, then the process that outlives the command."""
    listener = _free_listener()
    real, env, cwd = codex_entries(lab)
    entries = real if entries is None else entries
    plan = plan_for(lab, listener.getsockname()[1])
    try:
        steps = (
            _setup_command(plan),
            "python3 probe.py plan.json background",
            "python3 probe.py plan.json foreground",
        )
        answers = [in_codex_sandbox(lab, entries, env, cwd, step) for step in steps]
        result = {
            "exits": [a.returncode for a in answers],
            "errors": [a.stderr[-400:] for a in answers if a.returncode != 0],
        }
        if Path(plan["result"]).exists():
            result |= _read_result(plan["result"])
        result["background"] = _finish_background(plan)
        result["plan"] = plan
        return result
    finally:
        listener.close()


def guard_start(lab: Lab) -> Path:
    """The real start program with one difference: the secret comes from a file of the lab.

    Tests must not touch the keychain (W3-06 §12), so only the `reader` of `main` differs.
    """
    secret = lab.home / ".arr" / "guard-secret"
    secret.write_text(lab.token)
    secret.chmod(0o600)
    entry = (
        "from pathlib import Path; from arr.backends.claude_start import main; "
        f"main(reader=lambda service, account: Path({str(secret)!r}).read_text())"
    )
    shim = lab.start_folder / "claude-start-guard"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(shim_text(entry))
    shim.chmod(0o700)
    return shim


def fake_model_env(url: str) -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": url,
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def leaked(lab: Lab, options: ClaudeAgentOptions) -> ClaudeAgentOptions:
    """The mutation of A8: the secret back in the environment, as `claude_env` once did it."""
    name = {
        AuthKind.CLAUDE_OAUTH: "CLAUDE_CODE_OAUTH_TOKEN",
        AuthKind.CLAUDE_API_KEY: "ANTHROPIC_API_KEY",
    }[lab.auth.kind]
    env = {k: v for k, v in options.env.items() if k != START_PROFILE_VARIABLE}
    return replace(options, cli_path=None, env={**env, name: lab.token})


async def _claude_turn(
    lab: Lab, api: FakeMessagesApi, *, leak: bool = False, allow_in: Mapping[str, str] | None = None
) -> None:
    async def allow(_tool: str, tool_input: dict[str, Any], _context: Any) -> PermissionResultAllow:
        return PermissionResultAllow(updated_input=tool_input)

    link = tmp_link(lab.profile.tmp_dir, f"{lab.run.run_id}/{lab.agent.id}")
    env = claude_env(lab.auth, lambda _service, _account: lab.token) | fake_model_env(api.url)
    env |= {"HOME": str(lab.home), "ARR_HOME": str(lab.home / ".arr")}
    options = build_options(
        lab.agent, lab.profile, env=env, can_use_tool=allow, hooks=None, tmpdir=link
    )
    options = replace(options, max_budget_usd=None, cli_path=str(guard_start(lab)))
    if leak:
        options = leaked(lab, options)
    if allow_in:
        options = opened(options, allow_in)
    try:
        client = ClaudeSDKClient(options=options)
        await client.connect()
        try:
            await client.query("Starte die Probe.")
            async for _ in client.receive_response():
                pass
        finally:
            await client.disconnect()
    finally:
        remove_tmp_link(link, lab.profile.tmp_dir)


def opened(options: ClaudeAgentOptions, folder: Mapping[str, str]) -> ClaudeAgentOptions:
    """The mutation of A2: a read block of the sandbox settings is gone."""
    files = dict(options.sandbox["filesystem"])  # type: ignore[index]
    path = folder["path"]
    files["denyRead"] = [p for p in files["denyRead"] if not path.startswith(p.rstrip("/"))]
    files["denyRead"] = [p for p in files["denyRead"] if p != path]
    files["allowRead"] = [*files["allowRead"], path]
    return replace(options, sandbox={**options.sandbox, "filesystem": files})  # type: ignore[arg-type]


def bash(command: str, description: str = "Probe") -> ToolCall:
    return ToolCall("Bash", {"command": command, "description": description, "timeout": 170000})


def run_claude(lab: Lab) -> dict[str, Any]:
    """The whole probe through the real CLI and its sandbox, with a marker as login."""
    listener = _free_listener()
    plan = plan_for(lab, listener.getsockname()[1])
    script = [
        bash(_setup_command(plan), "Probe anlegen"),
        bash("python3 probe.py plan.json background", "Hintergrund"),
        bash("python3 probe.py plan.json foreground"),
    ]
    try:
        with FakeMessagesApi(script, credential=lab.token) as api:
            asyncio.run(asyncio.wait_for(_claude_turn(lab, api), 240))
        result: dict[str, Any] = {"tool_results": len(api.results), "errors": api.errors}
        result["texts"] = [text[-400:] for text in api.results]
        result["credential_seen"] = api.credential_seen
        if Path(plan["result"]).exists():
            result |= _read_result(plan["result"])
        result["background"] = _finish_background(plan)
        result["plan"] = plan
        return result
    finally:
        listener.close()


def in_claude(
    lab: Lab,
    mode: str,
    *,
    leak: bool = False,
    allow_in: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], FakeMessagesApi]:
    """One mode of the probe in the Claude sandbox, with the real CLI as the holder of the login."""
    plan = plan_for(lab, 9)
    script = [
        bash(_setup_command(plan), "Probe anlegen"),
        bash(f"python3 probe.py plan.json {mode} > answer.json"),
    ]
    with FakeMessagesApi(script, credential=lab.token) as api:
        asyncio.run(asyncio.wait_for(_claude_turn(lab, api, leak=leak, allow_in=allow_in), 240))
    assert not any(api.errors), [text[-200:] for text in api.results]
    return json.loads((lab.agent.work_dir / "answer.json").read_text()), api


WAIT_PROGRAM = """
import pathlib, time
pathlib.Path("waiting").write_text("")
while not pathlib.Path("go").exists():
    time.sleep(0.05)
"""


class HeldTurn:
    """A turn of the real CLI that stays open, so a second sandbox can look at the process."""

    def __init__(self, lab: Lab, *, leak: bool = False) -> None:
        self.lab = lab
        self.leak = leak
        self.work = lab.agent.work_dir
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.failure: BaseException | None = None

    def _run(self) -> None:
        (self.work / "wait.py").write_text(WAIT_PROGRAM)
        with FakeMessagesApi([bash("python3 wait.py", "warten")], credential=self.lab.token) as api:
            try:
                turn = _claude_turn(self.lab, api, leak=self.leak)
                asyncio.run(asyncio.wait_for(turn, 120))
            except BaseException as exc:  # noqa: BLE001 - reported by the test
                self.failure = exc

    def __enter__(self) -> "HeldTurn":
        self.thread.start()
        for _ in range(600):
            if (self.work / "waiting").exists() or not self.thread.is_alive():
                return self
            _sleep()
        raise AssertionError("the CLI did not reach the tool")

    def __exit__(self, *_: object) -> None:
        (self.work / "go").write_text("")
        self.thread.join(timeout=60)


FLIP_PROGRAM = r"""
import json, os, sys, time
link, safe, bad, hold_safe, hold_bad, seconds = json.load(open(sys.argv[1]))
if os.fork():
    os._exit(0)
os.setsid()
if os.fork():
    os._exit(0)
tmp = link + ".tmp"
end = time.time() + seconds
def turn(target):
    try:
        os.symlink(target, tmp)
        os.replace(tmp, link)
    except OSError:
        pass
while time.time() < end:
    turn(safe)
    time.sleep(hold_safe)
    turn(bad)
    time.sleep(hold_bad)
os._exit(0)
"""


async def _backend_turn(backend: ClaudeBackend) -> None:
    async for _ in backend.run_turn("Starte."):
        pass
    await backend.close()


def run_file_tools(lab: Lab, script: list[ToolCall], decider: Any = None) -> FakeMessagesApi:
    """`script` through the real backend: real hooks and `PolicyEngine`, outside the sandbox."""
    with FakeMessagesApi(script) as api:
        engine = PolicyEngine([lab.profile], log=ApprovalLog(lab.run.run_dir / "approvals.jsonl"))

        def factory(options: ClaudeAgentOptions) -> ClaudeSDKClient:
            env = {**options.env, **fake_model_env(api.url)}
            return ClaudeSDKClient(
                options=replace(
                    options, env=env, max_budget_usd=None, cli_path=str(guard_start(lab))
                )
            )

        backend = ClaudeBackend(
            lab.agent,
            run_id=lab.run.run_id,
            inbox=ApprovalInbox(engine, decider=decider, timeout=5 if decider else 0.05),
            auth=lab.auth,
            secret_reader=lambda _service, _account: lab.token,
            client_factory=factory,
        )
        asyncio.run(asyncio.wait_for(_backend_turn(backend), 200))
    return api
