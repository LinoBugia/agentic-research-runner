"""Codex launch settings of one agent: login folder, isolation, permission profile, thread config.

Pure data, no SDK import, so it is testable without the Codex binary. `CodexBackend` turns a
`SandboxProfile` (W1-05) into the settings of its own app-server process:

- **Login folder (`CODEX_HOME`)** per auth profile of kind `codex_chatgpt`: `login_dir` of the
  profile, else `app_dir()/auth/<profile>/codex_home`. arr never touches `~/.codex`.
- **Read allowlist (E19)** as a Codex permission profile `[permissions.arr]` selected with
  `default_permissions`. It replaces the legacy `workspace_write` sandbox (Codex refuses both at
  once): write roots get `write`, the allowlist `read`, `~`, `/Volumes`, the workfolder and the
  sensitive paths `deny`. Without `:tmpdir`/`:slash_tmp` entries `/tmp` and `$TMPDIR` stay
  read-only; `TMPDIR` points to Run-tmp. Network is off; the shell asks per command.
- **Isolation from the user's Codex config** (S2 §7, §8, §13, §15): own `CODEX_HOME`, no
  notify hook, plugins, apps, MCP servers of the user, user or system skills, no project
  root walk (`project_root_markers = []`), model and effort set per thread and turn.

See docs/01-architektur.md §4.2, docs/07-berechtigungen-und-freigaben.md §3 ① and
docs/spikes/S2_codex.md §11, §15.
"""

import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arr.models import AuthKind, AuthProfile, RunVenvMode
from arr.permissions.policy import RunPaths, SandboxProfile
from arr.runs.prepare import PreparedAgent
from arr.settings import app_dir
from arr.workfolder import read_template

__all__ = [
    "PERMISSION_PROFILE",
    "ROOM_SERVER",
    "CodexLaunch",
    "build_launch",
    "codex_home_for",
    "filesystem_entries",
    "isolation_overrides",
    "toml_value",
]

PERMISSION_PROFILE = "arr"
"""Name of the permission profile in the agent's app-server (one app-server per agent)."""

ROOM_SERVER = "arr-room"
"""MCP server name of the group chat; tools appear as `mcp__arr-room__*` (docs/01 §5)."""

SECRET_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
"""Variables blanked in the app-server environment: the login comes from `CODEX_HOME` only."""

_FEATURES_OFF = (
    "plugins",
    "apps",
    "computer_use",
    "browser_use",
    "image_generation",
    "multi_agent",
    "tool_suggest",
    "goals",
    "memories",
    "remote_plugin",
)
"""Codex features a research agent does not get. Not here: `code_mode_host` (it removes the
shell, S2 §3)."""

CACHE_ENV = ("XDG_CACHE_HOME", "MPLCONFIGDIR", "PIP_CACHE_DIR", "UV_CACHE_DIR")
"""Programs that cache in `~` fail behind the read/write block; their caches go to Run-tmp."""


def codex_home_for(auth: AuthProfile) -> Path:
    """Login folder (`CODEX_HOME`) of a `codex_chatgpt` profile; never `~/.codex`."""
    if auth.kind is not AuthKind.CODEX_CHATGPT:
        raise ValueError(f"Das Profil „{auth.name}“ ist kein ChatGPT-Profil (codex_chatgpt).")
    if auth.login_dir:
        return Path(auth.login_dir).expanduser().absolute()
    return app_dir() / "auth" / auth.name / "codex_home"


