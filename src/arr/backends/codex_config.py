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
- **`~` denied as a whole (variant 2, W3-00, docs/07 §3 ①):** a `deny` on a folder also
  denies its own metadata, so no Python whose program lies below `~` starts. Base venv and
  run venv therefore live outside `~` (`arr.venvs`), and `prepare_run` refuses a Codex run
  whose toolchain lies below `~`. The working folder below `~` stays usable (the allowed
  root itself is enough). No ancestor of the run venv may be denied: Python's start-up
  `realpath` of `bin/` fails then (W3-00 lab). The list depends only on the profile
  (allowlist, channels, rules), never on what lies in `~`, the workfolder or `runs/`: names
  are not visible, and a file that appears during a turn is denied like everything else.
- **Shell environment (W2-02.1 H1):** `shell_environment_policy.inherit = "core"` plus `set`
  with what the agent needs; no `CODEX_HOME`, tokens, keys or account data reach a command.
  The run venv is only on this `PATH`: the app-server's own `PATH` holds no folder an agent
  can write to (W3-06.3, B1).
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

import logging
import os
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arr.backends.process_path import outside_path, within
from arr.errors import ArrError
from arr.models import AuthKind, AuthProfile, RunVenvMode
from arr.permissions.policy import RunPaths, SandboxProfile
from arr.runs.prepare import PreparedAgent
from arr.settings import app_dir
from arr.venvs import glob_safe_name, venv_root
from arr.workfolder import read_template

