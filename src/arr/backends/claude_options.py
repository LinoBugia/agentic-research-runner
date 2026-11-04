"""`ClaudeAgentOptions` of one research agent (docs/01-architektur.md §4.1, S1 §3.1, E19).

The permission part comes from `claude_options(profile)` (PolicyEngine, W1-05): `cwd`,
`add_dirs` (writable run folders only), scoped `allowed_tools`/`disallowed_tools`, the sandbox
with the read allowlist. This module adds the rest: fixed tool set, system prompt (preset
`claude_code` + role text + static arr part, byte-identical per role), no setting sources,
the generated plugin with its skill list, only the `arr-room` MCP server, adaptive thinking
and the process environment (`TMPDIR` = Run-tmp). The CLI's own `PATH` holds no folder an
agent can write to; the agent's `PATH` (run venv, `path_prepend`) reaches only its Bash
commands, through `CLAUDE_ENV_FILE` (W3-06.3, B1).
"""

import contextlib
import hashlib
import os
import shlex
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
from claude_agent_sdk.types import CanUseTool, SystemPromptPreset

from arr.auth import START_PROFILE_VARIABLE
from arr.backends.claude_start import start_program
from arr.backends.process_path import no_git_folder, outside_path, within
from arr.errors import ArrError
from arr.models import Effort
from arr.permissions.policy import ROOM_TOOL_PREFIX, SandboxProfile, claude_options
from arr.room.server import AGENT_ID_ENV, RUN_DIR_ENV
from arr.runs.prepare import PreparedAgent

ROOM_SERVER = ROOM_TOOL_PREFIX.removeprefix("mcp__").removesuffix("__")
"""Name of the room MCP server; its tools are `mcp__arr-room__*` (W1-05, N8)."""

BASE_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill")
"""Fixed built-in tool set: a stable cache prefix (S1 §3.1). `Skill` must be listed."""

WEB_TOOLS = ("WebFetch", "WebSearch")

THINKING = {"type": "adaptive", "display": "summarized"}

PERMISSION_MODE = "default"
"""Every Bash, Write and Edit call goes to `can_use_tool` (PolicyEngine, then the inbox). The
scrub switch `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` makes the CLI force `default` anyway (W2-01.1);
set explicitly so the options say what happens (Nachprüfung W2-01.1 N2, W2-05)."""

ROOM_SERVER_ARGS = ("-I", "-m", "arr.room.server")
"""Start of the room server with arr's interpreter (see `room_server`)."""

SHELL_ENV_FILE = "shell-env.sh"
"""Name of the `CLAUDE_ENV_FILE` in the agent's folder (see `shell_env_file`)."""

TMP_LINK_ROOT = Path("/tmp")
"""Where the short alias of Run-tmp lives (see `tmp_link`)."""

_EFFORT = {
    Effort.NONE: "low",
    Effort.MINIMAL: "low",
    Effort.LOW: "low",
    Effort.MEDIUM: "medium",
    Effort.HIGH: "high",
    Effort.XHIGH: "xhigh",
    Effort.MAX: "max",
    Effort.ULTRA: "max",
}
"""arr effort → Claude effort (`low` … `max`); Codex-only values map to the nearest level."""


def static_prompt() -> str:
    """Static arr part of the system prompt (`templates/prompts/claude.md`)."""
    node = resources.files("arr").joinpath("templates", "prompts", "claude.md")
    return node.read_text(encoding="utf-8")


def system_prompt(agent: PreparedAgent) -> SystemPromptPreset:
    """Preset `claude_code` + `append` (role text + static arr part), no dynamic sections.

    Nothing run-specific goes in here, so agents with the same role share the cache prefix
    (S1 §2.3, §3.3). Run ID, channels and project context come with the first prompt (E10).
    """
    role = (agent.run_dir / agent.file.role_file).read_text(encoding="utf-8").strip()
    append = f"{role}\n\n{static_prompt().strip()}\n" if role else static_prompt()
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": append,
        "exclude_dynamic_sections": True,
    }


def agent_path(agent: PreparedAgent, venv_dir: Path | None, base_path: str | None = None) -> str:
    """`PATH` of the agent: run venv `bin/`, then `path_prepend`, then the inherited `PATH`.

    The run venv (`SandboxProfile.venv_dir`, outside `~`; None with `run_venv: off`) comes
    first so `pip install` lands there and never in the base venv (docs/07 §2); the
    RunManager creates it before the agents start. Only the Bash commands get this `PATH`
    (`shell_env_file`); the CLI runs with `outside_path` of it (W3-06.3, B1).
    """
    parts: list[str] = []
    if venv_dir is not None:
        parts.append(str(venv_dir / "bin"))
    parts += agent.file.path_prepend
    inherited = os.environ.get("PATH", os.defpath) if base_path is None else base_path
    parts += [p for p in inherited.split(os.pathsep) if p]
    return os.pathsep.join(dict.fromkeys(parts))


