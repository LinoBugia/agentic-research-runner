"""Create and validate the workfolder layout, manage projects and the project template.

Templates are package data under `arr/templates/` and are read through `importlib.resources`,
so everything works from an installed package as well.
See docs/02-workfolder-und-runs.md §1-2.
"""

import errno
import io
import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any

from pydantic import BaseModel
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from arr.errors import ArrError
from arr.models import ProjectSettings, WorkfolderSettings
from arr.resolve import load_yaml_model
from arr.skills import (
    SkillFileError,
    discover_skills,
    install_start_skills,
    link_user_skills,
    skills_for_user_agent,
)

__all__ = [
    "REMOVE",
    "ProjectSetup",
    "block_span",
    "create_project",
    "init_workfolder",
    "is_workfolder",
    "load_project_settings",
    "load_workfolder_settings",
    "os_reason",
    "path_within",
    "project_dir",
    "read_template",
    "replace_block",
    "set_setting",
    "setup_project",
    "update_settings_file",
]

SETTINGS_FILE = "arr.yaml"
PROJECT_SETTINGS_FILE = "project.yaml"
USER_AGENT_BLOCK = "user-agent"
RESEARCH_AGENT_BLOCK = "research-agent"

_PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_WORKFOLDER_DIRS = ("projects", "skills", "runs", ".arr", ".arr/logs")
_WORKFOLDER_FILES = (("arr.yaml", SETTINGS_FILE), ("README.md", "README.md"))
_PROJECT_FOLDERS = ("notes", "tex", "code", "results")

_OS_REASONS = {
    errno.EACCES: "keine Berechtigung",
    errno.EPERM: "keine Berechtigung",
    errno.EEXIST: "das gibt es schon",
    errno.EIO: "Fehler beim Zugriff auf den Datenträger",
    errno.EISDIR: "das ist ein Ordner",
    errno.ELOOP: "zu viele symbolische Links",
    errno.ENAMETOOLONG: "der Name ist zu lang",
    errno.ENOENT: "das gibt es nicht",
    errno.ENOSPC: "kein Speicherplatz frei",
    errno.ENOTDIR: "ein Teil des Pfads ist kein Ordner",
    errno.EROFS: "der Datenträger ist schreibgeschützt",
}

REMOVE = object()
"""Value for `set_setting`: take the key out of `arr.yaml`."""


def os_reason(exc: OSError) -> str:
    """The cause of an `OSError` in German; `strerror` is English."""
    return _OS_REASONS.get(exc.errno or 0, f"Systemfehler {exc.errno}")


def read_template(*parts: str) -> str:
    """Return the text of a template from the installed package, e.g. `("project", "CLAUDE.md")`."""
    node = resources.files("arr").joinpath("templates", *parts)
    return node.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------------
# Workfolder
# --------------------------------------------------------------------------------------------


def is_workfolder(path: str | Path) -> bool:
    """True if `path` holds an `arr.yaml`, i.e. was created by `arr init`."""
    return (Path(path) / SETTINGS_FILE).is_file()


def init_workfolder(path: Path) -> Path:
    """Create the workfolder layout at `path` (idempotent) and return its absolute path.

    Writes `arr.yaml` and `README.md` and creates `projects/`, `skills/`, `runs/` and `.arr/`
    (docs/02 §1). A new workfolder (no `arr.yaml` yet) also gets the start skills (W4-05).
    Existing files and folders are never overwritten or changed.
    """
    root = Path(path).expanduser().absolute()
    if root.exists() and not root.is_dir():
        raise ArrError(
            f"„{root}“ ist kein Ordner. Gib einen Ordner für den Workfolder an, z. B. ~/arr."
        )
    new = not os.path.lexists(root / SETTINGS_FILE)
    try:
        for sub in _WORKFOLDER_DIRS:
            (root / sub).mkdir(parents=True, exist_ok=True)
        for target, template in _WORKFOLDER_FILES:
            _write_new(root / target, read_template("workfolder", template))
    except PermissionError as exc:
        raise ArrError(
            f"Kein Schreibrecht für „{exc.filename or root}“. Wähle einen Ordner, in dem du "
            "schreiben darfst, z. B. ~/arr."
        ) from None
    except OSError as exc:
        raise ArrError(
            f"Der Workfolder „{root}“ ließ sich nicht anlegen ({os_reason(exc)}). Prüfe die "
            "Rechte oder wähle einen anderen Ordner."
        ) from None
    if new:
        try:
            install_start_skills(root)
        except SkillFileError as exc:
            raise ArrError(f"Die Start-Skills ließen sich nicht anlegen: {exc.message}") from None
    return root


