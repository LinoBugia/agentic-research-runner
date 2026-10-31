"""Area `catalog`: projects, skills and auth profiles, read only (W3-01, for „Neuer Run“,
docs/08-ux-konzept.md §5.4, §5.6, §5.7).

Projects come from `projects/` with their setup checks, skills from `discover_skills` with
the parameters of their `group.yaml`, profiles from `profiles.yaml` only: no test, no secret
(W3-05 adds testing and logins in `profiles`; W3-04 adds creating projects in `setup`;
W4-02 the skill editor).
"""

import os
import time
from pathlib import Path

from arr.errors import ArrError, ConfigError
from arr.models import GroupSpec, ParamSpec
from arr.resolve import _count_param, load_group_spec
from arr.service.api_models import (
    ParamInfo,
    ProfileInfo,
    ProjectCheck,
    ProjectFolder,
    ProjectInfo,
    ProjectRuns,
    Severity,
    SkillFile,
    SkillInfo,
    SkillIssue,
    SkillKind,
    SkillUsage,
)
from arr.service.real_api.base import ServiceState, not_found
from arr.service.real_api.profiles import load_status, profile_info, unreadable_profiles
from arr.service.real_api.rundir import open_items
from arr.skills import SkillAudience, SkillCatalog, discover_skills
from arr.workfolder import USER_AGENT_BLOCK, block_span, load_project_settings

MAX_FILES = 200
MAX_FILE_BYTES = 512 * 1024
_LANGUAGES = {".md": "markdown", ".yaml": "yaml", ".yml": "yaml", ".py": "python"}


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

    def _project(self, folder: Path) -> ProjectInfo:
        checks = []
        claude = folder / "CLAUDE.md"
        has_block = False
        if claude.is_file():
            try:
                has_block = block_span(claude.read_text("utf-8"), USER_AGENT_BLOCK) is not None
            except (OSError, ArrError):
                has_block = False
        checks.append(
            ProjectCheck(
                key="claude_md",
                ok=has_block,
                severity=Severity.INFO if has_block else Severity.WARNING,
                message="CLAUDE.md mit arr-Block vorhanden"
                if has_block
                else "CLAUDE.md ohne arr-Block: Claude Code kennt arr in diesem Projekt nicht",
            )
        )
        mcp = (folder / ".mcp.json").is_file()
        checks.append(
            ProjectCheck(
                key="mcp_json",
                ok=mcp,
                severity=Severity.INFO if mcp else Severity.WARNING,
                message=".mcp.json vorhanden" if mcp else ".mcp.json fehlt",
            )
        )
        agents = (folder / "AGENTS.md").is_file()
        checks.append(
            ProjectCheck(
                key="agents_md",
                ok=agents,
                severity=Severity.INFO,
                message="AGENTS.md vorhanden" if agents else "AGENTS.md fehlt (für Codex)",
            )
        )
        try:
            settings = load_project_settings(folder)
            copy_exclude = list(settings.copy_exclude)
            read_dirs = [str(d) for d in settings.read_dirs]
        except ArrError:
            copy_exclude, read_dirs = [], []
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
            title=folder.name,
            path=str(folder),
            setup_ok=all(c.ok for c in checks if c.severity is not Severity.INFO or c.ok)
            and has_block
            and mcp,
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
        return [self._project(folder) for folder in folders]

    async def get_project(self, name: str) -> ProjectInfo:
        folder = self.state.workfolder / "projects" / name
        if "/" in name or name in (".", "..") or not folder.is_dir():
            raise not_found("Projekt", name)
        return self._project(folder)

    # -- skills -----------------------------------------------------------------------------

    def _catalog(self) -> SkillCatalog:
        return discover_skills(self.state.workfolder / "skills")

    def _skill_files(self, root: Path) -> list[str]:
        files = []
        for base, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(names):
                if name.startswith("."):
                    continue
                files.append(str(Path(base, name).relative_to(root)))
                if len(files) >= MAX_FILES:
                    return files
        return files

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

    def _skill(self, name: str, catalog: SkillCatalog) -> SkillInfo:
        problem = catalog.problems.get(name)
        if problem is not None:
            return SkillInfo(
                name=name,
                description="",
                kind=SkillKind.TOOL,
                valid=False,
                issues=[
                    SkillIssue(file="SKILL.md", line=i.line, message=i.message)
                    for i in problem.issues
                ],
                used_by=self._usage(name),
            )
        info = catalog.skills[name]
        kind = (
            SkillKind.GROUP
            if info.is_group
            else (SkillKind.USER_AGENT if info.audience is SkillAudience.USER else SkillKind.TOOL)
        )
        spec: GroupSpec | None = None
        issues: list[SkillIssue] = []
        if info.group_file is not None:
            try:
                spec = load_group_spec(info.group_file)
            except ConfigError as exc:
                issues = [
                    SkillIssue(file="group.yaml", line=i.line, message=i.message)
                    for i in exc.issues
                ]
            except ArrError as exc:
                issues = [SkillIssue(file="group.yaml", message=exc.message)]
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
            description=info.description,
            kind=kind,
            audience=info.audience.value,
            files=self._skill_files(info.path),
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

    async def list_skills(self, kind: SkillKind | None = None) -> list[SkillInfo]:
        catalog = self._catalog()
        names = sorted({*catalog.skills, *catalog.problems})
        skills = [self._skill(name, catalog) for name in names]
        return [s for s in skills if kind is None or s.kind is kind]

    async def get_skill(self, name: str) -> SkillInfo:
        catalog = self._catalog()
        if name not in catalog.skills and name not in catalog.problems:
            raise not_found("Skill", name)
        return self._skill(name, catalog)

    async def get_skill_file(self, name: str, path: str) -> SkillFile:
        catalog = self._catalog()
        info = catalog.skills.get(name)
        if info is None:
            raise not_found("Skill", name)
        root = info.path.resolve()
        target = (root / path).resolve()
        if root not in target.parents or not target.is_file():
            raise not_found("Datei", f"{name}/{path}")
        size = target.stat().st_size
        content = target.read_text("utf-8", errors="replace")[:MAX_FILE_BYTES]
        return SkillFile(
            skill=name,
            path=path,
            content=content,
            language=_LANGUAGES.get(target.suffix, "text"),  # type: ignore[arg-type]
            size=size,
        )

    # -- profiles ---------------------------------------------------------------------------

    async def list_profiles(self) -> list[ProfileInfo]:
        """Profiles from `profiles.yaml` with the remembered test: no call, no keychain."""
        status = load_status()
        try:
            profiles = self.state.profiles_or_empty().profiles
        except (UnicodeDecodeError, OSError):
            raise unreadable_profiles() from None
        return [profile_info(self.state, profile, status.get(profile.name)) for profile in profiles]
