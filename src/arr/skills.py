"""Skill registry: discover and validate skills in the workfolder, decide who gets which skill,
and install skills as `.claude/skills` links (user agent), a generated Claude plugin or
`.agents/skills` (research agents).

A skill is a folder with a `SKILL.md` (frontmatter `name`, `description`, optional
`metadata.arr-audience`); a `group.yaml` next to it makes it a group skill.
See docs/03-skills-und-research-groups.md §1-3 and docs/spikes/S1_claude.md §3.1.
"""

import contextlib
import difflib
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from arr import __version__
from arr.errors import ArrError, ConfigError, ConfigIssue
from arr.models import ProjectSettings
from arr.resolve import too_nested, validate_model

__all__ = [
    "ARR_SKILL",
    "PLUGIN_NAME",
    "SkillAudience",
    "SkillCatalog",
    "SkillFileError",
    "START_SKILLS",
    "SkillInfo",
    "StartSkills",
    "SkillUpdate",
    "StartSkillUpdate",
    "StartSkillsUpdate",
    "check_research_skills",
    "copy_skill_tree",
    "discover_skills",
    "install_codex_skills",
    "install_start_skill",
    "install_start_skills",
    "internal_link_target",
    "link_user_skills",
    "load_skill",
    "read_text_file",
    "skills_for_research_agent",
    "skills_for_user_agent",
    "snapshot_skills",
    "start_skill_files",
    "start_skills_message",
    "tree_digest",
    "update_start_skills",
    "update_lines",
    "write_claude_plugin",
]

log = logging.getLogger("arr.skills")

SKILL_FILE = "SKILL.md"
GROUP_FILE = "group.yaml"
SKILLS_DIR = "skills"
"""Folder of the skills in the workfolder; messages name it instead of an absolute path."""
MAX_SKILL_FILE_BYTES = 256 * 1024
"""Largest skill file arr reads or writes (SKILL.md, group.yaml, roles; W4-02)."""
FRONTMATTER_TOO_NESTED = (
    "Der Kopfbereich der SKILL.md ist zu verschachtelt (zu viele Ebenen oder Verweise mit & und "
    "*). Schreibe ihn flach: „name“, „description“ und höchstens einfache Einträge unter "
    "„metadata“."
)
ARR_SKILL = "arr"
"""Name of the skill that explains arr to the user agent; never given to research agents."""
PLUGIN_NAME = "arr"
"""Name of the generated plugin; its skills are called `arr:<skill>` (docs/spikes/S1 §2.4)."""
AUDIENCE_KEY = "arr-audience"

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


class SkillAudience(StrEnum):
    """Who may use a skill: `metadata.arr-audience` in `SKILL.md` (docs/03 §1.1)."""

    USER = "user"
    RESEARCH = "research"
    BOTH = "both"


class SkillFrontmatter(BaseModel):
    """Frontmatter of a `SKILL.md`. Other keys of the open standard are ignored."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(max_length=64)
    description: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SkillInfo:
    """A valid skill in the workfolder.

    `audience` is the effective audience: the explicit `arr-audience` if given, otherwise
    `user` for group skills and the `arr` skill (they need the arr MCP server, E11) and `both`
    for everything else.
    """

    name: str
    path: Path
    description: str
    audience: SkillAudience
    explicit_audience: SkillAudience | None
    group_file: Path | None

    @property
    def is_group(self) -> bool:
        """True for a group skill, i.e. a skill with a `group.yaml`."""
        return self.group_file is not None

    @property
    def for_user_agent(self) -> bool:
        """True if the user agent (Claude Code) may get this skill."""
        return self.audience in (SkillAudience.USER, SkillAudience.BOTH)

    @property
    def for_research_agent(self) -> bool:
        """True if research agents may get this skill when it is listed for them."""
        return self.audience in (SkillAudience.RESEARCH, SkillAudience.BOTH)


@dataclass(slots=True)
class SkillCatalog:
    """The skills found in a workfolder's `skills/` folder.

    `skills` holds the valid ones by name. Folders with a broken `SKILL.md` do not block
    everything else: they are kept in `problems` and reported only when someone asks for them.
    """

    root: Path
    skills: dict[str, SkillInfo] = field(default_factory=dict)
    problems: dict[str, ConfigError] = field(default_factory=dict)

    def __contains__(self, name: object) -> bool:
        return name in self.skills

    def get(self, name: str, *, path: str = "") -> SkillInfo:
        """Return the skill or raise `ConfigError` (broken skill, or unknown with suggestion)."""
        issue = self.lookup_issue(name, path)
        if issue is not None:
            problem = self.problems.get(name)
            if problem is not None:
                raise problem
            raise ConfigError([issue], source=SKILLS_DIR)
        return self.skills[name]

    def lookup_issue(self, name: str, path: str = "") -> ConfigIssue | None:
        """Return why `name` cannot be used, or None if it is a valid skill."""
        if name in self.skills:
            return None
        problem = self.problems.get(name)
        if problem is not None:
            return ConfigIssue(
                path,
                f"Der Skill „{name}“ ist ungültig: {problem.issue_message}"
                f" (siehe {SKILLS_DIR}/{name}/{SKILL_FILE}).",
            )
        known = sorted({*self.skills, *self.problems})
        hint = difflib.get_close_matches(name, known, n=1)
        suggestion = f" Meintest du „{hint[0]}“?" if hint else ""
        # no absolute path in the message (docs/08 §8.4, W3-10 review finding 12)
        return ConfigIssue(
            path,
            f"Den Skill „{name}“ gibt es im Workfolder nicht.{suggestion} Lege ihn unter "
            "Skills an oder als Ordner mit einer SKILL.md im Ordner skills des Workfolders.",
        )


# --------------------------------------------------------------------------------------------
# Reading skill files without hanging (W4-02)
# --------------------------------------------------------------------------------------------


class SkillFileError(ArrError):
    """A skill file that arr does not read: no regular file, too large or no UTF-8 text."""


def read_text_file(path: str | Path, *, limit: int = MAX_SKILL_FILE_BYTES) -> str:
    """Read a skill file as UTF-8 text without blocking on FIFOs or devices.

    The file is opened non-blocking and checked with `fstat` on the open descriptor: only a
    regular file of at most `limit` bytes is read. Links are followed (the run's snapshot
    leaves links out of the skill anyway); the editor reads with `arr.service.real_api.
    skill_files`, which follows none. Raises `FileNotFoundError` or `SkillFileError`.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SkillFileError(f"Die Datei ist nicht lesbar ({exc.strerror or exc}).") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SkillFileError(
                "Das ist keine normale Datei (z. B. ein Gerät oder eine Pipe). Ersetze sie "
                "durch eine Textdatei."
            )
        if info.st_size > limit:
            raise SkillFileError(
                f"Die Datei ist zu groß ({info.st_size // 1024} KB, höchstens {limit // 1024} "
                "KB). Kürze sie."
            )
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, limit + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError as exc:
        raise SkillFileError(f"Die Datei ist nicht lesbar ({exc.strerror or exc}).") from None
    finally:
        os.close(fd)
    if len(data) > limit:
        raise SkillFileError(f"Die Datei ist zu groß (höchstens {limit // 1024} KB). Kürze sie.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise SkillFileError(
            "Die Datei ist keine UTF-8-Textdatei. Speichere sie im Editor als UTF-8."
        ) from None


