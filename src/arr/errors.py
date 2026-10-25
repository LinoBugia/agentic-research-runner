"""Exceptions with user-facing German messages, shared by all arr modules.

Messages follow the pattern of docs/08-ux-konzept.md §8.4: what happened (with names),
why if known, what the user can do. No "Fehler:" prefix, no stack traces.
"""

from dataclasses import dataclass


class ArrError(Exception):
    """Base class for errors whose message is shown to the user as is (German, docs/08 §8.4).

    The CLI prints the message without a traceback. Modules raise subclasses for expected
    failures (invalid configuration, missing files, …) and plain exceptions for bugs.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ConfigIssue:
    """One problem in a configuration file (see docs/02-workfolder-und-runs.md §3.3).

    `path` points to the offending value, e.g. `agents[1].model` or `params.judges`;
    an empty path means the file as a whole. `line` is the 1-based line in the YAML
    file when known.
    """

    path: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        where = self.path or "(Datei)"
        if self.line is not None:
            where = f"{where} (Zeile {self.line})"
        return f"{where}: {self.message}"


class ConfigError(ArrError):
    """Invalid run, group or settings configuration, reported before a run starts.

    Holds one or more `ConfigIssue`s. `path` and `message` refer to the first issue.
    `source` names the file (e.g. `run.yaml` or a full path) when known.
    See docs/02-workfolder-und-runs.md §3.3.
    """

    def __init__(self, issues: list[ConfigIssue], source: str | None = None) -> None:
        if not issues:
            raise ValueError("ConfigError needs at least one issue")
        self.issues = list(issues)
        self.source = source
        super().__init__(self._render())

    @classmethod
    def single(
        cls, path: str, message: str, *, source: str | None = None, line: int | None = None
    ) -> "ConfigError":
        """Build an error with exactly one issue."""
        return cls([ConfigIssue(path, message, line)], source=source)

    @property
    def path(self) -> str:
        """Path of the first issue, e.g. `agents[1].model`."""
        return self.issues[0].path

    @property
    def issue_message(self) -> str:
        """German message of the first issue, without path."""
        return self.issues[0].message

    def _render(self) -> str:
        head = f"Konfiguration ungültig ({self.source})" if self.source else ""
        lines = [str(issue) for issue in self.issues]
        if len(lines) == 1 and not head:
            return lines[0]
        if not head:
            head = "Konfiguration ungültig"
        return head + ":\n" + "\n".join(f"- {line}" for line in lines)
