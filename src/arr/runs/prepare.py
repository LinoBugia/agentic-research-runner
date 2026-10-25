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
from dataclasses import dataclass
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
    snapshot_skills,
    write_claude_plugin,
)
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
    "RunMeta",
    "copy_tree",
    "load_agent_file",
    "load_run_meta",
    "new_run_id",
    "prepare_run",
]

logger = logging.getLogger(__name__)

RUN_SOURCE = "run.yaml"
DEFAULT_ROLE = (
    "Du bist ein Research-Agent in diesem Run. Für dich ist keine Rolle festgelegt: "
    "Bearbeite die Aufgabe selbstständig und gib deine Ergebnisse mit `submit_result` ab.\n"
)
ALWAYS_EXCLUDED = (".claude/skills", ".agents/skills")
"""Not copied into snapshots and working copies: skills reach research agents only by explicit
listing, through the plugin (Claude) or `.agents/skills` (Codex), never from the project."""

_SDK_PACKAGES = ("claude-agent-sdk", "openai-codex")
_RUN_ID_RE = re.compile(r"^\d{8}-\d{4}_[A-Za-z0-9-]+_[A-Za-z0-9-]+_[0-9a-f]{4}$")


# --------------------------------------------------------------------------------------------
# Files written into the run folder
# --------------------------------------------------------------------------------------------


class RunMeta(Record):
    """Content of `meta.json` (docs/02 §4). The RunManager (W2-03) updates status and times."""

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


