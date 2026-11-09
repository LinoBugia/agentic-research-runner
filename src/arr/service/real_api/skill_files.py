"""Reading and writing skill files for the editor (W4-02, docs/08-ux-konzept.md §5.7).

Everything happens inside `<workfolder>/skills/<name>/` and follows no link: each folder below
the workfolder is opened on its own with `O_NOFOLLOW | O_DIRECTORY` relative to its parent's
descriptor, files with `O_NOFOLLOW | O_NONBLOCK`, and only regular files of at most
`MAX_BYTES` that are UTF-8 text are read or written. A path is relative, without `..`,
without hidden parts. Writing is atomic: a temporary file in the same folder, `fsync`, the old
file's permissions, then `rename`. A write states the version it started from (`version`, a
hash of the content when the file was opened); a file changed outside since is not
overwritten (`conflict`). A refused attempt writes nothing, also not outside.

New skills come from a small built-in template or as a copy of another skill; both are built
in a hidden temporary folder next to the others and renamed into place at the end.
"""

import contextlib
import errno
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from arr.service.api_models import Action, ApiException, ErrorCode
from arr.skills import (
    _FRONTMATTER_RE,
    _NAME_RE,
    ARR_SKILL,
    MAX_SKILL_FILE_BYTES,
    SKILL_FILE,
    SKILLS_DIR,
)

MAX_BYTES = MAX_SKILL_FILE_BYTES
"""Largest file the editor reads or writes (256 KB)."""
MAX_DEPTH = 6
"""Deepest path inside a skill the editor writes, e.g. `references/a/b.md` has depth 3."""
MAX_COPY_BYTES = 20 * 1024 * 1024
"""Largest file a duplicate copies; larger ones are left out and named."""
MAX_COPY_FILES = 500
MAX_NAME = 64

_PART_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
_LINK_ERRNOS = {errno.ELOOP, errno.EMLINK}

UNSAVED_HINT = "Laufende Runs nutzen ihren Schnappschuss, Änderungen gelten ab dem nächsten Run."


def _invalid(message: str, field: str = "path") -> ApiException:
    return ApiException.of(ErrorCode.INVALID, message, field=field)


def _conflict(message: str, **fields: object) -> ApiException:
    return ApiException.of(ErrorCode.CONFLICT, message, **fields)


def version_of(data: bytes) -> str:
    """Version of a file's content: the first 16 hex digits of its SHA-256."""
    return hashlib.sha256(data).hexdigest()[:16]


def check_path(path: str) -> list[str]:
    """The parts of a relative path inside a skill; `ApiException` (`invalid`) otherwise.

    Refused: empty, absolute, `\\`, NUL, `.`/`..`, hidden parts (`.x`), characters other than
    letters, digits, `.`, `_`, `-`, and more than `MAX_DEPTH` parts.
    """
    if not isinstance(path, str) or not path or len(path) > 512:
        raise _invalid("Gib einen Dateinamen im Skill an, z. B. roles/kritiker.md.")
    if path.startswith("/") or "\\" in path or "\x00" in path or path.startswith("~"):
        raise _invalid(
            f"„{path[:80]}“ ist kein Pfad im Skill. Gib ihn relativ zum Skill an, z. B. "
            "roles/kritiker.md."
        )
    parts = path.split("/")
    if len(parts) > MAX_DEPTH:
        raise _invalid(f"Der Pfad ist zu tief verschachtelt (höchstens {MAX_DEPTH} Ebenen).")
    for part in parts:
        if part in ("", ".", ".."):
            raise _invalid(
                f"„{path[:80]}“ enthält „{part or '//'}“. Gib den Pfad ohne „..“ und ohne "
                "leere Teile an, z. B. roles/kritiker.md."
            )
        if not _PART_RE.fullmatch(part):
            raise _invalid(
                f"„{part[:80]}“ ist als Datei- oder Ordnername nicht erlaubt. Nimm Buchstaben, "
                "Ziffern, „.“, „_“ und „-“, ohne Punkt am Anfang."
            )
    return parts


