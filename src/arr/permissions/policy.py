"""PolicyEngine: sandbox profile and fixed rules resolve to allow, deny or ask.

Layer ② of docs/07-berechtigungen-und-freigaben.md §3: a backend-neutral engine that both
adapters call with a normalized `PermissionRequest`. It answers `allow`, `deny` with a German
reason plus alternative, or `ask` (grey zone, goes to the inbox in `arr.permissions.inbox`).

- **Read allowlist (E19, docs/00 §7):** work folder, released run folders, read folders and
  the Python environment. System folders outside `~` stay readable; sensitive paths are
  always forbidden. Paths are normalized (symlinks, `..`, `~`, `$HOME`, relative paths,
  letter case on macOS) before any check.
- **Commands** are classified coarsely (`sudo`, `ssh`, `git push`, network tools, package
  installs). The OS sandbox stays the main protection; this is only a second layer.
- **Profiles as data:** `profile_for` builds a `SandboxProfile` per agent; `claude_options`
  and `codex_options` translate it into plain option structures (docs/01 §4.1, §4.2,
  docs/spikes/S1_claude.md §3.1, §3.4, docs/spikes/S2_codex.md §11, §15). Whether the SDKs
  enforce them is tested in W2-01/W2-02.
- **Always rules** live in `arr.yaml` under `permissions:` (`PermissionRules`); decisions with
  scope `agent`/`run` are kept as grants and can be revoked (docs/07 §3 ④, §4).
- Every decision is appended to `runs/<id>/approvals.jsonl`, with secrets redacted.
"""

import json
import os
import re
import shlex
import sys
import threading
import time
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field

from arr.errors import ArrError
from arr.models import (
    AgentConfig,
    Provider,
    Record,
    RunVenvMode,
    StrictModel,
    Toolchain,
    WorkspaceMode,
)
from arr.permissions.types import (
    DecidedBy,
    Decision,
    DecisionAction,
    DecisionScope,
    PermissionRequest,
    RequestCategory,
    Risk,
    RiskLevel,
)
from arr.settings import app_dir

__all__ = [
    "AlwaysRule",
    "ApprovalLog",
    "ApprovalLogEntry",
    "Assessment",
    "ClaudeOptions",
    "CodexOptions",
    "CommandInfo",
    "Grant",
    "LogEvent",
    "PermissionRules",
    "PolicyEngine",
    "RunPaths",
    "SandboxProfile",
    "TIMEOUT_REASON",
    "claude_options",
    "classify_command",
    "codex_options",
    "command_paths",
    "normalize_path",
    "profile_for",
    "redact",
]

# --------------------------------------------------------------------------------------------
# Tools, sensitive paths, command lists
# --------------------------------------------------------------------------------------------

READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "NotebookRead"})
"""File tools that only read (Claude names; Codex reads inside its shell)."""

SEARCH_TOOLS = frozenset({"Grep", "Glob", "LS"})
"""Read tools that walk a whole tree, so a folder that contains a sensitive path is refused."""

WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "fileChange"})
"""File tools that write (Claude names, Codex `fileChange`)."""

SHELL_TOOLS = frozenset({"Bash", "shell", "exec", "commandExecution", "local_shell"})
"""Terminal tools (Claude `Bash`, Codex `commandExecution` and its older names)."""

NETWORK_TOOLS = frozenset({"SandboxNetworkAccess", "WebFetch", "WebSearch"})
"""Requests that are about network access only; the host comes in `hosts` or `urls`."""

HARMLESS_TOOLS = frozenset({"Skill", "TodoWrite"})
"""Tools without file, shell or network access of their own."""

ROOM_TOOL_PREFIXES = ("mcp__room__", "mcp__arr-room__", "mcp__arr_room__")
"""MCP tools of arr's own room server (docs/06); always allowed."""

SENSITIVE_HOME_PATHS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".arr",
    ".azure",
    ".kube",
    ".docker",
    ".netrc",
    ".git-credentials",
    ".pypirc",
    ".npmrc",
    ".password-store",
    ".config/gh",
    ".config/gcloud",
    ".codex",
    ".claude",
    ".claude.json",
    "Library/Keychains",
    "Library/Cookies",
    "Library/Safari",
    "Library/Mail",
    "Library/Messages",
    "Library/Containers/com.apple.Safari",
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Chromium",
    "Library/Application Support/Firefox",
    "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft Edge",
    "Library/Application Support/Arc",
    "Library/Application Support/com.apple.TCC",
)
"""Paths below `~` that are never readable (docs/07 §3 ②: keys, `~/.arr`, keychain, browsers)."""

SENSITIVE_ABSOLUTE_PATHS = ("/Library/Keychains",)
"""Sensitive paths outside `~`."""

READ_BLOCKED_ROOTS = ("/Volumes",)
"""Besides `~` and the workfolder: roots that are not readable unless on the allowlist."""

_PRIVILEGED = "Befehle mit Systemrechten sind nicht erlaubt."
_CREDENTIALS = "Zugriff auf Schlüsselbund, SSH und Zugangsdaten ist nicht erlaubt."
_SYSTEM = "Systemeinstellungen und Systemdienste zu ändern ist nicht erlaubt."
_GIT_PUSH = "„git push“ ist nicht erlaubt. Ergebnisse bleiben im Run."
_GLOBAL_INSTALL = (
    "Installationen außerhalb der Run-venv (systemweit oder global) sind nicht erlaubt."
)

FORBIDDEN_PROGRAMS: Mapping[str, str] = {
    "sudo": _PRIVILEGED,
    "su": _PRIVILEGED,
    "doas": _PRIVILEGED,
    "security": _CREDENTIALS,
    "ssh": _CREDENTIALS,
    "scp": _CREDENTIALS,
    "sftp": _CREDENTIALS,
    "ssh-add": _CREDENTIALS,
    "ssh-agent": _CREDENTIALS,
    "ssh-keygen": _CREDENTIALS,
    "ssh-copy-id": _CREDENTIALS,
    "launchctl": _SYSTEM,
    "systemsetup": _SYSTEM,
    "networksetup": _SYSTEM,
    "scutil": _SYSTEM,
    "pmset": _SYSTEM,
    "nvram": _SYSTEM,
    "csrutil": _SYSTEM,
    "spctl": _SYSTEM,
    "tccutil": _SYSTEM,
    "dscl": _SYSTEM,
    "dseditgroup": _SYSTEM,
    "profiles": _SYSTEM,
    "kextload": _SYSTEM,
    "diskutil": _SYSTEM,
    "osascript": _SYSTEM,
    "crontab": _SYSTEM,
    "shutdown": _SYSTEM,
    "reboot": _SYSTEM,
    "halt": _SYSTEM,
    "chsh": _SYSTEM,
    "passwd": _SYSTEM,
    "softwareupdate": _GLOBAL_INSTALL,
    "installer": _GLOBAL_INSTALL,
    "brew": _GLOBAL_INSTALL,
    "port": _GLOBAL_INSTALL,
    "apt": _GLOBAL_INSTALL,
    "apt-get": _GLOBAL_INSTALL,
    "yum": _GLOBAL_INSTALL,
    "dnf": _GLOBAL_INSTALL,
    "gem": _GLOBAL_INSTALL,
}
"""Programs that are always refused (docs/07 §3 ②: `sudo`, system settings, `security`, …).

`brew`, `gem` & co. install outside the run; read-only uses of them are refused as well,
which keeps the rule simple and errs on the safe side.
"""

