"""Codex launch settings of one agent: login folder, isolation, permission profile, thread config.

Pure data, no SDK import, so it is testable without the Codex binary. `CodexBackend` turns a
`SandboxProfile` (W1-05) into the settings of its own app-server process:

- **Login folder (`CODEX_HOME`)** per auth profile of kind `codex_chatgpt`: `login_dir` of the
  profile, else `app_dir()/auth/<profile>/codex_home`. arr never touches `~/.codex`.
- **Read allowlist (E19)** as a Codex permission profile `[permissions.arr]` selected with
  `default_permissions`. It replaces the legacy `workspace_write` sandbox (Codex refuses both at
  once): write roots get `write`, the allowlist `read`, `/Volumes`, the sensitive paths and
  everything else below `~` `deny`. Without `:tmpdir`/`:slash_tmp` entries `/tmp` and `$TMPDIR`
  stay read-only; `TMPDIR` points to Run-tmp. Network is off; the shell asks per command.
- **Siblings instead of `~` (variant 1, user decision of 28.10., docs/07 §3 ①):** a `deny` on a
  folder also denies the folder's own metadata, and then no Python below `~` starts
  (`realpath` needs every parent). So a denied folder that leads to an allowed root stays
  readable as a folder, and each of its entries that exists now and is not on the way to an
  allowed root is denied one by one. The list is built when the app-server starts, i.e. per
  turn. Files arr creates during a run in these folders are denied by glob (`LATE_GLOBS`).
  Known gap: names in these folders are visible, and an unknown new entry is readable until
  the next turn.
- **Isolation from the user's Codex config** (S2 §7, §8, §13, §15): own `CODEX_HOME`, no
  notify hook, plugins, apps, MCP servers of the user, user or system skills, no project
  root walk (`project_root_markers = []`), model and effort set per thread and turn.
- **Project MCP servers off:** Codex starts the `mcp_servers` of `<cwd>/.codex/config.toml`
  although the untrusted project layer is otherwise disabled (W2-02, offline check). The
  working folder is a copy of the project, so each such server is switched off in the
  thread config (`enabled = false`); only `arr-room` runs. A `.mcp.json` is not read.

See docs/01-architektur.md §4.2, docs/07-berechtigungen-und-freigaben.md §3 ① and
docs/spikes/S2_codex.md §11, §15.
"""

import json
import logging
import os
import sys
import tomllib
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
    "LATE_GLOBS",
    "PERMISSION_PROFILE",
    "ROOM_SERVER",
    "CodexLaunch",
    "build_launch",
    "codex_home_for",
    "filesystem_entries",
    "glob_escape",
    "isolation_overrides",
    "project_mcp_servers",
    "toml_value",
]

logger = logging.getLogger(__name__)

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

_AGENT_FILES = ("*.json", "*.jsonl", "*.md", "*.log", "*.diff", "blobs", "blobs/**")
LATE_GLOBS: dict[str, tuple[str, ...]] = {
    ".": ("*.json", "*.jsonl", "*.yaml", "*.md", "*.log"),
    "agents/*": _AGENT_FILES + ("plugin", "plugin/**"),
    "groupchats": ("room.sqlite*", "*.md", "*.jsonl"),
}
"""Names arr creates during a run directly in a folder of the run that stays open for traversal
(docs/02 §4): `orchestrator.jsonl`, `approvals.jsonl`, `usage.json`, the agents' transcripts,
`session.json`, `stderr.log`, `changes.diff`, `blobs/`, the room database with `-wal`/`-shm`
and the channel exports. Keys are folders relative to the run (`agents/*` = every agent
folder), values Codex globs; `*` does not cross `/`, a folder needs `x` and `x/**`. Used
only for folders on the way to an allowed root: a readable run folder stays readable."""

_GLOB_CHARS = frozenset("*?[]{}")


def glob_escape(path: str) -> str:
    """Escape glob characters as one-character classes (`[` → `[[]`), as Codex reads them."""
    return "".join(f"[{c}]" if c in _GLOB_CHARS else c for c in path)


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


def _within(profile: SandboxProfile, path: Path, root: Path) -> bool:
    key, root_key = profile.key(path), profile.key(root).rstrip("/")
    return key == root_key or key.startswith(root_key + "/")