def check_name(name: str) -> None:
    """A new skill name: lowercase slug, at most 64 characters, not `arr` (`invalid`)."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or len(name) > MAX_NAME:
        raise _invalid(
            "Ungültiger Skill-Name. Erlaubt sind Kleinbuchstaben, Ziffern und „-“ (nicht am "
            "Anfang oder Ende), z. B. „proof-review“.",
            field="name",
        )
    if name == ARR_SKILL:
        raise _invalid(
            "Der Name „arr“ gehört dem Skill, der Claude Code arr erklärt. Wähle einen anderen.",
            field="name",
        )


def _refused_link(what: str) -> ApiException:
    return ApiException.of(
        ErrorCode.NOT_ALLOWED,
        f"{what} ist ein Link. arr folgt im Skill-Ordner keinen Links, weder bei Dateien noch "
        "bei Ordnern. Ersetze ihn außerhalb von arr durch eine echte Datei bzw. einen Ordner.",
    )


def _not_regular(what: str) -> ApiException:
    return ApiException.of(
        ErrorCode.NOT_ALLOWED,
        f"{what} ist keine normale Datei (z. B. eine Pipe, ein Gerät oder ein Ordner). Der "
        "Editor bearbeitet nur Textdateien.",
    )


def _not_folder(what: str) -> ApiException:
    return ApiException.of(
        ErrorCode.NOT_ALLOWED,
        f"{what} ist kein Ordner. Wähle einen anderen Pfad.",
    )


def _open_dir(parent: int | None, name: str, what: str, *, create: bool = False) -> int:
    """Open folder `name` below `parent` without following a link (`parent=None`: a path)."""
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise
    except OSError as exc:
        if exc.errno == errno.ENOTDIR:
            raise _not_folder(what) from None
        if exc.errno in _LINK_ERRNOS:
            raise _refused_link(what) from None
        raise
    os.mkdir(name, 0o755, dir_fd=parent)
    return os.open(name, _DIR_FLAGS, dir_fd=parent)


@contextlib.contextmanager
def _skills_root(workfolder: Path) -> Iterator[int]:
    """Descriptor of `<workfolder>/skills`; the folder itself must not be a link."""
    base = os.open(workfolder, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        try:
            fd = _open_dir(base, SKILLS_DIR, f"Der Ordner {SKILLS_DIR}")
        except FileNotFoundError:
            raise ApiException.of(
                ErrorCode.NOT_FOUND, "Im Workfolder gibt es noch keinen Ordner skills."
            ) from None
    finally:
        os.close(base)
    try:
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def _folder(workfolder: Path, skill: str, parts: list[str], *, create: bool) -> Iterator[int]:
    """Descriptor of the folder of `parts[:-1]` inside the skill, opened link by link."""
    check_name_like(skill)
    with _skills_root(workfolder) as root:
        try:
            current = _open_dir(root, skill, f"Der Skill-Ordner „{skill}“")
        except FileNotFoundError:
            raise ApiException.of(ErrorCode.NOT_FOUND, f"Skill „{skill}“ nicht gefunden.") from None
        try:
            for index, part in enumerate(parts[:-1]):
                where = "/".join(parts[: index + 1])
                try:
                    child = _open_dir(current, part, f"„{where}“", create=create)
                except FileNotFoundError:
                    raise ApiException.of(
                        ErrorCode.NOT_FOUND, f"Datei „{skill}/{'/'.join(parts)}“ nicht gefunden."
                    ) from None
                os.close(current)
                current = child
            yield current
        finally:
            os.close(current)


def check_name_like(skill: str) -> None:
    """An existing skill's folder name: a single plain part (no path), else `not_found`."""
    if not isinstance(skill, str) or not _PART_RE.fullmatch(skill or "") or skill.startswith("_"):
        raise ApiException.of(ErrorCode.NOT_FOUND, f"Skill „{str(skill)[:80]}“ nicht gefunden.")


