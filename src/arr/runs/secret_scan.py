"""Secret scan over a run folder (M1 criterion 5, docs/01 §8).

`scan_run_dir` searches the run folder for (a) the secret values of the auth profiles the run
used (`profile_secrets`: the keychain secret of a Claude or API-key profile, the tokens in the
login folder of a Codex profile; held in memory only) and (b) the patterns of
`arr.permissions.policy.redact` (API keys, private keys, `KEY=…` assignments, auth headers,
credentials in URLs). A finding names file, line and kind, never the value. `arr run` runs the
scan after the finalizer and records the result as `secret_scan` in `orchestrator.jsonl`;
`arr runs scan <id>` repeats it.

The profile values are searched as bytes (UTF-8, also UTF-16) in **every** regular file, binary
files, `tmp/home/`, `tmp/cache/` and the run venv included: these are the places agents can
write to besides their work folder (W2-05.1 M2). The run venv lies outside the run folder
since W3-00 (`meta.json`, `run_venv_location`); its files are named `run-venv/…`. Runs before
W3-00 have it as `.venv/` inside the run folder. The patterns are searched line by line in text
files (no NUL byte in the first 8 KB) outside the run venv, where installed software would give
false alarms. Only regular files are opened: symlinks are never followed, and a FIFO, socket or
device an agent could leave behind would block `open()` forever (W2-05.2 N1).
"""

import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arr.auth import AuthError, SecretReader, read_secret
from arr.models import AuthKind, AuthProfile
from arr.permissions.policy import (
    _AUTH_HEADER_RE,
    _SECRET_ASSIGN_RE,
    _SECRET_FLAG_RE,
    _SECRET_PATTERNS,
    _URL_CREDS_RE,
    REDACTED,
)
from arr.runs.orchestrator import OrchestratorLog
from arr.runs.prepare import run_venv_location

__all__ = [
    "PATTERN_SKIP",
    "RUN_VENV_LABEL",
    "SCAN_KIND",
    "Finding",
    "ScanReport",
    "format_report",
    "profile_secrets",
    "record_scan",
    "scan_run_dir",
]

SCAN_KIND = "secret_scan"
"""Kind of the entry in `orchestrator.jsonl` (`data`: `ScanReport.to_data()`)."""

RUN_VENV_LABEL = "run-venv"
"""Prefix of the run venv's files in a report when it lies outside the run folder (W3-00)."""

PATTERN_SKIP = (".venv/", RUN_VENV_LABEL + "/")
"""Folders (POSIX paths as reported) whose files get no pattern scan: installed software
(pip's own `KEY=…` lines, `https://user:pw@` examples). Profile values are searched there
too. `.venv/` is the run venv of runs before W3-00."""

MIN_SECRET_LENGTH = 8
"""Shorter profile secrets are not searched for (too many chance hits)."""

_SNIFF = 8192
_CHUNK = 1 << 20
_ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")
_TOKEN_KEY_RE = re.compile(r"(?i)token|key|secret")

PATTERN_KINDS = (
    "private_key",
    "anthropic_key",
    "openai_key",
    "github_token",
    "github_pat",
    "aws_access_key",
    "slack_token",
    "google_api_key",
)
"""Names of `policy._SECRET_PATTERNS` in their order; „muster“ if the tuple changes shape."""

_CODE_VALUE_RE = re.compile(
    r"(?P<lam>lambda\b)"
    r"|(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?:(?P<call>[(\[])|(?P<end>[),\]}]))"
)
"""Start of a value that is Python code, not a literal (W3-08 U2, W3-08.1): `lambda`, a call or
index (`itemgetter(1)`, `os.environ["X"]`), a dotted name (`counts.get)`) or a bare name
without digits (`len)`); a name only before Python syntax, never at the end of the value."""

_RANDOM_SEGMENT_RE = re.compile(r"(?=[^.]*\d)(?=[^.]*[A-Z])[A-Za-z0-9_]{16,}")
"""A name segment with digits and capitals of 16+ characters looks like a token, not code."""

_PLACEHOLDERS = (REDACTED, json.dumps(REDACTED).strip('"'))
"""`policy.redact`'s placeholder, raw and JSON-escaped: an already removed value."""

_PLACEHOLDER_END = ("", '"', "'", "\\", ",", ")", "]", "}")
"""What may follow the placeholder: the end of the value or a delimiter, nothing else
(`TOKEN=«entfernt»<secret>` written by an agent stays a hit, W3-08.1)."""