# --------------------------------------------------------------------------------------------
# Discovery and validation
# --------------------------------------------------------------------------------------------


def _parse_frontmatter(text: str, source: str) -> dict[str, Any]:
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise ConfigError.single(
            "",
            "Am Anfang der SKILL.md fehlt der Kopfbereich zwischen zwei Zeilen „---“ mit "
            "„name“ und „description“.",
            source=source,
        )
    try:
        data = YAML(typ="safe").load(match.group(1))
    except RecursionError:
        raise ConfigError.single("", FRONTMATTER_TOO_NESTED, source=source) from None
    except YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 2 if mark else None  # +1 for 1-based, +1 for the `---` line
        raise ConfigError.single(
            "",
            "Der Kopfbereich der SKILL.md ist kein gültiges YAML. Prüfe Einrückung, "
            "Doppelpunkte und Anführungszeichen (Texte mit „: “ in Anführungszeichen setzen).",
            source=source,
            line=line,
        ) from None
    if too_nested(data):
        raise ConfigError.single("", FRONTMATTER_TOO_NESTED, source=source)
    if not isinstance(data, dict):
        raise ConfigError.single(
            "",
            "Der Kopfbereich der SKILL.md muss Einträge der Form „name: wert“ enthalten.",
            source=source,
        )
    return data


def _parse_audience(metadata: dict[str, Any], source: str) -> SkillAudience | None:
    if AUDIENCE_KEY not in metadata:
        return None
    value = metadata[AUDIENCE_KEY]
    try:
        return SkillAudience(value)
    except ValueError:
        raise ConfigError.single(
            f"metadata.{AUDIENCE_KEY}",
            f"Unbekannte Zielgruppe „{value}“. Erlaubt sind user, research und both.",
            source=source,
        ) from None


def load_skill(path: str | Path) -> SkillInfo:
    """Read and validate the skill in folder `path`; raise `ConfigError` with the file named.

    Checks that `name` is a lowercase slug equal to the folder name, that `description` is
    set and that `metadata.arr-audience` is valid. Group skills and the `arr` skill cannot
    be offered to research agents (E11).
    """
    folder = Path(path)
    skill_file = folder / SKILL_FILE
    source = str(skill_file)
    try:
        text = read_text_file(skill_file)
    except FileNotFoundError:
        raise ConfigError.single(
            "", f"Im Ordner „{folder.name}“ fehlt die Datei {SKILL_FILE}.", source=source
        ) from None
    except SkillFileError as exc:
        raise ConfigError.single("", exc.message, source=source) from None

    data = _parse_frontmatter(text, source)
    front = validate_model(SkillFrontmatter, data, source=source)
    if not _NAME_RE.fullmatch(front.name):
        raise ConfigError.single(
            "name",
            f"Ungültiger Skill-Name „{front.name}“. Erlaubt sind Kleinbuchstaben, Ziffern und "
            "„-“, z. B. „proof-review“.",
            source=source,
        )
    if front.name != folder.name:
        raise ConfigError.single(
            "name",
            f"Der Name „{front.name}“ passt nicht zum Ordner „{folder.name}“. Benenne den "
            "Ordner oder den Eintrag „name“ um, beide müssen gleich heißen.",
            source=source,
        )
    explicit = _parse_audience(front.metadata, source)

    group_file = folder / GROUP_FILE
    group_path = group_file if group_file.is_file() else None
    user_only = group_path is not None or front.name == ARR_SKILL
    if user_only and explicit in (SkillAudience.RESEARCH, SkillAudience.BOTH):
        what = "Der Skill „arr“" if front.name == ARR_SKILL else "Ein Group-Skill"
        raise ConfigError.single(
            f"metadata.{AUDIENCE_KEY}",
            f"{what} braucht den arr-MCP-Server und geht nur an den User-Agent (keine Runs in "
            f"Runs, E11). Entferne „{AUDIENCE_KEY}“ oder setze „user“.",
            source=source,
        )
    audience = explicit or (SkillAudience.USER if user_only else SkillAudience.BOTH)
    return SkillInfo(
        name=front.name,
        path=folder,
        description=front.description.strip(),
        audience=audience,
        explicit_audience=explicit,
        group_file=group_path,
    )