def _read_fd(fd: int, label: str) -> bytes:
    """Content of an open file: regular, at most `MAX_BYTES` (else `not_allowed`)."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise _not_regular(label)
    if info.st_size > MAX_BYTES:
        raise _too_large(label, info.st_size)
    data = b""
    while len(data) <= MAX_BYTES:
        chunk = os.read(fd, MAX_BYTES + 1 - len(data))
        if not chunk:
            break
        data += chunk
    if len(data) > MAX_BYTES:
        raise _too_large(label, len(data))
    return data


def _too_large(label: str, size: int) -> ApiException:
    return ApiException.of(
        ErrorCode.NOT_ALLOWED,
        f"{label} ist zu groß zum Bearbeiten ({size // 1024} KB, höchstens {MAX_BYTES // 1024} "
        "KB). Bearbeite sie außerhalb von arr oder teile sie auf.",
    )


def _not_text(label: str) -> ApiException:
    return ApiException.of(
        ErrorCode.NOT_ALLOWED,
        f"{label} ist keine UTF-8-Textdatei. Der Editor bearbeitet nur Textdateien.",
    )


def _open_file(folder: int, name: str, label: str) -> int:
    try:
        return os.open(name, _READ_FLAGS, dir_fd=folder)
    except OSError as exc:
        if exc.errno in _LINK_ERRNOS:
            raise _refused_link(label) from None
        raise


@dataclass(frozen=True, slots=True)
class FileContent:
    """A skill file as the editor sees it."""

    text: str
    version: str
    size: int


def read_file(workfolder: Path, skill: str, path: str) -> FileContent:
    """Read `<skills>/<skill>/<path>` for the editor, following no link.

    `not_found` if it does not exist, `not_allowed` for links, FIFOs, devices, files over
    `MAX_BYTES` and non-UTF-8 content, `invalid` for a bad path.
    """
    parts = check_path(path)
    label = f"„{path}“"
    with _folder(workfolder, skill, parts, create=False) as folder:
        try:
            fd = _open_file(folder, parts[-1], label)
        except FileNotFoundError:
            raise ApiException.of(
                ErrorCode.NOT_FOUND, f"Datei „{skill}/{path}“ nicht gefunden."
            ) from None
        try:
            data = _read_fd(fd, label)
        finally:
            os.close(fd)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _not_text(label) from None
    return FileContent(text=text, version=version_of(data), size=len(data))


def _current(folder: int, name: str, label: str) -> tuple[bytes, int] | None:
    """Content and permission bits of the existing file, None if there is none."""
    try:
        info = os.stat(name, dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise _refused_link(label)
    if not stat.S_ISREG(info.st_mode):
        raise _not_regular(label)
    fd = _open_file(folder, name, label)
    try:
        return _read_fd(fd, label), stat.S_IMODE(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


def _changed_outside(skill: str, path: str) -> ApiException:
    """The conflict of 08 §5.7 „live“: „… wurde außerhalb geändert. Neu laden“."""
    return _conflict(
        f"{path} wurde außerhalb geändert. arr hat nichts überschrieben. Lade die Datei neu, "
        "bevor du speicherst.",
        action=Action(label="Neu laden", url=f"/skills/{skill}?datei={path}"),
    )


def _encode(text: str, label: str) -> bytes:
    if not isinstance(text, str) or "\x00" in text:
        raise _invalid(f"{label}: Der Text enthält Zeichen, die in keiner Textdatei stehen.")
    data = text.encode("utf-8", errors="strict")
    if len(data) > MAX_BYTES:
        raise _invalid(
            f"{label} wäre zu groß ({len(data) // 1024} KB, höchstens {MAX_BYTES // 1024} KB). "
            "Kürze den Text oder teile ihn auf mehrere Dateien auf.",
            field="content",
        )
    return data


def _write_temp(folder: int, name: str, data: bytes, mode: int) -> str:
    """Write `data` to a new hidden file next to `name`; returns its name."""
    temp = f".{name}.arr-{secrets.token_hex(6)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(temp, flags, 0o600, dir_fd=folder)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(temp, dir_fd=folder)
        raise
    os.close(fd)
    return temp


def write_file(
    workfolder: Path, skill: str, path: str, text: str, *, version: str | None
) -> FileContent:
    """Write `text` to `<skills>/<skill>/<path>` atomically, following no link.

    `version` is the version the editor opened; `None` creates a new file (and missing
    folders), which must not exist yet. A file that changed since `version` or that appeared
    meanwhile is not touched (`conflict`). The permission bits of an existing file are kept.
    """
    parts = check_path(path)
    label = f"„{path}“"
    data = _encode(text, label)
    create = version is None
    with _folder(workfolder, skill, parts, create=create) as folder:
        name = parts[-1]
        current = _current(folder, name, label)
        if create and current is not None:
            raise _conflict(f"Die Datei {label} gibt es schon. Wähle einen anderen Namen.")
        if not create:
            if current is None:
                raise _conflict(
                    f"{label} wurde außerhalb gelöscht. Speichere sie als neue Datei oder lade "
                    "den Skill neu."
                )
            if version_of(current[0]) != version:
                raise _changed_outside(skill, path)
        mode = current[1] if current is not None else 0o644
        temp = _write_temp(folder, name, data, mode)
        try:
            if create:
                os.link(temp, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
                os.unlink(temp, dir_fd=folder)
            else:
                again = _current(folder, name, label)
                if again is None or version_of(again[0]) != version:
                    raise _changed_outside(skill, path)
                os.rename(temp, name, src_dir_fd=folder, dst_dir_fd=folder)
        except FileExistsError:
            raise _conflict(
                f"Die Datei {label} gibt es schon. Wähle einen anderen Namen."
            ) from None
        finally:
            with contextlib.suppress(OSError):
                os.unlink(temp, dir_fd=folder)
    return FileContent(text=text, version=version_of(data), size=len(data))


# --------------------------------------------------------------------------------------------
# New skills: template and duplicate
# --------------------------------------------------------------------------------------------

TOOL_SKILL_MD = """---
name: {name}
description: {description}
---

