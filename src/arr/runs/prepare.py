"""Prepare a run: create the run folder, copy projects, snapshot skills and config,
and generate AGENTS.md.

`prepare_run` turns a resolved run configuration into the run folder of docs/02 §4: `run.yaml`,
`run.input.yaml`, `task.md`, `meta.json` (status `preparing`), the project snapshot `base/`, the
skill snapshot `skills/` and one folder per agent with `agent.json`, `role.md`, `context.md` and
the working folder. Projects are copied file by file as APFS clones (docs/spikes/S5 §7), with
a real copy as fallback. Starting the agents and the room database are not part of this step.

See docs/02-workfolder-und-runs.md §3.1, §4, §7 and docs/spikes/S1 §3.3.
"""

import contextlib
import ctypes
import errno
import importlib.metadata
import json
import logging
import os
import re
import secrets
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Any, Literal

from arr import __version__
from arr.errors import ArrError, ConfigError, ConfigIssue
from arr.models import (
    AgentConfig,
    AuthKind,
    ProfilesFile,
    Provider,
    Record,
    ResolvedRun,
    RunInput,
    RunStatus,
    RunVenvMode,
    Starter,
    WorkfolderSettings,
    WorkspaceMode,
)
from arr.resolve import dump_yaml_model
from arr.skills import (
    SkillCatalog,
    SkillInfo,
    check_research_skills,
    discover_skills,
    install_codex_skills,
    internal_link_target,
    snapshot_skills,
    write_claude_plugin,
)
from arr.venvs import base_venv_dir, glob_safe_name, run_venv_dir, venv_base_prefix, venv_root
from arr.workfolder import (
    RESEARCH_AGENT_BLOCK,
    USER_AGENT_BLOCK,
    block_span,
    load_project_settings,
    load_workfolder_settings,
    project_dir,
    read_template,
    replace_block,
)

__all__ = [
    "ALWAYS_EXCLUDED",
    "AgentFile",
    "CopyStats",
    "ExcludeSet",
    "PreparedAgent",
    "PreparedRun",
    "RESERVED_STATUSES",
    "RunMeta",
    "copy_tree",
    "load_agent_file",
    "load_run_meta",
    "new_run_id",
    "prepare_run",
    "run_venv_location",
]

logger = logging.getLogger(__name__)

RUN_SOURCE = "run.yaml"
DEFAULT_ROLE = (
    "Du bist ein Research-Agent in diesem Run. Für dich ist keine Rolle festgelegt: "
    "Bearbeite die Aufgabe selbstständig und gib deine Ergebnisse mit `submit_result` ab.\n"
)
ALWAYS_EXCLUDED = (".claude/skills", ".agents/skills", "/.mcp.json", "/AGENTS.override.md")
"""Never copied into `base/` and the working copies.

Skills reach research agents only by explicit listing, through the plugin (Claude) or
`.agents/skills` (Codex), never from the project. `.mcp.json` connects the user agent to arr,
which research agents must not have (E11). `AGENTS.override.md` would take precedence over the
generated `AGENTS.md` in Codex and hide the research block."""
_MAX_LINK_WARNINGS = 20

_SDK_PACKAGES = ("claude-agent-sdk", "openai-codex")
_RUN_ID_RE = re.compile(r"^\d{8}-\d{4}_[A-Za-z0-9-]+_[A-Za-z0-9-]+_[0-9a-f]{4}$")


# --------------------------------------------------------------------------------------------
# Files written into the run folder
# --------------------------------------------------------------------------------------------


class RunMeta(Record):
    """Content of `meta.json` (docs/02 §4). The RunManager (W2-03) updates status and times.

    `run_venv` is the absolute folder of the run venv outside `~` (`RunPaths.venv_dir`,
    W3-00), None with `run_venv: off` and in runs before W3-00 (`<run>/.venv`). Read it with
    `run_venv_location`.
    """

    run_id: str
    status: RunStatus = RunStatus.PREPARING
    starter: Starter
    project: str
    skill: str | None = None
    name: str | None = None
    created: float
    started: float | None = None
    ended: float | None = None
    arr_version: str
    sdk_versions: dict[str, str] = {}
    run_venv: str | None = None


class AgentFile(Record):
    """Content of `agents/<id>/agent.json`: the resolved `AgentConfig` plus runtime data.

    All paths are relative to the run folder (POSIX style), so the folder can be moved or
    archived. `path_prepend` holds absolute folders for the `PATH` of the agent's processes
    (base venv `bin/` first, then `toolchain.extra_path`). `base_python` is the folder of the
    base venv from `toolchain.python` (also when an interpreter was given there), or None if
    none is set or it does not exist; then its `bin/` is not in `path_prepend` either.
    `generated_paths` lists what the preparer wrote into `work_dir` (relative to it), so
    `changes.diff` can leave it out. There is no secret in here, only the auth profile's name
    (docs/01 §8).

    Project context for the first prompt (E10): `context_file` (`agents/<id>/context.md`) always
    holds the agent's own processed context (project `CLAUDE.md` with the research block, or the
    block alone for `empty`). In `copy` and `empty` mode, `AGENTS.md` in `work_dir` (and
    `CLAUDE.md`, if the project has one) contain exactly the same text. In `shared` mode they
    hold a neutral variant without name, role and channels, because several agents share the
    folder. So:

    - Codex reads `AGENTS.md` from its `cwd` itself and needs `context.md` in the first prompt
      only in `shared` mode (otherwise it would get the text twice).
    - Claude with `setting_sources=[]` loads no `CLAUDE.md` (docs/spikes/S1 §3.3), so the Claude
      backend puts `context.md` into the first prompt in every mode and again after a compaction.
    """

    config: AgentConfig
    work_dir: str
    role_file: str
    context_file: str
    plugin_dir: str | None = None
    skill_ids: list[str] = []
    generated_paths: list[str] = []
    path_prepend: list[str] = []
    base_python: str | None = None
    run_venv: RunVenvMode = RunVenvMode.OVERLAY
    copy_method: Literal["clone", "copy"] | None = None


