"""Area `catalog`: projects, skills and auth profiles (W3-01, for „Neuer Run“,
docs/08-ux-konzept.md §5.4, §5.6, §5.7), and the skill editor (W4-02).

Projects come from `projects/` with their setup checks, skills from `discover_skills` with
the parameters of their `group.yaml`, profiles from `profiles.yaml` only: no test, no secret
(W3-05 adds testing and logins in `profiles`; W3-04 adds creating projects in `setup`).
The checks show what `setup_project` would still do; arr has no `arr mcp` before W4-01, so
the `.mcp.json` check looks at the file only.

Every skill is checked when it is listed (`check_skill`): SKILL.md, `group.yaml` (YAML,
parameters, placeholders, patterns, resolution with the parameters' defaults), role files in
the skill, skills of the agents and `check_topology`. Every problem names file, line where
known, and what to do; a skill with problems cannot be started. Writing goes through
`skill_files` (no links, atomic, conflicts), and the skill is checked again right after.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

from arr.errors import ArrError, ConfigError, ConfigIssue
from arr.models import (
    GroupSpec,
    ParamSpec,
    ProjectSettings,
    Provider,
    ProviderDefaults,
    ResolvedRun,
    RunInput,
    WorkfolderSettings,
)
from arr.resolve import (
    _count_param,
    check_group_spec,
    line_of_path,
    parse_yaml_text,
    resolve_run,
    validate_model,
)
from arr.runs.topologies import check_topology
from arr.service.api_models import (
    Action,
    CreateSkillRequest,
    DuplicateSkillRequest,
    ParamInfo,
    ProfileInfo,
    ProjectCheck,
    ProjectFolder,
    ProjectInfo,
    ProjectRuns,
    SaveSkillFileRequest,
    SaveSkillFileResult,
    Severity,
    SkillChangeResult,
    SkillFile,
    SkillInfo,
    SkillIssue,
    SkillKind,
    SkillUsage,
)
from arr.service.real_api import skill_files
from arr.service.real_api.base import ServiceState, not_found
from arr.service.real_api.profiles import load_status, profile_info, unreadable_profiles
from arr.service.real_api.rundir import open_items
from arr.skills import (
    _FRONTMATTER_RE,
    GROUP_FILE,
    SKILL_FILE,
    SkillAudience,
    SkillCatalog,
    SkillFileError,
    check_research_skills,
    discover_skills,
    read_text_file,
    skills_for_user_agent,
)
from arr.skills import SkillInfo as CoreSkill
from arr.workfolder import (
    USER_AGENT_BLOCK,
    block_span,
    load_project_settings,
    load_workfolder_settings,
    os_reason,
)

MAX_FILES = 200
MAX_FILE_DEPTH = 12
"""Deepest folder below a skill that the file list shows (W4-02.1)."""
_LANGUAGES = {".md": "markdown", ".yaml": "yaml", ".yml": "yaml", ".py": "python"}
log = logging.getLogger("arr.service.catalog")

CHECK_PROFILE = "pruefung"
"""Stand-in profile while a skill is checked: profiles belong to the run, not to the skill."""
CHECK_MODELS = {Provider.CLAUDE: "claude-sonnet-5-5", Provider.CODEX: "gpt-5.5"}
"""Stand-in models while `arr.yaml` names none: the skill need not name one (docs/02 §3.3)."""
_PATTERN_HINT = " Passe das Muster an oder lege einen passenden Agenten unter „agents“ an."
_DEFAULT_HINT = " Passe „default“ an oder ändere „min“, „max“ bzw. „choices“."


def _count_files(folder: Path, limit: int = 5000) -> tuple[int, int]:
    """Number of files and their size below `folder` (capped), hidden folders left out."""
    count = size = 0
    stack = [folder]
    while stack and count < limit:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            count += 1
                            size += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return count, size


def _param_type(spec: ParamSpec) -> str:
    if spec.choices:
        return "choice"
    default = spec.default
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    return "str"


def _project_title(claude_text: str, fallback: str) -> str:
    """The title from the first line of the template's `CLAUDE.md`, „# Projekt: <title>“."""
    first = claude_text.split("\n", 1)[0]
    return first.removeprefix("# Projekt:").strip() if first.startswith("# Projekt:") else fallback