_VALUE_GROUP = r"""=(\"[^\"]*\"|'[^']*'|[^\s;&|]+)"""
_NAME_GROUP = _SECRET_ASSIGN_RE.pattern.removeprefix(r"(?i)\b(").removesuffix(")" + _VALUE_GROUP)
assert _SECRET_ASSIGN_RE.pattern == r"(?i)\b(" + _NAME_GROUP + ")" + _VALUE_GROUP, "policy changed"
_NAME_RE = re.compile(r"(?i)" + _NAME_GROUP)
"""The name part of `_SECRET_ASSIGN_RE` (KEY, TOKEN, …), checked with `fullmatch` on one word."""
_WORD_EQ_RE = re.compile(r"(?i)(?<!\w)[A-Z0-9_]+=")
"""A whole word of name characters right before `=`: where `_SECRET_ASSIGN_RE` can match."""
_STOP_RE = re.compile(r"[\s;&|]")
"""A character that ends an unquoted value (and so the value group cannot start with it)."""
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]{16,}")
_DIGIT_RE = re.compile(r"\d")
_UPPER_RE = re.compile(r"[A-Z]")


class _SecretAssignment:
    """`env_assignment`: `_SECRET_ASSIGN_RE` without Python code and redacted values (U2).

    Only a lower-case name left of `=` can be a Python keyword argument (`key=lambda …`,
    `key=itemgetter(1)`, `key=counts.get)`); its value is code in the forms of
    `_CODE_VALUE_RE`. `key=«entfernt»` in `approvals.jsonl` was redacted before. Everything
    else stays a hit: `API_KEY=…`, `apiKey=…`, quoted values, bare words with digits
    (`password=hunter2)`), token-like names. After a value counted as code or redacted the
    search goes on inside it, so a chained assignment is found (`--set key=value,API_KEY=…`,
    W3-08.1 N2).

    Linear in the length of the line (W3-07.2 NB1): it looks at every `NAME=` once and decides
    on the start of the value, in the line itself; it never takes the rest of the value. The
    result is that of `_SECRET_ASSIGN_RE.search` from one character after each code value.
    """

    def search(self, line: str) -> bool:
        quotes = _Quotes(line)
        for word in _WORD_EQ_RE.finditer(line):
            name = word.group()[:-1]
            start = word.end()
            if not _NAME_RE.fullmatch(name) or start == len(line):
                continue
            if _STOP_RE.match(line, start):
                continue  # `KEY= …`: no value, so no assignment
            if _secret_at(name, line, start, quotes):
                return True
        return False


class _Quotes:
    """Is there a closing quote after a position? Asked with growing positions: linear."""

    def __init__(self, line: str) -> None:
        self.line = line
        self.known: dict[str, tuple[int, int]] = {}  # quote → (searched from, found at or -1)

    def closes_after(self, quote: str, pos: int) -> bool:
        cached = self.known.get(quote)
        if cached is None or cached[0] > pos or -1 < cached[1] < pos:
            cached = (pos, self.line.find(quote, pos))
            self.known[quote] = cached
        return cached[1] != -1


def _redacted_at(line: str, start: int, quotes: _Quotes) -> bool:
    """`_redacted` of the value that starts at `start`, decided on the line (see `_redacted`):
    the character after the placeholder belongs to the value unless it ends an unquoted one."""
    quote = line[start] if line[start] in "\"'" else ""
    text_at = start + len(quote)
    for mark in _PLACEHOLDERS:
        if not line.startswith(mark, text_at):
            continue
        after = text_at + len(mark)
        if after == len(line):
            return True  # the value ends with the placeholder
        if _STOP_RE.match(line, after) and not (quote and quotes.closes_after(quote, after)):
            return True  # an unquoted value ends here
        if line[after] in _PLACEHOLDER_END:
            return True
    return False


def _secret_at(name: str, line: str, start: int, quotes: _Quotes) -> bool:
    """`_is_secret` for the value that starts at `start`, without taking it out of the line."""
    if _redacted_at(line, start, quotes):
        return False
    if name != name.lower():
        return True
    return _secret_code(_CODE_VALUE_RE.match(line, start))


def _redacted(value: str) -> bool:
    """Exactly the placeholder (quoted or not), followed by nothing or a delimiter."""
    text = value[1:] if value[:1] in "\"'" else value
    return any(
        text.startswith(mark) and text[len(mark) : len(mark) + 1] in _PLACEHOLDER_END
        for mark in _PLACEHOLDERS
    )


def _is_secret(name: str, value: str) -> bool:
    if _redacted(value):
        return False
    if name != name.lower():
        return True  # API_KEY=…, apiKey=…: an environment variable or a `toString`, no kwarg
    return _secret_code(_CODE_VALUE_RE.match(value))