def python_env(venv_dir: Path | None) -> dict[str, str]:
    """Python variables of the agent (W3-00 live check): `VIRTUAL_ENV` names the run venv,
    never arr's own (`uv run arr` sets it), and Python skips the user site folder below `~`.

    The run venv sees the system site packages (`--system-site-packages`), so Python would
    look for `~/Library/Python/…` and `pip list` fails behind the read block on `~`.
    """
    return {
        "VIRTUAL_ENV": str(venv_dir) if venv_dir is not None else "",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
        "PYTHONHOME": "",
    }


def shell_env_file(agent: PreparedAgent, profile: SandboxProfile, bash_path: str) -> Path:
    """Write `agents/<id>/shell-env.sh`, the file of `CLAUDE_ENV_FILE` (W3-06.3, B1).

    The CLI reads it itself and puts it in front of every Bash command, so the run venv
    reaches the commands inside the sandbox without being on the CLI's own `PATH`
    (measured with the real CLI). The folder is no write root: an agent cannot change what
    the next command runs with. Raises `ArrError` if it is one.
    """
    folder = agent.agent_dir
    path = folder / SHELL_ENV_FILE
    if any(within(folder, root) for root in profile.write_roots):
        raise ArrError(
            "Der Claude-Agent startet nicht: Der Ordner für die Umgebung seiner Befehle liegt "
            f"in einem Ordner, in den Agenten schreiben dürfen ({folder})."
        )
    venv = python_env(profile.venv_dir)["VIRTUAL_ENV"]
    text = f"export PATH={shlex.quote(bash_path)}\nexport VIRTUAL_ENV={shlex.quote(venv)}\n"
    folder.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=f".{SHELL_ENV_FILE}.")
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        file.write(text)
    os.replace(temp, path)
    return path


def tmp_link(target: Path, key: str, root: Path = TMP_LINK_ROOT) -> Path:
    """Short alias `/tmp/arr-<hash>` → Run-tmp, used as `TMPDIR` and `CLAUDE_CODE_TMPDIR`.

    The CLI puts the sockets of its sandbox proxy into `TMPDIR`; macOS allows only 104 bytes
    for an AF_UNIX path, and a Run-tmp below the home directory is often longer (E19 test:
    `ENAMETOOLONG`, the sandbox did not start). Inside the sandbox the CLI gives Bash its
    per-uid temp folder below `CLAUDE_CODE_TMPDIR` (default `/tmp/claude-<uid>`, shared by
    all agents and Claude Code sessions of the user) and only honours an override of at
    most ~30 bytes. With the alias, files still land in Run-tmp. The name depends
    on `key` and the target, so a restarted agent reuses its link. Creates Run-tmp if
    missing. Remove the link with `remove_tmp_link`.

    An existing entry is reused only if it is a symlink of this user to `target`; anything
    else (another target, another owner, a file) raises `ArrError` (W2-01.1, B5).
    """
    target.mkdir(parents=True, exist_ok=True)  # prepare_run does not create Run-tmp
    digest = hashlib.sha1(f"{key}\0{target}".encode()).hexdigest()[:12]
    link = root / f"arr-{digest}"
    try:
        os.symlink(target, link)
    except FileExistsError:
        if not _own_link(link, target):
            raise ArrError(
                f"Der Kurzpfad {link} für den Temp-Ordner des Agenten ist schon belegt "
                "(anderes Ziel oder anderer Besitzer). Entferne ihn und starte den Agenten neu."
            ) from None
    return link


def _own_link(link: Path, target: Path) -> bool:
    """Whether `link` is a symlink of this user that points to `target`."""
    try:
        info = os.lstat(link)
        return (
            stat.S_ISLNK(info.st_mode)
            and info.st_uid == os.getuid()
            and os.readlink(link) == str(target)
        )
    except OSError:
        return False


def remove_tmp_link(link: Path, target: Path) -> None:
    """Remove a link made by `tmp_link`; an entry with another target or owner stays."""
    if not _own_link(link, target):
        return
    with contextlib.suppress(FileNotFoundError):
        link.unlink()


NODE_CACHE_SWITCH = "NODE_DISABLE_COMPILE_CACHE"
"""Node keeps compiled code in `$TMPDIR/node-compile-cache` and loads it on the next start."""


def cache_env(run_tmp: Path) -> dict[str, str]:
    """Caches of pip, matplotlib and XDG tools in Run-tmp: `~` is not writable in the sandbox
    (W1-05 §7.2), and without this pip warns and works without a cache."""
    cache = run_tmp / "cache"
    return {
        "XDG_CACHE_HOME": str(cache),
        "PIP_CACHE_DIR": str(cache / "pip"),
        "MPLCONFIGDIR": str(cache / "matplotlib"),
    }