def _mcp_state(file: Path) -> tuple[bool, str]:
    """Whether the `.mcp.json` of a project names the server `arr`, with the check's text."""
    if not file.is_file():
        return False, ".mcp.json fehlt"
    try:
        servers = json.loads(file.read_text("utf-8")).get("mcpServers")
    except (OSError, ValueError, AttributeError):
        return False, ".mcp.json ist kein gültiges JSON"
    if isinstance(servers, dict) and "arr" in servers:
        from arr.workfolder import _arr_server_stale

        if _arr_server_stale(servers["arr"]):
            return False, ".mcp.json: „arr“ zeigt nicht auf das arr dieser Installation"
        return True, ".mcp.json vorhanden"
    return False, ".mcp.json ohne Server „arr“"


def _agents_count(spec: GroupSpec) -> int | None:
    """Agents a group starts with its defaults; None if a `count` has no default."""
    total = 0
    for agent in spec.agents:
        count = agent.count
        name = _count_param(agent)
        if name is not None and name in spec.params:
            count = spec.params[name].default
        if count is None:
            count = 1
        if not isinstance(count, int) or isinstance(count, bool):
            return None
        total += count
    return total


# --------------------------------------------------------------------------------------------
# Checking a skill (W4-02, docs/08 §5.7 „Prüfung“)
# --------------------------------------------------------------------------------------------


def _frontmatter_line(text: str, key: str) -> int | None:
    """Line of `key:` in the frontmatter of a SKILL.md (1-based), None if it is not there."""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    first = text[: match.start(1)].count("\n") + 1
    for offset, line in enumerate(match.group(1).splitlines()):
        if line.startswith(f"{key}:"):
            return first + offset
    return None


def _skill_md_issues(folder: Path, error: ConfigError) -> list[SkillIssue]:
    """Issues of a SKILL.md that `load_skill` refused, with the line of the field."""
    try:
        text = read_text_file(folder / SKILL_FILE)
    except (OSError, SkillFileError):
        text = ""
    out = []
    for issue in error.issues:
        key = issue.path.split(".", 1)[0]
        line = issue.line or (_frontmatter_line(text, key) if key else None)
        out.append(
            SkillIssue(file=SKILL_FILE, line=line, message=issue.message, field=issue.path or None)
        )
    return out


def _check_settings(settings: WorkfolderSettings) -> WorkfolderSettings:
    """The workfolder's settings with stand-ins where a provider has no model or profile.

    A skill need not name model and profile (they come from `arr.yaml` or the form), so their
    absence is no problem of the skill.
    """
    defaults = dict(settings.defaults)
    for provider in Provider:
        own = defaults.get(provider, ProviderDefaults())
        defaults[provider] = own.model_copy(
            update={
                "model": own.model or CHECK_MODELS[provider],
                "auth": own.auth or CHECK_PROFILE,
            }
        )
    return settings.model_copy(update={"defaults": defaults})


class _Issues:
    """Issues of one skill file with lines from its YAML, without duplicates."""

    def __init__(self, file: str, lines: Mapping[tuple[str | int, ...], int] | None) -> None:
        self.file = file
        self.lines = lines
        self.items: list[SkillIssue] = []
        self._seen: set[tuple[str, str]] = set()

    def add(self, path: str, message: str, line: int | None = None) -> None:
        if (path, message) in self._seen:
            return
        self._seen.add((path, message))
        if line is None and path:
            line = line_of_path(path, self.lines)
        self.items.append(
            SkillIssue(file=self.file, line=line, message=message, field=path or None)
        )

    def extend(self, issues: Iterable[ConfigIssue]) -> None:
        for issue in issues:
            message = issue.message
            if message.startswith("Das Muster") and "Passe" not in message:
                message += _PATTERN_HINT
            elif "Standardwert passt nicht" in message:
                message += _DEFAULT_HINT
            self.add(issue.path, message, issue.line)