@dataclass(frozen=True, slots=True)
class PreparedAgent:
    """A prepared agent: its `agent.json` content and absolute paths."""

    file: AgentFile
    run_dir: Path

    @property
    def id(self) -> str:
        """Agent ID, e.g. `explorer-1`."""
        return self.file.config.id

    @property
    def agent_dir(self) -> Path:
        """`agents/<id>/`."""
        return self.run_dir / "agents" / self.id

    @property
    def work_dir(self) -> Path:
        """Working folder of the agent (its `cwd`)."""
        return self.run_dir / self.file.work_dir

    @property
    def plugin_dir(self) -> Path | None:
        """Generated Claude plugin, None for Codex agents and agents without skills."""
        return self.run_dir / self.file.plugin_dir if self.file.plugin_dir else None


@dataclass(frozen=True, slots=True)
class PreparedRun:
    """Result of `prepare_run`.

    `resolved` is the configuration of `run.yaml` with `task_file="task.md"`, but unlike the
    file on disk (`task: null`) it carries the text of `task.md` in `task` for the topologies.
    """

    run_id: str
    run_dir: Path
    meta: RunMeta
    resolved: ResolvedRun
    agents: dict[str, PreparedAgent]
    warnings: list[str]


def load_run_meta(run_dir: str | Path) -> RunMeta:
    """Read `<run_dir>/meta.json`."""
    return RunMeta.model_validate_json((Path(run_dir) / "meta.json").read_text("utf-8"))


def run_venv_location(run_dir: str | Path) -> Path | None:
    """Folder of a run's venv: from `meta.json`, else `<run>/.venv` of a run before W3-00.

    None if the run has no venv (`run_venv: off`, not created yet or removed) or its
    `meta.json` is not readable.
    """
    folder = Path(run_dir)
    try:
        recorded = load_run_meta(folder).run_venv
    except (OSError, ValueError):
        recorded = None
    if recorded:
        return Path(recorded)
    legacy = folder / ".venv"
    return legacy if legacy.is_dir() and not legacy.is_symlink() else None


def load_agent_file(run_dir: str | Path, agent_id: str) -> AgentFile:
    """Read `<run_dir>/agents/<agent_id>/agent.json`."""
    path = Path(run_dir) / "agents" / agent_id / "agent.json"
    return AgentFile.model_validate_json(path.read_text("utf-8"))


# --------------------------------------------------------------------------------------------
# Run ID
# --------------------------------------------------------------------------------------------


def _id_part(text: str, fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]+", "-", text).strip("-") or fallback


def new_run_id(project: str, skill: str | None, *, now: datetime | None = None) -> str:
    """Build a run ID `<JJJJMMTT-HHMM>_<projekt>_<skill|custom>_<4 hex>` (docs/02 §4).

    Characters other than letters, digits and `-` in project or skill are replaced by `-`.
    """
    moment = now or datetime.now()
    return "_".join(
        (
            moment.strftime("%Y%m%d-%H%M"),
            _id_part(project, "projekt"),
            _id_part(skill or "custom", "custom"),
            secrets.token_hex(2),
        )
    )


# --------------------------------------------------------------------------------------------
# Exclusions (project.yaml copy_exclude)
# --------------------------------------------------------------------------------------------


def _compile_glob(pattern: str, index: int) -> tuple[re.Pattern[str], bool]:
    text = pattern.strip()
    if not text or text.startswith(("!", "#")) or not text.strip("/"):
        raise ConfigError.single(
            f"copy_exclude[{index}]",
            f"Das Muster „{pattern}“ ist ungültig. Erlaubt sind Muster wie „data/big/**“, "
            "„*.pdf“ oder „build/“, aber keine Negation mit „!“.",
            source="project.yaml",
        )
    dir_only = text.endswith("/")
    text = text.rstrip("/")
    anchored = text.startswith("/") or "/" in text
    text = text.lstrip("/")
    contents = text.endswith("/**")
    if contents:
        text = text[:-3]
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "*":
            if text.startswith("**", i):
                i += 2
                if text.startswith("/", i):
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[" and (end := text.find("]", i + 2)) != -1:
            body = text[i + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end
        else:
            out.append(re.escape(char))
        i += 1
    regex = ("" if anchored else "(?:.*/)?") + "".join(out) + ("(?:/.*)?" if contents else "")
    return re.compile(regex), dir_only


class ExcludeSet:
    """gitignore-like patterns for `copy_exclude`, matched against POSIX paths from the root.

    A pattern with a `/` inside is anchored at the root, otherwise it matches names at any
    depth. `dir/**` matches the folder and everything in it, a trailing `/` matches folders
    only, `**/` matches any number of folders, `*` and `?` stay within one name.
    """

    def __init__(self, patterns: Sequence[str], *, always: Sequence[str] = ()) -> None:
        """Compile `patterns` (errors name `copy_exclude[<i>]`); `always` are built-in extras."""
        self._rules = [_compile_glob(p, -1) for p in always]
        self._rules += [_compile_glob(p, i) for i, p in enumerate(patterns)]

    def matches(self, rel_path: str, *, is_dir: bool) -> bool:
        """True if the file or folder at `rel_path` is excluded."""
        return any(
            regex.fullmatch(rel_path) is not None and (is_dir or not dir_only)
            for regex, dir_only in self._rules
        )


# --------------------------------------------------------------------------------------------
# Copying: APFS clone per file with fallback
# --------------------------------------------------------------------------------------------

_FALLBACK_ERRNOS = frozenset({errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})
"""`clonefile` errors that mean "cannot clone here": other volume, other file system."""


def _load_clonefile() -> Any:
    if sys.platform != "darwin":
        return None
    try:
        function = ctypes.CDLL("libc.dylib", use_errno=True).clonefile
    except (OSError, AttributeError):
        return None
    function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    return function


_CLONEFILE = _load_clonefile()


def _clonefile(src: str, dst: str) -> None:
    """Clone one file with `clonefile(2)`; raise `OSError` (errno) if that fails."""
    if _CLONEFILE is None:
        raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP), src)
    if _CLONEFILE(os.fsencode(src), os.fsencode(dst), 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), src)