def _write_new(path: Path, text: str) -> bool:
    """Write `text` to `path` unless something (even a broken link) is already there."""
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        return False
    return True


def _not_utf8(file: Path) -> ArrError:
    return ArrError(
        f"Die Datei {file} ist nicht in UTF-8 gespeichert, deshalb kann arr sie nicht lesen. "
        "Speichere sie im Editor als UTF-8."
    )


def _load_settings[M: BaseModel](file: Path, model: type[M]) -> M:
    try:
        return load_yaml_model(file, model)
    except UnicodeDecodeError:
        raise _not_utf8(file) from None


def load_workfolder_settings(workfolder: str | Path) -> WorkfolderSettings:
    """Read and validate `<workfolder>/arr.yaml`; raise `ArrError`/`ConfigError` with a hint."""
    root = Path(workfolder)
    if not is_workfolder(root):
        raise ArrError(
            f"„{root}“ ist kein arr-Workfolder, die Datei {SETTINGS_FILE} fehlt. "
            f"Lege ihn mit „arr init {root}“ an."
        )
    return _load_settings(root / SETTINGS_FILE, WorkfolderSettings)


def path_within(path: Path, parent: Path) -> bool:
    """True if `path` is `parent` or lies below it; compare resolved paths."""
    key, parent_key = str(path), str(parent)
    if sys.platform == "darwin":  # case-insensitive file system
        key, parent_key = key.casefold(), parent_key.casefold()
    return key == parent_key or key.startswith(parent_key.rstrip("/") + "/")


def set_setting(data: Any, keys: Sequence[str], value: Any) -> None:
    """Set the value at `keys` in round-trip YAML data, or remove it with `REMOVE`.

    Comments stay: a comment after the last key of a section belongs to what follows, so a
    new key in a section goes in front of that last key. New top-level keys go to the end.
    """

    def put(node: Any, key: str, item: Any) -> None:
        if key in node or node is data:
            node[key] = item
        else:
            node.insert(max(len(node) - 1, 0), key, item)

    node = data
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            if value is REMOVE:
                return
            child = CommentedMap()
            put(node, key, child)
        node = child
    if value is REMOVE:
        node.pop(keys[-1], None)
    else:
        put(node, keys[-1], value)


def update_settings_file(workfolder: str | Path, edit: Callable[[Any], None]) -> WorkfolderSettings:
    """Change `arr.yaml` with `edit` on its round-trip data and return the new settings.

    Comments, the order of the keys and all values stay, but the round trip rewrites the
    whole file in one form: `---` goes, lists get indented, `{ a: b }` becomes `{a: b}`. The
    result is validated before it replaces the file (atomically), so a refused change leaves
    `arr.yaml` as it was. A write-protected file stays too: `os.replace` needs only the rights
    of the folder, so the check is here. Raises `ArrError`/`ConfigError` with a hint.
    """
    path = Path(workfolder) / SETTINGS_FILE
    load_workfolder_settings(workfolder)
    target = path.resolve()
    if not os.access(target, os.W_OK):
        raise ArrError(
            f"Die Datei {path} ist schreibgeschützt, deshalb ändert arr sie nicht. Hebe den "
            "Schreibschutz auf und speichere erneut."
        )
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    data = yaml.load(target.read_text(encoding="utf-8"))
    edit(data)
    buffer = io.StringIO()
    yaml.dump(data, buffer)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(buffer.getvalue(), encoding="utf-8")
        shutil.copymode(target, temp)
        settings = load_yaml_model(temp, WorkfolderSettings, source=str(path))
        os.replace(temp, target)
    except OSError as exc:
        raise ArrError(
            f"Die Datei {path} ließ sich nicht schreiben ({os_reason(exc)}). Prüfe die Rechte "
            "des Ordners."
        ) from None
    finally:
        temp.unlink(missing_ok=True)
    return settings


