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
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import PermissionResultAllow
from codex_cli_bin import bundled_codex_path
from fake_messages_api import FakeMessagesApi, ToolCall
from fake_responses_api import FakeResponsesApi, Patch, add_file

import arr
from arr.auth import (
    START_PROFILE_VARIABLE,
    claude_env,
    clean_process_env,
)
from arr.backends.claude import ClaudeBackend
from arr.backends.claude_options import build_options, remove_tmp_link, tmp_link
from arr.backends.claude_start import bundled_cli, shim_text
from arr.backends.codex import CodexBackend
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


def guard_bin(base: Path) -> Path:
    """A `security` that finds nothing, first on the `PATH` of the lab (W3-06.3).

    The real CLI asks the keychain for a login of its own, outside the sandbox and through its
    `PATH` (`find-generic-password -s "Claude Code-credentials" -w`). In the lab it gets „not
    found“, so the real keychain stays out (W3-06 §12). The probe inside the sandbox calls
    `/usr/bin/security` by its path and is not affected.
    """
    folder = base / "guard-bin"
    folder.mkdir(exist_ok=True)
    (folder / "security").write_text("#!/bin/sh\nexit 44\n")
    (folder / "security").chmod(0o755)
    return folder


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
        "PATH": f"{guard_bin(base)}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
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


def build_lab(
    provider: str, kind: AuthKind = AuthKind.CLAUDE_OAUTH, *, with_pip: bool = False
) -> Lab:
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
    (run_paths.agent_tmp_dir(agent.file.config) / "home").mkdir(parents=True, exist_ok=True)
    create_run_venv(run_paths, agent.file.base_python, with_pip=with_pip)
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
def open_lab(
    provider: str, kind: AuthKind = AuthKind.CLAUDE_OAUTH, *, with_pip: bool = False
) -> Iterator[Lab]:
    base = Path(tempfile.mkdtemp(prefix="arr-guard-", dir="/private/tmp")).resolve()
    base.chmod(0o700)
    home = base / "home"
    home.mkdir()
    with lab_env(base, home):
        lab = build_lab(provider, kind, with_pip=with_pip)
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
    guard_start(lab)  # the start program exists, as in a mixed run (review W3-06, B5)
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


async def _backend_turn(backend: Any, turns: int = 1) -> None:
    try:
        await backend.start()
        for _ in range(turns):
            async for _ in backend.run_turn("Starte."):
                pass
    finally:
        await backend.close()


def run_file_tools(
    lab: Lab, script: list[ToolCall], decider: Any = None, *, turns: int = 1
) -> FakeMessagesApi:
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
        asyncio.run(asyncio.wait_for(_backend_turn(backend, turns), 200))
    return api


def run_codex_backend(
    lab: Lab, script: list[str | Patch], decider: Any = None, *, turns: int = 1
) -> FakeResponsesApi:
    """`script` through the real `CodexBackend` and app-server (one per turn), no model.

    The app-server gets an empty `CODEX_HOME` of its own, the bait login folder stays as it is.
    """
    from openai_codex import AsyncCodex, CodexConfig

    home = lab.base / "codex-home"
    home.mkdir(exist_ok=True)
    with FakeResponsesApi(script) as api:
        engine = PolicyEngine([lab.profile], log=ApprovalLog(lab.run.run_dir / "approvals.jsonl"))

        def factory(config: CodexConfig) -> AsyncCodex:
            env = {**(config.env or {}), "CODEX_HOME": str(home)}
            overrides = (*config.config_overrides, *api.provider_overrides())
            return AsyncCodex(replace(config, env=env, config_overrides=overrides))

        backend = CodexBackend(
            lab.agent,
            run_id=lab.run.run_id,
            inbox=ApprovalInbox(engine, decider=decider, timeout=5 if decider else 0.05),
            auth=lab.auth,
            factory=factory,
        )
        asyncio.run(asyncio.wait_for(_backend_turn(backend, turns), 200))
    return api


# --------------------------------------------------------------------------------------------
# Programs outside the sandbox (W3-06.3, B1, B2)
# --------------------------------------------------------------------------------------------

