"""The user's mandate for the supervising agent and arr's hard check of it (`mandate_rule`).

A mandate is a Markdown file in the workfolder, `mandates/standard.md` by default (template
`templates/mandates/standard.md`, the mandate of the user of 09.11.2026, docs/07 §4). It has
two parts:

- **The part arr checks itself** (front matter between two lines `---`): `domains` a request may
  reach and `install: pypi` (`pip install` from PyPI into the run venv). In v1 only
  `pypi.org`, `files.pythonhosted.org` and `arxiv.org` can be listed, and read folders cannot be
  granted at all (user, 09.11.); such entries are refused with a message.
- **The plain text** below it, which only the supervising agent reads.

`check_request` is the hard pre-check (`mandate_rule`, docs/07 §4 Nr. 2): a request reaches the
supervising agent only if *everything* about it lies within the checked part. Only the
categories `network` and `install`, every host arr finds is on the list, a shell command is one
plain command that arr reads completely: `pip install` from PyPI (no index option, URL, VCS,
local path, editable, target, requirement or constraint file) or `curl`/`wget` with known
options and URLs on the list. Everything else goes to the user or is denied, without the
supervising agent. The check never allows anything itself; it only decides whether the
supervising agent may be asked. Fixed rules (layer ②) run before it in the PolicyEngine.

See docs/07-berechtigungen-und-freigaben.md §3 ④, §4 and SECURITY.md §4 layer 4.
"""

import os
import re
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from arr.errors import ArrError
from arr.models import StrictModel
from arr.permissions.policy import (
    NETWORK_TOOLS,
    SHELL_TOOLS,
    Assessment,
    classify_command,
    is_loopback,
)
from arr.permissions.types import DecisionAction, RequestCategory, RiskLevel

__all__ = [
    "MANDATE_DOMAINS",
    "STANDARD_MANDATE",
    "Mandate",
    "MandateChecks",
    "MandateError",
    "MandateVerdict",
    "check_request",
    "load_mandate",
    "parse_mandate",
    "standard_text",
]

STANDARD_MANDATE = "mandates/standard.md"
"""Default mandate file, relative to the workfolder (docs/07 §3 ④)."""

MANDATE_DOMAINS = ("pypi.org", "files.pythonhosted.org", "arxiv.org")
"""The only domains a mandate can grant in v1 (user, 09.11.2026)."""

PACKAGE_HOSTS = ("pypi.org", "files.pythonhosted.org")
"""Where `pip install` from PyPI downloads from; `install: pypi` needs both in `domains`."""

MAX_BYTES = 32 * 1024
"""A larger mandate file is refused (it is read for every request)."""

PYPI = "pypi"

_FENCE = "---"
_ALLOWED_CATEGORIES = frozenset({RequestCategory.NETWORK, RequestCategory.INSTALL})


class MandateError(ArrError):
    """The mandate cannot be read or is not valid; the message says why, in German."""


class MandateChecks(StrictModel):
    """The part of a mandate that arr checks itself („Vollmacht (von arr geprüft)“)."""

    domains: list[str] = Field(default_factory=list)
    install: str | None = None
    read_dirs: list[str] = Field(default_factory=list)

    @field_validator("domains", mode="before")
    @classmethod
    def _domain_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("„domains“ ist eine Liste von Domains, z. B. - pypi.org")
        found: list[str] = []
        for raw in value:
            name = raw.strip().rstrip(".").lower()
            if name not in MANDATE_DOMAINS:
                listed = ", ".join(MANDATE_DOMAINS)
                raise ValueError(
                    f"„{raw.strip()}“ kann die Vollmacht in v1 nicht freigeben. Erlaubt sind nur "
                    f"{listed}; andere Domains entscheidest du selbst."
                )
            if name not in found:
                found.append(name)
        return found

    @field_validator("install", mode="before")
    @classmethod
    def _install_source(cls, value: Any) -> Any:
        if value is None:
            return None
        if value != PYPI:
            raise ValueError(
                "„install“ kennt nur „pypi“: pip install von PyPI in die Run-venv. Lass die Zeile "
                "weg, wenn der Aufsichts-Agent keine Installation erlauben soll."
            )
        return value

    @model_validator(mode="after")
    def _install_needs_pypi(self) -> "MandateChecks":
        if self.install == PYPI and not set(PACKAGE_HOSTS) <= set(self.domains):
            raise ValueError(
                "„install: pypi“ braucht pypi.org und files.pythonhosted.org unter „domains“: "
                "Von dort lädt pip die Pakete."
            )
        return self

    @field_validator("read_dirs", mode="before")
    @classmethod
    def _no_read_dirs(cls, value: Any) -> Any:
        if value:
            raise ValueError(
                "Lese-Ordner lassen sich in v1 nicht über die Vollmacht freigeben (Entscheidung "
                "vom 09.11.). Entferne „read_dirs“; Lesen außerhalb des Runs entscheidest du "
                "selbst."
            )
        return []


