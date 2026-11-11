"""Logging setup for arr processes: one stderr handler with time, level and logger name, and for
the service also a log file (W4-01).

See docs/01-architektur.md §2. The service log (`arr serve`) goes to `~/.arr/logs/service.log`
as well: the same lines as in the terminal, rotating (5 files of at most 5 MB), mode 0600, and
without secrets. Every line of the file passes `SecretFilter`: known secrets of the process
(the service token) and token links become `…`, and `arr.permissions.policy.redact` removes
what looks like a key.
"""

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LOG_DIR = "logs"
SERVICE_LOG = "service.log"
MAX_BYTES = 5_000_000
BACKUPS = 4
TERMINAL_COPY = "terminal_copy"
"""Mark of a record that only copies a line the terminal shows already: file only."""
_TOKEN_LINK = re.compile(r"([?&]token=)[^&\s\"'<>]+")


def setup_logging(level: str = "INFO", *, file: Path | None = None) -> None:
    """Configure the root logger to write to stderr at the given level (and to `file`)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(lambda record: not getattr(record, TERMINAL_COPY, False))
    handlers: list[logging.Handler] = [handler]
    if file is not None:
        try:
            handlers.append(private_file_handler(file))
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            print(f"Das Service-Log {file} lässt sich nicht anlegen ({reason}).", file=sys.stderr)
    logging.basicConfig(level=level.upper(), handlers=handlers, force=True)


class SecretFilter(logging.Filter):
    """Removes secrets from a record before it is written (service log, W4-01)."""

    secrets: list[str] = []
    """Literal secrets of this process (the service token), added with `add_secret`."""

    def filter(self, record: logging.LogRecord) -> bool:
        from arr.permissions.policy import redact

        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken record is written as it is
            return True
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "…")
        text = redact(_TOKEN_LINK.sub(r"\1…", text))
        record.msg, record.args = text, None
        if record.exc_info and record.exc_text is None:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def add_secret(secret: str) -> None:
    """A secret of this process that never reaches the log file."""
    if secret and secret not in SecretFilter.secrets:
        SecretFilter.secrets.append(secret)


class _PrivateRotatingHandler(logging.handlers.RotatingFileHandler):
    """`RotatingFileHandler` whose files are created with mode 0600 (also after a rotation)."""

    def _open(self):  # type: ignore[no-untyped-def]
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(self.baseFilename, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)


def private_file_handler(file: Path) -> logging.Handler:
    """A rotating handler on `file` (folder 0700, files 0600) with `SecretFilter`."""
    file.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(file.parent, 0o700)
    handler = _PrivateRotatingHandler(
        file, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(SecretFilter())
    return handler


def service_log_file() -> Path:
    """`~/.arr/logs/service.log` (in tests below `ARR_HOME`)."""
    from arr.settings import app_dir

    return app_dir() / LOG_DIR / SERVICE_LOG