def load_project_settings(project: str | Path) -> ProjectSettings:
    """Read `<project>/project.yaml`; without that optional file the defaults apply."""
    file = Path(project) / PROJECT_SETTINGS_FILE
    if not file.is_file():
        return ProjectSettings()
    return _load_settings(file, ProjectSettings)


def project_dir(workfolder: str | Path, name: str) -> Path:
    """Return the folder of an existing project; raise `ArrError` for odd or unknown names."""
    if not name or name.startswith(".") or "/" in name or "\\" in name or name != name.strip():
        raise ArrError(
            f"Ungültiger Projektname „{name}“. Gib nur den Ordnernamen unter projects/ an."
        )
    folder = Path(workfolder) / "projects" / name
    if not folder.is_dir():
        raise ArrError(
            f"Das Projekt „{name}“ gibt es nicht ({folder}). Lege es an oder prüfe den Namen."
        )
    return folder


# --------------------------------------------------------------------------------------------
# Marked blocks in CLAUDE.md (E10)
# --------------------------------------------------------------------------------------------


def _marks(kind: str) -> tuple[str, str]:
    return f"<!-- arr:{kind} -->", f"<!-- /arr:{kind} -->"


def block_span(text: str, kind: str) -> tuple[int, int] | None:
    """Return start and end index of the block `<!-- arr:KIND -->…<!-- /arr:KIND -->`.

    The end includes the closing mark. None if there is no block; `ArrError` if a mark is
    missing its partner.
    """
    opening, closing = _marks(kind)
    start = text.find(opening)
    if start < 0:
        if closing in text:
            raise ArrError(
                f"In der CLAUDE.md steht „{closing}“ ohne öffnendes „{opening}“. "
                "Ergänze die fehlende Marke oder entferne die andere."
            )
        return None
    end = text.find(closing, start + len(opening))
    if end < 0:
        raise ArrError(
            f"In der CLAUDE.md fehlt „{closing}“ zu „{opening}“. Ergänze die schließende Marke."
        )
    return start, end + len(closing)


def replace_block(text: str, kind: str, replacement: str) -> str:
    """Replace the block of this kind by `replacement`; append it if there is none.

    A second block of the same kind is removed, so exactly one `replacement` remains.
    """
    span = block_span(text, kind)
    if span is None:
        if not text.strip():
            return replacement.rstrip("\n") + "\n"
        return text.rstrip("\n") + "\n\n" + replacement.rstrip("\n") + "\n"
    head, tail = text[: span[0]], text[span[1] :]
    while (extra := block_span(tail, kind)) is not None:
        tail = tail[: extra[0]].rstrip(" \t") + tail[extra[1] :].lstrip("\n")
    return head + replacement.rstrip("\n") + tail


def _template_block(template_text: str, kind: str) -> str:
    span = block_span(template_text, kind)
    assert span is not None, f"template without {kind} block"
    return template_text[span[0] : span[1]]