@dataclass(frozen=True, slots=True)
class Mandate:
    """A parsed mandate: the whole text, the plain text for the agent and the checked part."""

    text: str
    plain: str
    checks: MandateChecks


def standard_text() -> str:
    """The template `templates/mandates/standard.md` (mandate of the user of 09.11.)."""
    from arr.workfolder import read_template

    return read_template("mandates", "standard.md")


def _split(text: str) -> tuple[str | None, str]:
    """Front matter and body; None if the text does not start with a line `---`."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return None, text
    for index in range(1, len(lines)):
        if lines[index].strip() == _FENCE:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise MandateError(
        "Der von arr geprüfte Teil ist nicht abgeschlossen: Nach dem ersten „---“ fehlt das "
        "zweite. Nimm das Beispiel als Vorlage."
    )


def _checks(front: str | None) -> MandateChecks:
    if front is None or not front.strip():
        return MandateChecks()
    try:
        data = YAML(typ="safe", pure=True).load(front)
    except YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (Zeile {mark.line + 2})" if mark is not None else ""
        raise MandateError(f"Der von arr geprüfte Teil ist kein gültiges YAML{where}.") from None
    if data is None:
        return MandateChecks()
    if not isinstance(data, dict):
        raise MandateError(
            "Der von arr geprüfte Teil braucht Einträge wie „domains:“ und „install:“."
        )
    try:
        return MandateChecks.model_validate(data)
    except ValidationError as exc:
        error = exc.errors()[0]
        if error.get("type") == "unknown_field":
            name = (error.get("ctx") or {}).get("field", "?")
            raise MandateError(
                f"Unbekannter Eintrag „{name}“ im von arr geprüften Teil. Erlaubt sind "
                "„domains“ und „install“."
            ) from None
        message = str(error.get("msg", "")).removeprefix("Value error, ")
        raise MandateError(message) from None


def parse_mandate(text: str) -> Mandate:
    """Parse a mandate; `MandateError` with a German message if it is not valid."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise MandateError(f"Die Vollmacht ist zu lang (höchstens {MAX_BYTES // 1024} KB).")
    if "\x00" in text:
        raise MandateError("Die Vollmacht enthält ein Nullzeichen. Speichere sie als Text.")
    front, body = _split(text)
    checks = _checks(front)
    plain = body.strip()
    if not plain:
        raise MandateError(
            "Der Klartext der Vollmacht fehlt. Beschreibe unter dem geprüften Teil, was der "
            "Aufsichts-Agent erlauben darf."
        )
    return Mandate(text=text, plain=plain, checks=checks)


def load_mandate(workfolder: Path, relative: str = STANDARD_MANDATE) -> Mandate:
    """Read and parse the mandate file of a workfolder (fail closed: `MandateError`).

    Only a regular file below the workfolder, up to `MAX_BYTES`, is read; a link, a FIFO or a
    path outside the workfolder is refused.
    """
    path = mandate_path(workfolder, relative)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise MandateError(f"Die Vollmacht {relative} fehlt im Workfolder.") from None
    except OSError:
        raise MandateError(f"Die Vollmacht {relative} ließ sich nicht öffnen.") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise MandateError(f"Die Vollmacht {relative} ist keine normale Datei.")
        if info.st_size > MAX_BYTES:
            raise MandateError(f"Die Vollmacht ist zu lang (höchstens {MAX_BYTES // 1024} KB).")
        raw = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise MandateError(f"Die Vollmacht {relative} ist nicht in UTF-8 gespeichert.") from None
    return parse_mandate(text)


def mandate_path(workfolder: Path, relative: str = STANDARD_MANDATE) -> Path:
    """The mandate file below the workfolder; `MandateError` for a path outside of it."""
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not relative.endswith(".md"):
        raise MandateError(
            f"„{relative}“ ist kein Pfad für eine Vollmacht. Gib eine .md-Datei im Workfolder an, "
            "z. B. mandates/standard.md."
        )
    return workfolder / candidate


# --------------------------------------------------------------------------------------------
# The hard pre-check (`mandate_rule`)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MandateVerdict:
    """Result of `check_request`: inside the checked part or not, with a German sentence.

    `hosts` and `packages` are what arr found in the request (for the supervising agent).
    """

    inside: bool
    note: str
    hosts: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()


def _outside(note: str) -> MandateVerdict:
    """Not within the checked part; the GUI shows `note` after „Vollmacht (von arr geprüft):“."""
    return MandateVerdict(False, note)


def _host(raw: str) -> str:
    name = raw.strip().lower()
    if name.startswith("[") or name.count(":") > 1:
        return name  # an IPv6 address is never on the list
    return name.rsplit(":", 1)[0].rstrip(".") if ":" in name else name.rstrip(".")


_UNSAFE_OUTSIDE_QUOTES = frozenset("*?[]{}~#")
_NEVER = frozenset("$`\\\n\r\x00")
_SEPARATORS = frozenset(";&|()<>")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def plain_argv(command: str) -> list[str] | None:
    """The words of one plain command, or None if arr cannot read it completely.

    Refused: variables, substitutions, escapes, several lines, separators and redirections
    (`;`, `&&`, `|`, `>` …), globs, `~` and comments outside quotes, and a leading variable
    assignment (`PIP_INDEX_URL=… pip …`).
    """
    if not command.strip() or any(char in _NEVER for char in command):
        return None
    quote: str | None = None
    for char in command:
        if quote is None and char in ("'", '"'):
            quote = char
        elif quote is not None and char == quote:
            quote = None
        elif quote is None and char in _UNSAFE_OUTSIDE_QUOTES:
            return None
    if quote is not None:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens or any(t and set(t) <= _SEPARATORS for t in tokens):
        return None
    if _ASSIGN_RE.match(tokens[0]):
        return None
    return tokens


# -- pip ---------------------------------------------------------------------------------------

_PIP_RE = re.compile(r"^pip(?:3(?:\.\d+)?)?$")
_PYTHON_RE = re.compile(r"^python(?:3(?:\.\d+)?)?$")
_REQUIREMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?)"
    r"(?:\[[A-Za-z0-9](?:[A-Za-z0-9._,-]{0,98}[A-Za-z0-9])?\])?"
    r"(?:(?:~=|===|==|!=|<=|>=|<|>)[A-Za-z0-9.*+!_-]{1,64}"
    r"(?:,(?:~=|===|==|!=|<=|>=|<|>)[A-Za-z0-9.*+!_-]{1,64}){0,7})?$"
)
_PIP_ARCHIVES = (
    *(".zip", ".whl", ".tar.bz2", ".tbz", ".tar.xz", ".txz", ".tlz", ".tar.lz", ".tar.lzma"),
    *(".tar.gz", ".tgz", ".tar"),
)
"""pip's `ARCHIVE_EXTENSIONS`: a requirement whose name ends in one is a local file for pip."""
_FILE_ENDINGS = (*_PIP_ARCHIVES, ".gz", ".bz2", ".xz", ".lz", ".lzma", ".egg", ".pyz")
_PIP_SHORT = frozenset("Uqv")
_PIP_LONG = frozenset(
    {
        "--upgrade",
        "--quiet",
        "--verbose",
        "--no-deps",
        "--pre",
        "--no-cache-dir",
        "--prefer-binary",
        "--no-input",
        "--disable-pip-version-check",
        "--force-reinstall",
        "--no-color",
    }
)
_PIP_REFUSED = {
    "--index-url": "aus einer fremden Paketquelle (--index-url)",
    "-i": "aus einer fremden Paketquelle (-i)",
    "--extra-index-url": "mit einer zusätzlichen Paketquelle (--extra-index-url)",
    "--find-links": "aus einer fremden Paketquelle (--find-links)",
    "-f": "aus einer fremden Paketquelle (-f)",
    "--trusted-host": "aus einer fremden Paketquelle (--trusted-host)",
    "--no-index": "ohne PyPI (--no-index)",
    "--editable": "aus einem lokalen Ordner (--editable)",
    "-e": "aus einem lokalen Ordner (-e)",
    "--target": "mit einem Ziel außerhalb der Run-venv (--target)",
    "-t": "mit einem Ziel außerhalb der Run-venv (-t)",
    "--prefix": "mit einem Ziel außerhalb der Run-venv (--prefix)",
    "--root": "mit einem Ziel außerhalb der Run-venv (--root)",
    "--user": "mit einem Ziel außerhalb der Run-venv (--user)",
    "--requirement": "mit einer Datei mit weiteren Angaben (--requirement)",
    "-r": "mit einer Datei mit weiteren Angaben (-r)",
    "--constraint": "mit einer Datei mit weiteren Angaben (--constraint)",
    "-c": "mit einer Datei mit weiteren Angaben (-c)",
    "--config-settings": "mit Einstellungen für den Bau (--config-settings)",
    "--isolated": "mit geänderter pip-Konfiguration (--isolated)",
}