def _spec_index(spec: GroupSpec, base_id: str) -> int:
    return next((i for i, a in enumerate(spec.agents) if a.id == base_id), 0)


def _role_issue(folder: Path, role: str) -> str | None:
    """Why `role` is no regular file inside the skill folder, None if it is one."""
    if Path(role).is_absolute() or ".." in Path(role).parts:
        return "liegt außerhalb des Skills. Der Pfad muss relativ zum Skill sein, ohne „..“"
    root = folder.resolve()
    candidate = (folder / role).resolve()
    if not candidate.is_relative_to(root):
        return "ist ein Link aus dem Skill hinaus. Lege die Rolle als Datei in den Skill"
    if not candidate.exists():
        return "nicht gefunden"
    if not candidate.is_file():
        return "ist keine normale Datei. Ersetze sie durch eine Textdatei"
    return None


def _check_resolved(
    spec: GroupSpec, resolved: ResolvedRun, folder: Path, catalog: SkillCatalog, out: _Issues
) -> None:
    """Roles in the skill, skills of the agents and the topology, for the resolved defaults."""
    roles_done: set[str] = set()
    skills_done: set[str] = set()
    for agent in resolved.agents:
        index = _spec_index(spec, agent.base_id)
        own = spec.agents[index]
        if agent.role and agent.role not in roles_done:
            roles_done.add(agent.role)
            problem = _role_issue(folder, agent.role)
            if problem is not None:
                where = f"agents[{index}].role" if own.role else "defaults.role"
                if problem == "nicht gefunden":
                    text = (
                        f"Rolle {agent.role} nicht gefunden. Lege sie an oder entferne den "
                        f"Agenten {agent.base_id}."
                    )
                else:
                    text = f"Die Rolle {agent.role} {problem}."
                out.add(where, text)
        if agent.base_id in skills_done:
            continue
        skills_done.add(agent.base_id)
        base = f"agents[{index}].skills" if own.skills is not None else "defaults.skills"
        _found, issues = check_research_skills(catalog, agent.skills, base)
        out.extend(issues)
    try:
        check_topology(resolved)
    except ConfigError as exc:
        out.extend(exc.issues)
    except ArrError as exc:
        out.add("topology", exc.message)


def check_group(
    folder: Path, catalog: SkillCatalog, settings: WorkfolderSettings
) -> tuple[GroupSpec | None, list[SkillIssue]]:
    """`_check_group`; an unexpected failure becomes an issue of this skill (W4-02.1)."""
    try:
        return _check_group(folder, catalog, settings)
    except Exception as exc:  # noqa: BLE001 - a hostile group.yaml never breaks the caller
        log.warning("Prüfung der group.yaml von %s fehlgeschlagen", folder.name, exc_info=True)
        return None, [
            SkillIssue(
                file=GROUP_FILE,
                message=f"Die Prüfung ist fehlgeschlagen ({type(exc).__name__}). Vereinfache "
                "die group.yaml.",
            )
        ]