__all__ = [
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

ROOM_SERVER_ARGS = ("-I", "-m", "arr.room.server")
"""The app-server starts the room server outside the sandbox in the working folder, a write
root: `-I` keeps the `cwd` out of `sys.path`, so an `arr/` of an agent is never imported
(W3-06.3, B2)."""

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

ENTRIES_WARN = 250
"""From this many permission entries on, every start logs a warning (W2-02.1 M1, W2-05).

With variant 2 a profile has a few dozen entries; the bounds stay as a guard against very
many read folders, channels or always-allow rules."""

ENTRIES_MAX = 500
"""From this many entries on, the turn is not started. Seatbelt compiles the profile for every
command: measured ~0.6 s at 250, ~2.2 s at 500 entries; around 1000 `sandbox-exec` fails."""

PROFILE_MAX_BYTES = 40_000
"""Cautious upper bound for the TOML text of the `filesystem` map (W2-05, W2-05.1 G3).

Not a limit of the sandbox: the review of W2-05 ran maps of 47 KB to 205 KB (and 1203 short
entries, 88 KB) through `codex sandbox` without an error. The bound keeps every command fast
(Seatbelt compiles the profile for each one) and fails with the German message long before the
`Argument list too long` that W2-02 saw at ≈ 2000 long entries."""

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


def toml_string(text: str) -> str:
    """A TOML basic string: `\\`, `"` and control characters escaped, the rest as is.

    Raises `ArrError` for lone surrogates (names with bytes that are no valid UTF-8, kept by
    `surrogateescape`): TOML cannot hold them, and leaving such an entry out would open it.
    """
    out = []
    for ch in text:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            raise ArrError(
                f"Der Pfad „{text.encode('utf-8', 'replace').decode()}“ enthält Zeichen, die "
                "kein gültiges UTF-8 sind. Codex kann ihn nicht sperren. Benenne die Datei um."
            )
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def toml_value(value: Any) -> str:
    """Encode a value as inline TOML for `--config key=value`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, Mapping):
        items = ", ".join(f"{toml_string(str(k))} = {toml_value(v)}" for k, v in value.items())
        return "{" + items + "}"
    raise TypeError(f"no TOML encoding for {type(value).__name__}")


def _within(profile: SandboxProfile, path: Path, root: Path) -> bool:
    key, root_key = profile.key(path), profile.key(root).rstrip("/")
    return key == root_key or key.startswith(root_key + "/")


def _entries(
    profile: SandboxProfile,
    run_paths: RunPaths,
    channels: Sequence[str],
    run_channels: Sequence[str],
    hard_deny: Sequence[Path],
) -> dict[str, tuple[Path, str]]:
    """Path entries `key → (path, read|write|deny)`; the most specific one applies."""
    skills = profile.resolve(run_paths.skills_dir)
    attachments = profile.resolve(run_paths.attachments_dir)
    plugin = profile.resolve(run_paths.run_dir / "agents" / profile.agent_id / "plugin")
    hidden = (skills, attachments, plugin)
    venvs = profile.resolve(venv_root())

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
        if profile.key(path) != profile.key(venvs):  # `_venv_globs` instead (W3-00)
            put(path, "deny")
    for channel in channels:
        put(attachments / channel, "read")
    for channel in run_channels:
        if channel not in channels:  # also before the room creates the folder (L15)
            put(attachments / channel, "deny")

    def closed(path: Path) -> bool:
        return profile.is_sensitive(path) or any(_within(profile, path, h) for h in hard_deny)

    for key in [k for k, (p, a) in entries.items() if a != "deny" and closed(p)]:
        del entries[key]  # never readable, not even as a read folder
    for path in (*profile.sensitive, *hard_deny):
        put(path, "deny")
    return entries


def _others(profile: SandboxProfile, folder: Path, own: Path | None) -> list[str]:
    """Globs that deny every entry of `folder` except `own` (a direct child) and its tree.

    A name differs from `own` at some position `i` (`own[:i][!own[i]]*`), is a proper
    prefix of it (`own[:i]`) or is longer (`own?*`); each glob covers the entry and its
    tree (`{,/**}`, checked offline: a big `{…}` alternation breaks the Seatbelt profile, a
    small one works). The globs match names that do not exist yet as well: a run venv that
    appears during a turn is denied. `2 × len(own)` entries, nothing is listed.
    """
    base = glob_escape(str(folder))
    if own is None or profile.key(own.parent) != profile.key(folder):
        return [f"{base}/*{{,/**}}"]
    name = own.name
    if not glob_safe_name(name):  # also checked by `prepare_run` (W3-00.2)
        raise ArrError(
            f"Der Name „{name}“ in {folder} enthält ein Zeichen, das Codex in einem Muster "
            "nicht ausschließen kann (Umlaute, Akzente und andere Nicht-ASCII-Zeichen sowie "
            "[ ] \\ ! ^ { } * ? ,). Benenne die venv um, z. B. nur mit a–z, 0–9, - und _."
        )
    globs = [f"{base}/{name[:i]}[!{ch}]*" for i, ch in enumerate(name)]
    globs += [f"{base}/{name[:i]}" for i in range(1, len(name))]
    globs.append(f"{base}/{name}?*")
    return [g + "{,/**}" for g in globs]


def _venv_globs(profile: SandboxProfile) -> list[str]:
    """W3-00: other runs' venvs and other base venvs below the venv root stay closed.

    The root and `runs/` cannot be denied themselves: a denied ancestor of the run venv
    stops Python (`realpath` of `bin/`). So every other name in `runs/` and `venvs/` is
    denied by glob; their names stay visible (`ls`), only run IDs and venv names.
    """
    root = profile.resolve(venv_root())
    own_run = profile.venv_dir if profile.run_venv else None
    return _others(profile, root / "runs", own_run) + _others(
        profile, root / "venvs", profile.base_venv
    )


def _sensitive_globs(profile: SandboxProfile) -> list[str]:
    home = glob_escape(str(profile.home))
    globs = [f"{home}/{p}" for p in profile.sensitive_patterns]
    globs += [f"{g}/**" for g in globs]
    return globs + [f"/**/{p}" for p in profile.sensitive_names]


def filesystem_entries(
    profile: SandboxProfile,
    run_paths: RunPaths,
    channels: Sequence[str] = (),
    *,
    run_channels: Sequence[str] = (),
    hard_deny: Sequence[Path] = (),
) -> dict[str, str]:
    """Permission profile entries `path → read|write|deny` for one agent (E19, variant 2).

    Codex applies the most specific entry; a glob `deny` beats every path entry. `:root`
    keeps system folders readable (Python, toolchain, the venvs outside `~`; docs/01 §8).
    Denied as a whole: `~`, `/Volumes`, the workfolder and the user's temp folders
    (`SandboxProfile.read_deny`). Allowed inside: the write roots (work folder,
    Run-tmp, run venv), the allowlist (`output/`, read folders, base venv and toolchain …).
    Three folders are denied explicitly, so a broader read folder (e.g. `run:`) cannot reopen
    them:

    - the run's `skills/` snapshot (L14) and the Claude plugin, which the `SandboxProfile`
      allows: Codex agents read their skills from the copies in `work/.agents/skills/`;
    - chat attachments except the folders of the agent's own channels,
      `groupchats/attachments/<channel>/` (L15, docs/02 §6). The folders of the other
      channels of the run (`run_channels`) are denied by name, also before they exist.

    `hard_deny` (the login folder) is denied like a sensitive path: an allowed root inside
    it is dropped. Globs deny the sensitive patterns (history files, password managers,
    `*.kdbx` …) also inside allowed roots. Nothing here reads a folder: the entries depend
    on the profile only, not on what lies in `~`, the workfolder or `runs/`.

    Raises `ValueError` for an allowed path with a glob character: Codex would read it as a
    pattern, and patterns can only deny.
    """
    entries = _entries(profile, run_paths, channels, run_channels, hard_deny)
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
    for glob in _venv_globs(profile) + _sensitive_globs(profile):
        result[glob] = "deny"
    _check_size(len(result), len(toml_value(result).encode("utf-8")), run_paths.run_dir.name)
    return result


def _too_many(run_id: str) -> str:
    """Why the list is big and what helps (variant 2: no folder contents, W3-00.1 G1)."""
    return (
        "So viele Einträge machen jeden Befehl in der Sandbox sehr langsam oder lassen ihn "
        "scheitern. Die Liste wächst mit den Lese-Ordnern („read_dirs“), den Kanälen und den "
        "Freigaberegeln des Agenten und mit der Länge der Run-ID: Jedes Zeichen kostet zwei "
        f"Einträge, und die Run-ID „{run_id}“ hat {len(run_id)} Zeichen. Fasse Lese-Ordner "
        "zusammen oder entferne nicht nötige; bei sehr langen Projekt- oder Skill-Namen "
        "hilft ein kürzerer Name."
    )


def _check_size(count: int, size: int, run_id: str) -> None:
    if count >= ENTRIES_MAX:
        raise ArrError(
            f"Die Lese-Sperrliste für Codex hat {count} Einträge (Grenze {ENTRIES_MAX}). "
            + _too_many(run_id)
        )
    if size >= PROFILE_MAX_BYTES:
        raise ArrError(
            f"Die Lese-Sperrliste für Codex ist {size} Bytes groß (Grenze {PROFILE_MAX_BYTES} "
            "Bytes). " + _too_many(run_id)
        )
    if count >= ENTRIES_WARN:
        logger.warning(
            "Lese-Sperrliste für Codex mit %d Einträgen (Warnung ab %d): jeder Befehl wird "
            "langsamer. Weniger Lese-Ordner oder kürzere Projekt- und Skill-Namen helfen.",
            count,
            ENTRIES_WARN,
        )


def isolation_overrides(
    profile: SandboxProfile,
    run_paths: RunPaths,
    *,
    model: str,
    channels: Sequence[str] = (),
    filesystem: Mapping[str, str] | None = None,
    shell_env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """`--config` overrides of the agent's app-server: isolation plus permission profile.

    `filesystem` is the result of `filesystem_entries` if the caller has it already.
    `shell_env` is the whole environment of the agent's commands: Codex passes only its core
    variables (`HOME`, `PATH`, `TMPDIR`, `USER`, `LOGNAME`, `SHELL`, `LC_CTYPE`) from the
    app-server on, plus these (W2-02.1 H1). Without it, every command would inherit arr's
    whole environment, tokens and account data included.
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
        'shell_environment_policy.inherit="core"',
        "shell_environment_policy.set=" + toml_value(dict(shell_env or {})),
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
    shell_env: dict[str, str]


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


def command_home(agent: PreparedAgent, profile: SandboxProfile) -> Path:
    """`HOME` of the agent's commands: `<Run-tmp>/home/<id>`, writable for them."""
    return profile.tmp_dir / "home" / agent.id


def server_home(agent: PreparedAgent, profile: SandboxProfile) -> Path:
    """`HOME` of the app-server: `agents/<id>/home`, no write root (W3-06.3).

    The app-server runs `git` outside the sandbox, and `git` reads `$HOME/.gitconfig`. In a
    writable `HOME` an agent could define a clean filter there, select it in `.gitattributes`
    and have it run outside the sandbox in the next turn (measured). Raises `ArrError` if the
    folder lies in a write root.
    """
    home = agent.agent_dir / "home"
    if any(within(home, root) for root in profile.write_roots):
        raise ArrError(
            f"Der Codex-Agent {agent.id} startet nicht: Sein Home-Ordner {home} liegt in einem "
            "Ordner, in den Agenten schreiben dürfen."
        )
    return home


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
    run_channels: Sequence[str] = (),
) -> CodexLaunch:
    """Settings for the agent's own `AsyncCodex` and its thread (S2 §15).

    The permission profile comes from `filesystem_entries` (variant 2, no folder is read);
    the backend still builds it per app-server start, i.e. per turn. `run_channels` are all
    channels of the run (`run.yaml`); the attachments of the others are denied. `env` is the
    app-server's environment, `shell_env` the one of the agent's commands (without
    `CODEX_HOME`).
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
        "HOME": str(server_home(agent, profile)),
        # the app-server runs outside the sandbox and starts `git` through it (W3-06.3, B1)
        "PATH": outside_path(path, profile.write_roots),
        "TMPDIR": str(tmp),
    }
    env |= {name: str(tmp / "cache" / name.lower()) for name in CACHE_ENV}
    # arr's own venv must not leak into the agent (`uv run arr` sets VIRTUAL_ENV), and the run
    # venv is for the commands only; uv reads no config files: it looks for
    # `pyproject.toml`/`uv.toml` in the denied parents.
    overlay = agent.file.run_venv is RunVenvMode.OVERLAY
    env["VIRTUAL_ENV"] = ""
    env |= {"PYTHONPATH": "", "PYTHONHOME": "", "UV_NO_CONFIG": "1"}
    env |= {name: "" for name in SECRET_ENV if environ.get(name)}
    shell_env = {name: env[name] for name in ("TMPDIR", *CACHE_ENV)}
    shell_env["HOME"] = str(command_home(agent, profile))
    shell_env["PATH"] = os.pathsep.join(path)
    shell_env["LANG"] = environ.get("LANG") or "C.UTF-8"
    shell_env["UV_NO_CONFIG"] = "1"
    if overlay:
        shell_env["VIRTUAL_ENV"] = str(run_paths.venv_dir)
    room = {
        "command": sys.executable,
        "args": [*ROOM_SERVER_ARGS],
        "env": {"AGENT_ID": agent.id, "RUN_DIR": str(agent.run_dir)},
        "default_tools_approval_mode": "approve",
    }
    servers: dict[str, Any] = {
        name: {"command": "/usr/bin/false", "enabled": False}
        for name in project_mcp_servers(profile.work_dir)
    }
    servers[ROOM_SERVER] = room
    config = agent.file.config
    filesystem = filesystem_entries(
        profile,
        run_paths,
        config.channels,
        run_channels=run_channels,
        hard_deny=[profile.resolve(codex_home)],
    )
    return CodexLaunch(
        codex_home=codex_home,
        cwd=str(profile.work_dir),
        env=env,
        config_overrides=isolation_overrides(
            profile, run_paths, model=config.model, filesystem=filesystem, shell_env=shell_env
        ),
        thread_config={"mcp_servers": servers},
        developer_instructions=_role_text(agent),
        filesystem=filesystem,
        network_access=False,
        shell_env=shell_env,
    )