NETWORK_PROGRAMS = frozenset(
    {
        "curl",
        "wget",
        "nc",
        "ncat",
        "netcat",
        "telnet",
        "ftp",
        "http",
        "https",
        "aria2c",
        "gh",
        "rsync",
        "ping",
        "dig",
        "nslookup",
        "host",
        "uvx",
        "pipx",
    }
)
"""Programs that need the network (docs/07 §3 ②: ❓ approval)."""

_GIT_NETWORK = frozenset({"clone", "fetch", "pull", "ls-remote", "submodule", "remote"})
_GIT_FORBIDDEN = frozenset({"push", "send-email"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
_WRAPPERS = frozenset(
    {"env", "command", "builtin", "exec", "nohup", "time", "nice", "timeout", "xargs"}
)
_WRAPPERS_WITH_ARG = {"nice": {"-n"}, "timeout": set(), "env": {"-u"}, "xargs": {"-n", "-I"}}

_BAN_CATEGORIES = {
    _PRIVILEGED: RequestCategory.PRIVILEGED,
    _CREDENTIALS: RequestCategory.PRIVILEGED,
    _SYSTEM: RequestCategory.PRIVILEGED,
    _GIT_PUSH: RequestCategory.NETWORK,
    _GLOBAL_INSTALL: RequestCategory.INSTALL,
}

_CATEGORY_LABELS = {
    RequestCategory.NETWORK: "Netzwerk",
    RequestCategory.INSTALL: "Installation",
    RequestCategory.READ_OUTSIDE: "Lesen außerhalb der Erlaubnisliste",
    RequestCategory.WRITE_OUTSIDE: "Schreiben außerhalb des Runs",
    RequestCategory.PRIVILEGED: "Systemrechte",
    RequestCategory.OTHER: "Sonstiges",
}

TIMEOUT_REASON = "Keine Freigabe erhalten, arbeite ohne weiter oder frage im Chat nach."
"""Reason of the automatic `deny` after the approval timeout (docs/07 §3 ③)."""

# German reasons with an alternative for the agent (docs/07 §3 ②).
_R_SENSITIVE = (
    "Dieser Pfad ist geschützt (Schlüssel, Zugangsdaten, Browser- oder arr-Daten) und darf "
    "nie gelesen oder geschrieben werden. Arbeite ohne diese Daten weiter."
)
_R_SEARCH_SENSITIVE = (
    "Dieser Ordner enthält geschützte Pfade und darf nicht als Ganzes durchsucht werden. "
    "Suche gezielt in deinem Arbeitsordner oder einem Lese-Ordner."
)
_R_WRITE_OUTSIDE = (
    "Schreiben außerhalb des Runs ist nicht erlaubt. Lege die Datei in deinem Arbeitsordner "
    "an und hänge sie im Chat an."
)
_R_READ_ONLY = (
    "Dieser Ordner ist nur lesbar. Kopiere die Datei in deinen Arbeitsordner und bearbeite "
    "sie dort."
)
_R_SHELL_READ = (
    "Terminal-Befehle dürfen nur Pfade aus der Erlaubnisliste lesen (Arbeitsordner, Run, "
    "Lese-Ordner, Python-Umgebung). Nutze für andere Dateien das Read-Tool, dann wird eine "
    "Freigabe angefragt."
)
_R_INVALID_PATH = "Der Pfad ist ungültig. Gib einen normalen Pfad ohne Steuerzeichen an."
_R_UNKNOWN_AGENT = "Für diesen Agenten gibt es kein Berechtigungsprofil."
_R_USER_INSTALL = (
    "Installationen mit „--user“ landen außerhalb des Runs. Installiere ohne „--user“ in die "
    "Run-venv."
)
_R_NO_RUN_VENV = (
    "Dieser Run hat keine Run-venv (toolchain.run_venv: off), deshalb sind Installationen "
    "nicht möglich. Arbeite mit den vorhandenen Paketen weiter."
)
_ALTERNATIVE = " Arbeite im Arbeitsordner weiter oder beschreibe im Chat, was du brauchst."

# --------------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------------

_HOME_VAR_RE = re.compile(r"^(?:\$HOME|\$\{HOME\})(?=/|$)")


def default_case_insensitive() -> bool:
    """Whether paths are compared case-insensitively by default (macOS file systems)."""
    return sys.platform == "darwin"


def normalize_path(raw: str | Path, *, base: Path, home: Path) -> Path:
    """Return the absolute, symlink-free form of `raw` (docs/07 §3 ②).

    Expands `~`, `~user`, `$HOME` and `${HOME}`, resolves relative paths against `base`,
    collapses leading `//`, removes `..` and follows symlinks of existing components.
    Raises `ValueError` for empty paths and paths with NUL bytes.
    """
    text = str(raw).strip()
    if not text or "\x00" in text:
        raise ValueError(_R_INVALID_PATH)
    if text.startswith("file://"):
        text = urlsplit(text).path or "/"
    text = _HOME_VAR_RE.sub(lambda _: str(home), text)
    if text == "~" or text.startswith("~/"):
        text = str(home) + text[1:]
    elif text.startswith("~"):
        text = os.path.expanduser(text)
    if text.startswith("//"):
        text = "/" + text.lstrip("/")
    path = Path(text)
    if not path.is_absolute():
        path = base / path
    return Path(os.path.realpath(path))


def _key(path: Path | str, case_insensitive: bool) -> str:
    text = unicodedata.normalize("NFC", str(path))
    return text.casefold() if case_insensitive else text


def _key_within(child: str, parent: str) -> bool:
    if child == parent:
        return True
    return child.startswith(parent.rstrip("/") + "/")


def _dedupe(paths: Iterable[Path], case_insensitive: bool) -> tuple[Path, ...]:
    seen: set[str] = set()
    result = []
    for path in paths:
        key = _key(path, case_insensitive)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return tuple(result)


def _strip_glob(raw: str) -> str:
    """Cut a glob pattern at the first component with wildcards (`~/.ssh/*` → `~/.ssh`)."""
    parts = raw.split("/")
    for index, part in enumerate(parts):
        if any(char in part for char in "*?[{"):
            prefix = "/".join(parts[:index])
            return prefix if prefix or not raw.startswith("/") else "/"
    return raw


# --------------------------------------------------------------------------------------------
# Rules in arr.yaml (`permissions:`)
# --------------------------------------------------------------------------------------------


class AlwaysRule(StrictModel):
    """A fixed rule from an „Immer“ decision of the user (docs/07 §3 ④, layer ②).

    `tools` restricts the rule to these tools (empty: any tool). An item of a request is
    covered when it matches one of `paths` (folder and everything below), `hosts` (exact or
    `*.domain`), `commands` (exact, or a prefix ending in ` *` for commands without shell
    operators) or, for unknown tools, `tools` alone. Allow rules never lift hard bans.
    """

    action: Literal["allow", "deny"]
    tools: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    hosts: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    reason: str = ""


class PermissionRules(StrictModel):
    """Schema of `permissions:` in `arr.yaml` (docs/02 §1.1, docs/07 §3 ②).

    `always`: fixed rules from „Immer“ decisions. `sensitive_paths`: further paths that are
    never readable, in addition to the built-in list.
    """

    always: list[AlwaysRule] = Field(default_factory=list)
    sensitive_paths: list[str] = Field(default_factory=list)

    @classmethod
    def from_settings(cls, data: Mapping[str, Any] | None) -> "PermissionRules":
        """Parse `WorkfolderSettings.permissions` (plain data) into rules."""
        return cls.model_validate(dict(data or {}))


# --------------------------------------------------------------------------------------------
# Sandbox and read profile
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunPaths:
    """Folders of one run that the profile needs (docs/02 §1, §4).

    Run-tmp is `<run>/tmp`, the run venv `<run>/.venv`. Relative read folders are resolved
    against the workfolder.
    """

    workfolder: Path
    run_dir: Path
    toolchain: Toolchain = field(default_factory=Toolchain)

    @property
    def tmp_dir(self) -> Path:
        """Run-tmp; `TMPDIR` of every agent points here (docs/07 §3 ①)."""
        return self.run_dir / "tmp"

    @property
    def venv_dir(self) -> Path:
        """Run venv over the base venv (docs/07 §2)."""
        return self.run_dir / ".venv"

    @property
    def output_dir(self) -> Path:
        """Results of the run, readable for all agents (docs/07 §3 ②)."""
        return self.run_dir / "output"

    @property
    def attachments_dir(self) -> Path:
        """Chat attachments, readable for all agents (docs/07 §3 ②)."""
        return self.run_dir / "groupchats" / "attachments"

    def work_dir(self, agent: AgentConfig) -> Path:
        """Working folder of an agent: `shared/<name>` or `agents/<id>/work` (docs/02 §3.1)."""
        if agent.workspace.mode is WorkspaceMode.SHARED and agent.workspace.name:
            return self.run_dir / "shared" / agent.workspace.name
        return self.run_dir / "agents" / agent.id / "work"


@dataclass(frozen=True, slots=True)
class SandboxProfile:
    """Sandbox and read profile of one agent as plain data (docs/07 §3 ①, E19).

    All paths are normalized. Reading: a sensitive path is never readable; otherwise the
    most specific entry of `read_allow` and `read_deny` wins (deny on a tie), and a path
    under neither stays readable (system folders outside `~`). Writing: only below
    `write_roots`.
    """

    agent_id: str
    provider: Provider
    home: Path
    workfolder: Path
    work_dir: Path
    tmp_dir: Path
    write_roots: tuple[Path, ...]
    read_allow: tuple[Path, ...]
    read_deny: tuple[Path, ...]
    sensitive: tuple[Path, ...]
    network: bool
    allowed_domains: tuple[str, ...] = ()
    run_venv: bool = True
    case_insensitive: bool = field(default_factory=default_case_insensitive)

    def resolve(self, raw: str | Path, base: Path | None = None) -> Path:
        """Normalize a path given by the agent (relative to `base` or the work folder)."""
        return normalize_path(raw, base=base or self.work_dir, home=self.home)

    def key(self, path: Path | str) -> str:
        """Comparison key of a normalized path (case folded on macOS)."""
        return _key(path, self.case_insensitive)

    def _within_any(self, path: Path, roots: Iterable[Path]) -> int:
        """Length of the longest root that contains `path`, or -1."""
        key = self.key(path)
        best = -1
        for root in roots:
            root_key = self.key(root)
            if _key_within(key, root_key):
                best = max(best, len(root_key))
        return best

    def is_sensitive(self, path: Path) -> bool:
        """True if `path` is a sensitive path or lies below one."""
        return self._within_any(path, self.sensitive) >= 0

    def contains_sensitive(self, path: Path) -> bool:
        """True if a sensitive path lies below `path` (e.g. `~` or `/`)."""
        key = self.key(path)
        return any(_key_within(self.key(s), key) for s in self.sensitive)

    def can_read(self, path: Path) -> bool:
        """Whether the read profile allows `path` (same answer for file tools and Bash)."""
        if self.is_sensitive(path):
            return False
        deny = self._within_any(path, self.read_deny)
        if deny < 0:
            return True
        return self._within_any(path, self.read_allow) > deny

    def can_write(self, path: Path) -> bool:
        """Whether `path` lies below a write root and is not sensitive."""
        return not self.is_sensitive(path) and self._within_any(path, self.write_roots) >= 0

    @property
    def read_only(self) -> tuple[Path, ...]:
        """Allowlist entries that are not below a write root (read folders, toolchain)."""
        return tuple(p for p in self.read_allow if self._within_any(p, self.write_roots) < 0)


def _venv_base(venv: Path) -> Path | None:
    """Prefix of the Python installation a venv is built on (`home` in `pyvenv.cfg`)."""
    try:
        text = (venv / "pyvenv.cfg").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "home" and value.strip():
            return Path(value.strip()).parent
    return None


def profile_for(
    agent: AgentConfig,
    run_paths: RunPaths,
    rules: PermissionRules | None = None,
    *,
    home: Path | None = None,
    case_insensitive: bool | None = None,
) -> SandboxProfile:
    """Build the sandbox and read profile of an agent (docs/07 §3 ①, E19, docs/01 §4.1).

    Write roots: the work folder, Run-tmp and, with `run_venv: overlay`, the run venv.
    Read allowlist: write roots, `output/`, chat attachments, `read_dirs`, the Python
    environment (base venv, its installation prefix, `extra_path`) and paths of always-allow
    read rules. Read blocks: `~`, `/Volumes` and the workfolder (other projects, other
    agents). Network: `tools.web`; always-allow host rules become allowed domains.
    """
    rules = rules or PermissionRules()
    home = Path(os.path.realpath(home or Path.home()))
    ci = default_case_insensitive() if case_insensitive is None else case_insensitive
    workfolder = normalize_path(run_paths.workfolder, base=Path.cwd(), home=home)
    run_dir = normalize_path(run_paths.run_dir, base=workfolder, home=home)
    paths = RunPaths(workfolder, run_dir, run_paths.toolchain)

    def norm(raw: str | Path, base: Path = workfolder) -> Path:
        return normalize_path(raw, base=base, home=home)

    work_dir = norm(paths.work_dir(agent))
    tmp_dir = norm(paths.tmp_dir)
    run_venv = paths.toolchain.run_venv is RunVenvMode.OVERLAY
    write_roots = [work_dir, tmp_dir] + ([norm(paths.venv_dir)] if run_venv else [])

    read_allow = list(write_roots)
    read_allow += [norm(paths.output_dir), norm(paths.attachments_dir)]
    for entry in agent.read_dirs:
        if entry.startswith("run:"):
            read_allow.append(norm(run_dir / entry.removeprefix("run:")))
        else:
            read_allow.append(norm(entry))
    if paths.toolchain.python:
        venv = norm(paths.toolchain.python)
        read_allow.append(venv)
        base = _venv_base(venv)
        if base is not None:
            read_allow.append(norm(base))
    read_allow += [norm(p) for p in paths.toolchain.extra_path]
    domains: list[str] = []
    for rule in rules.always:
        if rule.action != "allow" or rule.commands:
            continue
        if not rule.tools or set(rule.tools) & READ_TOOLS:
            read_allow += [norm(p) for p in rule.paths]
        if not rule.tools or set(rule.tools) & NETWORK_TOOLS:
            domains += [h.lower() for h in rule.hosts]

    sensitive = [norm(home / p) for p in SENSITIVE_HOME_PATHS]
    sensitive += [norm(p) for p in SENSITIVE_ABSOLUTE_PATHS]
    sensitive += [norm(app_dir()), norm(workfolder / ".arr")]
    sensitive += [norm(p) for p in rules.sensitive_paths]

    read_deny = [home, *(norm(p) for p in READ_BLOCKED_ROOTS), workfolder]

    return SandboxProfile(
        agent_id=agent.id,
        provider=agent.provider,
        home=home,
        workfolder=workfolder,
        work_dir=work_dir,
        tmp_dir=tmp_dir,
        write_roots=_dedupe(write_roots, ci),
        read_allow=_dedupe(read_allow, ci),
        read_deny=_dedupe(read_deny, ci),
        sensitive=_dedupe(sensitive, ci),
        network=agent.tools.web,
        allowed_domains=tuple(dict.fromkeys(domains)),
        run_venv=run_venv,
        case_insensitive=ci,
    )


# --------------------------------------------------------------------------------------------
# Translators into backend options (data only)
# --------------------------------------------------------------------------------------------


def _claude_path(path: Path, tree: bool) -> str:
    # Claude permission rules take absolute paths with a leading `//` (S1 §3.1).
    return f"/{path}/**" if tree else f"/{path}"


@dataclass(frozen=True, slots=True)
class ClaudeOptions:
    """Permission-related `ClaudeAgentOptions` of one agent (docs/01 §4.1, S1 §3.1, §3.4).

    `add_dirs` holds only writable run folders; read folders are bound read-only through
    `Read(//…)` rules and `sandbox.filesystem.allowRead`. Never a bare `Read`/`Bash` rule.
    """

    cwd: str
    add_dirs: list[str]
    allowed_tools: list[str]
    disallowed_tools: list[str]
    permission_mode: str
    sandbox: dict[str, Any]
    env: dict[str, str]


def claude_options(profile: SandboxProfile) -> ClaudeOptions:
    """Translate a profile into Claude options (W2-01 checks that the SDK enforces them)."""
    writable = [p for p in profile.write_roots if p != profile.work_dir]
    allowed = [
        f"Read({_claude_path(p, tree=True)})"
        for p in profile.read_allow
        if p not in profile.write_roots
    ]
    disallowed: list[str] = []
    for path in profile.sensitive:
        for tool in ("Read", "Edit"):
            disallowed += [
                f"{tool}({_claude_path(path, tree=False)})",
                f"{tool}({_claude_path(path, tree=True)})",
            ]
    disallowed += [f"Edit({_claude_path(p, tree=True)})" for p in profile.read_only]
    for program in (*FORBIDDEN_PROGRAMS, "git push"):
        disallowed += [f"Bash({program})", f"Bash({program} *)"]
    sandbox = {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "failIfUnavailable": True,
        "network": {"allowedDomains": list(profile.allowed_domains)},
        "filesystem": {
            "allowWrite": [str(p) for p in profile.write_roots],
            "denyRead": [str(p) for p in (*profile.read_deny, *profile.sensitive)],
            "allowRead": [str(p) for p in profile.read_allow],
        },
    }
    return ClaudeOptions(
        cwd=str(profile.work_dir),
        add_dirs=[str(p) for p in writable],
        allowed_tools=allowed,
        disallowed_tools=disallowed,
        permission_mode="acceptEdits",
        sandbox=sandbox,
        env={"TMPDIR": str(profile.tmp_dir)},
    )


@dataclass(frozen=True, slots=True)
class CodexOptions:
    """Permission-related thread settings of one Codex agent (docs/01 §4.2, S2 §11, §15).

    `permission_profile` is the read allowlist as neutral data (`path` + `access`
    `write`/`read`/`deny`, most specific entry wins). ⚠️ The concrete Codex syntax
    (`[permissions.<id>]`, `FileSystemSandboxEntry`) is tested first in W2-02.
    """

    cwd: str
    sandbox: str
    sandbox_workspace_write: dict[str, Any]
    approvals_reviewer: str
    env: dict[str, str]
    permission_profile: dict[str, Any]


def codex_options(profile: SandboxProfile) -> CodexOptions:
    """Translate a profile into Codex options (W2-02 checks that the SDK enforces them)."""
    entries: list[dict[str, str]] = []
    entries += [{"path": str(p), "access": "write"} for p in profile.write_roots]
    entries += [
        {"path": str(p), "access": "read"}
        for p in profile.read_allow
        if p not in profile.write_roots
    ]
    entries += [{"path": str(p), "access": "deny"} for p in profile.read_deny]
    entries += [{"path": str(p), "access": "deny"} for p in profile.sensitive]
    return CodexOptions(
        cwd=str(profile.work_dir),
        sandbox="workspace_write",
        sandbox_workspace_write={
            "writable_roots": [str(p) for p in profile.write_roots if p != profile.work_dir],
            "network_access": profile.network,
            "exclude_slash_tmp": True,
            "exclude_tmpdir_env_var": True,
        },
        approvals_reviewer="user",
        env={"TMPDIR": str(profile.tmp_dir)},
        permission_profile={"name": f"arr-{profile.agent_id}", "filesystem": entries},
    )


# --------------------------------------------------------------------------------------------
# Command classification
# --------------------------------------------------------------------------------------------

_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "|&", ";;", "(", ")", "\n"})
_REDIRECTS_RE = re.compile(r"^\d*[<>]+&?\d*$")
_SUBST_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`|<\(([^()]*)\)")
_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"`<>]+")
_SCP_HOST_RE = re.compile(r"(?:^|\s)[\w.-]+@([\w-]+(?:\.[\w-]+)+):")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_VERSIONED_RE = re.compile(r"^(python|pip)[0-9.]*$")
_METACHARS = frozenset(";&|`$<>()\n")
_CMD_PATH_RE = re.compile(
    r"(?:(?<=[\s'\"=(,:])|^)("
    r"(?:~|\$HOME|\$\{HOME\})(?=/|$|[\s'\";|&)])[^\s'\"`;|&<>(),]*"
    r"|\.\.?/[^\s'\"`;|&<>(),]*"
    r"|/[^\s'\"`;|&<>(),/][^\s'\"`;|&<>(),]*"
    r"|/(?=$|[\s'\";|&)])"
    r")"
)


