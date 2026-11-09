"""Skill registry: discover and validate skills in the workfolder, decide who gets which skill,
and install skills as `.claude/skills` links (user agent), a generated Claude plugin or
`.agents/skills` (research agents).

A skill is a folder with a `SKILL.md` (frontmatter `name`, `description`, optional
`metadata.arr-audience`); a `group.yaml` next to it makes it a group skill.
See docs/03-skills-und-research-groups.md §1-3 and docs/spikes/S1_claude.md §3.1.
"""

import difflib
import json
import os
import re
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
from arr.resolve import validate_model

__all__ = [
    "ARR_SKILL",
    "PLUGIN_NAME",
    "SkillAudience",
    "SkillCatalog",
    "SkillFileError",
    "SkillInfo",
    "check_research_skills",
    "copy_skill_tree",
    "discover_skills",
    "install_codex_skills",
    "internal_link_target",
    "link_user_skills",
    "load_skill",
    "read_text_file",
    "skills_for_research_agent",
    "skills_for_user_agent",
    "snapshot_skills",
    "write_claude_plugin",
]

SKILL_FILE = "SKILL.md"
GROUP_FILE = "group.yaml"
SKILLS_DIR = "skills"
"""Folder of the skills in the workfolder; messages name it instead of an absolute path."""
MAX_SKILL_FILE_BYTES = 256 * 1024
"""Largest skill file arr reads or writes (SKILL.md, group.yaml, roles; W4-02)."""
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