def toml_value(value: Any) -> str:
    """Encode a value as inline TOML for `--config key=value` (strings JSON-escaped)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, Mapping):
        items = ", ".join(f"{json.dumps(str(k))} = {toml_value(v)}" for k, v in value.items())
        return "{" + items + "}"
    raise TypeError(f"no TOML encoding for {type(value).__name__}")


def filesystem_entries(
    profile: SandboxProfile, run_paths: RunPaths, channels: Sequence[str] = ()
) -> dict[str, str]:
    """Permission profile entries `path → read|write|deny` for one agent (E19).

    Codex applies the most specific entry. `:root` keeps system folders readable (Python,
    toolchain; docs/01 §8). Narrower than the `SandboxProfile` in two places, both denied
    explicitly so a broader read folder (e.g. `run:`) cannot reopen them:

    - the run's `skills/` snapshot and the Claude plugin: Codex agents read their skills from
      the copies in `work/.agents/skills/` (L14);
    - chat attachments: only the folders of the agent's own channels,
      `groupchats/attachments/<channel>/` (L15, docs/02 §6).
    """
    skills = profile.resolve(run_paths.skills_dir)
    attachments = profile.resolve(run_paths.attachments_dir)
    plugin = profile.resolve(run_paths.run_dir / "agents" / profile.agent_id / "plugin")
    hidden = [profile.key(p) for p in (skills, attachments, plugin)]

    def is_hidden(path: Path) -> bool:
        key = profile.key(path)
        return any(key == h or key.startswith(h.rstrip("/") + "/") for h in hidden)

    entries: dict[str, str] = {":root": "read"}
    for path in profile.write_roots:
        entries[str(path)] = "write"
    for path in profile.read_allow:
        if str(path) not in entries and not is_hidden(path):
            entries[str(path)] = "read"
    for path in (*profile.read_deny, skills, attachments, plugin):
        entries[str(path)] = "deny"
    for channel in channels:
        entries[str(attachments / channel)] = "read"
    for path in profile.sensitive:
        entries[str(path)] = "deny"
    return entries


def isolation_overrides(
    profile: SandboxProfile,
    run_paths: RunPaths,
    *,
    model: str,
    channels: Sequence[str] = (),
) -> tuple[str, ...]:
    """`--config` overrides of the agent's app-server: isolation plus permission profile."""
    web = "live" if profile.web else "disabled"
    overrides = [
        f"model={toml_value(model)}",
        'service_tier="default"',
        "notify=[]",
        f"web_search={toml_value(web)}",
        "project_root_markers=[]",
        "project_doc_fallback_filenames=[]",
        "skills.bundled.enabled=false",
        'history.persistence="none"',
        "check_for_update_on_startup=false",
        "hide_agent_reasoning=false",
        'model_reasoning_summary="detailed"',
        f"default_permissions={toml_value(PERMISSION_PROFILE)}",
        f"permissions.{PERMISSION_PROFILE}.filesystem="
        + toml_value(filesystem_entries(profile, run_paths, channels)),
    ]
    overrides += [f"features.{feature}=false" for feature in _FEATURES_OFF]
    return tuple(overrides)


@dataclass(frozen=True, slots=True)
class CodexLaunch:
    """Everything an agent's app-server and thread need (data only, see module docstring)."""

    codex_home: Path
    cwd: str
    env: dict[str, str]
    config_overrides: tuple[str, ...]
    thread_config: dict[str, Any]
    developer_instructions: str
    filesystem: dict[str, str]
    network_access: bool


def _role_text(agent: PreparedAgent) -> str:
    role = (agent.run_dir / agent.file.role_file).read_text(encoding="utf-8").strip()
    static = read_template("prompts", "codex.md").strip()
    return f"{role}\n\n{static}\n"


def build_launch(
    agent: PreparedAgent,
    profile: SandboxProfile,
    run_paths: RunPaths,
    *,
    codex_home: Path,
    environ: Mapping[str, str] | None = None,
) -> CodexLaunch:
    """Settings for the agent's own `AsyncCodex` and its thread (S2 §15)."""
    environ = os.environ if environ is None else environ
    tmp = profile.tmp_dir
    path = [str(p) for p in agent.file.path_prepend]
    if agent.file.run_venv is RunVenvMode.OVERLAY:
        path.insert(0, str(run_paths.venv_dir / "bin"))
    path += [p for p in environ.get("PATH", "").split(os.pathsep) if p and p not in path]
    # Own HOME: Codex finds user skills via `$HOME/.agents/skills` even with an own
    # CODEX_HOME (S2 §8). This is isolation, not E19 protection (the sandbox does that).
    env = {
        "CODEX_HOME": str(codex_home),
        "HOME": str(tmp / "home" / agent.id),
        "PATH": os.pathsep.join(path),
        "TMPDIR": str(tmp),
    }
    env |= {name: str(tmp / "cache" / name.lower()) for name in CACHE_ENV}
    env |= {name: "" for name in SECRET_ENV if environ.get(name)}
    room = {
        "command": sys.executable,
        "args": ["-m", "arr.room.server"],
        "env": {"AGENT_ID": agent.id, "RUN_DIR": str(agent.run_dir)},
        "default_tools_approval_mode": "approve",
    }
    config = agent.file.config
    return CodexLaunch(
        codex_home=codex_home,
        cwd=str(profile.work_dir),
        env=env,
        config_overrides=isolation_overrides(
            profile, run_paths, model=config.model, channels=config.channels
        ),
        thread_config={"mcp_servers": {ROOM_SERVER: room}},
        developer_instructions=_role_text(agent),
        filesystem=filesystem_entries(profile, run_paths, config.channels),
        network_access=False,
    )