def _pip_args(argv: list[str]) -> list[str] | None:
    """The words after `install` of a pip command, or None if it is no plain pip install."""
    program = argv[0]
    if _PIP_RE.match(program) and argv[1:2] == ["install"]:
        return argv[2:]
    if _PYTHON_RE.match(program) and argv[1:4] == ["-m", "pip", "install"]:
        return argv[4:]
    return None


def _check_pip(args: list[str]) -> tuple[str | None, list[str]]:
    """Why these pip arguments are no plain install from PyPI (None: they are), and the names."""
    names: list[str] = []
    for arg in args:
        option = arg.split("=", 1)[0]
        if arg.startswith("--"):
            if option in _PIP_REFUSED:
                return f"pip install {_PIP_REFUSED[option]}", names
            if arg not in _PIP_LONG:
                return f"pip install mit der Option „{option}“, die arr nicht prüft", names
            continue
        if arg.startswith("-"):
            refused = next((f"-{c}" for c in arg[1:] if f"-{c}" in _PIP_REFUSED), None)
            if refused is not None:
                return f"pip install {_PIP_REFUSED[refused]}", names
            if len(arg) < 2 or not set(arg[1:]) <= _PIP_SHORT:
                return f"pip install mit der Option „{arg}“, die arr nicht prüft", names
            continue
        lowered = arg.lower()
        if "://" in arg or lowered.startswith(("git+", "hg+", "svn+", "bzr+")) or "@" in arg:
            return "pip install aus einer URL oder einem Versionsverwaltungs-Repository", names
        if "/" in arg or "\\" in arg or arg.startswith("."):
            return "pip install aus einem lokalen Pfad oder einer Datei", names
        # positive list: a PyPI name (PEP 508/503) with optional extras and version (G1)
        match = _REQUIREMENT_RE.match(arg)
        if match is None:
            return f"pip install mit „{arg[:60]}“, das arr nicht als Paketname liest", names
        name = match.group("name").lower()
        if name.endswith(_FILE_ENDINGS) or lowered.split("[", 1)[0].endswith(_FILE_ENDINGS):
            # pip reads such a name as a local archive, not as a package of PyPI
            return "pip install aus einem lokalen Pfad oder einer Datei", names
        names.append(name)
    if not names:
        return "pip install ohne Paketnamen", names
    return None, names