# {title}

Beschreibe hier, wann Agenten oder Claude Code diesen Skill nutzen und wie.

## Vorgehen

1. …
"""

GROUP_SKILL_MD = """---
name: {name}
description: {description}
---

# {title} (Research-Group)

## Wann sinnvoll
- …

## Vorbereitung
Schreibe eine task.md mit der Frage und dem, worauf die Agenten achten sollen.

## Parameter
| Name | Standard | Bedeutung |
|---|---|---|
| rounds | 2 | Anzahl der Runden |
"""

GROUP_YAML = """kind: research-group
version: 1
description: {description_yaml}
topology: roundtable

params:
  rounds: {{ default: 2, min: 1, max: 5, doc: "Anzahl der Runden" }}

agents:
  - id: teilnehmer
    count: 2
    role: roles/teilnehmer.md

groupchats:
  - name: main
    purpose: "Gemeinsame Diskussion der Aufgabe"
    members: [teilnehmer-*]
    mode: rounds
"""

GROUP_ROLE = """# Rolle: Teilnehmer

Du diskutierst die Aufgabe mit den anderen Teilnehmern im Kanal #main. Lies die neuen
Nachrichten, bring eigene Überlegungen ein und prüfe die Beiträge der anderen kritisch.
Gib dein Ergebnis mit `submit_result` ab, sobald es steht.
"""


def _yaml_text(value: str) -> str:
    """A one-line YAML string in double quotes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def template_files(name: str, kind: str, description: str) -> dict[str, str]:
    """The files of a new skill: `tool` (only SKILL.md) or `group` (roundtable, one role)."""
    description = " ".join(description.split()) or "Neuer Skill"
    title = name.replace("-", " ").capitalize()
    fields = {
        "name": name,
        "title": title,
        "description": _yaml_text(description),
        "description_yaml": _yaml_text(description),
    }
    if kind == "group":
        return {
            SKILL_FILE: GROUP_SKILL_MD.format(**fields),
            "group.yaml": GROUP_YAML.format(**fields),
            "roles/teilnehmer.md": GROUP_ROLE,
        }
    return {SKILL_FILE: TOOL_SKILL_MD.format(**fields)}