@dataclass(slots=True)
class CommandInfo:
    """Coarse classification of a shell command (docs/07 §3 ②).

    `forbidden`: German reasons for a hard ban. `categories`: grey-zone categories.
    `hosts`: hosts found in URLs. `risky`: install from a URL or VCS, or download piped
    into a shell.
    """

    forbidden: list[str] = field(default_factory=list)
    categories: set[RequestCategory] = field(default_factory=set)
    hosts: list[str] = field(default_factory=list)
    risky: bool = False
    user_install: bool = False
    programs: list[str] = field(default_factory=list)


def _segments(command: str) -> list[list[str]]:
    text = command.replace("\n", " ; ")
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = None
    if tokens is None:
        # Unbalanced quotes: the shell would refuse it, but be conservative and treat every
        # word as a possible program start.
        words = re.sub(r"([;&|()'\"])", r" ", text).split()
        return [words[i:] for i in range(len(words))]
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SEPARATORS or (token and set(token) <= set(";&|()")):
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _program(token: str) -> str:
    name = token.rsplit("/", 1)[-1].casefold()
    match = _VERSIONED_RE.match(name)
    return match.group(1) if match else name


def _strip_wrappers(argv: list[str]) -> list[str]:
    argv = list(argv)
    while argv:
        while argv and _ASSIGN_RE.match(argv[0]):
            argv.pop(0)
        if not argv:
            break
        name = _program(argv[0])
        if name not in _WRAPPERS:
            break
        argv.pop(0)
        with_arg = _WRAPPERS_WITH_ARG.get(name, set())
        while argv and (argv[0].startswith("-") or _ASSIGN_RE.match(argv[0])):
            flag = argv.pop(0)
            if flag in with_arg and argv:
                argv.pop(0)
        if name == "timeout" and argv:
            argv.pop(0)
    return argv