def discover_skills(skills_dir: str | Path) -> SkillCatalog:
    """Find all skills in `skills_dir` (the workfolder's `skills/`).

    Subfolders starting with `.` or `_` and plain files are ignored. A missing folder gives
    an empty catalog.
    """
    root = Path(skills_dir)
    catalog = SkillCatalog(root=root)
    if not root.is_dir():
        return catalog
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.startswith((".", "_")) or not entry.is_dir():
            continue
        try:
            catalog.skills[entry.name] = load_skill(entry)
        except ConfigError as exc:
            catalog.problems[entry.name] = exc
        except Exception as exc:  # noqa: BLE001 - one broken skill never blocks the others
            log.warning("Skill %s ließ sich nicht laden", entry.name, exc_info=True)
            catalog.problems[entry.name] = ConfigError.single(
                "",
                f"Die SKILL.md ließ sich nicht prüfen ({type(exc).__name__}). Vereinfache die "
                "Datei oder lege sie neu an.",
                source=str(entry / SKILL_FILE),
            )
    return catalog


# --------------------------------------------------------------------------------------------
# Who gets which skill (docs/03 §1.1)
# --------------------------------------------------------------------------------------------


def skills_for_user_agent(catalog: SkillCatalog, project: ProjectSettings) -> list[SkillInfo]:
    """Skills offered to the user agent in a project, sorted by name.

    All skills with audience `user` or `both`, restricted by `project.yaml skills` if that is a
    list. Raises `ConfigError` (path `skills[<i>]`) for listed names that are unknown or
    meant for research agents only.
    """
    if project.skills == "all":
        return [s for s in catalog.skills.values() if s.for_user_agent]
    issues: list[ConfigIssue] = []
    chosen: dict[str, SkillInfo] = {}
    for index, name in enumerate(project.skills):
        path = f"skills[{index}]"
        issue = catalog.lookup_issue(name, path)
        if issue is not None:
            issues.append(issue)
            continue
        skill = catalog.skills[name]
        if not skill.for_user_agent:
            issues.append(
                ConfigIssue(
                    path,
                    f"Der Skill „{name}“ ist nur für Research-Agenten gedacht und kann dem "
                    "User-Agent nicht angeboten werden. Entferne ihn aus der Liste.",
                )
            )
            continue
        chosen[name] = skill
    if issues:
        raise ConfigError(issues, source="project.yaml")
    return sorted(chosen.values(), key=lambda s: s.name)


def check_research_skills(
    catalog: SkillCatalog, names: Sequence[str], path: str = "skills"
) -> tuple[list[SkillInfo], list[ConfigIssue]]:
    """Look up the skills explicitly listed for a research agent.

    Returns the usable skills (in the given order, without duplicates) and one issue per
    unusable name: unknown, broken, or not allowed for research agents (group skills and
    `arr`, E11). `path` is the location of the list, e.g. `agents[2].skills`.
    """
    found: dict[str, SkillInfo] = {}
    issues: list[ConfigIssue] = []
    for index, name in enumerate(names):
        where = f"{path}[{index}]"
        issue = catalog.lookup_issue(name, where)
        if issue is not None:
            issues.append(issue)
            continue
        skill = catalog.skills[name]
        if not skill.for_research_agent:
            reason = (
                "ist ein Group-Skill" if skill.is_group else "ist nur für den User-Agent gedacht"
            )
            issues.append(
                ConfigIssue(
                    where,
                    f"Der Skill „{name}“ {reason} und geht nicht an Research-Agenten (keine Runs "
                    "in Runs, E11). Entferne ihn aus „skills“.",
                )
            )
            continue
        found.setdefault(name, skill)
    return list(found.values()), issues


def skills_for_research_agent(
    catalog: SkillCatalog, names: Sequence[str], path: str = "skills"
) -> list[SkillInfo]:
    """Like `check_research_skills`, but raise `ConfigError` with all issues."""
    skills, issues = check_research_skills(catalog, names, path)
    if issues:
        raise ConfigError(issues)
    return skills


# --------------------------------------------------------------------------------------------
# Installation
# --------------------------------------------------------------------------------------------


def internal_link_target(link: Path, root_real: str) -> str | None:
    """Relative link text for a symlink whose target lies inside `root_real`, else None.

    `root_real` is the resolved root of the tree being copied. The link is resolved fully
    (also absolute links and chains); a target outside the tree, a broken link or a loop gives
    None. The returned text is relative to the link's folder, so it points into the copy.
    """
    try:
        real = os.path.realpath(link, strict=True)
    except OSError:
        return None
    if real != root_real and not real.startswith(root_real.rstrip(os.sep) + os.sep):
        return None
    return os.path.relpath(real, start=os.path.realpath(link.parent))