def _check_group(
    folder: Path, catalog: SkillCatalog, settings: WorkfolderSettings
) -> tuple[GroupSpec | None, list[SkillIssue]]:
    """Check the `group.yaml` of a skill folder; the spec (if it loads) and its issues.

    Order: read (regular file, at most 256 KB, UTF-8) → YAML → fields → placeholders and
    `when` → resolution with the parameters' defaults (patterns, IDs, stages, raters) → roles,
    skills of the agents, topology. Stops at the first stage that fails, because later stages
    need its result.
    """
    file = folder / GROUP_FILE
    try:
        text = read_text_file(file)
    except FileNotFoundError:
        return None, [SkillIssue(file=GROUP_FILE, message="Die group.yaml fehlt. Lege sie an.")]
    except SkillFileError as exc:
        return None, [SkillIssue(file=GROUP_FILE, message=exc.message)]
    try:
        data, lines = parse_yaml_text(text, GROUP_FILE)
    except ConfigError as exc:
        out = _Issues(GROUP_FILE, None)
        out.extend(exc.issues)
        return None, out.items
    out = _Issues(GROUP_FILE, lines)
    try:
        spec = validate_model(GroupSpec, data, source=GROUP_FILE, lines=lines)
        check_group_spec(spec, source=GROUP_FILE)
    except ConfigError as exc:
        out.extend(exc.issues)
        return None, out.items
    run_input = RunInput(project="pruefung", skill=folder.name, task="Prüfung")
    try:
        resolved = resolve_run(run_input, spec, _check_settings(settings))
    except ConfigError as exc:
        out.extend(exc.issues)
        return spec, out.items
    _check_resolved(spec, resolved, folder, catalog, out)
    return spec, out.items