def _free(root: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise _conflict(
        f"Den Namen „{name}“ gibt es im Workfolder schon. Wähle einen anderen.", field="name"
    )


def _write_new(folder: int, rel: str, data: bytes, mode: int = 0o644) -> None:
    parts = rel.split("/")
    current = folder
    opened: list[int] = []
    try:
        for part in parts[:-1]:
            current = _open_dir(current, part, f"„{part}“", create=True)
            opened.append(current)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(parts[-1], flags, 0o600, dir_fd=current)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
    finally:
        for fd in opened:
            os.close(fd)


def _remove_tree(root: int, name: str) -> None:
    """Remove a folder arr just built (no links inside: they are never created)."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=root)
    except OSError:
        return
    try:
        for entry in os.listdir(fd):
            info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _remove_tree(fd, entry)
            else:
                os.unlink(entry, dir_fd=fd)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        os.rmdir(name, dir_fd=root)


def _install(workfolder: Path, name: str, files: dict[str, tuple[bytes, int]]) -> None:
    """Build the skill in a hidden folder and rename it to `name`; nothing stays on failure."""
    check_name(name)
    with _skills_root(workfolder) as root:
        _free(root, name)
        temp = f".{name}.arr-{secrets.token_hex(6)}.tmp"
        os.mkdir(temp, 0o755, dir_fd=root)
        try:
            folder = os.open(temp, _DIR_FLAGS, dir_fd=root)
            try:
                for rel, (data, mode) in files.items():
                    _write_new(folder, rel, data, mode)
            finally:
                os.close(folder)
            _free(root, name)
            os.rename(temp, name, src_dir_fd=root, dst_dir_fd=root)
        except BaseException:
            _remove_tree(root, temp)
            raise


def create_skill(workfolder: Path, name: str, kind: str, description: str) -> list[str]:
    """Create `skills/<name>` from the template; returns the files written."""
    if kind not in ("tool", "group"):
        raise _invalid("Wähle die Art: Werkzeug oder Group.", field="kind")
    files = template_files(name, kind, description)
    _install(workfolder, name, {k: (v.encode("utf-8"), 0o644) for k, v in files.items()})
    return sorted(files)


def _collect(folder: int, prefix: str, out: dict[str, tuple[bytes, int]], left: list[str]) -> None:
    """Regular files below an open folder, without following links; the rest goes to `left`."""
    for entry in sorted(os.listdir(folder)):
        rel = f"{prefix}{entry}"
        info = os.stat(entry, dir_fd=folder, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            if entry.startswith("."):
                continue
            sub = os.open(entry, _DIR_FLAGS, dir_fd=folder)
            try:
                _collect(sub, f"{rel}/", out, left)
            finally:
                os.close(sub)
        elif stat.S_ISREG(info.st_mode) and not entry.startswith("."):
            if info.st_size > MAX_COPY_BYTES or len(out) >= MAX_COPY_FILES:
                left.append(rel)
                continue
            fd = os.open(entry, _READ_FLAGS, dir_fd=folder)
            try:
                data = b""
                while len(data) <= MAX_COPY_BYTES:
                    chunk = os.read(fd, 1 << 20)
                    if not chunk:
                        break
                    data += chunk
                if len(data) > MAX_COPY_BYTES:
                    left.append(rel)
                    continue
                out[rel] = (data, stat.S_IMODE(os.fstat(fd).st_mode))
            finally:
                os.close(fd)
        elif not entry.startswith("."):
            left.append(rel)  # link, FIFO, device: not copied


def renamed_skill_md(data: bytes, name: str) -> bytes:
    """SKILL.md with `name:` in its frontmatter set to the new name (other text unchanged)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return data
    head = match.group(1)
    new_head, count = re.subn(r"(?m)^name:[^\n]*$", f"name: {name}", head, count=1)
    if count == 0:
        new_head = f"name: {name}\n{head}"
    return (text[: match.start(1)] + new_head + text[match.end(1) :]).encode("utf-8")


def duplicate_skill(workfolder: Path, source: str, name: str) -> list[str]:
    """Copy `skills/<source>` to `skills/<name>` (regular files only, `name:` adjusted).

    Returns the paths left out: links, FIFOs, devices, files over `MAX_COPY_BYTES`.
    """
    check_name(name)
    check_name_like(source)
    files: dict[str, tuple[bytes, int]] = {}
    left: list[str] = []
    with _skills_root(workfolder) as root:
        try:
            folder = _open_dir(root, source, f"Der Skill-Ordner „{source}“")
        except FileNotFoundError:
            raise ApiException.of(
                ErrorCode.NOT_FOUND, f"Skill „{source}“ nicht gefunden."
            ) from None
        try:
            _collect(folder, "", files, left)
        finally:
            os.close(folder)
    if SKILL_FILE in files:
        data, mode = files[SKILL_FILE]
        files[SKILL_FILE] = (renamed_skill_md(data, name), mode)
    _install(workfolder, name, files)
    return left