def copy_skill_tree(source: Path, target: Path) -> list[str]:
    """Copy a skill folder without following symlinks out of it.

    Links that stay inside the skill are kept as relative links, all others are left out;
    their paths (relative to the skill) are returned. Copy errors become a `ConfigError`.
    """
    root_real = os.path.realpath(source)
    skipped: list[str] = []

    def walk(source_dir: Path, target_dir: Path, rel: str) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)
        with os.scandir(source_dir) as scan:
            entries = sorted(scan, key=lambda e: e.name)
        for entry in entries:
            rel_path = f"{rel}/{entry.name}" if rel else entry.name
            destination = target_dir / entry.name
            if entry.is_symlink():
                text = internal_link_target(Path(entry.path), root_real)
                if text is None:
                    skipped.append(rel_path)
                else:
                    os.symlink(text, destination)
            elif entry.is_dir(follow_symlinks=False):
                walk(Path(entry.path), destination, rel_path)
            elif entry.is_file(follow_symlinks=False):
                shutil.copy2(entry.path, destination)

    try:
        walk(source, target, "")
    except OSError as exc:
        raise ConfigError.single(
            "",
            f"Der Skill „{source.name}“ konnte nicht kopiert werden "
            f"({exc.strerror or exc}). Prüfe die Dateien im Skill-Ordner.",
            source=str(source),
        ) from None
    return skipped


def _managed_link(link: Path, target_real: str, skills_real: str, name: str) -> bool:
    """True if `link` is a skill link arr made: it points into the workfolder `skills/`, or
    it is broken and its text ends in `skills/<name>` (e.g. computed from a linked project)."""
    text = os.readlink(link)
    points_to = os.path.normpath(os.path.join(target_real, text))
    if os.path.dirname(points_to) == skills_real:
        return True
    parts = os.path.normpath(text).split(os.sep)
    return not link.exists() and parts[-2:] == ["skills", name]


def link_user_skills(
    project_dir: Path, skills_dir: Path, skills: Sequence[SkillInfo]
) -> tuple[list[str], list[str]]:
    """Maintain the symlinks `<project>/.claude/skills/<name>` for the user agent.

    Links are relative and computed from the resolved folders (`../../../../skills/<name>`
    inside a workfolder), so they also work when `projects/<p>` is a link to a folder elsewhere.
    Links that arr manages but are no longer wanted are removed, wrong ones are repaired. Real
    folders, files and links to other places are never touched.
    Returns the names of the links created or changed and of the entries left alone.
    """
    target_dir = project_dir / ".claude" / "skills"
    skills_real = os.path.realpath(skills_dir)
    wanted = {s.name: s for s in skills}
    changed: list[str] = []
    skipped: list[str] = []

    if not wanted and not target_dir.is_dir():
        return changed, skipped
    target_dir.mkdir(parents=True, exist_ok=True)
    target_real = os.path.realpath(target_dir)

    for entry in sorted(target_dir.iterdir(), key=lambda p: p.name):
        if (
            entry.is_symlink()
            and entry.name not in wanted
            and _managed_link(entry, target_real, skills_real, entry.name)
        ):
            entry.unlink()
            changed.append(entry.name)

    for name, skill in wanted.items():
        link = target_dir / name
        relative = os.path.relpath(os.path.join(skills_real, skill.path.name), start=target_real)
        if link.is_symlink():
            if os.readlink(link) == relative:
                continue
            if not _managed_link(link, target_real, skills_real, name):
                skipped.append(name)
                continue
            link.unlink()
        elif os.path.lexists(link):
            skipped.append(name)
            continue
        link.symlink_to(relative)
        changed.append(name)
    return changed, skipped


def snapshot_skills(skills: Sequence[SkillInfo], snapshot_dir: Path) -> list[str]:
    """Copy the given skills to `<snapshot_dir>/<name>/` (the run's `skills/` snapshot).

    Returns the symlinks left out because they lead out of their skill, as `<skill>/<path>`.
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    skipped: list[str] = []
    for skill in skills:
        skipped += [
            f"{skill.name}/{rel}" for rel in copy_skill_tree(skill.path, snapshot_dir / skill.name)
        ]
    return skipped


def write_claude_plugin(plugin_dir: Path, snapshot_dir: Path, names: Sequence[str]) -> list[str]:
    """Generate a local Claude plugin with the given skills from the run's snapshot.

    Layout (docs/spikes/S1 §2.4): `.claude-plugin/plugin.json` and `skills/<name>/`. The agent
    loads it with `plugins=[{"type": "local", "path": …}]`, `setting_sources=[]` and
    `skills=["arr:<name>", …]`. Returns those skill IDs.
    """
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": PLUGIN_NAME,
        "version": __version__,
        "description": "Skills dieses arr-Runs (generiert)",
    }
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    skills_root = plugin_dir / "skills"
    skills_root.mkdir(exist_ok=True)
    for name in names:
        copy_skill_tree(snapshot_dir / name, skills_root / name)
    return [f"{PLUGIN_NAME}:{name}" for name in names]


def install_codex_skills(work_dir: Path, snapshot_dir: Path, names: Sequence[str]) -> None:
    """Copy the given skills from the run's snapshot to `<work_dir>/.agents/skills/<name>/`."""
    target = work_dir / ".agents" / "skills"
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        copy_skill_tree(snapshot_dir / name, target / name)


# --------------------------------------------------------------------------------------------
# Start skills (W4-05)
# --------------------------------------------------------------------------------------------