# -- curl and wget -----------------------------------------------------------------------------

_FETCH_OPTIONS: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str]]] = {
    # program: (short flags, short flags with a value, long flags, long flags with a value)
    "curl": (
        frozenset("sSLfOIi"),
        frozenset("om"),
        frozenset(
            {
                "--silent",
                "--show-error",
                "--location",
                "--fail",
                "--remote-name",
                "--head",
                "--include",
                "--compressed",
            }
        ),
        frozenset({"--output", "--max-time", "--retry", "--connect-timeout"}),
    ),
    "wget": (
        frozenset("qc"),
        frozenset("OTt"),
        frozenset({"--quiet", "--continue", "--no-verbose"}),
        frozenset({"--output-document", "--timeout", "--tries"}),
    ),
}
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _check_fetch(argv: list[str]) -> tuple[str | None, list[str]]:
    """Why this curl/wget call is not plainly readable (None: it is), and its URL hosts."""
    from urllib.parse import urlsplit

    short, short_value, long, long_value = _FETCH_OPTIONS[argv[0]]
    hosts: list[str] = []
    index = 1
    while index < len(argv):
        arg = argv[index]
        index += 1
        if arg == "-nv" and argv[0] == "wget":
            continue
        if arg.startswith("--"):
            option, has_value, _ = arg.partition("=")
            if option in long and not has_value:
                continue
            if option in long_value:
                index += 0 if has_value else 1
                continue
            return f"{argv[0]} mit der Option „{option}“, die arr nicht prüft", hosts
        if arg.startswith("-") and len(arg) > 1:
            for position, char in enumerate(arg[1:], start=1):
                if char in short:
                    continue
                if char in short_value:
                    index += 0 if position < len(arg) - 1 else 1
                    break
                return f"{argv[0]} mit der Option „-{char}“, die arr nicht prüft", hosts
            continue
        if not _URL_RE.match(arg):
            return f"{argv[0]} mit „{arg[:60]}“, das arr nicht als Adresse liest", hosts
        try:
            name = urlsplit(arg).hostname
        except ValueError:
            name = None
        if not name:
            return f"{argv[0]} mit einer Adresse ohne Host", hosts
        hosts.append(_host(name))
    if not hosts:
        return f"{argv[0]} ohne Adresse", hosts
    return None, hosts