STUB_NAMES = tuple(
    [
        "git",
        "security",
        "ps",
        "npm",
        "node",
        "env",
        "which",
        "uname",
        "sw_vers",
        "rg",
        "sh",
        "bash",
        "zsh",
        "uv",
        "pip",
        "pip3",
        "gh",
        "pgrep",
        "lsof",
        "tput",
        "stty",
        "osascript",
        "sysctl",
        "sandbox-exec",
        "cat",
        "ls",
        "sed",
        "grep",
        "find",
        "date",
        "id",
        "whoami",
        "hostname",
        "tr",
        "cut",
        "awk",
        "mktemp",
        "rm",
        "mkdir",
        "stat",
        "readlink",
        "dirname",
        "basename",
        "tail",
        "head",
        "wc",
        "sort",
        "xargs",
        "file",
    ]
)
"""Programs a process outside the sandbox could look up on its `PATH` (seen in the review of
W3-06: `git`, `security`, `ps`, `npm`, `env`), plus shells and common tools. Not `python`:
in the run venv it is a link to the real interpreter, and a stub never writes through it."""

SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
PATH_PROBES = {
    "venv": "arr-guard-venv",
    "prepend": "arr-guard-prepend",
    "inherited": "arr-guard-tmp",
}
"""One program per source of the agent's `PATH`, each in a write root."""


@dataclass
class Traps:
    outside: Path  # written only by a program that runs without the sandbox
    inside: Path  # in Run-tmp: the agent's commands report here
    shadow: Path  # written by an `arr/__init__.py` of the working folder
    folders: dict[str, Path]

    def outside_lines(self) -> list[str]:
        return self.outside.read_text().splitlines() if self.outside.exists() else []

    def inside_lines(self) -> list[str]:
        return self.inside.read_text().splitlines() if self.inside.exists() else []


def _stub(path: Path, text: str) -> None:
    if path.is_symlink() or path.exists():
        path.unlink()  # replace the entry itself, never write through a link out of the lab
    path.write_text(text)
    path.chmod(0o755)


def set_traps(lab: Lab) -> Traps:
    """A stub of every program in each write root on a `PATH`, and `arr/` in the working folder.

    The three sources of the agent's `PATH` each get a write root: the run venv, `path_prepend`
    (working folder) and the inherited `PATH` (Run-tmp). Every stub notes its name in a file
    outside every write root, which only a process outside the sandbox can write. The
    harness itself starts its programs by absolute path.
    """
    folders = {
        "venv": lab.run_paths.venv_dir / "bin",
        "prepend": lab.agent.work_dir / "bin",
        "inherited": lab.profile.tmp_dir / "bin",
    }
    traps = Traps(
        outside=lab.base / "ran-outside.log",
        inside=lab.profile.tmp_dir / "ran-inside.log",
        shadow=lab.base / "shadow-ran",
        folders=folders,
    )
    for source, folder in folders.items():
        folder.mkdir(parents=True, exist_ok=True)
        for name in STUB_NAMES:
            real = shutil.which(name, path=SYSTEM_PATH)
            tail = {"security": "exit 44", "git": "exit 128"}.get(name)
            tail = tail or (f'exec {real} "$@"' if real else "exit 127")
            note = f'echo "{name} {source}" >> {traps.outside} 2>/dev/null'
            _stub(folder / name, f"#!/bin/sh\n{note}\n{tail}\n")
        probe = PATH_PROBES[source]
        _stub(
            folder / probe,
            f'#!/bin/sh\necho "{probe}" >> {traps.outside} 2>/dev/null\n'
            f'echo "{probe}" >> {traps.inside}\n',
        )
    shadow = lab.agent.work_dir / "arr"
    shadow.mkdir()
    (shadow / "__init__.py").write_text(f"open({str(traps.shadow)!r}, 'w').write('ran')\n")
    file = lab.agent.file.model_copy(
        update={"path_prepend": [str(folders["prepend"]), *lab.agent.file.path_prepend]}
    )
    lab.agent = replace(lab.agent, file=file)
    guard = guard_bin(lab.base)
    os.environ["PATH"] = os.pathsep.join([str(folders["inherited"]), str(guard), SYSTEM_PATH])
    return traps


def trap_commands(traps: Traps) -> list[str]:
    """What the agent runs: one program of each source, then Python of the run venv."""
    probes = "; ".join(PATH_PROBES.values())
    python = f"python3 -c 'import sys; print(\"prefix\", sys.prefix)' >> {traps.inside}"
    return [f"{probes}; {python}", "git status; echo zwei"]