START_SKILLS = (
    ARR_SKILL,
    "exploration",
    "latex-writeup",
    "proof",
    "proof-review",
    "roundtable",
    "sympy-check",
)
"""Skills arr ships (docs/03 §6): package data under `templates/skills/<name>/`; the text of
`arr` is `templates/project/arr-skill.md` (also the skill W4-01 creates for a new project)."""

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


@dataclass(frozen=True, slots=True)
class StartSkills:
    """Result of `install_start_skills`: names created and names left alone (already there)."""

    created: list[str]
    existing: list[str]


def start_skill_files(name: str) -> dict[str, bytes]:
    """The files of start skill `name` from the package, as relative POSIX path → content."""
    from importlib import resources

    templates = resources.files("arr").joinpath("templates")
    if name == ARR_SKILL:
        return {SKILL_FILE: templates.joinpath("project", "arr-skill.md").read_bytes()}
    if name not in START_SKILLS:
        raise KeyError(name)
    files: dict[str, bytes] = {}

    def walk(node: Any, prefix: str) -> None:
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            if child.name.startswith((".", "_")):
                continue  # .gitkeep, __pycache__
            if child.is_dir():
                walk(child, f"{prefix}{child.name}/")
            else:
                files[f"{prefix}{child.name}"] = child.read_bytes()

    walk(templates.joinpath("skills", name), "")
    return files