def room_server(agent: PreparedAgent, outside_path: str | None = None) -> dict[str, Any]:
    """stdio config of the room server for this agent (docs/01 §5).

    The CLI starts it outside the sandbox in the working folder, a write root: `-I` keeps the
    `cwd` out of `sys.path` (and `PYTHON*`, the user site), so an `arr/` written by an agent
    is never imported (W3-06.3, B2). `outside_path` is the CLI's own `PATH`, with the `git`
    stub first (W3-06.4).
    """
    path = {"PATH": outside_path} if outside_path is not None else {}
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": [*ROOM_SERVER_ARGS],
        "env": {AGENT_ID_ENV: agent.id, RUN_DIR_ENV: str(agent.run_dir), **path},
    }


def tool_set(agent: PreparedAgent) -> list[str]:
    """Built-in tools: the base set, without `Bash` for `tools.shell: false`, web on demand."""
    tools = agent.file.config.tools
    names = [t for t in BASE_TOOLS if tools.shell or t != "Bash"]
    return names + (list(WEB_TOOLS) if tools.web else [])


def build_options(
    agent: PreparedAgent,
    profile: SandboxProfile,
    *,
    env: Mapping[str, str],
    can_use_tool: CanUseTool | None,
    hooks: Mapping[str, Sequence[HookMatcher]] | None,
    max_budget_usd: float | None = None,
    tmpdir: Path | None = None,
    stderr: Any = None,
) -> ClaudeAgentOptions:
    """All options of one agent; `env` holds the auth part (`arr.auth.claude_env`).

    An `env` that names a profile for the start program gets it as `cli_path`: only that
    program hands the secret to the CLI (`arr.backends.claude_start`). `tmpdir` replaces
    `TMPDIR` (Run-tmp) with its short alias from `tmp_link` and also becomes
    `CLAUDE_CODE_TMPDIR`, the temp root the CLI gives sandboxed commands.
    """
    config = agent.file.config
    perms = claude_options(profile)
    plugin = agent.plugin_dir
    # one source for the run venv: the profile (W3-00.1 G4), as for add_dirs and the sandbox
    bash_path = agent_path(agent, profile.venv_dir)
    process_env = {**env, **perms.env, **python_env(profile.venv_dir)}
    # the CLI runs outside the sandbox: nothing on its PATH may come from an agent (B1)
    # and no `git` at all there: the first `git` on it does nothing (W3-06.4, 3b)
    no_git = str(no_git_folder(profile.write_roots))
    outside = outside_path([no_git, *bash_path.split(os.pathsep)], profile.write_roots)
    process_env["PATH"] = outside
    process_env["VIRTUAL_ENV"] = ""
    process_env["CLAUDE_ENV_FILE"] = str(shell_env_file(agent, profile, bash_path))
    process_env.update(cache_env(Path(perms.env["TMPDIR"])))
    # `npm` of the CLI runs outside the sandbox and would load its compile cache from TMPDIR,
    # a write root (W3-06.4, Punkt 5)
    process_env[NODE_CACHE_SWITCH] = "1"
    if tmpdir is not None:
        process_env["TMPDIR"] = process_env["CLAUDE_CODE_TMPDIR"] = str(tmpdir)
    needs_start = START_PROFILE_VARIABLE in env
    return ClaudeAgentOptions(
        cli_path=start_program(profile.write_roots) if needs_start else None,
        model=config.model,
        cwd=Path(perms.cwd),
        add_dirs=list(perms.add_dirs),
        tools=tool_set(agent),
        allowed_tools=list(perms.allowed_tools),
        disallowed_tools=list(perms.disallowed_tools),
        permission_mode=PERMISSION_MODE,  # not `perms.permission_mode` (acceptEdits), see above
        sandbox=perms.sandbox,  # type: ignore[arg-type]
        system_prompt=system_prompt(agent),
        setting_sources=[],
        plugins=[{"type": "local", "path": str(plugin)}] if plugin else [],
        skills=list(agent.file.skill_ids),
        strict_mcp_config=True,
        mcp_servers={ROOM_SERVER: room_server(agent, outside)},  # type: ignore[dict-item]
        can_use_tool=can_use_tool,
        hooks={k: list(v) for k, v in hooks.items()} if hooks else None,  # type: ignore[misc]
        thinking=THINKING,  # type: ignore[arg-type]
        effort=_EFFORT[config.effort] if config.effort else None,  # type: ignore[arg-type]
        max_budget_usd=max_budget_usd,
        env=process_env,
        stderr=stderr,
    )