def _git_subcommand(args: list[str]) -> str | None:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            index += 2
        elif arg.startswith("-"):
            index += 1
        else:
            return arg.casefold()
    return None


def _host_of(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.rstrip(".").lower() if host else None


def _classify_argv(argv: list[str], info: CommandInfo, depth: int) -> None:
    argv = _strip_wrappers(argv)
    argv = [a for a in argv if not _REDIRECTS_RE.match(a)]
    if not argv:
        return
    name = _program(argv[0])
    args = argv[1:]
    lowered = [a.casefold() for a in args]
    info.programs.append(name)
    if name in FORBIDDEN_PROGRAMS:
        info.forbidden.append(FORBIDDEN_PROGRAMS[name])
        return
    if name == "defaults" and lowered[:1] and lowered[0] in ("write", "delete", "import"):
        info.forbidden.append(_SYSTEM)
        return
    if name in _SHELLS or name == "eval":
        script = None
        if name == "eval":
            script = " ".join(args)
        elif "-c" in args and args.index("-c") + 1 < len(args):
            script = args[args.index("-c") + 1]
        if script and depth < 4:
            _classify(script, info, depth + 1)
        return
    if name == "git":
        sub = _git_subcommand(args)
        if sub in _GIT_FORBIDDEN:
            info.forbidden.append(_GIT_PUSH)
        elif sub in _GIT_NETWORK:
            info.categories.add(RequestCategory.NETWORK)
        return
    if name in NETWORK_PROGRAMS:
        info.categories.add(RequestCategory.NETWORK)
        return
    _classify_install(name, args, lowered, info)


def _classify_install(name: str, args: list[str], lowered: list[str], info: CommandInfo) -> None:
    sub = lowered[0] if lowered else ""
    install_args: list[str] | None = None
    if name == "pip" and sub in ("install", "download"):
        install_args = args[1:]
    elif name == "python" and lowered[:2] == ["-m", "pip"] and lowered[2:3] == ["install"]:
        install_args = args[3:]
    elif name == "uv":
        if lowered[:2] == ["pip", "install"]:
            install_args = args[2:]
        elif sub in ("add", "sync"):
            install_args = args[1:]
        elif lowered[:2] == ["tool", "install"]:
            info.forbidden.append(_GLOBAL_INSTALL)
            return
    elif name in ("conda", "mamba", "micromamba") and sub in ("install", "create", "update"):
        install_args = args[1:]
    elif name in ("npm", "pnpm", "yarn") and sub in ("install", "i", "add", "ci"):
        if any(a in ("-g", "--global") for a in lowered):
            info.forbidden.append(_GLOBAL_INSTALL)
            return
        install_args = args[1:]
    elif name == "cargo" and sub == "install":
        info.forbidden.append(_GLOBAL_INSTALL)
        return
    elif name == "sage" and lowered[:2] == ["-pip", "install"]:
        install_args = args[2:]
    if install_args is None:
        return
    info.categories |= {RequestCategory.INSTALL, RequestCategory.NETWORK}
    if any(a.casefold() == "--user" for a in install_args):
        info.user_install = True
    if any(
        "://" in a or a.casefold().startswith(("git+", "-e", "--editable")) for a in install_args
    ):
        info.risky = True


def _classify(command: str, info: CommandInfo, depth: int) -> None:
    for match in _SUBST_RE.finditer(command):
        inner = next(g for g in match.groups() if g is not None)
        if depth < 4:
            _classify(inner, info, depth + 1)
    for argv in _segments(command):
        _classify_argv(argv, info, depth)


def classify_command(command: str) -> CommandInfo:
    """Classify a shell command coarsely: bans, network, installs, hosts (docs/07 §3 ②).

    Handles `;`, `&&`, `|`, newlines, `$(…)`, backticks, `bash -c`, `eval`, wrappers such as
    `env`/`nohup`/`timeout`, variable assignments, full program paths and letter case.
    Not a full shell parser: the OS sandbox stays the main protection.
    """
    info = CommandInfo()
    _classify(command, info, 0)
    for url in _URL_RE.findall(command):
        host = _host_of(url)
        if host:
            info.hosts.append(host)
            info.categories.add(RequestCategory.NETWORK)
    info.hosts += [h.lower() for h in _SCP_HOST_RE.findall(command)]
    info.hosts = list(dict.fromkeys(info.hosts))
    if RequestCategory.NETWORK in info.categories and set(info.programs) & _SHELLS:
        info.risky = True
    if RequestCategory.NETWORK in info.categories and "python" in info.programs and "|" in command:
        info.risky = True
    return info


def command_paths(command: str, base: Path | None = None) -> list[str]:
    """Path-like words in a command, unnormalized (docs/07 §3 ②).

    Finds absolute paths, `~`, `$HOME`, `./` and `../` anywhere (also inside `python -c`
    strings), and words of the command line that contain `/` or, with `base`, name an
    existing entry below `base` (e.g. a symlink in the work folder).
    """
    found = [m.group(1) for m in _CMD_PATH_RE.finditer(command)]
    for argv in _segments(command):
        for word in argv[1:]:
            value = word.split("=", 1)[1] if word.startswith("-") and "=" in word else word
            if not value or value.startswith("-") or "://" in value:
                continue
            if "/" in value or (base is not None and os.path.lexists(base / value)):
                found.append(value)
    return list(dict.fromkeys(found))


def _normalize_command(command: str) -> str:
    return " ".join(command.split())


# --------------------------------------------------------------------------------------------
# Redaction and approvals.jsonl
# --------------------------------------------------------------------------------------------

REDACTED = "«entfernt»"

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
)
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)[A-Z0-9_]*)"
    r"=(\"[^\"]*\"|'[^']*'|[^\s;&|]+)"
)
_SECRET_FLAG_RE = re.compile(
    r"(?i)(--?(?:password|passwd|token|api[-_]?key|secret|auth)(?:=|\s+))(\"[^\"]*\"|'[^']*'|\S+)"
)
_AUTH_HEADER_RE = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=\-]{8,}")
_URL_CREDS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@:]+:[^\s/@]+@")