def _strict_entries(
    profile: SandboxProfile, run_paths: RunPaths, channels: Sequence[str]
) -> dict[str, tuple[Path, str]]:
    """Entries before opening the parents: `key → (path, read|write|deny)`."""
    skills = profile.resolve(run_paths.skills_dir)
    attachments = profile.resolve(run_paths.attachments_dir)
    plugin = profile.resolve(run_paths.run_dir / "agents" / profile.agent_id / "plugin")
    hidden = (skills, attachments, plugin)

    entries: dict[str, tuple[Path, str]] = {}

    def put(path: Path, access: str, *, overwrite: bool = True) -> None:
        if overwrite or profile.key(path) not in entries:
            entries[profile.key(path)] = (path, access)

    for path in profile.write_roots:
        put(path, "write")
    for path in profile.read_allow:
        if not any(_within(profile, path, h) for h in hidden):
            put(path, "read", overwrite=False)
    for path in (*profile.read_deny, *hidden):
        put(path, "deny")
    for channel in channels:
        put(attachments / channel, "read")
    for key in [k for k, (p, a) in entries.items() if a != "deny" and profile.is_sensitive(p)]:
        del entries[key]  # a sensitive path is never readable, not even as a read folder
    for path in profile.sensitive:
        put(path, "deny")
    return entries


def _governing(profile: SandboxProfile, path: Path, entries: dict[str, tuple[Path, str]]) -> str:
    """Key of the most specific entry that covers `path` ("" = `:root`)."""
    best = ""
    for key, (root, _) in entries.items():
        if _within(profile, path, root) and len(key) > len(best):
            best = key
    return best


def _children(folder: Path) -> list[Path]:
    try:
        return sorted(folder.iterdir())
    except OSError:
        return []


def _open_parents(
    profile: SandboxProfile, entries: dict[str, tuple[Path, str]]
) -> tuple[dict[str, tuple[Path, str]], list[Path]]:
    """Variant 1: open denied folders on the way to an allowed root, deny their other entries.

    Returns the new entries and the folders that stay open for traversal only.
    """
    allowed = [p for p, a in entries.values() if a != "deny"]
    opened = {
        key
        for key, (path, access) in entries.items()
        if access == "deny"
        and any(_within(profile, a, path) and profile.key(a) != key for a in allowed)
    }
    passage: dict[str, Path] = {}
    for root in allowed:
        for folder in root.parents:
            if _governing(profile, folder, entries) in opened:
                passage[profile.key(folder)] = folder
    result = {k: v for k, v in entries.items() if k not in opened}
    for folder in passage.values():
        for child in _children(folder):
            key = profile.key(child)
            if key not in entries and key not in passage:
                result[key] = (child, "deny")
    return result, sorted(passage.values())


def _late_globs(profile: SandboxProfile, run_dir: Path, passage: Sequence[Path]) -> list[str]:
    open_keys = {profile.key(p) for p in passage}
    globs = []
    for folder, patterns in LATE_GLOBS.items():
        parent = folder.removesuffix("/*")
        if profile.key(profile.resolve(run_dir / parent)) not in open_keys:
            continue
        base = glob_escape(str(profile.resolve(run_dir / parent)))
        if folder != parent:
            base += "/*"
        globs += [f"{base}/{pattern}" for pattern in patterns]
    return globs


def _sensitive_globs(profile: SandboxProfile) -> list[str]:
    home = glob_escape(str(profile.home))
    globs = [f"{home}/{p}" for p in profile.sensitive_patterns]
    globs += [f"{g}/**" for g in globs]
    return globs + [f"/**/{p}" for p in profile.sensitive_names]


