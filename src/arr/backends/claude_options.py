"""`ClaudeAgentOptions` of one research agent (docs/01-architektur.md §4.1, S1 §3.1, E19).

The permission part comes from `claude_options(profile)` (PolicyEngine, W1-05): `cwd`,
`add_dirs` (writable run folders only), scoped `allowed_tools`/`disallowed_tools`, the sandbox
with the read allowlist. This module adds the rest: fixed tool set, system prompt (preset
`claude_code` + role text + static arr part, byte-identical per role), no setting sources,
the generated plugin with its skill list, only the `arr-room` MCP server, adaptive thinking
and the process environment (`PATH` with `path_prepend`, `TMPDIR` = Run-tmp).
"""

import hashlib
import os
import sys
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
from claude_agent_sdk.types import CanUseTool, SystemPromptPreset

from arr.models import Effort, RunVenvMode
from arr.permissions.policy import ROOM_TOOL_PREFIX, SandboxProfile, claude_options
from arr.room.server import AGENT_ID_ENV, RUN_DIR_ENV
from arr.runs.prepare import PreparedAgent

ROOM_SERVER = ROOM_TOOL_PREFIX.removeprefix("mcp__").removesuffix("__")
"""Name of the room MCP server; its tools are `mcp__arr-room__*` (W1-05, N8)."""

BASE_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep", "Bash", "Skill")
"""Fixed built-in tool set: a stable cache prefix (S1 §3.1). `Skill` must be listed."""

WEB_TOOLS = ("WebFetch", "WebSearch")

THINKING = {"type": "adaptive", "display": "summarized"}

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


def agent_path(agent: PreparedAgent, base_path: str | None = None) -> str:
    """`PATH` of the agent: run venv `bin/`, then `path_prepend`, then the inherited `PATH`.

    The run venv comes first so `pip install` lands there and never in the base venv
    (docs/07 §2); the RunManager creates it before the agents start.
    """
    parts: list[str] = []
    if agent.file.run_venv is RunVenvMode.OVERLAY:
        parts.append(str(agent.run_dir / ".venv" / "bin"))
    parts += agent.file.path_prepend
    inherited = os.environ.get("PATH", os.defpath) if base_path is None else base_path
    parts += [p for p in inherited.split(os.pathsep) if p]
    return os.pathsep.join(dict.fromkeys(parts))


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
    """
    target.mkdir(parents=True, exist_ok=True)  # prepare_run does not create Run-tmp
    digest = hashlib.sha1(f"{key}\0{target}".encode()).hexdigest()[:12]
    link = root / f"arr-{digest}"
    try:
        os.symlink(target, link)
    except FileExistsError:
        if not (link.is_symlink() and os.readlink(link) == str(target)):
            raise
    return link


def remove_tmp_link(link: Path) -> None:
    """Remove a link made by `tmp_link`; anything that is not a symlink stays."""
    if link.is_symlink():
        link.unlink(missing_ok=True)


def room_server(agent: PreparedAgent) -> dict[str, Any]:
    """stdio config of the room server for this agent (docs/01 §5)."""
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "arr.room.server"],
        "env": {AGENT_ID_ENV: agent.id, RUN_DIR_ENV: str(agent.run_dir)},
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

    `tmpdir` replaces `TMPDIR` (Run-tmp) with its short alias from `tmp_link` and also
    becomes `CLAUDE_CODE_TMPDIR`, the temp root the CLI gives sandboxed commands.
    """
    config = agent.file.config
    perms = claude_options(profile)
    plugin = agent.plugin_dir
    process_env = {**env, **perms.env, "PATH": agent_path(agent)}
    if tmpdir is not None:
        process_env["TMPDIR"] = process_env["CLAUDE_CODE_TMPDIR"] = str(tmpdir)
    return ClaudeAgentOptions(
        model=config.model,
        cwd=Path(perms.cwd),
        add_dirs=list(perms.add_dirs),
        tools=tool_set(agent),
        allowed_tools=list(perms.allowed_tools),
        disallowed_tools=list(perms.disallowed_tools),
        permission_mode=perms.permission_mode,  # type: ignore[arg-type]
        sandbox=perms.sandbox,  # type: ignore[arg-type]
        system_prompt=system_prompt(agent),
        setting_sources=[],
        plugins=[{"type": "local", "path": str(plugin)}] if plugin else [],
        skills=list(agent.file.skill_ids),
        strict_mcp_config=True,
        mcp_servers={ROOM_SERVER: room_server(agent)},  # type: ignore[dict-item]
        can_use_tool=can_use_tool,
        hooks={k: list(v) for k, v in hooks.items()} if hooks else None,  # type: ignore[misc]
        thinking=THINKING,  # type: ignore[arg-type]
        effort=_EFFORT[config.effort] if config.effort else None,  # type: ignore[arg-type]
        max_budget_usd=max_budget_usd,
        env=process_env,
        stderr=stderr,
    )