# -- the check ---------------------------------------------------------------------------------


@dataclass
class _Found:
    hosts: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)


def _shell(command: str, checks: MandateChecks, found: _Found) -> str | None:
    """Why a shell command is not inside the checked part (None: it is)."""
    argv = plain_argv(command)
    if argv is None:
        return (
            "Der Befehl ist nicht ein einzelner, einfacher Befehl (z. B. mit ;, |, >, Variablen "
            "oder Mustern), deshalb kann arr ihn nicht vollständig prüfen."
        )
    pip = _pip_args(argv)
    if pip is not None:
        if checks.install != PYPI:
            return "Installationen gehören nicht zur Vollmacht."
        problem, names = _check_pip(pip)
        found.packages += names
        if problem is not None:
            return f"Erlaubt ist nur pip install von PyPI in die Run-venv, hier: {problem}."
        return None
    if argv[0] in _FETCH_OPTIONS:
        problem, hosts = _check_fetch(argv)
        found.hosts += hosts
        return problem + "." if problem is not None else None
    return (
        f"„{argv[0][:40]}“ gehört nicht zu dem, was die Vollmacht abdeckt (pip install von PyPI, "
        "curl oder wget zu den Domains der Vollmacht)."
    )


def check_request(assessment: Assessment, checks: MandateChecks) -> MandateVerdict:
    """The hard pre-check: is everything about this request within the checked part?

    Only then may the supervising agent decide it (docs/07 §4 Nr. 2). Reads only what the
    PolicyEngine and the command itself say, never the agent's reason.
    """
    request = assessment.request
    decision = assessment.decision
    if decision.action is not DecisionAction.ASK:
        return _outside("Nur offene Anfragen kann der Aufsichts-Agent entscheiden.")
    if assessment.no_rules:
        return _outside(
            "Codex will den Befehl mit erhöhten Rechten ausführen. Was er dann erreicht, kann "
            "arr nicht vollständig prüfen."
        )
    categories = set(request.categories)
    if not categories or not categories <= _ALLOWED_CATEGORIES:
        return _outside("Allein erlauben kann der Aufsichts-Agent nur Netzwerk und Installation.")
    if not assessment.items or any(kind not in ("host", "command") for kind, _ in assessment.items):
        return _outside("arr kann nicht erkennen, wohin die Anfrage geht.")
    found = _Found()
    command = (request.command or "").strip()
    if request.tool in SHELL_TOOLS and command:
        problem = _shell(command, checks, found)
        if problem is not None:
            return _outside(problem)
        found.hosts += classify_command(command).hosts
    elif (
        command
        or request.paths
        or not (request.tool in NETWORK_TOOLS or (request.tool in SHELL_TOOLS and request.hosts))
    ):
        # a plain host request: Claude's `SandboxNetworkAccess`, or Codex asking for one host
        # without a command (a Codex command with a host is an escalation: `no_rules` above)
        return _outside("Das Werkzeug gehört nicht zu dem, was die Vollmacht abdeckt.")
    from urllib.parse import urlsplit

    for url in request.urls:
        try:
            name = urlsplit(url).hostname
        except ValueError:
            name = None
        if not name:
            return _outside("Eine Adresse der Anfrage hat keinen lesbaren Host.")
        found.hosts.append(name)
    found.hosts += list(request.hosts)
    found.hosts += [value for kind, value in assessment.items if kind == "host"]
    hosts = list(dict.fromkeys(_host(h) for h in found.hosts))
    if RequestCategory.NETWORK in categories and not hosts and not found.packages:
        return _outside("arr kann nicht erkennen, wohin die Anfrage verbindet.")
    if decision.risk is not None and decision.risk.level is RiskLevel.HIGH:
        return _outside("Anfragen mit hohem Risiko entscheidest nur du.")
    allowed = set(checks.domains)
    for host in hosts:
        if is_loopback(host) or host not in allowed:
            listed = ", ".join(checks.domains) or "keine Domain"
            return _outside(f"Netzwerk nur zu {listed}. {host} gehört nicht dazu.")
    packages = tuple(dict.fromkeys(found.packages))
    note = "Die Anfrage liegt im geprüften Teil der Vollmacht."
    return MandateVerdict(True, note, tuple(hosts), packages)