def filesystem_entries(
    profile: SandboxProfile, run_paths: RunPaths, channels: Sequence[str] = ()
) -> dict[str, str]:
    """Permission profile entries `path → read|write|deny` for one agent (E19, variant 1).

    Codex applies the most specific entry; a glob `deny` beats every path entry. `:root`
    keeps system folders readable (Python, toolchain; docs/01 §8). Narrower than the
    `SandboxProfile` in two places, both denied explicitly so a broader read folder (e.g.
    `run:`) cannot reopen them:

    - the run's `skills/` snapshot and the Claude plugin: Codex agents read their skills from
      the copies in `work/.agents/skills/` (L14);
    - chat attachments: only the folders of the agent's own channels,
      `groupchats/attachments/<channel>/` (L15, docs/02 §6).

    Then the denied parents of allowed roots are opened and their other entries denied one
    by one (module docstring), plus globs for `LATE_GLOBS` and the sensitive patterns. Reads
    the folders on the way, so call it when the app-server starts.

    Raises `ValueError` for an allowed path with a glob character: Codex would read it as a
    pattern, and patterns can only deny.
    """
    strict = _strict_entries(profile, run_paths, channels)
    entries, passage = _open_parents(profile, strict)
    result: dict[str, str] = {":root": "read"}
    for path, access in entries.values():
        text = str(path)
        if not _GLOB_CHARS & set(text):
            result[text] = access
        elif access == "deny":
            result[glob_escape(text)] = result[glob_escape(text) + "/**"] = "deny"
        else:
            raise ValueError(
                f"Der Pfad „{text}“ enthält eines der Zeichen * ? [ ] {{ }}. Codex liest ihn als "
                "Muster und kann ihn dann nicht freigeben. Benenne den Ordner um."
            )
    for glob in _late_globs(profile, run_paths.run_dir, passage) + _sensitive_globs(profile):
        result[glob] = "deny"
    return result


def isolation_overrides(
    profile: SandboxProfile,
    run_paths: RunPaths,
    *,
    model: str,
    channels: Sequence[str] = (),
    filesystem: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """`--config` overrides of the agent's app-server: isolation plus permission profile.

    `filesystem` is the result of `filesystem_entries` if the caller has it already.
    """
    web = "live" if profile.web else "disabled"
    if filesystem is None:
        filesystem = filesystem_entries(profile, run_paths, channels)
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
        f"permissions.{PERMISSION_PROFILE}.filesystem=" + toml_value(filesystem),
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


def project_mcp_servers(cwd: Path) -> list[str]:
    """Names of the MCP servers in `<cwd>/.codex/config.toml` (empty if there is none)."""
    path = cwd / ".codex" / "config.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Projekt-Konfiguration %s nicht lesbar: %s", path, exc)
        return []
    servers = data.get("mcp_servers")
    return sorted(str(name) for name in servers) if isinstance(servers, dict) else []


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
    """Settings for the agent's own `AsyncCodex` and its thread (S2 §15).

    Reads the folders on the way to the allowed roots (`filesystem_entries`): build it anew
    for every app-server start, i.e. per turn.
    """
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
    # arr's own venv must not leak into the agent (`uv run arr` sets VIRTUAL_ENV); uv reads
    # no config files, the parents on the way may hold a denied `pyproject.toml`.
    overlay = agent.file.run_venv is RunVenvMode.OVERLAY
    env["VIRTUAL_ENV"] = str(run_paths.venv_dir) if overlay else ""
    env |= {"PYTHONPATH": "", "PYTHONHOME": "", "UV_NO_CONFIG": "1"}
    env |= {name: "" for name in SECRET_ENV if environ.get(name)}
    room = {
        "command": sys.executable,
        "args": ["-m", "arr.room.server"],
        "env": {"AGENT_ID": agent.id, "RUN_DIR": str(agent.run_dir)},
        "default_tools_approval_mode": "approve",
    }
    servers: dict[str, Any] = {
        name: {"command": "/usr/bin/false", "enabled": False}
        for name in project_mcp_servers(profile.work_dir)
    }
    servers[ROOM_SERVER] = room
    config = agent.file.config
    filesystem = filesystem_entries(profile, run_paths, config.channels)
    return CodexLaunch(
        codex_home=codex_home,
        cwd=str(profile.work_dir),
        env=env,
        config_overrides=isolation_overrides(
            profile, run_paths, model=config.model, filesystem=filesystem
        ),
        thread_config={"mcp_servers": servers},
        developer_instructions=_role_text(agent),
        filesystem=filesystem,
        network_access=False,
    )