@contextmanager
def old_way() -> Iterator[None]:
    """The mutation: the whole agent `PATH` for CLI and app-server, `python -m` without `-I`."""
    from arr.backends import claude_options, codex_config

    saved = {
        module: (module.outside_path, module.ROOM_SERVER_ARGS)
        for module in (claude_options, codex_config)
    }
    for module in saved:
        module.outside_path = lambda entries, _roots: os.pathsep.join(entries)
        module.ROOM_SERVER_ARGS = ("-m", "arr.room.server")
    try:
        yield
    finally:
        for module, (path, args) in saved.items():
            module.outside_path, module.ROOM_SERVER_ARGS = path, args


def run_git_filter(lab: Lab, provider: str) -> list[str]:
    """The agent defines a clean filter in `$HOME/.gitconfig`, selects it in `.gitattributes`
    and touches a tracked file; the next CLI or app-server runs `git status` outside the
    sandbox. Returns what the filter noted outside every write root."""
    work = lab.agent.work_dir
    (work / "a.txt").write_text("hallo\n")
    git = ["/usr/bin/git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "a.txt"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True, capture_output=True)
    log = lab.base / "filter-ran.log"
    script = lab.profile.tmp_dir / "filter.sh"
    script.write_text(f"#!/bin/sh\necho filter >> {log} 2>/dev/null\ncat\n")
    script.chmod(0o755)
    plant = (
        f'/usr/bin/git config --file "$HOME/.gitconfig" filter.x.clean {script} && '
        "echo '* filter=x' > .gitattributes && touch -t 200101010000 a.txt && echo planted"
    )
    # Codex runs `git` in the background at the start of a turn: the next command gives it time
    wait = "sleep 2; echo zwei"
    if provider == "claude":
        for _ in range(2):
            run_file_tools(lab, [bash(plant), bash(wait)], turns=2)
    else:
        run_codex_backend(lab, [plant, wait, wait], turns=3)
    return log.read_text().splitlines() if log.exists() else []


@contextmanager
def old_home() -> Iterator[None]:
    """The mutation: the app-server gets the writable `HOME` of the commands again."""
    from arr.backends import codex_config

    saved = codex_config.server_home
    codex_config.server_home = codex_config.command_home
    try:
        yield
    finally:
        codex_config.server_home = saved


# --------------------------------------------------------------------------------------------
# Names the sandbox blocks for Bash, against the file tools (W3-06.4, N1)
# --------------------------------------------------------------------------------------------

WRITE_CANDIDATES = tuple(
    [
        # the CLI's own lists (2.1.310): shell, Git and tool settings, editors, `.claude`
        ".gitconfig",
        ".gitmodules",
        ".gitattributes",
        ".lfsconfig",
        ".bashrc",
        ".bash_profile",
        ".bash_login",
        ".bash_logout",
        ".bash_aliases",
        ".profile",
        ".zshrc",
        ".zprofile",
        ".zshenv",
        ".zlogin",
        ".zlogout",
        ".envrc",
        ".ripgreprc",
        ".mcp.json",
        ".claude.json",
        ".lsp.json",
        ".npmrc",
        ".yarnrc",
        ".yarnrc.yml",
        ".pnp.cjs",
        ".pnpmfile.cjs",
        "bunfig.toml",
        ".bazelrc",
        ".pre-commit-config.yaml",
        "lefthook.yml",
        ".devcontainer.json",
        "pyrightconfig.json",
        ".worktreeinclude",
        "CLAUDE.md",
        "CLAUDE.local.md",
        "AGENTS.md",
        ".vscode/settings.json",
        ".idea/workspace.xml",
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/commands/x.md",
        ".claude/agents/x.md",
        ".claude/skills/x/SKILL.md",
        ".claude/x.txt",
        ".claude-plugin/plugin.json",
        # Codex
        ".codex/config.toml",
        ".agents/skills/x/SKILL.md",
        # Git: a repo, a bare repo at the top, spellings
        ".git",
        ".GIT",
        ".git/config",
        ".git/hooks/post-commit",
        ".git/info/attributes",
        ".git/HEAD",
        ".git/objects/x",
        "HEAD",
        "config",
        "objects/x",
        "refs/heads/x",
        "sub/.git",
        "sub/.gitmodules",
        "sub/.bashrc",
        "sub/HEAD",
        ".GITATTRIBUTES",
        ".Gitmodules",
    ]
)
"""Paths below a write root to try with Bash: what the CLI, Codex and arr protect, and more."""

WRITE_PROGRAM = r"""
import json, os, subprocess, sys
plan = json.load(open(sys.argv[1])); out = {}
git = subprocess.run(["git", "status", "--porcelain"], cwd=plan["roots"][0], capture_output=True)
out["git status"] = str(git.returncode)
for root in plan["roots"]:
    for rel in plan["paths"]:
        path = os.path.join(root, rel)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a"):
                pass
            out[path] = "ok"
        except OSError as exc:
            out[path] = type(exc).__name__
json.dump(out, open(plan["out"], "w"))
"""


def bash_writes(lab: Lab, provider: str, paths: Sequence[str]) -> dict[str, str]:
    """Try to create `paths` in every write root with Bash in the real sandbox; path → `ok`
    or the error. Nothing is cleaned up: call it once per lab and state."""
    work = lab.agent.work_dir
    out = work / f"writes-{secrets.token_hex(4)}.json"
    plan = work / f"{out.stem}-plan.json"
    roots = [str(root) for root in lab.profile.write_roots]
    plan.write_text(json.dumps({"roots": roots, "paths": list(paths), "out": str(out)}))
    (work / "writes.py").write_text(WRITE_PROGRAM)
    command = f"python3 writes.py {plan.name}"
    if provider == "claude":
        run_file_tools(lab, [bash(command)])
    else:
        run_codex_backend(lab, [command])
    return json.loads(out.read_text())


def _state(path: str) -> bytes | str | None:
    if os.path.isdir(path):
        return "dir"
    return Path(path).read_bytes() if os.path.lexists(path) else None


def apply_patches(lab: Lab, paths: Sequence[str], decider: Any = None) -> list[str]:
    """Codex's own `apply_patch` tool on each path: a new file, or one more line in a file that
    is there (W3-06.4). Returns the paths it changed."""
    before = {path: _state(path) for path in paths}
    script: list[str | Patch] = []
    for path, state in before.items():
        if state is None:
            script.append(add_file(path, "# x"))  # valid TOML, Git and shell text
        elif isinstance(state, bytes) and state.strip():
            first = state.decode(errors="replace").splitlines()[0]
            text = f"*** Begin Patch\n*** Update File: {path}\n@@\n {first}\n+# x\n*** End Patch\n"
            script.append(Patch(text))
    if script:
        run_codex_backend(lab, script, decider)
    return [path for path in paths if _state(path) != before[path]]


@contextmanager
def no_protection() -> Iterator[None]:
    """The mutation: no protected names, neither in the `PolicyEngine` nor in `denyWrite` nor
    in the Codex profile."""
    from arr.backends import codex_config
    from arr.permissions import policy

    saved = policy.SandboxProfile.is_protected, policy.protected_write_globs
    names = codex_config.CODEX_READ_ONLY_NAMES
    policy.SandboxProfile.is_protected = lambda _self, _path: False  # type: ignore[method-assign]
    policy.protected_write_globs = lambda _profile: []
    codex_config.CODEX_READ_ONLY_NAMES = ()
    try:
        yield
    finally:
        policy.SandboxProfile.is_protected, policy.protected_write_globs = saved
        codex_config.CODEX_READ_ONLY_NAMES = names


def _old_file_request(self: Any, params: dict[str, Any]) -> Any:
    """`_file_request` before W3-06.5: only the source path, no move target, grantRoot as a
    plain path. The countertest for N5 restores this."""
    item = self._items.get(str(params.get("itemId")), {})
    paths = [str(c["path"]) for c in item.get("changes") or [] if c.get("path")]
    if params.get("grantRoot"):
        paths.append(str(params["grantRoot"]))
    return self._request(
        tool="fileChange",
        tool_call_id=params.get("itemId"),
        paths=paths,
        work_dir=self.work_dir,
        reason=params.get("reason"),
    )


@contextmanager
def old_file_request() -> Iterator[None]:
    """The mutation of W3-06.5: the file request names only the source, as before (N5)."""
    from arr.backends import codex_approvals

    saved = codex_approvals.ApprovalBridge._file_request
    codex_approvals.ApprovalBridge._file_request = _old_file_request  # type: ignore[method-assign]
    try:
        yield
    finally:
        codex_approvals.ApprovalBridge._file_request = saved  # type: ignore[method-assign]


def apply_raw_patches(lab: Lab, patches: list[Patch | str], decider: Any = None) -> None:
    """Run raw apply_patch steps (move, delete, grantRoot can't be built from a path alone)."""
    run_codex_backend(lab, [*patches, "echo ende"], decider, turns=1)


def move_patch(src: str, dst: str) -> Patch:
    body = f"*** Update File: {src}\n*** Move to: {dst}\n@@\n eins\n+zwei\n"
    return Patch(f"*** Begin Patch\n{body}*** End Patch\n")


def delete_patch(path: str) -> Patch:
    return Patch(f"*** Begin Patch\n*** Delete File: {path}\n*** End Patch\n")


# --------------------------------------------------------------------------------------------
# No `git` outside the sandbox (W3-06.4, 3b)
# --------------------------------------------------------------------------------------------


def lay_filter_repo(lab: Lab) -> Path:
    """The lab's repo with a clean and a process filter that note outside every write root,
    selected for a changed tracked file: what a project could bring along. Returns the note."""
    work = lab.agent.work_dir
    note = lab.base / "filter-outside.log"
    driver = lab.base / "filter.sh"
    driver.write_text(f"#!/bin/sh\necho filter >> {note}\ncat\n")
    driver.chmod(0o755)
    git = ["/usr/bin/git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t"]
    (work / "a.txt").write_text("eins\n")
    subprocess.run([*git, "add", "a.txt"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True, capture_output=True)
    subprocess.run([*git, "config", "filter.x.clean", str(driver)], check=True)
    subprocess.run([*git, "config", "filter.y.process", str(driver)], check=True)
    (work / ".gitattributes").write_text("*.txt filter=x\n*.md filter=y\n")
    (work / "a.txt").write_text("zwei\n")
    return note


def run_with_filter_repo(lab: Lab, provider: str) -> dict[str, Any]:
    """Two CLI starts with two turns each, or three app-servers; the agent runs `git` in its
    shell. Returns how often a filter ran outside and the exit codes of the agent's `git`."""
    note = lay_filter_repo(lab)
    inside = lab.profile.tmp_dir / "git-inside.log"
    commands = [f"git status --porcelain > /dev/null; echo $? >> {inside}; sleep 2", "echo zwei"]
    if provider == "claude":
        for _ in range(2):
            run_file_tools(lab, [bash(command) for command in commands], turns=2)
    else:
        run_codex_backend(lab, [*commands, "sleep 2; echo drei"], turns=3)
    return {
        "outside": note.read_text().count("filter") if note.exists() else 0,
        "inside": inside.read_text().split() if inside.exists() else [],
    }


@contextmanager
def with_git() -> Iterator[None]:
    """The mutation: no `git` stub first on the `PATH` outside the sandbox."""
    from arr.backends import claude_options, codex_config

    saved = {module: module.no_git_folder for module in (claude_options, codex_config)}
    for module in saved:
        module.no_git_folder = lambda _roots: Path("/nonexistent-arr-no-git")
    try:
        yield
    finally:
        for module, function in saved.items():
            module.no_git_folder = function


# --------------------------------------------------------------------------------------------
# The chains of the review of W3-06.3 (N1) and of W3-06.4, 3a and 3c
# --------------------------------------------------------------------------------------------

EVIL_REPO_PROGRAM = r"""
import json, os, subprocess
stolen, bait = json.load(open("chain.json"))
def git(*args):
    return subprocess.run(["git", "--git-dir=evil", "--work-tree=.", *args], capture_output=True)
subprocess.run(["git", "init", "-q", "--bare", "evil"], capture_output=True)
open("a.txt", "w").write("eins\n")
git("add", "a.txt")
git("-c", "user.name=a", "-c", "user.email=a@b", "commit", "-qm", "x")
open("x.sh", "w").write(f"#!/bin/sh\ncat {bait} > {stolen} 2>/dev/null\ncat\n")
os.chmod("x.sh", 0o755)
with open("evil/config", "a") as config:
    config.write(f"[filter \"x\"]\n\tclean = {os.path.abspath('x.sh')}\n")
    config.write(f"[core]\n\tbare = false\n\tworktree = {os.getcwd()}\n")
try:
    open(".gitattributes", "w").write("* filter=x\n")
except OSError:
    pass
open("a.txt", "w").write("zwei\n")
print("gebaut")
"""


def stolen_bait(lab: Lab) -> bool:
    """Whether a filter outside the sandbox copied the bait to `lab.base` (no write root)."""
    stolen = lab.base / "stolen.txt"
    return stolen.exists() and lab.bait in stolen.read_text()


def claude_git_chain(lab: Lab) -> dict[str, Any]:
    """`gitfile_e2e_claude.py` of the review: Bash builds a repo `evil/` with a filter, the
    Write tool lays `.git` (`gitdir: evil`), the next CLI start runs `git` in the folder."""
    work = lab.agent.work_dir
    shutil.rmtree(work / ".git")
    (work / "w.py").write_text(EVIL_REPO_PROGRAM)
    # the paths in a file: a command that names `~/.ssh` is refused before it runs
    (work / "chain.json").write_text(
        json.dumps([str(lab.base / "stolen.txt"), str(lab.baits["ssh"])])
    )
    build = bash("python3 w.py")
    lay = ToolCall("Write", {"file_path": str(work / ".git"), "content": "gitdir: evil\n"})
    api = run_file_tools(lab, [build, lay, ToolCall("Read", {"file_path": str(work / "a.txt")})])
    run_file_tools(lab, [ToolCall("Read", {"file_path": str(work / "a.txt")}), bash("echo zwei")])
    return {"errors": api.errors, "git_laid": os.path.lexists(work / ".git")}


def codex_git_chain(lab: Lab) -> dict[str, Any]:
    """3a: `apply_patch` puts a filter into `.git/config` and selects it in `.gitattributes`,
    the agent touches a tracked file, the next app-server runs `git` in the folder."""
    work = lab.agent.work_dir
    git = ["/usr/bin/git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t"]
    (work / "a.txt").write_text("eins\n")
    subprocess.run([*git, "add", "a.txt"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True, capture_output=True)
    config = work / ".git" / "config"
    first = config.read_text().splitlines()[0]
    filter_line = f'+[filter "x"]\n+\tclean = {work / "x.sh"}\n'
    script: list[str | Patch] = [
        f"printf '#!/bin/sh\\ncat {lab.baits['ssh']} > {lab.base / 'stolen.txt'} 2>/dev/null\\n"
        "cat\\n' > x.sh && chmod +x x.sh && echo zwei > a.txt && echo gebaut",
        Patch(
            f"*** Begin Patch\n*** Update File: .git/config\n@@\n {first}\n{filter_line}"
            "*** End Patch\n"
        ),
        add_file(".gitattributes", "* filter=x"),
        "sleep 2; echo weiter",
        "sleep 2; echo ende",
    ]
    api = run_codex_backend(lab, script, turns=3)
    return {"outputs": api.outputs, "config_changed": "filter" in config.read_text()}


def codex_mcp_chain(lab: Lab) -> dict[str, Any]:
    """3c: an MCP server in `.codex/config.toml`, laid during the run (by `apply_patch` while
    the walls of 3a are off); the next app-server would start it outside the sandbox."""
    marker = lab.base / "mcp-ran"
    lines = ["[mcp_servers.evil]", 'command = "/bin/sh"', f'args = ["-c", "touch {marker}"]']
    patch = "".join(f"+{line}\n" for line in lines)
    script: list[str | Patch] = [
        Patch(f"*** Begin Patch\n*** Add File: .codex/config.toml\n{patch}*** End Patch\n"),
        "sleep 3; echo zwei",
        "sleep 3; echo drei",
    ]
    api = run_codex_backend(lab, script, turns=3)
    return {
        "laid": (lab.agent.work_dir / ".codex" / "config.toml").exists(),
        "ran": marker.exists(),
        "outputs": api.outputs,
    }


@contextmanager
def no_project_check() -> Iterator[None]:
    """The mutation of 3c: no comparison of `.codex`, and arr disables no project MCP server."""
    from arr.backends import codex, codex_config

    saved = codex.CodexBackend._check_project_config, codex_config.project_mcp_servers
    codex.CodexBackend._check_project_config = lambda _self: None  # type: ignore[method-assign]
    codex_config.project_mcp_servers = lambda _cwd: []
    try:
        yield
    finally:
        codex.CodexBackend._check_project_config, codex_config.project_mcp_servers = saved


@contextmanager
def no_name_list() -> Iterator[None]:
    """arr disables no project MCP server by name: only the comparison of 3c is left."""
    from arr.backends import codex_config

    saved = codex_config.project_mcp_servers
    codex_config.project_mcp_servers = lambda _cwd: []
    try:
        yield
    finally:
        codex_config.project_mcp_servers = saved


@contextmanager
def with_node_cache() -> Iterator[None]:
    """The mutation of Punkt 5: Node's compile cache stays on for the CLI and its `npm`."""
    from arr.backends import claude_options

    saved = claude_options.NODE_CACHE_SWITCH
    claude_options.NODE_CACHE_SWITCH = "ARR_GUARD_UNUSED"
    try:
        yield
    finally:
        claude_options.NODE_CACHE_SWITCH = saved