def _remove_tree(parent: int, name: str) -> None:
    """Remove a folder arr just built below `parent` (it contains no links)."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except OSError:
        return
    try:
        for entry in os.listdir(fd):
            if stat.S_ISDIR(os.stat(entry, dir_fd=fd, follow_symlinks=False).st_mode):
                _remove_tree(fd, entry)
            else:
                os.unlink(entry, dir_fd=fd)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        os.rmdir(name, dir_fd=parent)


def _write_tree(folder: int, files: dict[str, bytes]) -> None:
    """Create `files` below the open, new `folder`: new folders and files only, no links."""
    for rel, data in files.items():
        *dirs, filename = rel.split("/")
        current = folder
        opened: list[int] = []
        try:
            for part in dirs:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o755, dir_fd=current)
                current = os.open(part, _DIR_FLAGS, dir_fd=current)
                opened.append(current)
            fd = os.open(filename, _NEW_FILE_FLAGS, 0o644, dir_fd=current)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
            finally:
                os.close(fd)
        finally:
            for fd in opened:
                os.close(fd)


def _exists(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _install_one(root: int, name: str) -> bool:
    """Create `skills/<name>` from the package unless something is there; True if created.

    Like the editor of W4-02: built in a hidden folder next to the others, renamed at the end,
    no link followed; on failure nothing stays behind.
    """
    if _exists(root, name):
        return False
    files = start_skill_files(name)
    temp = f".{name}.arr-{secrets.token_hex(6)}.tmp"
    os.mkdir(temp, 0o755, dir_fd=root)
    try:
        folder = os.open(temp, _DIR_FLAGS, dir_fd=root)
        try:
            _write_tree(folder, files)
        finally:
            os.close(folder)
        if _exists(root, name):
            _remove_tree(root, temp)
            return False
        try:
            os.rename(temp, name, src_dir_fd=root, dst_dir_fd=root)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY, errno.EISDIR, errno.ENOTDIR):
                _remove_tree(root, temp)
                return False  # appeared meanwhile: never replaced
            raise
    except BaseException:
        _remove_tree(root, temp)
        raise
    return True


SKILLS_LINK = (
    f"Der Ordner {SKILLS_DIR} im Workfolder ist ein Link oder kein Ordner. arr folgt dort keinen "
    "Links. Ersetze ihn durch einen echten Ordner."
)


def _open_skills_root(workfolder: str | Path) -> int:
    """Descriptor of `<workfolder>/skills` (created if missing); it must not be a link."""
    try:
        base = os.open(Path(workfolder), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise SkillFileError(f"Der Workfolder ist nicht lesbar ({exc.strerror or exc}).") from None
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(SKILLS_DIR, 0o755, dir_fd=base)
        return os.open(SKILLS_DIR, _DIR_FLAGS, dir_fd=base)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
            raise SkillFileError(SKILLS_LINK) from None
        raise SkillFileError(
            f"Der Ordner {SKILLS_DIR} ließ sich nicht öffnen ({exc.strerror or exc})."
        ) from None
    finally:
        os.close(base)


def install_start_skills(
    workfolder: str | Path, names: Sequence[str] = START_SKILLS
) -> StartSkills:
    """Create the missing start skills `names` in `<workfolder>/skills/` (W4-05).

    A skill folder (or file or link) with the same name is never overwritten or changed, also
    when it is broken; it is reported in `existing`. `skills/` itself must not be a link; it is
    created if missing. Running it again changes nothing. Raises `SkillFileError` with a German
    message if the folder cannot be written.

    Accepted (as in W4-02): an empty folder that appears with the same name between the last
    check and `rename` is replaced (POSIX `rename`); nothing is lost.
    """
    created: list[str] = []
    existing: list[str] = []
    root = _open_skills_root(workfolder)
    try:
        for name in names:
            try:
                made = _install_one(root, name)
            except OSError as exc:
                raise SkillFileError(
                    f"Der Start-Skill „{name}“ ließ sich nicht anlegen ({exc.strerror or exc}). "
                    "Prüfe die Rechte im Ordner skills."
                ) from None
            (created if made else existing).append(name)
    finally:
        os.close(root)
    if created:
        log.info("Start-Skills angelegt: %s", ", ".join(created))
    return StartSkills(created=created, existing=existing)


def install_start_skill(workfolder: str | Path, name: str) -> bool:
    """Create the one start skill `name` unless something is there; True if created."""
    return name in install_start_skills(workfolder, (name,)).created


def start_skills_message(created: Sequence[str], existing: Sequence[str]) -> str:
    """German sentence about `install_start_skills` for the GUI and the API."""
    parts = []
    if created:
        parts.append(f"Start-Skills angelegt: {', '.join(created)}.")
    if existing and created:
        parts.append(f"Schon vorhanden und nicht verändert: {', '.join(existing)}.")
    elif existing:
        parts.append(
            f"Alle Start-Skills sind schon da ({', '.join(existing)}). arr hat nichts verändert."
        )
    return " ".join(parts)


# --------------------------------------------------------------------------------------------
# Updating unchanged start skills (W4b-06a, B1 of W4-06)
# --------------------------------------------------------------------------------------------


def tree_digest(files: dict[str, bytes]) -> str:
    """Checksum of a skill tree: sorted relative POSIX paths with their contents (the files of
    `start_skill_files`; names starting with `.` or `_` are not part of a tree)."""
    digest = hashlib.sha256()
    for rel in sorted(files):
        data = files[rel]
        path = rel.encode("utf-8")
        digest.update(len(path).to_bytes(8, "big") + path + len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


CURRENT_DIGESTS: dict[str, tuple[str, str]] = {
    ARR_SKILL: ("c82f909", "ef12554041a5820b4665cf46c5b70e9c7d7035175f432bf2276a371c3d2557f7"),
    "exploration": (
        "W4b-06a.3",
        "b7f69ed596b701188bc34a9c8f2e2148843f03cebf9703a67c161b30c65e4d15",
    ),
    "latex-writeup": (
        "81cb75e",
        "44dcbbcbe1a996f725eb9651dd08fc636317eb1d2db965349471d73f957d1f36",
    ),
    "proof": ("a3da52c", "64fa3add21db893a9b036f1bd551c4c65c817adc1beabcdf1de2aa5b2439c7af"),
    "proof-review": (
        "W4b-06a.3",
        "9bf889c9ed0160186c33f9c708a461da2bbe4aa0cfa60d903e3f980f3dcf31b8",
    ),
    "roundtable": ("a3da52c", "d30453152f47d9dafc6937a9710ddcd7aea61a861cfdbefbff599f303c21cd2f"),
    "sympy-check": (
        "1fccb85",
        "324a9f2a829f7c2d99a48935685fcd14fb70d67107713f07a8c2a8d396c1a342",
    ),
}
"""The version of each start skill this arr ships: (where it came from, `tree_digest` of
`start_skill_files`). `tests/test_templates_update.py` holds the checksums: whoever changes a
start skill moves the old entry into `EARLIER_DIGESTS` and enters the new one here."""

EARLIER_DIGESTS: dict[str, tuple[tuple[str, str], ...]] = {
    ARR_SKILL: (
        ("81cb75e", "1e6af18c02255b66e4341b2fab99056ca23f2be2454b6b6c068f82489df93d0e"),
        ("fe2b0aa", "7bdf5f9ad35848d60c125d753211a0f46bb13a29c97e209a5261ce74790ed791"),
    ),
    "exploration": (
        ("81cb75e", "1e75c86906f2aa5df9ad048ec99061b7277e4adaf00e243bad6cbc00fb093fbc"),
        ("fe2b0aa", "3e2b880fe8767073315bd617b94bf0d8b1ecbc5caa0352622756210ccb5772c1"),
        ("5676589", "f764620e80d26be632aebf15d200c6969166b8332962b62c1185db79a4a49c25"),
    ),
    "latex-writeup": (),
    "proof": (
        ("81cb75e", "fdff9f91315ee4bf859a0430734b3ca57b6c69cb44db2b1cd57852d5e141c634"),
        ("fe2b0aa", "e119ffc82f7c1a0d1e573b81fc91897f6faccbb9a73fab67aa6cc779e4a7d006"),
        ("5676589", "f4e5a3693120877bd34f5f693aa71dc6862680cff7bb795cf5860aa77a4110d4"),
    ),
    "proof-review": (
        ("81cb75e", "16d762f430ac361593eea3d330b18f02297ddc3c5c2639a0e0712ac823647a23"),
        ("fe2b0aa", "bd11f3df4eac44d744a92dbb084c918c9cbb4e8fdb2ac9ffcdd9f52402477866"),
        ("5676589", "c2f3f09a0f31bddee83a3ee35b2c949393ffcbf4c0849caebfc146c9286a5cd0"),
        ("f12039f", "844329d6889123651a9ea400a1a523162695ff3fb6fd1842be0c7ea7fbf09891"),
    ),
    "roundtable": (
        ("81cb75e", "b9c0ebd140a928e733c69d5108cfd3d0aa7dda597106023b538ced2ab2d2d772"),
        ("fe2b0aa", "b2ada940d002bea12fa8443587a395bbe9225012bb4781ec9e704bbc776977d1"),
        ("5676589", "b9584cdb638802d17c1395cb3bdde4890d925f02201f242f87327401a9e9a387"),
    ),
    "sympy-check": (
        ("81cb75e", "9f1d68ac74189e3923a4b0b2f6e769ea2e55cc1646a96cc4bc7b68a9ccb949bc"),
        ("fe2b0aa", "118aff87e23bf5a05ff80297a20305f1a897a81c825664768bd84f59353bc4cf"),
        ("5676589", "e77e972f1a6ebd9f15172c097e830af41798c4ee5a34e612a3bd78667c29619c"),
        ("34b7a1f", "2516e120b0a79bc6176d28c3437841fd605d8bfd56b04e0459293794b6ffec63"),
    ),
}
"""Every earlier version of each start skill arr shipped since W4-05 (`81cb75e`): (commit,
checksum), computed once from the Git history of `src/arr/templates/skills/<name>/` and
`src/arr/templates/project/arr-skill.md` (each commit that changed them; the same checksum
counts once, with its first commit). A folder equal to one of them was never changed by the
user and is replaced by `update_start_skills`."""


class SkillUpdate(StrEnum):
    """What `update_start_skills` did with a start skill."""

    UPDATED = "updated"
    """It was an earlier version of arr; now it is the current one."""
    CURRENT = "current"
    """It is the current version already."""
    CHANGED = "changed"
    """It matches no version arr shipped (changed by the user, own files beside the shipped
    ones, or not a plain folder): left as it is."""
    MISSING = "missing"
    """There is none; it is not created (that is „Start-Skills anlegen“)."""
    FAILED = "failed"
    """A read or write failed; the start skill is as before (`StartSkillUpdate.note` says)."""


@dataclass(frozen=True, slots=True)
class StartSkillUpdate:
    """The result for one start skill. `note`: with `UPDATED` the hidden folder of the old
    version that could not be removed (empty if none), with `FAILED` the German reason."""

    result: SkillUpdate
    note: str = ""


@dataclass(frozen=True, slots=True)
class StartSkillsUpdate:
    """Result of `update_start_skills`: one entry per start skill, in the order asked."""

    skills: dict[str, StartSkillUpdate]

    @property
    def results(self) -> dict[str, SkillUpdate]:
        return {name: item.result for name, item in self.skills.items()}

    @property
    def failed(self) -> bool:
        return any(item.result is SkillUpdate.FAILED for item in self.skills.values())


_UPDATE_TEXT = {
    SkillUpdate.UPDATED: "aktualisiert",
    SkillUpdate.CURRENT: "schon aktuell",
    SkillUpdate.CHANGED: "von dir geändert, bleibt unverändert",
    SkillUpdate.MISSING: "fehlt, wird nicht angelegt (das geht über „Start-Skills anlegen“)",
}

IGNORED_NAMES = frozenset({".DS_Store", "__pycache__"})
"""Names below a start skill that `update_start_skills` neither compares nor keeps (Finder
and Python leave them). Every other name starting with `.` or `_` is a file of the user: the
skill then counts as changed and is never replaced (W4b-06a.1, H1)."""


class _Irregular(Exception):
    """A link, a file instead of a folder, anything but plain files and folders, or an own
    file of the user (a name starting with `.` or `_`, except `IGNORED_NAMES`)."""


class _OldKept(OSError):
    """The swap failed and the old version could not be put back: it lies under `kept`."""

    def __init__(self, cause: OSError, kept: str) -> None:
        super().__init__(cause.errno, cause.strerror or str(cause))
        self.kept = kept


def _read_tree(parent: int, name: str) -> dict[str, bytes] | None:
    """The files below `parent/name`, read through descriptors without following any link;
    None if there is nothing. Raises `_Irregular` for anything but plain files and folders
    (links included), for an own file of the user and for a tree too big to be a skill."""
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise _Irregular(name)
    files: dict[str, bytes] = {}
    total = 0

    def walk(folder: int, prefix: str) -> None:
        nonlocal total
        for entry in sorted(os.listdir(folder)):
            if entry in IGNORED_NAMES:
                continue
            if entry.startswith((".", "_")):
                raise _Irregular(entry)  # arr ships no such name: the user's own file
            mode = os.stat(entry, dir_fd=folder, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                sub = os.open(entry, _DIR_FLAGS, dir_fd=folder)
                try:
                    walk(sub, f"{prefix}{entry}/")
                finally:
                    os.close(sub)
            elif stat.S_ISREG(mode):
                fd = os.open(
                    entry, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=folder
                )
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise _Irregular(entry)
                    chunks = []
                    while chunk := os.read(fd, 1 << 16):
                        total += len(chunk)
                        if total > 8 * MAX_SKILL_FILE_BYTES:
                            raise _Irregular(entry)  # no start skill is this big
                        chunks.append(chunk)
                finally:
                    os.close(fd)
                files[f"{prefix}{entry}"] = b"".join(chunks)
            else:
                raise _Irregular(entry)

    try:
        folder = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise _Irregular(name) from None
        raise
    try:
        walk(folder, "")
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise _Irregular(name) from None
        raise
    finally:
        os.close(folder)
    return files


def _digest_in(root: int, name: str) -> str | None:
    """`tree_digest` of `skills/<name>`; None if missing; `""` if it is no plain tree."""
    try:
        files = _read_tree(root, name)
    except _Irregular:
        return ""
    return None if files is None else tree_digest(files)


def _swap(root: int, name: str, new: str) -> str:
    """Put the folder `new` (hidden, below `root`) in place of `name`; the old one goes to a
    hidden name first and is removed after. On a failure of the swap the old one is back in
    place (`_OldKept` if even that fails). Returns the hidden name of the old version if it
    could not be removed after the swap (W4b-06a.1, M3), else `""`."""
    old = f".{name}.arr-{secrets.token_hex(6)}.old"
    os.rename(name, old, src_dir_fd=root, dst_dir_fd=root)
    try:
        os.rename(new, name, src_dir_fd=root, dst_dir_fd=root)
    except BaseException as exc:  # also Ctrl-C between the two renames (W4b-06a.3, NP4)
        try:
            os.rename(old, name, src_dir_fd=root, dst_dir_fd=root)
        except OSError:
            log.error("Start-Skill %s: alte Fassung liegt unter skills/%s", name, old)
            if isinstance(exc, OSError):
                raise _OldKept(exc, old) from None
        raise
    try:
        _remove_tree(root, old)
    except OSError:
        log.warning("Start-Skill %s: alte Fassung bleibt unter skills/%s", name, old)
    return old if _exists(root, old) else ""


def _update_one(root: int, name: str) -> StartSkillUpdate:
    found = _digest_in(root, name)
    if found is None:
        return StartSkillUpdate(SkillUpdate.MISSING)
    files = start_skill_files(name)
    if found == tree_digest(files):
        return StartSkillUpdate(SkillUpdate.CURRENT)
    shipped = {digest for _commit, digest in EARLIER_DIGESTS.get(name, ())}
    if found not in shipped:
        return StartSkillUpdate(SkillUpdate.CHANGED)
    temp = f".{name}.arr-{secrets.token_hex(6)}.tmp"
    os.mkdir(temp, 0o755, dir_fd=root)
    try:
        folder = os.open(temp, _DIR_FLAGS, dir_fd=root)
        try:
            _write_tree(folder, files)
        finally:
            os.close(folder)
        if _digest_in(root, name) != found:  # changed meanwhile: never replaced
            _remove_tree(root, temp)
            return StartSkillUpdate(SkillUpdate.CHANGED)
        kept = _swap(root, name, temp)
    except BaseException:
        _remove_tree(root, temp)
        raise
    return StartSkillUpdate(SkillUpdate.UPDATED, kept)


def _existing_skills_root(workfolder: str | Path) -> int | None:
    """Descriptor of `<workfolder>/skills`, None if there is none (updating creates nothing,
    W4b-06a.1 N2); it must not be a link."""
    try:
        base = os.open(Path(workfolder), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise SkillFileError(f"Der Workfolder ist nicht lesbar ({exc.strerror or exc}).") from None
    try:
        return os.open(SKILLS_DIR, _DIR_FLAGS, dir_fd=base)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
            raise SkillFileError(SKILLS_LINK) from None
        raise SkillFileError(
            f"Der Ordner {SKILLS_DIR} ließ sich nicht öffnen ({exc.strerror or exc})."
        ) from None
    finally:
        os.close(base)


def update_start_skills(
    workfolder: str | Path, names: Sequence[str] = START_SKILLS
) -> StartSkillsUpdate:
    """Bring unchanged start skills in `<workfolder>/skills/` to the version of this arr.

    A start skill equal byte for byte to an earlier version arr shipped (`EARLIER_DIGESTS`) is
    replaced by the current one: built in a hidden folder next to it, its checksum read once
    more right before the swap, no link followed; on a failure the old version stays complete.
    One that matches no shipped version (changed by the user, an own file with `.` or `_`, a
    link, a file) is never touched; a missing one is not created, nor is `skills/`. A failure
    at one start skill is reported for it and the others are still done (W4b-06a.1 N1).
    Raises `SkillFileError` with a German message for a name that is no start skill (before
    reading anything) or a `skills/` that cannot be opened.
    """
    unknown = [n for n in names if n not in START_SKILLS]
    if unknown:
        raise SkillFileError(f"„{unknown[0]}“ ist kein Start-Skill.")
    root = _existing_skills_root(workfolder)
    if root is None:
        return StartSkillsUpdate({n: StartSkillUpdate(SkillUpdate.MISSING) for n in names})
    out: dict[str, StartSkillUpdate] = {}
    try:
        for name in names:
            try:
                out[name] = _update_one(root, name)
            except _OldKept as exc:
                out[name] = StartSkillUpdate(
                    SkillUpdate.FAILED,
                    f"ließ sich nicht aktualisieren ({exc.strerror}). Die bisherige Fassung liegt "
                    f"unter {SKILLS_DIR}/{exc.kept}; benenne sie zurück in „{name}“.",
                )
            except OSError as exc:
                out[name] = StartSkillUpdate(
                    SkillUpdate.FAILED,
                    f"ließ sich nicht aktualisieren ({exc.strerror or exc}). Die bisherige "
                    "Fassung ist unverändert.",
                )
    finally:
        os.close(root)
    updated = [n for n, r in out.items() if r.result is SkillUpdate.UPDATED]
    if updated:
        log.info("Start-Skills aktualisiert: %s", ", ".join(updated))
    return StartSkillsUpdate(out)


def update_lines(update: StartSkillsUpdate) -> list[str]:
    """One German line per start skill for `arr skills update`."""
    lines = []
    for name, item in update.skills.items():
        if item.result is SkillUpdate.FAILED:
            lines.append(f"{name}: {item.note}")
        elif item.result is SkillUpdate.UPDATED and item.note:
            lines.append(
                f"{name}: aktualisiert; die alte Fassung liegt noch unter "
                f"{SKILLS_DIR}/{item.note}, lösche sie selbst."
            )
        else:
            lines.append(f"{name}: {_UPDATE_TEXT[item.result]}")
    return lines