@dataclass(slots=True)
class CopyStats:
    """What `copy_tree` did: files cloned, files copied for real (with their size), others."""

    cloned: int = 0
    copied: int = 0
    bytes_copied: int = 0
    symlinks: int = 0
    skipped_special: int = 0
    external_links: list[str] = field(default_factory=list)

    @property
    def method(self) -> Literal["clone", "copy"]:
        """`copy` if at least one file had to be copied for real, else `clone`."""
        return "copy" if self.copied else "clone"


def copy_tree(src: Path, dst: Path, excludes: ExcludeSet | None = None) -> CopyStats:
    """Copy a folder tree file by file as APFS clones; `dst` must not exist.

    Every regular file is cloned with `clonefile(2)` (never a whole folder, which the man page
    discourages, and never `cp -c`, which silently copies for real). On `EXDEV`/`ENOTSUP` the
    rest of the tree is copied for real with `shutil.copy2`; other errors are raised. See
    docs/spikes/S5 §5, §7.

    Symlinks whose target (fully resolved, also absolute links) lies inside `src` are copied as
    relative links that point into the copy. Links leading out of `src`, broken links and loops
    are left out and listed in `CopyStats.external_links`, so nothing in the copy can write
    through to the original or to files elsewhere. Sockets and the like are skipped.
    """
    stats = CopyStats()
    can_clone = True
    root_real = os.path.realpath(src)

    def copy_file(source: str, target: Path) -> None:
        nonlocal can_clone
        if can_clone:
            try:
                _clonefile(source, str(target))
            except OSError as exc:
                if exc.errno not in _FALLBACK_ERRNOS:
                    raise
                can_clone = False
            else:
                stats.cloned += 1
                return
        shutil.copy2(source, target)
        stats.copied += 1
        stats.bytes_copied += target.stat().st_size

    def walk(source_dir: Path, target_dir: Path, rel: str) -> None:
        target_dir.mkdir(parents=not rel)
        with os.scandir(source_dir) as scan:
            entries = sorted(scan, key=lambda e: e.name)
        for entry in entries:
            rel_path = f"{rel}/{entry.name}" if rel else entry.name
            is_dir = entry.is_dir(follow_symlinks=False)
            if excludes is not None and excludes.matches(rel_path, is_dir=is_dir):
                continue
            target = target_dir / entry.name
            if entry.is_symlink():
                text = internal_link_target(Path(entry.path), root_real)
                if text is None:
                    stats.external_links.append(rel_path)
                else:
                    os.symlink(text, target)
                    stats.symlinks += 1
            elif is_dir:
                walk(Path(entry.path), target, rel_path)
            elif entry.is_file(follow_symlinks=False):
                copy_file(entry.path, target)
            else:
                stats.skipped_special += 1
        with contextlib.suppress(OSError):
            shutil.copystat(source_dir, target_dir, follow_symlinks=False)

    walk(src, dst, "")
    return stats


# --------------------------------------------------------------------------------------------
# Run log
# --------------------------------------------------------------------------------------------


