"""Area `catalog`: projects, skills and auth profiles, read only (W3-01, for „Neuer Run“,
docs/08-ux-konzept.md §5.4, §5.6, §5.7).

Projects come from `projects/` with their setup checks, skills from `discover_skills` with
the parameters of their `group.yaml`, profiles from `profiles.yaml` only: no test, no secret
(W3-05 adds testing and logins in `profiles`; W3-04 adds creating projects in `setup`;
W4-02 the skill editor). The checks show what `setup_project` would still do; arr has no
`arr mcp` before W4-01, so the `.mcp.json` check looks at the file only.
"""

import json
import os
import time
from pathlib import Path

from arr.errors import ArrError, ConfigError
from arr.models import AuthKind, GroupSpec, ParamSpec, ProjectSettings, Provider
from arr.resolve import _count_param, load_group_spec
from arr.service.api_models import (
    Action,
    ParamInfo,
    ProfileConcurrency,
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
from arr.service.real_api.rundir import open_items
from arr.skills import SkillAudience, SkillCatalog, discover_skills, skills_for_user_agent
from arr.workfolder import USER_AGENT_BLOCK, block_span, load_project_settings, os_reason

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


def _provider_of(kind: AuthKind) -> Provider:
    codex = (AuthKind.CODEX_CHATGPT, AuthKind.OPENAI_API_KEY)
    return Provider.CODEX if kind in codex else Provider.CLAUDE


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
                claude_problem = f"{os_reason(exc).capitalize()}. Prüfe die Rechte der Datei."
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
        """Profiles from `profiles.yaml` only: no test, no secret (W3-05 fills the rest)."""
        from arr.backends.codex_config import codex_home_for

        settings = self.state.settings
        out = []
        for profile in self.state.profiles_or_empty().profiles:
            provider = _provider_of(profile.kind)
            gate = self.state.gates.get(profile.name)
            login_dir = None
            present = False
            if profile.kind is AuthKind.CODEX_CHATGPT:
                home = codex_home_for(profile)
                login_dir = str(home)
                present = (home / "auth.json").is_file()
            keychain = None
            if profile.keychain is not None:
                keychain = f"{profile.keychain.service}/{profile.keychain.account}"
            out.append(
                ProfileInfo(
                    name=profile.name,
                    kind=profile.kind,
                    provider=provider,
                    default_for=[p for p, d in settings.defaults.items() if d.auth == profile.name],
                    concurrency=ProfileConcurrency(
                        limit=settings.concurrency.get(profile.name, 0),
                        in_use=gate.active if gate is not None else 0,
                    ),
                    secret_present=present,
                    keychain_label=keychain,
                    login_dir=login_dir,
                )
            )
        return out