class AgentFile(Record):
    """Content of `agents/<id>/agent.json`: the resolved `AgentConfig` plus runtime data.

    All paths are relative to the run folder (POSIX style), so the folder can be moved or
    archived. `path_prepend` holds absolute folders for the `PATH` of the agent's processes
    (base venv `bin/` first, then `toolchain.extra_path`). `generated_paths` lists what the
    preparer wrote into `work_dir` (relative to it), so `changes.diff` can leave it out.
    There is no secret in here, only the auth profile's name (docs/01 §8).
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
    """Result of `prepare_run`."""

    run_id: str
    run_dir: Path
    meta: RunMeta
    resolved: ResolvedRun
    agents: dict[str, PreparedAgent]
    warnings: list[str]


def load_run_meta(run_dir: str | Path) -> RunMeta:
    """Read `<run_dir>/meta.json`."""
    return RunMeta.model_validate_json((Path(run_dir) / "meta.json").read_text("utf-8"))


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

    @property
    def method(self) -> Literal["clone", "copy"]:
        """`copy` if at least one file had to be copied for real, else `clone`."""
        return "copy" if self.copied else "clone"


def copy_tree(src: Path, dst: Path, excludes: ExcludeSet | None = None) -> CopyStats:
    """Copy a folder tree file by file as APFS clones; `dst` must not exist.

    Every regular file is cloned with `clonefile(2)` (never a whole folder, which the man page
    discourages, and never `cp -c`, which silently copies for real). On `EXDEV`/`ENOTSUP` the
    rest of the tree is copied for real with `shutil.copy2`; other errors are raised. Symlinks
    stay symlinks, sockets and the like are skipped. See docs/spikes/S5 §5, §7.
    """
    stats = CopyStats()
    can_clone = True

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
                os.symlink(os.readlink(entry.path), target)
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


def _read_text_if_file(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _write_context_files(
    work_dir: Path, base_dir: Path, mode: WorkspaceMode, block: str
) -> list[str]:
    """Write `CLAUDE.md` (if the project has one) and `AGENTS.md` with the processed context.

    Returns the file names written. For `empty` there is no project context: only the block.
    """
    source = None
    claude_text = None
    if mode is not WorkspaceMode.EMPTY:
        claude_text = _read_text_if_file(base_dir / "CLAUDE.md")
        source = (
            claude_text if claude_text is not None else _read_text_if_file(base_dir / "AGENTS.md")
        )
    text = _processed_context(source or "", block)
    written = []
    if claude_text is not None:
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
    group_skill: SkillInfo | None
    roles: dict[str, Path | None]
    agent_skills: dict[str, list[SkillInfo]]
    shared_names: list[str]


def _resolve_role(role: str, folders: Sequence[Path]) -> Path | None:
    parts = Path(role).parts
    if Path(role).is_absolute() or ".." in parts:
        return None
    for folder in folders:
        candidate = folder / role
        if candidate.is_file():
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


def _make_plan(
    root: Path,
    project: Path,
    resolved: ResolvedRun,
    catalog: SkillCatalog,
    task_base: Path | None,
    profiles: ProfilesFile | None,
) -> _Plan:
    """Check everything the preparer reads and report all problems at once (docs/02 §3.3)."""
    issues: list[ConfigIssue] = []

    task_text = ""
    if resolved.task is not None:
        task_text = resolved.task
    elif resolved.task_file is not None:
        task_path = Path(resolved.task_file).expanduser()
        if not task_path.is_absolute():
            task_path = (task_base / task_path) if task_base else task_path
        try:
            if not task_path.is_absolute() and task_base is None:
                raise FileNotFoundError
            task_text = task_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            where = f" ({task_path})" if task_base is not None else ""
            issues.append(
                ConfigIssue(
                    "task_file",
                    f"Die Aufgabendatei „{resolved.task_file}“ ist nicht lesbar{where}. "
                    "Prüfe den Pfad oder gib die Aufgabe direkt unter „task“ an.",
                )
            )

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
    return _Plan(task_text, group_skill, roles, agent_skills, shared_names)


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
        base_python = str(python)
        bin_dir = python.parent if python.is_file() else python / "bin"
        if not bin_dir.is_dir():
            log.warning(
                "toolchain_missing",
                f"Die Python-Umgebung „{toolchain.python}“ wurde nicht gefunden ({bin_dir}). "
                "Die Agenten nutzen das System-Python. Prüfe „toolchain.python“ in arr.yaml.",
                path=str(bin_dir),
            )
        prepend.append(str(bin_dir))
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


def _sdk_versions() -> dict[str, str]:
    versions = {}
    for package in _SDK_PACKAGES:
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            versions[package] = importlib.metadata.version(package)
    return versions


def _reserve_run_dir(
    runs_dir: Path, make_id: Callable[[], str], fixed_id: str | None
) -> tuple[str, Path]:
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
            raise ArrError(
                f"Der Run-Ordner „{fixed_id}“ existiert schon ({folder}). "
                "arr überschreibt keinen Run."
            ) from None
        return fixed_id, folder
    for _ in range(50):
        run_id = make_id()
        folder = runs_dir / run_id
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        return run_id, folder
    raise ArrError("Es konnte keine freie Run-ID gefunden werden. Versuche es noch einmal.")


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
) -> PreparedRun:
    """Create the run folder for a resolved run and return what was prepared.

    - `run_input` is stored as `run.input.yaml` (as given), `resolved` as `run.yaml`; the task
      goes to `task.md` (from `task`, or from `task_file` relative to `task_base`).
    - `base/` is a clone of the project without `copy_exclude` (and without project skills);
      working copies are clones of `base/`. `shared/<name>/` for `shared`, an empty folder for
      `empty`. If cloning is impossible the files are copied for real and a warning goes to
      `orchestrator.jsonl` and `PreparedRun.warnings`.
    - Skills: snapshot in `skills/`; Claude agents get a generated plugin in `agents/<id>/plugin/`,
      Codex agents `.agents/skills/` in their working folder. Group skills and `arr` are never
      installed for research agents (E11).
    - Each working copy gets the processed project context as `CLAUDE.md` (if the project has
      one) and `AGENTS.md`, and each agent a `context.md` with the same text (E10).
    - `meta.json` gets the status `preparing`. Starting the agents is up to the RunManager.

    All configuration problems (unknown project, role or skill, bad profile …) are collected and
    raised as one `ConfigError` before anything is created. `run_id` reserves a fixed ID
    (docs/02 §7); it must not exist yet. If something fails later, the new folder is removed.
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
    plan = _make_plan(root, project, resolved, catalog, task_base, profiles)

    moment = now or datetime.now()
    run_id, run_dir = _reserve_run_dir(
        root / "runs", lambda: new_run_id(resolved.project, resolved.skill, now=moment), run_id
    )
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
        shutil.rmtree(run_dir, ignore_errors=True)
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
    )
    (run_dir / "meta.json").write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (run_dir / "run.input.yaml").write_text(
        dump_yaml_model(run_input, exclude_unset=True), encoding="utf-8"
    )
    stored = resolved.model_copy(update={"task": None, "task_file": "task.md"})
    (run_dir / "run.yaml").write_text(dump_yaml_model(stored), encoding="utf-8")
    (run_dir / "task.md").write_text(plan.task_text, encoding="utf-8")
    for sub in ("output/results", "groupchats/attachments"):
        (run_dir / sub).mkdir(parents=True)

    # Project snapshot and skills
    base_dir = run_dir / "base"
    base_stats = copy_tree(project, base_dir, excludes)
    _warn_if_copied(log, "base/", base_stats)

    used: dict[str, SkillInfo] = {}
    if plan.group_skill is not None:
        used[plan.group_skill.name] = plan.group_skill
    for skills in plan.agent_skills.values():
        used.update({s.name: s for s in skills})
    snapshot_dir = run_dir / "skills"
    snapshot_skills(list(used.values()), snapshot_dir)

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
            generated = _write_context_files(work_dir, base_dir, mode, block)

        own_block = _research_block(run_id, resolved, agent, mode, agent.workspace.name)
        source = (
            None
            if mode is WorkspaceMode.EMPTY
            else (
                _read_text_if_file(base_dir / "CLAUDE.md")
                or _read_text_if_file(base_dir / "AGENTS.md")
            )
        )
        (agent_dir / "context.md").write_text(
            _processed_context(source or "", own_block), encoding="utf-8"
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

    _finish_shared(run_dir, base_dir, snapshot_dir, resolved, run_id, plan, prepared)

    for agent_id, file in prepared.items():
        (run_dir / "agents" / agent_id / "agent.json").write_text(
            file.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    agents = {i: PreparedAgent(file=f, run_dir=run_dir) for i, f in prepared.items()}
    return PreparedRun(
        run_id=run_id,
        run_dir=run_dir,
        meta=meta,
        resolved=stored,
        agents=agents,
        warnings=log.warnings,
    )


def _finish_shared(
    run_dir: Path,
    base_dir: Path,
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
        generated = _write_context_files(folder, base_dir, WorkspaceMode.SHARED, block)
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