class _RunLog:
    """Warnings of the preparation: Python logger, `orchestrator.jsonl` and a list.

    The line format `{"ts", "kind", "data"}` is provisional until the orchestrator log is
    defined (W2-03).
    """

    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / "orchestrator.jsonl"
        self.warnings: list[str] = []

    def warning(self, code: str, message: str, **data: object) -> None:
        logger.warning(message)
        self.warnings.append(message)
        line = {
            "ts": time.time(),
            "kind": "warning",
            "data": {"code": code, "message": message, **data},
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
            handle.flush()


# --------------------------------------------------------------------------------------------
# Project context (E10)
# --------------------------------------------------------------------------------------------


def _role_label(agent: AgentConfig) -> str:
    return Path(agent.role).stem if agent.role else agent.base_id


def _parts(resolved: ResolvedRun, agent_id: str) -> list[str]:
    """What an agent's part of the run is, from `run.yaml` (docs/09 §4.7): its stages, rating,
    moderating, summarizing."""
    parts = []
    for stage in resolved.stages:
        if stage.agent == agent_id:
            produces = f", liefert `{stage.produces}`" if stage.produces else ""
            parts.append(f"Stufe „{stage.name}“{produces}")
    if agent_id in resolved.raters:
        parts.append("bewertet Ergebnisse")
    parts += [f"moderiert #{c.name}" for c in resolved.groupchats if c.moderator == agent_id]
    if resolved.params.get("summarizer") == agent_id:
        parts.append("fasst die Diskussion zusammen")
    return parts


def _team_items(resolved: ResolvedRun, agent: AgentConfig) -> list[str]:
    """Own part and the others with role and part (docs/09 §4.7, User 08.11.).

    Agents that share an anonymous channel with `agent` are left out: there it knows them only
    by their alias, and a list with IDs and roles would tell who is behind which one.
    """
    hidden = {
        member
        for c in resolved.groupchats
        if c.anonymous and agent.id in c.members
        for member in c.members
    }
    items = []
    mine = _parts(resolved, agent.id)
    if mine:
        items.append(f"- Dein Teil: {'; '.join(mine)}.")
    others = [a for a in resolved.agents if a.id != agent.id and a.id not in hidden]
    if others:
        items.append("- Die anderen Agenten:")
        for other in others:
            part = _parts(resolved, other.id)
            extra = f"; {'; '.join(part)}" if part else ""
            items.append(f"  - `{other.id}` (Rolle: `{_role_label(other)}`{extra})")
    if hidden - {agent.id}:
        items.append(
            "- In anonymen Kanälen kennst du die anderen nur als „Mitglied A/B/…“, sie dich auch."
        )
    return items


def _research_block(
    run_id: str,
    resolved: ResolvedRun,
    agent: AgentConfig | None,
    mode: WorkspaceMode,
    shared_name: str | None = None,
) -> str:
    """The `arr:research-agent` block; `agent=None` gives the neutral text of a shared folder."""
    items = [f"- Run: `{run_id}`"]
    if agent is not None:
        items.append(f"- Du bist `{agent.id}` (Rolle: `{_role_label(agent)}`).")
        items += _team_items(resolved, agent)
    else:
        items.append(
            f"- Du teilst dir diesen Ordner „{shared_name}“ mit anderen Agenten des Runs. "
            "Dein Name, deine Rolle und deine Kanäle stehen im Kontext zu Beginn deines Auftrags."
        )
    project = resolved.project
    if mode is WorkspaceMode.EMPTY:
        items.append("- Dieser Ordner ist **leer**. Das Projekt ist hier nicht enthalten.")
    elif mode is WorkspaceMode.SHARED:
        items.append(
            f"- Dieser Ordner ist eine **geteilte Kopie** des Projekts „{project}“. Andere Agenten "
            "schreiben hier ebenfalls. Änderungen bleiben in diesem Run, das Original wird nie "
            "verändert."
        )
    else:
        items.append(
            f"- Dieser Ordner ist eine **Kopie** des Projekts „{project}“. Änderungen bleiben in "
            "diesem Run, das Original wird nie verändert."
        )
    if agent is not None:
        purposes = {c.name: c.purpose for c in resolved.groupchats}
        if agent.channels:
            items.append("- Gruppenchats:")
            for name in agent.channels:
                purpose = purposes.get(name)
                items.append(f"  - `{name}`" + (f": {purpose}" if purpose else ""))
        else:
            items.append("- Du bist in keinem Gruppenchat.")
    items.append(
        "- Ergebnisse gibst du mit `submit_result` ab. Dateien in diesem Ordner sind "
        "Arbeitsmaterial; erst was du abgibst, zählt als Ergebnis."
    )
    template = Template(read_template("run", "research-agent-block.md"))
    return template.substitute(items="\n".join(items)).rstrip("\n")


def _processed_context(source: str, block: str) -> str:
    """Swap the `arr:user-agent` block of a project's `CLAUDE.md` for the research block.

    Without a user-agent block, an already present research block is replaced, otherwise the
    new block is appended.
    """
    if block_span(source, USER_AGENT_BLOCK) is None and (
        block_span(source, RESEARCH_AGENT_BLOCK) is not None
    ):
        return replace_block(source, RESEARCH_AGENT_BLOCK, block)
    return replace_block(source, USER_AGENT_BLOCK, block)


def _write_text(path: Path, text: str) -> None:
    """Write a file, replacing a symlink or file there instead of writing through a link."""
    if path.is_symlink() or path.exists():
        path.unlink()
    path.write_text(text, encoding="utf-8")


@dataclass(frozen=True, slots=True)
class _ContextSource:
    """The project context a run starts from: text and whether it came from `CLAUDE.md`."""

    text: str = ""
    from_claude_md: bool = False


def _context_source(project: Path) -> _ContextSource:
    """Read the project's context: `CLAUDE.md`, else `AGENTS.md`, else nothing.

    The single place that decides the source, for the working copies and `context.md` alike. It
    reads from the project, not from `base/`, so `copy_exclude` cannot drop the context. A link
    leading out of the project is not followed (same rule as for copies). Raises `ArrError` for
    an unreadable file or a mark without its partner.
    """
    root_real = os.path.realpath(project)
    for name in ("CLAUDE.md", "AGENTS.md"):
        file = project / name
        if file.is_symlink() and internal_link_target(file, root_real) is None:
            continue
        if not file.is_file():
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise ArrError(
                f"Die Datei {file} ist nicht lesbar (keine UTF-8-Textdatei oder keine Rechte)."
            ) from None
        for kind in (USER_AGENT_BLOCK, RESEARCH_AGENT_BLOCK):
            try:
                block_span(text, kind)
            except ArrError as exc:
                raise ArrError(f"{file}: {exc.message}") from None
        return _ContextSource(text, from_claude_md=name == "CLAUDE.md")
    return _ContextSource()


def _context_text(source: _ContextSource, mode: WorkspaceMode, block: str) -> str:
    """Processed context: the project's text with the research block, only the block if empty."""
    return _processed_context("" if mode is WorkspaceMode.EMPTY else source.text, block)


def _write_context_files(
    work_dir: Path, source: _ContextSource, mode: WorkspaceMode, block: str
) -> list[str]:
    """Write `AGENTS.md` and, if the project's context is a `CLAUDE.md`, also `CLAUDE.md`.

    Both get the same processed text. Returns the file names written.
    """
    text = _context_text(source, mode, block)
    written = []
    if source.from_claude_md and mode is not WorkspaceMode.EMPTY:
        _write_text(work_dir / "CLAUDE.md", text)
        written.append("CLAUDE.md")
    _write_text(work_dir / "AGENTS.md", text)
    written.append("AGENTS.md")
    return written


# --------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Plan:
    task_text: str
    context: _ContextSource
    group_skill: SkillInfo | None
    roles: dict[str, Path | None]
    agent_skills: dict[str, list[SkillInfo]]
    shared_names: list[str]


def _resolve_role(role: str, folders: Sequence[Path]) -> Path | None:
    """Find the role file in the first folder that has it; links must stay inside that folder."""
    parts = Path(role).parts
    if Path(role).is_absolute() or ".." in parts:
        return None
    for folder in folders:
        candidate = (folder / role).resolve()
        if candidate.is_relative_to(folder.resolve()) and candidate.is_file():
            return candidate
    return None


def _check_profiles(
    resolved: ResolvedRun, profiles: ProfilesFile, issues: list[ConfigIssue]
) -> None:
    by_name = {p.name: p for p in profiles.profiles}
    fits = {
        Provider.CLAUDE: {AuthKind.CLAUDE_OAUTH, AuthKind.CLAUDE_API_KEY},
        Provider.CODEX: {AuthKind.CODEX_CHATGPT, AuthKind.OPENAI_API_KEY},
    }
    for index, agent in enumerate(resolved.agents):
        profile = by_name.get(agent.auth)
        if profile is None:
            known = ", ".join(sorted(by_name)) or "keine"
            issues.append(
                ConfigIssue(
                    f"agents[{index}].auth",
                    f"Das Auth-Profil „{agent.auth}“ gibt es nicht (vorhanden: {known}). "
                    "Lege es unter Auth-Profile an oder wähle ein vorhandenes.",
                )
            )
        elif profile.kind not in fits[agent.provider]:
            issues.append(
                ConfigIssue(
                    f"agents[{index}].auth",
                    f"Das Profil „{agent.auth}“ (Art {profile.kind}) passt nicht zum Anbieter "
                    f"„{agent.provider}“ von „{agent.id}“. Wähle ein Profil für diesen Anbieter.",
                )
            )


def _read_task_file(
    task_file: str, task_base: Path | None, allow_external: bool
) -> tuple[str, ConfigIssue | None]:
    """Read `task_file`; it must resolve to a file under `task_base` unless `allow_external`.

    Absolute paths, `~`, `..` and links leading out of `task_base` are refused, so a run cannot
    send arbitrary files of the user to the model providers (E19).
    """
    raw = Path(task_file).expanduser()
    if not raw.is_absolute() and task_base is None:
        return "", ConfigIssue(
            "task_file",
            f"Die Aufgabendatei „{task_file}“ ist relativ, aber es ist kein Ordner bekannt, auf "
            "den sie sich bezieht. Gib die Aufgabe direkt unter „task“ an.",
        )
    path = (raw if raw.is_absolute() else Path(task_base or ".") / raw).resolve()
    inside = task_base is not None and path.is_relative_to(Path(task_base).resolve())
    if not (inside or allow_external):
        return "", ConfigIssue(
            "task_file",
            f"Die Aufgabendatei „{task_file}“ liegt außerhalb des Ordners der Run-Konfiguration. "
            "Lege sie in diesen Ordner oder gib die Aufgabe direkt unter „task“ an.",
        )
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError):
        return "", ConfigIssue(
            "task_file",
            f"Die Aufgabendatei „{task_file}“ ist nicht lesbar. Prüfe den Pfad oder gib die "
            "Aufgabe direkt unter „task“ an.",
        )