# --------------------------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class ProjectSetup:
    """What `setup_project` did, as paths relative to the project folder."""

    path: Path
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    skill_links: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def create_project(workfolder: str | Path, name: str, *, title: str | None = None) -> Path:
    """Create `projects/<name>/` from the project template and return its folder.

    `name` is a lowercase slug (letters, digits, `-`, `_`), `title` goes into `CLAUDE.md`
    (default: the name). The recommended folders of docs/02 §2 are created too. An existing
    project is never touched: use `setup_project` to bring it up to date.
    """
    root = Path(workfolder)
    if not _PROJECT_NAME_RE.fullmatch(name):
        raise ArrError(
            f"Ungültiger Projektname „{name}“. Erlaubt sind Kleinbuchstaben, Ziffern, „-“ und "
            "„_“ (höchstens 64 Zeichen), z. B. „collatz“."
        )
    if not is_workfolder(root):
        raise ArrError(
            f"„{root}“ ist kein arr-Workfolder, die Datei {SETTINGS_FILE} fehlt. "
            f"Lege ihn mit „arr init {root}“ an."
        )
    folder = root / "projects" / name
    if folder.exists():
        raise ArrError(
            f"Das Projekt „{name}“ gibt es schon ({folder}). Wähle einen anderen Namen oder "
            "richte das vorhandene Projekt ein."
        )
    folder.mkdir(parents=True)
    for sub in _PROJECT_FOLDERS:
        (folder / sub).mkdir()
    setup_project(folder, workfolder=root, title=title or name)
    return folder


def _find_workfolder(project: Path, given: str | Path | None) -> Path:
    root = Path(given).expanduser().absolute() if given is not None else project.parent.parent
    if not is_workfolder(root):
        raise ArrError(
            f"Das Projekt „{project.name}“ liegt in keinem arr-Workfolder (gesucht: {root}). "
            "Verschiebe es nach <workfolder>/projects/ oder gib den Workfolder an."
        )
    return root


def arr_command() -> str:
    """The `arr` of this installation, absolute: the script next to the interpreter, else the
    one on `PATH`, else plain `arr` (W4-01: `.mcp.json` names the path, never a token)."""
    import shutil
    import sys

    beside = Path(sys.executable).with_name("arr")
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    found = shutil.which("arr")
    return str(Path(found).absolute()) if found else "arr"


def mcp_entry() -> dict[str, Any]:
    """The server `arr` of a project's `.mcp.json`: `arr mcp` of this installation (W4-01)."""
    server: dict[str, Any] = json.loads(read_template("project", ".mcp.json"))["mcpServers"]["arr"]
    server["command"] = arr_command()
    return server


def _mcp_text() -> str:
    return json.dumps({"mcpServers": {"arr": mcp_entry()}}, ensure_ascii=False, indent=2) + "\n"


def _arr_server_stale(server: Any) -> bool:
    """An `arr` server arr wrote itself (`arr mcp`) that names another `arr` than this one."""
    if not isinstance(server, dict) or server.get("args") != ["mcp"]:
        return False
    command = str(server.get("command") or "")
    return Path(command).name == "arr" and command != arr_command()


def _arr_skill_placeholder(root: Path) -> bool:
    """Create `skills/arr/SKILL.md` from the template if the workfolder has no skill `arr`."""
    folder = root / "skills" / "arr"
    if folder.exists() or folder.is_symlink():
        return False
    folder.mkdir(parents=True)
    return _write_new(folder / "SKILL.md", read_template("project", "arr-skill.md"))