def _secret_code(code: re.Match[str] | None) -> bool:
    """A lower-case name: is its value (matched by `_CODE_VALUE_RE`) a secret, not code?"""
    if code is None:
        return True
    if code.group("lam"):
        return False
    ident = code.group("name")
    if code.group("end") and "." not in ident and _DIGIT_RE.search(ident):
        return True  # a bare word with digits: `password=hunter2)`, hex tokens
    return _random_segment(ident)


def _random_segment(ident: str) -> bool:
    """`_RANDOM_SEGMENT_RE.search(ident)` in one pass: a run of 16+ ASCII word characters
    after which, up to the next dot, a digit and a capital follow."""
    for segment in ident.split("."):
        digit = max((m.start() for m in _DIGIT_RE.finditer(segment)), default=-1)
        upper = max((m.start() for m in _UPPER_RE.finditer(segment)), default=-1)
        if any(min(digit, upper) >= run.start() for run in _ASCII_WORD_RE.finditer(segment)):
            return True
    return False


_NAMED_PATTERNS: tuple[tuple[str, re.Pattern[str] | _SecretAssignment], ...] = (
    tuple(zip(PATTERN_KINDS, _SECRET_PATTERNS, strict=True))
    if len(PATTERN_KINDS) == len(_SECRET_PATTERNS)
    else tuple(("muster", pattern) for pattern in _SECRET_PATTERNS)
) + (
    ("env_assignment", _SecretAssignment()),
    ("cli_flag", _SECRET_FLAG_RE),
    ("auth_header", _AUTH_HEADER_RE),
    ("url_credentials", _URL_CREDS_RE),
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One hit: the file (relative to the run folder), the line and the kind, never the value."""

    file: str
    line: int
    kind: str

    def to_data(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "kind": self.kind}


@dataclass(slots=True)
class ScanReport:
    """Result of `scan_run_dir`."""

    files: int = 0
    secrets: int = 0
    findings: list[Finding] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings

    def to_data(self) -> dict[str, Any]:
        """JSON data for `orchestrator.jsonl` (kinds and places only)."""
        return {
            "clean": self.clean,
            "files": self.files,
            "secrets": self.secrets,
            "findings": [f.to_data() for f in self.findings],
            "skipped": list(self.skipped),
        }


def _codex_tokens(profile: AuthProfile) -> list[str]:
    """Token-like strings of `auth.json` in the profile's login folder (keys named token/key)."""
    from arr.backends.codex_config import codex_home_for

    try:
        data = json.loads((codex_home_for(profile) / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    values: list[str] = []

    def walk(node: Any, key: str) -> None:
        if isinstance(node, dict):
            for name, value in node.items():
                walk(value, str(name))
        elif isinstance(node, list):
            for value in node:
                walk(value, key)
        elif isinstance(node, str) and _TOKEN_KEY_RE.search(key) and len(node) >= 20:
            values.append(node)

    walk(data, "")
    return values


def profile_secrets(
    profiles: Iterable[AuthProfile], reader: SecretReader | None = None
) -> dict[str, str]:
    """`label → secret value` of the given profiles, in memory only.

    Claude and API-key profiles: the keychain secret (none stored → nothing). Codex profiles:
    the tokens of `auth.json` in the login folder. A keychain that is not reachable is skipped
    (the pattern scan still runs). Never log or print the values.
    """
    found: dict[str, str] = {}
    for profile in profiles:
        if profile.kind is AuthKind.CODEX_CHATGPT:
            for number, value in enumerate(_codex_tokens(profile), start=1):
                found[f"Profil „{profile.name}“, Login-Token {number}"] = value
            continue
        try:
            secret = read_secret(profile, reader)
        except AuthError:
            secret = None
        if secret:
            found[f"Profil „{profile.name}“"] = secret
    return found


def _skipped(rel: str, skip: Sequence[str]) -> bool:
    return any(rel.startswith(prefix) for prefix in skip)


def _needles(values: Sequence[tuple[str, str]]) -> list[tuple[str, bytes]]:
    """`(label, bytes)` of every value in every encoding, without duplicates."""
    needles: dict[bytes, str] = {}
    for label, value in values:
        for encoding in _ENCODINGS:
            needles.setdefault(value.encode(encoding), label)
    return [(label, needle) for needle, label in needles.items()]


def _value_hits(handle: Any, needles: Sequence[tuple[str, bytes]]) -> set[tuple[int, str]]:
    """`(line, label)` of every needle found in the file, read in chunks of `_CHUNK` bytes.

    The last `longest needle - 1` bytes of a chunk are kept in front of the next one, so a
    value across the boundary is found; a hit that ends inside that carry was counted before.
    """
    if not needles:
        return set()
    hits: set[tuple[int, str]] = set()
    keep = max(len(needle) for _, needle in needles) - 1
    carry = b""
    base_line = 1  # line number of the first byte of `carry`
    while chunk := handle.read(_CHUNK):
        buffer = carry + chunk
        for label, needle in needles:
            start = 0
            while (index := buffer.find(needle, start)) != -1:
                if index + len(needle) > len(carry):
                    hits.add((base_line + buffer.count(b"\n", 0, index), label))
                start = index + 1
        cut = max(len(buffer) - keep, 0)
        base_line += buffer.count(b"\n", 0, cut)
        carry = buffer[cut:]
    return hits


def _pattern_hits(handle: Any) -> list[tuple[int, str]]:
    """`(line, kind)` of the patterns in a text file; empty for a binary file."""
    if b"\0" in handle.read(_SNIFF):
        return []
    handle.seek(0)
    hits = []
    for number, raw in enumerate(handle, start=1):
        line = raw.decode("utf-8", errors="replace")
        hits += [(number, kind) for kind, pattern in _NAMED_PATTERNS if pattern.search(line)]
    return hits


def _scan_tree(
    report: ScanReport,
    root: Path,
    shown_as: str,
    needles: Sequence[tuple[str, bytes]],
    pattern_skip: Sequence[str],
) -> None:
    """Scan every regular file below `root`; files are reported as `<shown_as>/<path>`."""
    for folder, dirs, files in os.walk(root):
        rel_folder = Path(folder).relative_to(root).as_posix()
        prefix = shown_as + "/" if shown_as else ""
        prefix += "" if rel_folder == "." else rel_folder + "/"
        dirs.sort()
        for name in sorted(files):
            path = Path(folder) / name
            rel = prefix + name
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue  # symlinks, FIFOs, sockets, devices: `open()` could block (N1)
                with path.open("rb") as handle:
                    report.files += 1
                    found = [(n, f"Wert von {label}") for n, label in _value_hits(handle, needles)]
                    if not _skipped(rel, pattern_skip):
                        handle.seek(0)
                        found += _pattern_hits(handle)
            except OSError:
                report.skipped.append(rel)
                continue
            report.findings += [Finding(rel, number, kind) for number, kind in sorted(found)]


def _outside_venv(run_dir: Path) -> Path | None:
    """The run venv if it is a real folder outside the run folder (not a symlink)."""
    venv = run_venv_location(run_dir)
    if venv is None:
        return None
    try:
        if not stat.S_ISDIR(os.lstat(venv).st_mode):
            return None
    except OSError:
        return None
    if Path(os.path.realpath(venv)).is_relative_to(os.path.realpath(run_dir)):
        return None  # `<run>/.venv` of a run before W3-00: part of the walk anyway
    return venv


def scan_run_dir(
    run_dir: str | Path,
    secrets: Mapping[str, str] | None = None,
    *,
    pattern_skip: Sequence[str] = PATTERN_SKIP,
) -> ScanReport:
    """Scan every regular file below `run_dir` and in the run venv (`run-venv/…`): the profile
    values everywhere, the patterns in text files outside `pattern_skip` (see the module
    docstring)."""
    root = Path(run_dir)
    values = [(k, v) for k, v in (secrets or {}).items() if len(v) >= MIN_SECRET_LENGTH]
    needles = _needles(values)
    report = ScanReport(secrets=len(values))
    _scan_tree(report, root, "", needles, pattern_skip)
    venv = _outside_venv(root)
    if venv is not None:
        _scan_tree(report, venv, RUN_VENV_LABEL, needles, pattern_skip)
    return report


def format_report(report: ScanReport, *, limit: int = 40) -> str:
    """German text of a report for the terminal (never a value)."""
    checked = f"{report.files} Dateien geprüft, {report.secrets} Profil-Werte gesucht"
    if report.clean:
        text = f"Secret-Scan: sauber ({checked})."
    else:
        lines = [f"Secret-Scan: {len(report.findings)} Funde ({checked}):"]
        lines += [f"  - {f.file}:{f.line} – {f.kind}" for f in report.findings[:limit]]
        if len(report.findings) > limit:
            lines.append(f"  … und {len(report.findings) - limit} weitere")
        text = "\n".join(lines)
    if report.skipped:
        text += f"\n  Nicht lesbar: {', '.join(report.skipped[:10])}"
    return text


def record_scan(run_dir: str | Path, report: ScanReport) -> dict[str, Any]:
    """Append the report as `secret_scan` to `orchestrator.jsonl`; returns the entry."""
    return OrchestratorLog(Path(run_dir)).write(SCAN_KIND, **report.to_data())
