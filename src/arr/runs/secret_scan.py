"""Secret scan over a run folder (M1 criterion 5, docs/01 §8).

`scan_run_dir` searches every text file of a run folder for (a) the secret values of the auth
profiles the run used (`profile_secrets`: the keychain secret of a Claude or API-key profile,
the tokens in the login folder of a Codex profile; held in memory only) and (b) the patterns of
`arr.permissions.policy.redact` (API keys, private keys, `KEY=…` assignments, auth headers,
credentials in URLs). A finding names file, line and kind, never the value. `arr run` runs the
scan after the finalizer and records the result as `secret_scan` in `orchestrator.jsonl`;
`arr runs scan <id>` repeats it.

Skipped: the run venv (`.venv/`, installed software), caches and the Codex homes below `tmp/`,
symlinks and binary files (NUL byte in the first 8 KB).
"""

import json
import os
import re
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
)
from arr.runs.orchestrator import OrchestratorLog

__all__ = [
    "SCAN_KIND",
    "SKIP_PREFIXES",
    "Finding",
    "ScanReport",
    "format_report",
    "profile_secrets",
    "record_scan",
    "scan_run_dir",
]

SCAN_KIND = "secret_scan"
"""Kind of the entry in `orchestrator.jsonl` (`data`: `ScanReport.to_data()`)."""

SKIP_PREFIXES = (".venv/", "tmp/cache/", "tmp/home/")
"""Folders of the run that are not scanned (POSIX paths relative to the run folder)."""

MIN_SECRET_LENGTH = 8
"""Shorter profile secrets are not searched for (too many chance hits)."""

_SNIFF = 8192
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

_NAMED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    tuple(zip(PATTERN_KINDS, _SECRET_PATTERNS, strict=True))
    if len(PATTERN_KINDS) == len(_SECRET_PATTERNS)
    else tuple(("muster", pattern) for pattern in _SECRET_PATTERNS)
) + (
    ("env_assignment", _SECRET_ASSIGN_RE),
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
    return any(rel == prefix.rstrip("/") or rel.startswith(prefix) for prefix in skip)


def _kinds(line: str, values: Sequence[tuple[str, str]]) -> list[str]:
    kinds = [f"Wert von {label}" for label, value in values if value in line]
    kinds += [kind for kind, pattern in _NAMED_PATTERNS if pattern.search(line)]
    return kinds


def scan_run_dir(
    run_dir: str | Path,
    secrets: Mapping[str, str] | None = None,
    *,
    skip: Sequence[str] = SKIP_PREFIXES,
) -> ScanReport:
    """Scan every text file below `run_dir` for the secret values and the patterns."""
    root = Path(run_dir)
    values = [(k, v) for k, v in (secrets or {}).items() if len(v) >= MIN_SECRET_LENGTH]
    report = ScanReport(secrets=len(values))
    for folder, dirs, files in os.walk(root):
        rel_folder = Path(folder).relative_to(root).as_posix()
        prefix = "" if rel_folder == "." else rel_folder + "/"
        dirs[:] = sorted(d for d in dirs if not _skipped(prefix + d, skip))
        for name in sorted(files):
            path = Path(folder) / name
            rel = prefix + name
            if path.is_symlink() or _skipped(rel, skip):
                continue
            try:
                with path.open("rb") as handle:
                    if b"\0" in handle.read(_SNIFF):
                        continue
                    handle.seek(0)
                    report.files += 1
                    for number, raw in enumerate(handle, start=1):
                        line = raw.decode("utf-8", errors="replace")
                        for kind in _kinds(line, values):
                            report.findings.append(Finding(rel, number, kind))
            except OSError:
                report.skipped.append(rel)
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