def _make_plan(
    root: Path,
    project: Path,
    resolved: ResolvedRun,
    catalog: SkillCatalog,
    task_base: Path | None,
    profiles: ProfilesFile | None,
    allow_external_task_file: bool,
) -> _Plan:
    """Check everything the preparer reads and report all problems at once (docs/02 §3.3)."""
    issues: list[ConfigIssue] = []

    task_text = ""
    if resolved.task is not None:
        task_text = resolved.task
    elif resolved.task_file is not None:
        task_text, issue = _read_task_file(resolved.task_file, task_base, allow_external_task_file)
        if issue is not None:
            issues.append(issue)

    context = _ContextSource()
    if any(a.workspace.mode is not WorkspaceMode.EMPTY for a in resolved.agents):
        try:
            context = _context_source(project)
        except ArrError as exc:
            issues.append(ConfigIssue("project", exc.message))

    group_skill: SkillInfo | None = None
    skill_failed = False
    if resolved.skill is not None:
        issue = catalog.lookup_issue(resolved.skill, "skill")
        if issue is not None:
            issues.append(issue)
            skill_failed = True  # its roles cannot be found; the cause is reported once
        else:
            group_skill = catalog.skills[resolved.skill]

    shared_names = sorted(
        {
            a.workspace.name
            for a in resolved.agents
            if a.workspace.mode is WorkspaceMode.SHARED and a.workspace.name
        }
    )
    role_folders = [f for f in (group_skill.path if group_skill else None, project) if f]
    roles: dict[str, Path | None] = {}
    agent_skills: dict[str, list[SkillInfo]] = {}
    for index, agent in enumerate(resolved.agents):
        if agent.role:
            found = _resolve_role(agent.role, role_folders)
            if found is None and not skill_failed:
                where = " und ".join(f"„{f.name}“" for f in role_folders)
                issues.append(
                    ConfigIssue(
                        f"agents[{index}].role",
                        f"Die Rollendatei „{agent.role}“ gibt es nicht (gesucht relativ zum "
                        f"Skill bzw. Projekt: {where}). Der Pfad muss relativ sein, ohne „..“.",
                    )
                )
            roles[agent.id] = found
        else:
            roles[agent.id] = None
        found_skills, skill_issues = check_research_skills(
            catalog, agent.skills, f"agents[{index}].skills"
        )
        agent_skills[agent.id] = found_skills
        issues.extend(skill_issues)
        for j, read_dir in enumerate(agent.read_dirs):
            if (
                read_dir.startswith("run:shared/")
                and read_dir.removeprefix("run:shared/") not in shared_names
            ):
                issues.append(
                    ConfigIssue(
                        f"agents[{index}].read_dirs[{j}]",
                        f"„{read_dir}“ verweist auf eine geteilte Kopie, die kein Agent dieses "
                        "Runs anlegt. Prüfe den Namen unter „workspace“.",
                    )
                )
    if profiles is not None:
        _check_profiles(resolved, profiles, issues)
    if issues:
        raise ConfigError(issues, source=RUN_SOURCE)
    return _Plan(task_text, context, group_skill, roles, agent_skills, shared_names)


# --------------------------------------------------------------------------------------------
# prepare_run
# --------------------------------------------------------------------------------------------


def _toolchain(
    root: Path, settings: WorkfolderSettings, log: _RunLog
) -> tuple[list[str], str | None]:
    """Folders for the agents' `PATH` and the base Python, from `arr.yaml toolchain`."""
    toolchain = settings.toolchain
    prepend: list[str] = []
    base_python: str | None = None
    if toolchain.python:
        python = Path(toolchain.python).expanduser()
        if not python.is_absolute():
            python = root / python
        if python.is_file():  # an interpreter such as <venv>/bin/python
            venv, bin_dir = python.parent.parent, python.parent
        else:
            venv, bin_dir = python, python / "bin"
        if bin_dir.is_dir():
            base_python = str(venv)
            prepend.append(str(bin_dir))
        else:
            log.warning(
                "toolchain_missing",
                f"Die Python-Umgebung „{toolchain.python}“ wurde nicht gefunden ({bin_dir}). "
                "Die Agenten nutzen das System-Python. Prüfe „toolchain.python“ in arr.yaml.",
                path=str(bin_dir),
            )
    for entry in toolchain.extra_path:
        folder = Path(entry).expanduser()
        if not folder.is_absolute():
            folder = root / folder
        if not folder.is_dir():
            log.warning(
                "toolchain_missing",
                f"Der Ordner „{entry}“ aus „toolchain.extra_path“ existiert nicht.",
                path=str(folder),
            )
        prepend.append(str(folder))
    return prepend, base_python