class CatalogArea:
    """Read-only lists for „Neuer Run“, projects and skills pages."""

    def __init__(self, state: ServiceState) -> None:
        self.state = state

    # -- projects ---------------------------------------------------------------------------

    def _project_runs(self, name: str) -> ProjectRuns:
        total = active = waiting = 0
        for record in self.state.store.scan(refresh=False):
            if record.meta is None or record.meta.project != name:
                continue
            total += 1
            if record.status.is_active:
                active += 1
                if open_items(record):
                    waiting += 1
        return ProjectRuns(total=total, active=active, waiting=waiting)

    def _skill_links(
        self, folder: Path, settings: ProjectSettings, catalog: SkillCatalog
    ) -> tuple[bool, str]:
        """Links in `.claude/skills/` against the skills offered to the user agent."""
        try:
            wanted = skills_for_user_agent(catalog, settings)
        except ArrError as exc:
            return False, exc.message
        links = folder / ".claude" / "skills"
        found = sum(
            1 for s in wanted if (links / s.name).is_symlink() and (links / s.name).exists()
        )
        if not wanted:
            return True, "Skill-Links: keine Skills für Claude Code im Workfolder"
        return found == len(wanted), f"Skill-Links: {found} von {len(wanted)}"

    def _project(self, folder: Path, catalog: SkillCatalog) -> ProjectInfo:
        repair = Action(label="Reparieren", url=f"/projekte/{folder.name}")

        def check(
            key: str, ok: bool, message: str, *, detail: str | None = None, repairable: bool = True
        ) -> ProjectCheck:
            return ProjectCheck(
                key=key,
                ok=ok,
                severity=Severity.INFO
                if ok
                else (Severity.WARNING if repairable else Severity.ERROR),
                message=message,
                detail=detail,
                action=repair if repairable and not ok else None,
            )

        claude = folder / "CLAUDE.md"
        claude_text = ""
        has_block = False
        claude_problem = None
        if claude.is_file():
            try:
                claude_text = claude.read_text("utf-8")
                has_block = block_span(claude_text, USER_AGENT_BLOCK) is not None
            except ArrError as exc:
                claude_problem = exc.message
            except UnicodeDecodeError:
                claude_problem = "Sie ist nicht in UTF-8 gespeichert. Speichere sie im Editor so."
            except OSError as exc:
                reason = os_reason(exc)
                claude_problem = f"{reason[0].upper()}{reason[1:]}. Prüfe die Rechte der Datei."
        try:
            settings = load_project_settings(folder)
        except ArrError:
            settings = ProjectSettings()
        mcp, mcp_text = _mcp_state(folder / ".mcp.json")
        links, links_text = self._skill_links(folder, settings, catalog)
        agents = (folder / "AGENTS.md").is_file()
        checks = [
            check(
                "claude_md",
                False,
                "CLAUDE.md lässt sich nicht lesen",
                detail=claude_problem,
                repairable=False,
            )
            if claude_problem
            else check(
                "claude_md",
                has_block,
                "CLAUDE.md mit arr-Block vorhanden"
                if has_block
                else "CLAUDE.md ohne arr-Block: Claude Code kennt arr in diesem Projekt nicht",
            ),
            check("mcp_json", mcp, mcp_text),
            check("skill_links", links, links_text),
            check(
                "agents_md",
                agents,
                "AGENTS.md vorhanden" if agents else "AGENTS.md fehlt (für Codex)",
            ),
        ]
        copy_exclude = list(settings.copy_exclude)
        read_dirs = [str(d) for d in settings.read_dirs]
        folders = []
        try:
            for entry in sorted(folder.iterdir()):
                if entry.is_dir() and not entry.name.startswith("."):
                    folders.append(ProjectFolder(name=entry.name, count=_count_files(entry)[0]))
        except OSError:
            pass
        _files, size = _count_files(folder)
        try:
            modified = folder.stat().st_mtime
        except OSError:
            modified = time.time()
        return ProjectInfo(
            name=folder.name,
            title=_project_title(claude_text, folder.name),
            path=str(folder),
            setup_ok=has_block and mcp and links,
            size_bytes=size,
            modified_at=modified,
            runs=self._project_runs(folder.name),
            checks=checks,
            folders=folders,
            copy_exclude=copy_exclude,
            read_dirs=read_dirs,
        )

    async def list_projects(self) -> list[ProjectInfo]:
        projects = self.state.workfolder / "projects"
        try:
            folders = sorted(
                p for p in projects.iterdir() if p.is_dir() and not p.name.startswith(".")
            )
        except OSError:
            return []
        catalog = self._catalog()
        return [self._project(folder, catalog) for folder in folders]

    async def get_project(self, name: str) -> ProjectInfo:
        folder = self.state.workfolder / "projects" / name
        if "/" in name or name in (".", "..") or not folder.is_dir():
            raise not_found("Projekt", name)
        return self._project(folder, self._catalog())

    # -- skills -----------------------------------------------------------------------------

    def _catalog(self) -> SkillCatalog:
        return discover_skills(self.state.workfolder / "skills")

    def _skill_files(self, root: Path) -> list[str]:
        """Files of a skill (at most `MAX_FILES`, `MAX_FILE_DEPTH` folders deep, hidden ones
        left out); a folder that cannot be read (e.g. a path too long) is skipped."""
        files: list[str] = []
        stack: list[tuple[str, str, int]] = [(str(root), "", 0)]
        while stack:
            folder, prefix, depth = stack.pop()
            try:
                with os.scandir(folder) as it:
                    entries = sorted(it, key=lambda e: e.name, reverse=True)
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                rel = f"{prefix}{entry.name}"
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    if depth + 1 < MAX_FILE_DEPTH:
                        stack.append((entry.path, f"{rel}/", depth + 1))
                    continue
                files.append(rel)
        files.sort()
        return files[:MAX_FILES]

    def _usage(self, name: str) -> SkillUsage:
        projects: set[str] = set()
        count = 0
        last = None
        for record in self.state.store.scan(refresh=False):
            meta = record.meta
            if meta is None or meta.skill != name:
                continue
            count += 1
            projects.add(meta.project)
            last = max(last or 0.0, meta.created)
        return SkillUsage(projects=sorted(projects), runs_count=count, last_run_at=last)

    def _settings(self) -> WorkfolderSettings:
        try:
            return load_workfolder_settings(self.state.workfolder)
        except (ArrError, OSError, UnicodeDecodeError):
            return WorkfolderSettings()

    def _skill(
        self, name: str, catalog: SkillCatalog, settings: WorkfolderSettings | None = None
    ) -> SkillInfo:
        """A skill with the result of its check (W4-02): `valid` only without any issue."""
        settings = settings if settings is not None else self._settings()
        folder = catalog.root / name
        info: CoreSkill | None = catalog.skills.get(name)
        issues: list[SkillIssue] = []
        problem = catalog.problems.get(name)
        if problem is not None:
            issues = _skill_md_issues(folder, problem)
        is_group = info.is_group if info is not None else (folder / GROUP_FILE).is_file()
        spec: GroupSpec | None = None
        if is_group:
            spec, group_issues = check_group(folder, catalog, settings)
            issues += group_issues
        if info is None:
            kind = SkillKind.GROUP if is_group else SkillKind.TOOL
        elif info.is_group:
            kind = SkillKind.GROUP
        else:
            kind = SkillKind.USER_AGENT if info.audience is SkillAudience.USER else SkillKind.TOOL
        params = []
        if spec is not None:
            for pname, pspec in spec.params.items():
                params.append(
                    ParamInfo(
                        name=pname,
                        type=_param_type(pspec),  # type: ignore[arg-type]
                        default=pspec.default,
                        min=pspec.min,
                        max=pspec.max,
                        choices=pspec.choices,
                        doc=pspec.doc,
                    )
                )
        return SkillInfo(
            name=name,
            description=info.description if info is not None else "",
            kind=kind,
            audience=info.audience.value if info is not None else None,
            files=self._skill_files(folder),
            valid=not issues,
            issues=issues,
            used_by=self._usage(name),
            topology=spec.topology if spec is not None else None,
            params=params,
            agents_spec=[a.model_dump(mode="json", exclude_none=True) for a in spec.agents]
            if spec is not None
            else [],
            channels_spec=[c.model_dump(mode="json", exclude_none=True) for c in spec.groupchats]
            if spec is not None
            else [],
            rubric=spec.rubric.model_dump(mode="json")
            if spec is not None and spec.rubric
            else None,
            agents_count=_agents_count(spec) if spec is not None else None,
        )

    def _list(self, kind: SkillKind | None) -> list[SkillInfo]:
        catalog = self._catalog()
        settings = self._settings()
        names = sorted({*catalog.skills, *catalog.problems})
        skills = [self._checked(name, catalog, settings) for name in names]
        return [s for s in skills if kind is None or s.kind is kind]

    def _checked(
        self, name: str, catalog: SkillCatalog, settings: WorkfolderSettings | None = None
    ) -> SkillInfo:
        """`_skill`, but an unexpected failure only marks this one skill as invalid."""
        try:
            return self._skill(name, catalog, settings)
        except Exception as exc:  # noqa: BLE001 - one broken skill never blocks the others
            log.warning("Prüfung von Skill %s fehlgeschlagen", name, exc_info=True)
            return SkillInfo(
                name=name,
                description="",
                kind=SkillKind.TOOL,
                valid=False,
                issues=[
                    SkillIssue(
                        file=SKILL_FILE,
                        message=f"Die Prüfung ist fehlgeschlagen ({type(exc).__name__}). "
                        "Vereinfache die Dateien des Skills oder lege ihn neu an.",
                    )
                ],
            )

    async def list_skills(self, kind: SkillKind | None = None) -> list[SkillInfo]:
        return await asyncio.to_thread(self._list, kind)

    def _one(self, name: str) -> SkillInfo:
        catalog = self._catalog()
        if name not in catalog.skills and name not in catalog.problems:
            raise not_found("Skill", name)
        return self._checked(name, catalog)

    async def get_skill(self, name: str) -> SkillInfo:
        return await asyncio.to_thread(self._one, name)

    def _exact(self, name: str) -> SkillCatalog:
        """The catalog, if `name` is exactly the folder name of a skill in it, else `not_found`.

        On APFS `RUNDE` opens the folder `runde`; only the name as listed counts (W4-02.1).
        """
        catalog = self._catalog()
        if name not in catalog.skills and name not in catalog.problems:
            raise not_found("Skill", name)
        return catalog

    def _file(self, name: str, path: str) -> SkillFile:
        self._exact(name)
        content = skill_files.read_file(self.state.workfolder, name, path)
        return SkillFile(
            skill=name,
            path=path,
            content=content.text,
            language=_LANGUAGES.get(Path(path).suffix, "text"),  # type: ignore[arg-type]
            size=content.size,
            version=content.version,
        )

    async def get_skill_file(self, name: str, path: str) -> SkillFile:
        """A text file of a skill for the editor: no link followed, only regular UTF-8 files
        of at most 256 KB (`skill_files.read_file`)."""
        return await asyncio.to_thread(self._file, name, path)

    # -- skill editor (W4-02) ---------------------------------------------------------------

    def _save(self, name: str, path: str, request: SaveSkillFileRequest) -> SaveSkillFileResult:
        self._exact(name)
        written = skill_files.write_file(
            self.state.workfolder, name, path, request.content, version=request.version
        )
        file = SkillFile(
            skill=name,
            path=path,
            content=written.text,
            language=_LANGUAGES.get(Path(path).suffix, "text"),  # type: ignore[arg-type]
            size=written.size,
            version=written.version,
        )
        catalog = self._catalog()
        if name not in catalog.skills and name not in catalog.problems:
            raise not_found("Skill", name)
        return SaveSkillFileResult(file=file, skill=self._checked(name, catalog))

    async def save_skill_file(
        self, name: str, path: str, request: SaveSkillFileRequest
    ) -> SaveSkillFileResult:
        """Save a text file of a skill (also when the skill is invalid afterwards) and check
        the skill right after; `conflict` if the file changed outside since it was opened."""
        return await asyncio.to_thread(self._save, name, path, request)

    def _created(self, name: str, message: str, left_out: list[str]) -> SkillChangeResult:
        return SkillChangeResult(skill=self._one(name), left_out=left_out, message=message)

    def _create(self, request: CreateSkillRequest) -> SkillChangeResult:
        skill_files.create_skill(
            self.state.workfolder, request.name, request.kind, request.description
        )
        what = "Group-Skill" if request.kind == "group" else "Werkzeug-Skill"
        return self._created(request.name, f"{what} „{request.name}“ angelegt.", [])

    async def create_skill(self, request: CreateSkillRequest) -> SkillChangeResult:
        """A new skill from the built-in template (tool: SKILL.md; group: roundtable)."""
        return await asyncio.to_thread(self._create, request)

    def _duplicate(self, name: str, request: DuplicateSkillRequest) -> SkillChangeResult:
        catalog = self._catalog()
        if name not in catalog.skills and name not in catalog.problems:
            raise not_found("Skill", name)
        left = skill_files.duplicate_skill(self.state.workfolder, name, request.name)
        message = f"„{name}“ als „{request.name}“ dupliziert."
        if left:
            shown = ", ".join(left[:5]) + (" …" if len(left) > 5 else "")
            message += f" Nicht kopiert (Links, Pipes oder zu groß): {shown}."
        return self._created(request.name, message, left)

    async def duplicate_skill(self, name: str, request: DuplicateSkillRequest) -> SkillChangeResult:
        """Copy a skill under a new, free name (regular files only, `name:` adjusted)."""
        return await asyncio.to_thread(self._duplicate, name, request)

    # -- profiles ---------------------------------------------------------------------------

    async def list_profiles(self) -> list[ProfileInfo]:
        """Profiles from `profiles.yaml` with the remembered test: no call, no keychain."""
        status = load_status()
        try:
            profiles = self.state.profiles_or_empty().profiles
        except (UnicodeDecodeError, OSError):
            raise unreadable_profiles() from None
        return [profile_info(self.state, profile, status.get(profile.name)) for profile in profiles]