def redact(text: str) -> str:
    """Replace secrets (API keys, tokens, passwords, private keys) with a placeholder.

    Used for everything that goes into `approvals.jsonl` or onto the terminal (docs/01 §8).
    """
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    text = _SECRET_FLAG_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    return _URL_CREDS_RE.sub(lambda m: f"{m.group(1)}{REDACTED}@", text)


def _redact_data(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_data(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_data(v) for k, v in value.items()}
    return value


def redact_request(request: PermissionRequest) -> PermissionRequest:
    """Return a copy of the request with all secrets in its texts replaced."""
    data = _redact_data(request.model_dump(mode="json", by_alias=True))
    return PermissionRequest.model_validate(data)


class LogEvent(StrEnum):
    """Kind of a line in `approvals.jsonl` (docs/02 §4, docs/07 §4)."""

    DECISION = "decision"
    """PolicyEngine result (allow, deny or ask)."""
    OPEN = "open"
    """An ask became an inbox item."""
    DECIDED = "decided"
    """The user (or later the gatekeeper) decided an item."""
    EXPIRED = "expired"
    """Timeout without decision, counts as deny."""
    REVOKED = "revoked"
    """A decision with scope agent/run was withdrawn."""
    RULE = "rule"
    """An „Immer“ decision became a fixed rule."""
    OBSOLETE = "obsolete"
    """An open item is no longer needed (run stopped, waiting agent gone)."""


class ApprovalLogEntry(Record):
    """One line of `runs/<id>/approvals.jsonl` (docs/02 §4, docs/07 §4). Never a secret."""

    ts: float = Field(default_factory=time.time)
    event: LogEvent
    item_id: str | None = None
    request: PermissionRequest | None = None
    decision: Decision | None = None
    rule: AlwaysRule | None = None
    note: str | None = None


class ApprovalLog:
    """Append-only writer for `approvals.jsonl`; redacts every string before writing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(self, entry: ApprovalLogEntry) -> None:
        """Append one entry as a JSON line."""
        data = _redact_data(entry.model_dump(mode="json", by_alias=True, exclude_none=True))
        line = json.dumps(data, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def entries(self) -> list[dict[str, Any]]:
        """Read all entries as plain dicts (empty if the file does not exist)."""
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]


# --------------------------------------------------------------------------------------------
# PolicyEngine
# --------------------------------------------------------------------------------------------

ItemKey = tuple[str, str]
"""A grey-zone item of a request: `("read", path)`, `("host", h)`, `("command", c)`, …"""


@dataclass(slots=True)
class _Analysis:
    forbidden: list[str] = field(default_factory=list)
    categories: set[RequestCategory] = field(default_factory=set)
    grey: dict[ItemKey, RequestCategory] = field(default_factory=dict)
    facts: set[ItemKey] = field(default_factory=set)
    labels: dict[ItemKey, str] = field(default_factory=dict)
    risky: bool = False

    def ban(self, reason: str, category: RequestCategory | None = None) -> None:
        if reason not in self.forbidden:
            self.forbidden.append(reason)
        if category is not None:
            self.categories.add(category)

    def ask(self, key: ItemKey, category: RequestCategory) -> None:
        self.grey.setdefault(key, category)
        self.facts.add(key)
        self.categories.add(category)


@dataclass(frozen=True, slots=True)
class Assessment:
    """Result of `PolicyEngine.assess`: the decision and the request with its categories."""

    decision: Decision
    request: PermissionRequest
    items: frozenset[ItemKey]


@dataclass(frozen=True, slots=True)
class Grant:
    """A remembered decision with scope `agent` or `run` (docs/07 §3 ④); revocable (§4)."""

    item_id: str
    run_id: str
    agent_id: str | None
    decision: Decision
    items: frozenset[ItemKey]
    request: PermissionRequest | None = None

    def applies_to(self, request: PermissionRequest) -> bool:
        """Whether the grant's reach covers this request's run and agent."""
        if request.run_id != self.run_id:
            return False
        return self.agent_id is None or self.agent_id == request.agent_id


def _covers(granted: ItemKey, item: ItemKey) -> bool:
    if granted[0] != item[0]:
        return False
    if granted[0] == "read":
        return _key_within(item[1], granted[1])
    return granted[1] == item[1]


def _host_matches(pattern: str, host: str) -> bool:
    pattern = pattern.rstrip(".").lower()
    if pattern.startswith("*."):
        return host.endswith(pattern[1:])
    return host == pattern


def _command_matches(pattern: str, command: str) -> bool:
    pattern = _normalize_command(pattern)
    if pattern.endswith(" *"):
        prefix = pattern[:-1]
        return not (set(command) & _METACHARS) and command.startswith(prefix)
    return command == pattern


_RISK_TEXT = {
    "allow": "Bleibt innerhalb der Sandbox und der Erlaubnisliste.",
    "forbidden": "Die Aktion verstößt gegen eine feste Regel und würde Schutzgrenzen umgehen.",
    "risky": (
        "Der Befehl lädt Code aus dem Netz und führt ihn aus oder installiert aus einer "
        "unbekannten Quelle."
    ),
    "network": "Daten aus dem Netz können den Agenten beeinflussen, Inhalte können abfließen.",
    "install": "Fremder Code wird in die Run-venv installiert und später ausgeführt.",
    "read_outside": "Was der Agent liest, geht als Kontext an den Modell-Anbieter.",
    "other": "Das Werkzeug ist arr nicht bekannt und lässt sich nicht automatisch prüfen.",
}


class PolicyEngine:
    """Layer ② of docs/07 §3: fixed rules, always rules and grants → allow, deny or ask.

    One engine per run. Profiles come from `profile_for`, one per agent. `evaluate` is the
    `can_use_tool` path (logs every decision), `hard_deny` the `PreToolUse` path (logs only
    bans). `remember` stores decisions from the inbox, `revoke` withdraws them (docs/07 §4).
    """

    def __init__(
        self,
        profiles: Iterable[SandboxProfile] | Mapping[str, SandboxProfile],
        *,
        rules: PermissionRules | None = None,
        log: ApprovalLog | None = None,
    ) -> None:
        values = profiles.values() if isinstance(profiles, Mapping) else profiles
        self._profiles = {p.agent_id: p for p in values}
        self.rules = rules or PermissionRules()
        self.log = log
        self._grants: dict[str, Grant] = {}

    def add_profile(self, profile: SandboxProfile) -> None:
        """Register or replace the profile of an agent."""
        self._profiles[profile.agent_id] = profile

    def profile(self, agent_id: str) -> SandboxProfile:
        """Return the profile of an agent; `KeyError` if there is none."""
        return self._profiles[agent_id]

    @property
    def grants(self) -> Sequence[Grant]:
        """Active grants (decisions with scope agent/run), oldest first."""
        return list(self._grants.values())

    # -- public API ---------------------------------------------------------------------

    def evaluate(self, request: PermissionRequest) -> Decision:
        """Decide a request: `allow`, `deny` with reason, or `ask` (docs/07 §3 ②, §7)."""
        return self.assess(request).decision

    def assess(self, request: PermissionRequest) -> Assessment:
        """Like `evaluate`, but also return the request with its categories filled in."""
        assessment = self._assess(request)
        self._write(LogEvent.DECISION, assessment.request, assessment.decision)
        return assessment

    def hard_deny(self, request: PermissionRequest) -> Decision | None:
        """Return a `deny` for hard bans and always-deny rules, else `None` (`PreToolUse`)."""
        assessment = self._assess(request)
        decision = assessment.decision
        if decision.action is DecisionAction.DENY and decision.decided_by is DecidedBy.FIXED_RULE:
            self._write(LogEvent.DECISION, assessment.request, decision)
            return decision
        return None

    def remember(
        self, item_id: str, request: PermissionRequest, decision: Decision
    ) -> AlwaysRule | None:
        """Keep a final decision according to its scope (docs/07 §3 ④).

        `once` is not kept. `agent` and `run` become grants that cover the same items later.
        `always` (user only, enforced by `Decision`) becomes an `AlwaysRule` in `self.rules`;
        it is returned so the caller can store it in `arr.yaml`.
        """
        if decision.action not in (DecisionAction.ALLOW, DecisionAction.DENY):
            return None
        if decision.scope is DecisionScope.ONCE:
            return None
        profile = self._profiles.get(request.agent_id)
        if profile is None:
            return None
        analysis = self._analyze(request, profile)
        items = frozenset(analysis.grey)
        if not items:
            return None
        if decision.scope is DecisionScope.ALWAYS:
            rule = _rule_from_items(analysis, decision)
            self.rules.always.append(rule)
            self._write(LogEvent.RULE, request, decision, item_id=item_id, rule=rule)
            return rule
        agent_id = request.agent_id if decision.scope is DecisionScope.AGENT else None
        self._grants[item_id] = Grant(
            item_id, request.run_id, agent_id, decision, items, redact_request(request)
        )
        return None

    def revoke(self, item_id: str) -> Grant:
        """Withdraw a decision with scope agent/run; it no longer applies (docs/07 §4).

        Raises `ArrError` if there is no such grant or the decision is not revocable.
        „Immer“ rules are removed in the settings, not here.
        """
        grant = self._grants.get(item_id)
        if grant is None:
            raise ArrError(
                f"Zur Entscheidung „{item_id}“ gibt es nichts zurückzuziehen. Einmalige "
                "Entscheidungen gelten nur für ihre Anfrage, „Immer“-Regeln entfernst du in "
                "den Einstellungen."
            )
        if not grant.decision.revocable:
            raise ArrError(f"Die Entscheidung „{item_id}“ lässt sich nicht zurückziehen.")
        del self._grants[item_id]
        self._write(LogEvent.REVOKED, grant.request, grant.decision, item_id=item_id)
        return grant

    # -- internals ----------------------------------------------------------------------

    def _write(
        self,
        event: LogEvent,
        request: PermissionRequest | None,
        decision: Decision | None,
        *,
        item_id: str | None = None,
        rule: AlwaysRule | None = None,
    ) -> None:
        if self.log is not None:
            self.log.write(
                ApprovalLogEntry(
                    event=event, item_id=item_id, request=request, decision=decision, rule=rule
                )
            )

    def _assess(self, request: PermissionRequest) -> Assessment:
        profile = self._profiles.get(request.agent_id)
        if profile is None:
            decision = _deny(_R_UNKNOWN_AGENT, "forbidden")
            return Assessment(decision, request, frozenset())
        analysis = self._analyze(request, profile)
        categories = sorted(set(request.categories) | analysis.categories)
        request = request.model_copy(update={"categories": categories})
        items = frozenset(analysis.grey)
        return Assessment(self._decide(request, profile, analysis), request, items)

    def _decide(
        self, request: PermissionRequest, profile: SandboxProfile, analysis: _Analysis
    ) -> Decision:
        if analysis.forbidden:
            return _deny(" ".join(analysis.forbidden), "forbidden")
        for rule in self.rules.always:
            if rule.action == "deny" and any(
                _rule_covers(rule, request, key, profile) for key in analysis.facts
            ):
                reason = rule.reason or "Abgelehnt durch eine feste Regel aus arr.yaml."
                return _deny(reason + _ALTERNATIVE, "forbidden")
        grey = analysis.grey
        for grant in self._grants.values():
            if (
                grant.decision.action is DecisionAction.DENY
                and grant.applies_to(request)
                and any(_covers(g, key) for g in grant.items for key in grey)
            ):
                return _from_grant(grant, "Abgelehnt durch eine frühere Entscheidung")
        if not grey:
            return Decision(
                action=DecisionAction.ALLOW,
                decided_by=DecidedBy.FIXED_RULE,
                reason="Innerhalb der Sandbox und der Erlaubnisliste erlaubt.",
                risk=Risk(level=RiskLevel.LOW, text=_RISK_TEXT["allow"]),
            )
        open_items = {
            key: cat
            for key, cat in grey.items()
            if not any(
                rule.action == "allow" and _rule_covers(rule, request, key, profile)
                for rule in self.rules.always
            )
            and not (profile.network and _web_covers(key, cat))
        }
        if not open_items:
            return Decision(
                action=DecisionAction.ALLOW,
                decided_by=DecidedBy.FIXED_RULE,
                reason="Erlaubt durch eine feste Regel (arr.yaml oder tools.web).",
                risk=_risk(analysis, grey),
            )
        allow_grants = [
            g
            for g in self._grants.values()
            if g.decision.action is DecisionAction.ALLOW and g.applies_to(request)
        ]
        covering = [
            next((g for g in allow_grants if any(_covers(i, key) for i in g.items)), None)
            for key in open_items
        ]
        if all(covering):
            return _from_grant(covering[0], "Erlaubt durch eine frühere Entscheidung")
        labels = ", ".join(_CATEGORY_LABELS[c] for c in sorted(set(open_items.values())))
        return Decision(
            action=DecisionAction.ASK,
            decided_by=DecidedBy.FIXED_RULE,
            reason=f"Braucht eine Freigabe ({labels}).",
            risk=_risk(analysis, open_items),
        )

    def _analyze(self, request: PermissionRequest, profile: SandboxProfile) -> _Analysis:
        analysis = _Analysis()
        tool = request.tool
        try:
            base = profile.resolve(request.work_dir) if request.work_dir else profile.work_dir
        except ValueError:
            base = profile.work_dir
        if tool.startswith(ROOM_TOOL_PREFIXES) or tool in HARMLESS_TOOLS:
            pass
        elif tool in READ_TOOLS:
            self._check_reads(request.paths, tool in SEARCH_TOOLS, base, profile, analysis)
        elif tool in WRITE_TOOLS:
            self._check_writes(request.paths, base, profile, analysis)
            if not request.paths:
                analysis.ask(("tool", tool), RequestCategory.OTHER)
        elif tool in SHELL_TOOLS:
            self._check_command(request, base, profile, analysis)
        elif tool in NETWORK_TOOLS:
            if not request.hosts and not request.urls:
                analysis.ask(("tool", tool), RequestCategory.NETWORK)
        else:
            analysis.ask(("tool", tool), RequestCategory.OTHER)
        hosts = [h.rstrip(".").lower() for h in request.hosts]
        hosts += [h for h in (_host_of(u) for u in request.urls) if h]
        for host in dict.fromkeys(hosts):
            analysis.ask(("host", host.rsplit(":", 1)[0]), RequestCategory.NETWORK)
        self._adapter_categories(request, analysis)
        return analysis

    def _adapter_categories(self, request: PermissionRequest, analysis: _Analysis) -> None:
        # Categories set by the adapter are never downgraded (e.g. a Codex escalation that
        # would run outside the sandbox): what the engine did not cover itself is asked.
        subject: ItemKey = (
            ("command", _normalize_command(request.command))
            if request.command
            else ("tool", request.tool)
        )
        for category in request.categories:
            if category is RequestCategory.PRIVILEGED:
                analysis.ban(_PRIVILEGED, category)
            elif category is RequestCategory.WRITE_OUTSIDE and not request.paths:
                analysis.ban(_R_WRITE_OUTSIDE, category)
            elif category not in analysis.categories and not (
                category is RequestCategory.READ_OUTSIDE and request.paths
            ):
                analysis.ask(subject, category)

    def _check_reads(
        self,
        paths: list[str],
        search: bool,
        base: Path,
        profile: SandboxProfile,
        analysis: _Analysis,
    ) -> None:
        for raw in paths:
            try:
                path = profile.resolve(_strip_glob(raw), base)
            except ValueError:
                analysis.ban(_R_INVALID_PATH)
                continue
            key: ItemKey = ("read", profile.key(path))
            analysis.facts.add(key)
            if profile.is_sensitive(path):
                analysis.ban(_R_SENSITIVE, RequestCategory.READ_OUTSIDE)
            elif search and profile.contains_sensitive(path):
                analysis.ban(_R_SEARCH_SENSITIVE, RequestCategory.READ_OUTSIDE)
            elif not profile.can_read(path):
                analysis.ask(key, RequestCategory.READ_OUTSIDE)
                analysis.labels[key] = str(path)

    def _check_writes(
        self, paths: list[str], base: Path, profile: SandboxProfile, analysis: _Analysis
    ) -> None:
        for raw in paths:
            try:
                path = profile.resolve(raw, base)
            except ValueError:
                analysis.ban(_R_INVALID_PATH)
                continue
            analysis.facts.add(("write", profile.key(path)))
            if profile.is_sensitive(path):
                analysis.ban(_R_SENSITIVE, RequestCategory.WRITE_OUTSIDE)
            elif profile.can_write(path):
                continue
            elif any(_key_within(profile.key(path), profile.key(p)) for p in profile.read_only):
                analysis.ban(_R_READ_ONLY, RequestCategory.WRITE_OUTSIDE)
            else:
                analysis.ban(_R_WRITE_OUTSIDE, RequestCategory.WRITE_OUTSIDE)

    def _check_command(
        self,
        request: PermissionRequest,
        base: Path,
        profile: SandboxProfile,
        analysis: _Analysis,
    ) -> None:
        command = request.command or ""
        info = classify_command(command)
        for reason in info.forbidden:
            analysis.ban(reason + _ALTERNATIVE, _BAN_CATEGORIES[reason])
        for raw in [*command_paths(command, base), *request.paths]:
            try:
                path = profile.resolve(raw, base)
            except ValueError:
                analysis.ban(_R_INVALID_PATH)
                continue
            analysis.facts.add(("read", profile.key(path)))
            if profile.is_sensitive(path):
                analysis.ban(_R_SENSITIVE, RequestCategory.READ_OUTSIDE)
            elif not (profile.can_read(path) or profile.can_write(path)):
                analysis.ban(_R_SHELL_READ, RequestCategory.READ_OUTSIDE)
        normalized = _normalize_command(command)
        analysis.facts.add(("command", normalized))
        if RequestCategory.INSTALL in info.categories:
            if info.user_install:
                analysis.ban(_R_USER_INSTALL, RequestCategory.INSTALL)
            elif not profile.run_venv:
                analysis.ban(_R_NO_RUN_VENV, RequestCategory.INSTALL)
            analysis.ask(("command", normalized), RequestCategory.INSTALL)
            analysis.categories.add(RequestCategory.NETWORK)
        elif RequestCategory.NETWORK in info.categories:
            if info.hosts:
                for host in info.hosts:
                    analysis.ask(("host", host), RequestCategory.NETWORK)
            else:
                analysis.ask(("command", normalized), RequestCategory.NETWORK)
        analysis.categories |= info.categories
        analysis.risky = analysis.risky or info.risky


def _web_covers(key: ItemKey, category: RequestCategory) -> bool:
    # tools.web switches the network on for an agent; installs still need an approval.
    return category is RequestCategory.NETWORK and key[0] in ("host", "command", "tool")


def _rule_covers(
    rule: AlwaysRule, request: PermissionRequest, key: ItemKey, profile: SandboxProfile
) -> bool:
    kind, value = key
    if kind == "tool":
        return value in rule.tools and not (rule.paths or rule.hosts or rule.commands)
    if rule.tools and request.tool not in rule.tools:
        return False
    if kind in ("read", "write"):
        return any(_key_within(value, _rule_path_key(p, profile)) for p in rule.paths)
    if kind == "host":
        return any(_host_matches(p, value) for p in rule.hosts)
    if kind == "command":
        return any(_command_matches(p, value) for p in rule.commands)
    return False


def _rule_path_key(raw: str, profile: SandboxProfile) -> str:
    # Rule paths are absolute or `~/…`; relative ones count from the workfolder.
    try:
        return profile.key(profile.resolve(raw, profile.workfolder))
    except ValueError:
        return "\x00"


def _rule_from_items(analysis: _Analysis, decision: Decision) -> AlwaysRule:
    # Paths keep their real spelling; the comparison key may be case folded.
    items = analysis.grey
    tools = [v for k, v in items if k == "tool"]
    return AlwaysRule(
        action="allow" if decision.action is DecisionAction.ALLOW else "deny",
        tools=tools,
        paths=sorted(analysis.labels.get((k, v), v) for k, v in items if k == "read"),
        hosts=sorted(v for k, v in items if k == "host"),
        commands=sorted(v for k, v in items if k == "command"),
        reason=decision.reason,
    )


def _deny(reason: str, risk_key: str) -> Decision:
    return Decision(
        action=DecisionAction.DENY,
        decided_by=DecidedBy.FIXED_RULE,
        reason=reason,
        risk=Risk(level=RiskLevel.HIGH, text=_RISK_TEXT[risk_key]),
    )


def _from_grant(grant: Grant, prefix: str) -> Decision:
    previous = grant.decision
    return Decision(
        action=previous.action,
        scope=previous.scope,
        decided_by=previous.decided_by,
        reason=f"{prefix} ({grant.item_id}, Reichweite „{previous.scope}“).",
        risk=previous.risk,
    )


def _risk(analysis: _Analysis, items: Mapping[ItemKey, RequestCategory]) -> Risk:
    categories = set(items.values())
    if analysis.risky:
        return Risk(level=RiskLevel.HIGH, text=_RISK_TEXT["risky"])
    for category, key in (
        (RequestCategory.INSTALL, "install"),
        (RequestCategory.NETWORK, "network"),
        (RequestCategory.READ_OUTSIDE, "read_outside"),
    ):
        if category in categories:
            return Risk(level=RiskLevel.MEDIUM, text=_RISK_TEXT[key])
    return Risk(level=RiskLevel.MEDIUM, text=_RISK_TEXT["other"])