def setup_project(
    path: Path, *, workfolder: str | Path | None = None, title: str | None = None
) -> ProjectSetup:
    """Bring a project folder up to the arr template without overwriting anything of yours.

    - `CLAUDE.md`: created from the template; an existing one gets the `arr:user-agent` block
      appended if it has none.
    - `.mcp.json`: created; an existing one gets the `arr` server added, other servers stay.
    - `project.yaml`, `AGENTS.md` (a pointer to `CLAUDE.md`): created if missing, else left alone.
    - `.claude/skills/`: relative symlinks to the workfolder skills offered to the user agent.

    The workfolder is `<project>/../..` unless given. Running it again changes nothing.
    """
    project = Path(path).expanduser().absolute()
    if not project.is_dir():
        raise ArrError(f"Der Projektordner „{project}“ existiert nicht.")
    root = _find_workfolder(project, workfolder)
    result = ProjectSetup(path=project)

    # Read everything that can fail before changing anything.
    settings = load_project_settings(project)
    catalog = discover_skills(root / "skills")
    user_skills = skills_for_user_agent(catalog, settings)
    mcp_file = project / ".mcp.json"
    mcp_data: dict[str, Any] | None = None
    if mcp_file.is_file():
        try:
            mcp_data = json.loads(mcp_file.read_text(encoding="utf-8"))
            servers = mcp_data.get("mcpServers", {})
            if not isinstance(servers, dict):
                raise ValueError
        except (ValueError, AttributeError):
            raise ArrError(
                f"Die Datei {mcp_file} ist kein gültiges JSON mit „mcpServers“. Korrigiere sie "
                "oder benenne sie um, dann kann arr den Server „arr“ eintragen."
            ) from None
    claude_file = project / "CLAUDE.md"
    try:
        claude_text = claude_file.read_text(encoding="utf-8") if claude_file.is_file() else None
    except UnicodeDecodeError:
        raise _not_utf8(claude_file) from None
    if claude_text is not None:
        block_span(claude_text, USER_AGENT_BLOCK)  # raises for a lone mark

    # CLAUDE.md
    claude_template = read_template("project", "CLAUDE.md")
    if claude_text is None:
        if _write_new(
            claude_file, Template(claude_template).safe_substitute(projekt=title or project.name)
        ):
            result.created.append("CLAUDE.md")
        else:
            result.skipped.append("CLAUDE.md")
    elif block_span(claude_text, USER_AGENT_BLOCK) is None:
        block = _template_block(claude_template, USER_AGENT_BLOCK)
        claude_file.write_text(replace_block(claude_text, USER_AGENT_BLOCK, block), "utf-8")
        result.updated.append("CLAUDE.md")
    else:
        result.skipped.append("CLAUDE.md")

    # .mcp.json
    servers = mcp_data.get("mcpServers", {}) if mcp_data is not None else {}
    if mcp_data is None:
        if _write_new(mcp_file, _mcp_text()):
            result.created.append(".mcp.json")
        else:
            result.skipped.append(".mcp.json")
    elif "arr" in servers and not _arr_server_stale(servers["arr"]):
        result.skipped.append(".mcp.json")
    else:
        mcp_data.setdefault("mcpServers", {})["arr"] = mcp_entry()
        mcp_file.write_text(json.dumps(mcp_data, ensure_ascii=False, indent=2) + "\n", "utf-8")
        result.updated.append(".mcp.json")

    # project.yaml and AGENTS.md
    for name, template in (
        (PROJECT_SETTINGS_FILE, "project.yaml"),
        ("AGENTS.md", "AGENTS.md"),
    ):
        if _write_new(project / name, read_template("project", template)):
            result.created.append(name)
        else:
            result.skipped.append(name)

    # Skill `arr`: a placeholder while the workfolder has none (its text comes with W4-05)
    if _arr_skill_placeholder(root):
        result.warnings.append(
            "Der Skill „arr“ fehlte im Workfolder; arr hat einen Platzhalter angelegt."
        )
        catalog = discover_skills(root / "skills")
        user_skills = skills_for_user_agent(catalog, settings)

    # Skill links
    changed, skipped = link_user_skills(project, root / "skills", user_skills)
    result.skill_links = changed
    for name in skipped:
        result.warnings.append(
            f".claude/skills/{name} ist bereits belegt und wurde nicht verändert."
        )
    for name, problem in catalog.problems.items():
        result.warnings.append(f"Der Skill „{name}“ wird übersprungen: {problem.issue_message}")
    return result