def _below(path: Path, home: Path) -> bool:
    key, home_key = str(path), str(home)
    if sys.platform == "darwin":  # case-insensitive file system
        key, home_key = key.casefold(), home_key.casefold()
    return key == home_key or key.startswith(home_key.rstrip("/") + "/")


def _shown(path: Path, home: Path) -> str:
    return "~" + str(path)[len(str(home)) :] if _below(path, home) else str(path)


def _check_codex_toolchain(
    root: Path, resolved: ResolvedRun, settings: WorkfolderSettings, *, home: Path | None = None
) -> None:
    """Refuse a toolchain below `~` for runs with a Codex agent (W3-00, variant 2).

    The Codex profile denies `~` as a whole, so neither Python nor a program below `~`
    starts there. Checked (symlinks resolved): `toolchain.python` (venv or interpreter), the
    Python installation it is built on and its interpreter, every `toolchain.extra_path`
    entry and, with `run_venv: overlay`, the folder of the run venvs. Runs with Claude
    agents only are not affected: Claude's sandbox keeps the toolchain readable.
    """
    codex = [a.id for a in resolved.agents if a.provider is Provider.CODEX]
    if not codex:
        return
    home = Path(os.path.realpath(home or Path.home()))
    toolchain = settings.toolchain
    names = ", ".join(f"„{a}“" for a in codex)
    way = (
        f"Lege die Basis-venv außerhalb von ~ unter {base_venv_dir('<name>').parent}/ an, mit "
        "einem Python, das nicht von uv verwaltet wird (z. B. python.org-Python unter "
        "/Library/Frameworks), und trage sie als „toolchain.python“ in arr.yaml ein."
    )

    def absolute(raw: str) -> Path:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else root / path

    issues: list[ConfigIssue] = []

    def check(where: str, label: str, path: Path, hint: str) -> None:
        real = Path(os.path.realpath(path))
        if _below(real, home):
            shown = _shown(path, home)
            if real != path.absolute():
                shown += f" (= {_shown(real, home)})"
            issues.append(
                ConfigIssue(
                    where,
                    f"{label} {shown} liegt unter deinem Benutzerordner (~). Die Codex-Sandbox "
                    f"sperrt ~ ganz, deshalb ist es für {names} dort nicht nutzbar. {hint}",
                )
            )

    if toolchain.python:
        python = absolute(toolchain.python)
        venv = python.parent.parent if python.is_file() else python
        check("toolchain.python", "Die Python-Umgebung", python, way)
        real = Path(os.path.realpath(venv))
        if real.parent == Path(os.path.realpath(venv_root())) / "venvs" and not glob_safe_name(
            real.name
        ):
            issues.append(
                ConfigIssue(
                    "toolchain.python",
                    f"Der Name der Basis-venv „{real.name}“ enthält Umlaute, Akzente oder "
                    "andere Nicht-ASCII-Zeichen bzw. eines der Zeichen [ ] \\ ! ^ { } * ? ,. "
                    f"Damit kann die Codex-Sandbox für {names} andere venvs nicht sicher "
                    "sperren. Benenne die venv um, z. B. nur mit a–z, 0–9, - und _.",
                )
            )
        if not issues:
            base = venv_base_prefix(venv)
            if base is not None:
                check("toolchain.python", "Das Python der Basis-venv", base, way)
            for name in ("python3", "python"):
                interpreter = venv / "bin" / name
                if not issues and interpreter.exists():
                    check("toolchain.python", "Das Python der Basis-venv", interpreter, way)
                    break
    for index, entry in enumerate(toolchain.extra_path):
        check(
            f"toolchain.extra_path[{index}]",
            "Der Ordner",
            absolute(entry),
            "Nimm den Ordner aus „toolchain.extra_path“ oder installiere das Programm "
            "außerhalb von ~ (z. B. mit Homebrew).",
        )
    if toolchain.run_venv is RunVenvMode.OVERLAY:
        check(
            "toolchain.run_venv",
            "Der Ordner der venvs",
            venv_root(),
            "Setze ARR_VENV_ROOT auf einen Ordner außerhalb von ~ oder entferne die Variable.",
        )
    if issues:
        raise ConfigError(issues, source="arr.yaml")


def _sdk_versions() -> dict[str, str]:
    versions = {}
    for package in _SDK_PACKAGES:
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            versions[package] = importlib.metadata.version(package)
    return versions


RESERVED_STATUSES = frozenset({RunStatus.AWAITING_APPROVAL, RunStatus.QUEUED})
"""Statuses of a reserved run folder that `prepare_run(run_id=…)` takes over (docs/02 §7)."""


@dataclass(frozen=True, slots=True)
class _Reserved:
    """A run folder that existed before `prepare_run`: its `meta.json` and its entries."""

    meta: RunMeta
    meta_bytes: bytes
    entries: frozenset[str]


def _reserve_run_dir(
    runs_dir: Path, make_id: Callable[[], str], fixed_id: str | None
) -> tuple[str, Path, _Reserved | None]:
    runs_dir.mkdir(parents=True, exist_ok=True)
    if fixed_id is not None:
        if not _RUN_ID_RE.fullmatch(fixed_id):
            raise ArrError(
                f"Die Run-ID „{fixed_id}“ hat nicht die Form JJJJMMTT-HHMM_projekt_skill_xxxx."
            )
        folder = runs_dir / fixed_id
        try:
            folder.mkdir()
        except FileExistsError:
            return fixed_id, folder, _take_over(folder)
        return fixed_id, folder, None
    for _ in range(50):
        run_id = make_id()
        folder = runs_dir / run_id
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        return run_id, folder, None
    raise ArrError("Es konnte keine freie Run-ID gefunden werden. Versuche es noch einmal.")


def _take_over(folder: Path) -> _Reserved:
    """Accept an existing folder only if it is a reserved run (`awaiting_approval`, `queued`)."""
    meta_path = folder / "meta.json"
    try:
        raw = meta_path.read_bytes() if folder.is_dir() and not folder.is_symlink() else None
        meta = RunMeta.model_validate_json(raw) if raw is not None else None
    except (OSError, ValueError):
        meta = raw = None
    if meta is None or raw is None or meta.status not in RESERVED_STATUSES:
        raise ArrError(
            f"Der Run-Ordner „{folder.name}“ existiert schon ({folder}). "
            "arr überschreibt keinen Run."
        )
    return _Reserved(meta, raw, frozenset(p.name for p in folder.iterdir()))


def _undo_take_over(run_dir: Path, reserved: _Reserved) -> None:
    """Remove what `prepare_run` added to a reserved folder and restore its `meta.json`."""
    for path in run_dir.iterdir():
        if path.name in reserved.entries:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    (run_dir / "meta.json").write_bytes(reserved.meta_bytes)


def _rel(run_dir: Path, path: Path) -> str:
    return path.relative_to(run_dir).as_posix()


def prepare_run(
    workfolder: str | Path,
    run_input: RunInput,
    resolved: ResolvedRun,
    settings: WorkfolderSettings | None = None,
    *,
    starter: Starter = Starter.CLI,
    task_base: Path | None = None,
    profiles: ProfilesFile | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
    allow_external_task_file: bool = False,
) -> PreparedRun:
    """Create the run folder for a resolved run and return what was prepared.

    - `run_input` is stored as `run.input.yaml` (as given), `resolved` as `run.yaml`; the task
      goes to `task.md` (from `task`, or from `task_file` relative to `task_base`). `task_file`
      must lie under `task_base`; only `allow_external_task_file=True` (for a file the user
      names on the command line) lifts that.
    - `base/` is a clone of the project without `copy_exclude` (and without project skills);
      working copies are clones of `base/`. `shared/<name>/` for `shared`, an empty folder for
      `empty`. If cloning is impossible the files are copied for real and a warning goes to
      `orchestrator.jsonl` and `PreparedRun.warnings`. Symlinks leading out of the project
      (or skill) are left out, with a warning `external_symlink`.
    - Skills: snapshot in `skills/`; Claude agents get a generated plugin in `agents/<id>/plugin/`,
      Codex agents `.agents/skills/` in their working folder. Group skills and `arr` are never
      installed for research agents (E11).
    - Each working copy gets the processed project context as `CLAUDE.md` (if the project has
      one) and `AGENTS.md`, and each agent a `context.md` (E10, see `AgentFile`).
    - `meta.json` gets the status `preparing` and the folder of the run venv
      (`RunPaths.venv_dir`, outside `~`); creating it and starting the agents is up to the
      RunManager.
    - A run with a Codex agent whose toolchain lies below `~` is refused with `ConfigError`
      before anything is created (W3-00, `_check_codex_toolchain`).

    All configuration problems (unknown project, role or skill, bad profile …) are collected and
    raised as one `ConfigError` before anything is created. `run_id` reserves a fixed ID
    (docs/02 §7). If something fails later, the new folder is removed.

    Reserved folders (W2-03): if the folder of `run_id` already exists with a `meta.json` in
    status `awaiting_approval` or `queued`, the run is prepared in that folder; `created` and
    `starter` are kept from the reserved `meta.json`. If preparing fails, only what
    `prepare_run` added is removed and the reserved `meta.json` is restored; the folder and
    its earlier entries stay. Any other existing folder is refused.
    """
    root = Path(workfolder).expanduser().absolute()
    settings = settings or load_workfolder_settings(root)
    try:
        project = project_dir(root, resolved.project)
    except ArrError as exc:
        raise ConfigError.single("project", exc.message, source=RUN_SOURCE) from None
    project_settings = load_project_settings(project)
    excludes = ExcludeSet(project_settings.copy_exclude, always=ALWAYS_EXCLUDED)
    catalog = discover_skills(root / "skills")
    plan = _make_plan(
        root, project, resolved, catalog, task_base, profiles, allow_external_task_file
    )
    _check_codex_toolchain(root, resolved, settings)

    moment = now or datetime.now()
    run_id, run_dir, reserved = _reserve_run_dir(
        root / "runs", lambda: new_run_id(resolved.project, resolved.skill, now=moment), run_id
    )
    if reserved is not None:
        starter = reserved.meta.starter
        moment = datetime.fromtimestamp(reserved.meta.created)
    try:
        return _build_run(
            root,
            run_id,
            run_dir,
            run_input,
            resolved,
            settings,
            plan,
            excludes,
            project,
            starter,
            moment,
        )
    except BaseException:
        if reserved is None:
            shutil.rmtree(run_dir, ignore_errors=True)
        else:
            _undo_take_over(run_dir, reserved)
        raise


def _build_run(
    root: Path,
    run_id: str,
    run_dir: Path,
    run_input: RunInput,
    resolved: ResolvedRun,
    settings: WorkfolderSettings,
    plan: _Plan,
    excludes: ExcludeSet,
    project: Path,
    starter: Starter,
    moment: datetime,
) -> PreparedRun:
    log = _RunLog(run_dir)
    meta = RunMeta(
        run_id=run_id,
        starter=starter,
        project=resolved.project,
        skill=resolved.skill,
        name=resolved.name,
        created=moment.timestamp(),
        arr_version=__version__,
        sdk_versions=_sdk_versions(),
        run_venv=(
            str(run_venv_dir(run_id))
            if settings.toolchain.run_venv is RunVenvMode.OVERLAY
            else None
        ),
    )
    (run_dir / "meta.json").write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (run_dir / "run.input.yaml").write_text(
        dump_yaml_model(run_input, exclude_unset=True), encoding="utf-8"
    )
    stored = resolved.model_copy(update={"task": None, "task_file": "task.md"})
    (run_dir / "run.yaml").write_text(dump_yaml_model(stored), encoding="utf-8")
    (run_dir / "task.md").write_text(plan.task_text, encoding="utf-8")
    for sub in ("output/results", "groupchats/attachments"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)

    # Project snapshot and skills
    base_dir = run_dir / "base"
    base_stats = copy_tree(project, base_dir, excludes)
    _warn_if_copied(log, "base/", base_stats)
    _warn_external_links(log, "im Projekt", "aus dem Projekt", base_stats.external_links)

    used: dict[str, SkillInfo] = {}
    if plan.group_skill is not None:
        used[plan.group_skill.name] = plan.group_skill
    for skills in plan.agent_skills.values():
        used.update({s.name: s for s in skills})
    snapshot_dir = run_dir / "skills"
    skipped_links = snapshot_skills(list(used.values()), snapshot_dir)
    _warn_external_links(log, "im Skill", "aus dem Skill", skipped_links)

    prepend, base_python = _toolchain(root, settings, log)

    # Agents
    shared_stats: dict[str, CopyStats] = {}
    prepared: dict[str, AgentFile] = {}
    for agent in resolved.agents:
        agent_dir = run_dir / "agents" / agent.id
        agent_dir.mkdir(parents=True)
        (agent_dir / "blobs").mkdir()
        role_source = plan.roles[agent.id]
        if role_source is not None:
            shutil.copyfile(role_source, agent_dir / "role.md")
        else:
            (agent_dir / "role.md").write_text(DEFAULT_ROLE, encoding="utf-8")

        mode = agent.workspace.mode
        generated: list[str] = []
        copy_method: Literal["clone", "copy"] | None = None
        if mode is WorkspaceMode.SHARED:
            name = str(agent.workspace.name)
            work_dir = run_dir / "shared" / name
            if name not in shared_stats:
                shared_stats[name] = copy_tree(base_dir, work_dir)
                _warn_if_copied(log, f"shared/{name}/", shared_stats[name])
            copy_method = shared_stats[name].method
        else:
            work_dir = agent_dir / "work"
            if mode is WorkspaceMode.COPY:
                stats = copy_tree(base_dir, work_dir)
                _warn_if_copied(log, f"agents/{agent.id}/work/", stats)
                copy_method = stats.method
            else:
                work_dir.mkdir()
            block = _research_block(run_id, resolved, agent, mode)
            generated = _write_context_files(work_dir, plan.context, mode, block)

        own_block = _research_block(run_id, resolved, agent, mode, agent.workspace.name)
        (agent_dir / "context.md").write_text(
            _context_text(plan.context, mode, own_block), encoding="utf-8"
        )

        skills = plan.agent_skills[agent.id]
        plugin_dir: str | None = None
        skill_ids: list[str] = []
        if skills and agent.provider is Provider.CLAUDE:
            plugin = agent_dir / "plugin"
            skill_ids = write_claude_plugin(plugin, snapshot_dir, [s.name for s in skills])
            plugin_dir = _rel(run_dir, plugin)
        elif skills and mode is not WorkspaceMode.SHARED:
            install_codex_skills(work_dir, snapshot_dir, [s.name for s in skills])
            skill_ids = [s.name for s in skills]
            generated.append(".agents/skills")
        elif skills:
            skill_ids = [s.name for s in skills]

        prepared[agent.id] = AgentFile(
            config=agent,
            work_dir=_rel(run_dir, work_dir),
            role_file=_rel(run_dir, agent_dir / "role.md"),
            context_file=_rel(run_dir, agent_dir / "context.md"),
            plugin_dir=plugin_dir,
            skill_ids=skill_ids,
            generated_paths=generated,
            path_prepend=prepend,
            base_python=base_python,
            run_venv=settings.toolchain.run_venv,
            copy_method=copy_method,
        )

    _finish_shared(run_dir, snapshot_dir, resolved, run_id, plan, prepared)

    for agent_id, file in prepared.items():
        (run_dir / "agents" / agent_id / "agent.json").write_text(
            file.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    agents = {i: PreparedAgent(file=f, run_dir=run_dir) for i, f in prepared.items()}
    return PreparedRun(
        run_id=run_id,
        run_dir=run_dir,
        meta=meta,
        resolved=stored.model_copy(update={"task": plan.task_text}),
        agents=agents,
        warnings=log.warnings,
    )


def _finish_shared(
    run_dir: Path,
    snapshot_dir: Path,
    resolved: ResolvedRun,
    run_id: str,
    plan: _Plan,
    prepared: dict[str, AgentFile],
) -> None:
    """Write context and Codex skills once per shared folder (they cannot differ per agent)."""
    for name in plan.shared_names:
        members = [a for a in resolved.agents if a.workspace.name == name]
        folder = run_dir / "shared" / name
        block = _research_block(run_id, resolved, None, WorkspaceMode.SHARED, name)
        generated = _write_context_files(folder, plan.context, WorkspaceMode.SHARED, block)
        codex_skills: dict[str, SkillInfo] = {}
        for member in members:
            if member.provider is Provider.CODEX:
                codex_skills.update({s.name: s for s in plan.agent_skills[member.id]})
        if codex_skills:
            install_codex_skills(folder, snapshot_dir, list(codex_skills))
            generated.append(".agents/skills")
        for member in members:
            prepared[member.id] = prepared[member.id].model_copy(
                update={"generated_paths": generated}
            )


def _warn_external_links(log: _RunLog, where: str, out_of: str, links: Sequence[str]) -> None:
    """One warning per left-out link (path only, never the target), at most a few."""
    for rel in links[:_MAX_LINK_WARNINGS]:
        log.warning(
            "external_symlink",
            f"Der Symlink „{rel}“ {where} zeigt {out_of} hinaus oder ins Leere und wurde nicht "
            "übernommen. Lege die Datei selbst dort ab, wenn die Agenten sie brauchen.",
            path=rel,
        )
    if len(links) > _MAX_LINK_WARNINGS:
        rest = len(links) - _MAX_LINK_WARNINGS
        log.warning(
            "external_symlink",
            f"Dazu {rest} weitere Symlinks {where}, die nicht übernommen wurden.",
            count=rest,
        )


def _warn_if_copied(log: _RunLog, label: str, stats: CopyStats) -> None:
    if not stats.copied:
        return
    megabytes = stats.bytes_copied / (1024 * 1024)
    log.warning(
        "clone_fallback",
        f"Kopie statt Klon für {label}: {stats.copied} Dateien ({megabytes:.1f} MB) wurden "
        "echt kopiert, weil Quelle und Run nicht auf demselben APFS-Volume liegen oder das "
        "Dateisystem keine Klone kann. Das belegt den Platz voll.",
        target=label,
        files=stats.copied,
        bytes=stats.bytes_copied,
    )
