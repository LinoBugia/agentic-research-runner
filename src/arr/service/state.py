"""Files of the service in the app folder (`~/.arr/`, docs/04 §1): `service.json`, the list of
recently used workfolders and the app settings.

`service.json` (mode 0600) tells a second `arr serve`, the MCP bridge (W4-01) and the Dock
start (W5-02) where the running service listens and how to open it: hostname
(`<random>.localhost`, W3-01.1 M1), port, token, PID, start time and workfolder. It is
written at start and removed at the end; a stale file (process gone) is overwritten by the
next start. `workfolders.json` remembers the workfolders the service was started with, most
recent first, so `arr serve` without `--workfolder` opens the last one. `known_workfolders.json`
(W4-00.1) keeps every workfolder in which a run started (`arr run`) or that the service opened,
never shortened, for „Nachweis je Profil“; `known_workfolders.missed` lists the ones that could
not be added (then the record says „unvollständig“). `settings.yaml` holds
what the settings page keeps for this Mac (docs/02 §1): `open_in`, `appearance` and
`notifications` (event → on/off, keys of `NotificationEvent`; a missing key means on).
"""

import contextlib
import fcntl
import io
import json
import logging
import os
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML, YAMLError

from arr.settings import app_dir

SERVICE_FILE = "service.json"
WORKFOLDERS_FILE = "workfolders.json"
SETTINGS_FILE = "settings.yaml"
KNOWN_FILE = "known_workfolders.json"
KNOWN_LOCK = ".known_workfolders.lock"
KNOWN_MISSED = "known_workfolders.missed"
MAX_RECENT = 10
LOCK_WAIT_S = 5.0
"""How long `record_workfolder` waits for the lock; then the entry counts as missed."""

log = logging.getLogger(__name__)


class ServiceInfo(BaseModel):
    """Content of `~/.arr/service.json`."""

    model_config = ConfigDict(extra="ignore")

    version: int = 1
    host: str
    port: int
    token: str
    pid: int
    started: float = Field(default_factory=time.time)
    workfolder: str

    @property
    def url(self) -> str:
        """Base URL of the service (its own hostname), without token."""
        return f"http://{self.host}:{self.port}"

    @property
    def open_url(self) -> str:
        """The link that opens the GUI in the browser once (sets the cookie, W3-01 §3.2)."""
        return f"{self.url}/?token={self.token}"

    def alive(self) -> bool:
        """True if the process of the service still exists (it may still be starting)."""
        return pid_alive(self.pid)


def pid_alive(pid: int) -> bool:
    """True if a process with this ID exists (signal 0)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def service_file() -> Path:
    return app_dir() / SERVICE_FILE


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path` readable by the owner only (0600), atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def write_service_info(info: ServiceInfo) -> Path:
    """Write `service.json` with mode 0600 and return its path."""
    path = service_file()
    _write_private(path, info.model_dump_json(indent=2) + "\n")
    return path


def read_service_info() -> ServiceInfo | None:
    """The `service.json`, or None if there is none or it is unreadable."""
    path = service_file()
    try:
        return ServiceInfo.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def remove_service_info(pid: int | None = None) -> None:
    """Remove `service.json`; with `pid`, only if it belongs to that process."""
    path = service_file()
    if pid is not None:
        info = read_service_info()
        if info is not None and info.pid != pid:
            return
    path.unlink(missing_ok=True)


def workfolders_file() -> Path:
    return app_dir() / WORKFOLDERS_FILE


def recent_workfolders() -> list[Path]:
    """Workfolders the service was started with, most recent first (may no longer exist)."""
    try:
        data = json.loads(workfolders_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("workfolders") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [Path(item) for item in items if isinstance(item, str) and item]


def remember_workfolder(path: str | Path) -> None:
    """Put `path` in front of the recent list (at most `MAX_RECENT` entries) and into the
    list of known workfolders (`record_workfolder`, never shortened)."""
    folder = str(Path(path).expanduser().absolute())
    record_workfolder(folder)
    recent = [str(p) for p in recent_workfolders() if str(p) != folder]
    data = {"version": 1, "workfolders": [folder, *recent][:MAX_RECENT]}
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _write_private(Path(os.path.realpath(workfolders_file())), text)  # a user's link stays (B3)


def known_workfolders_file() -> Path:
    return app_dir() / KNOWN_FILE


def missed_workfolders_file() -> Path:
    return app_dir() / KNOWN_MISSED


def _paths_in(text: str) -> list[str]:
    data = json.loads(text)
    items = data.get("workfolders") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("no list of workfolders")
    return [item for item in items if isinstance(item, str) and item]


@contextlib.contextmanager
def _locked(wait_s: float) -> Iterator[None]:
    """An exclusive `flock` on the lock file in `app_dir()`; `TimeoutError` after `wait_s`."""
    path = app_dir() / KNOWN_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("lock of the known workfolders is held") from None
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def record_workfolder(path: str | Path, *, wait_s: float = LOCK_WAIT_S) -> bool:
    """Add `path` to `known_workfolders.json` (W4-00.1); True if it is in the list now.

    Under a file lock, so a run of `arr run` and the service starting at the same moment keep
    both entries; written atomically with mode 0600 (to the target of a link the user made).
    Never raises: if the list cannot be read or written, the run starts all the same, the
    reason goes to the log and the path to `known_workfolders.missed`, so the record knows it
    is incomplete (and still counts the path).
    """
    folder = str(Path(path).expanduser().absolute())
    target = Path(os.path.realpath(known_workfolders_file()))
    try:
        with _locked(wait_s):
            try:
                known = _paths_in(target.read_text(encoding="utf-8"))
            except FileNotFoundError:
                known = None  # first time: start from what the GUI knew
            if known is not None and folder in known:
                return True
            if known is None:
                known = [str(p) for p in recent_workfolders() if str(p) != folder]
            data = {"version": 1, "workfolders": [*known, folder]}
            _write_private(target, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            return True
    except (OSError, ValueError, TimeoutError) as exc:
        log.warning(
            "Workfolder %s ließ sich nicht in %s vermerken (%s); der Nachweis je Profil ist "
            "damit unvollständig.",
            folder,
            KNOWN_FILE,
            type(exc).__name__,
        )
        with contextlib.suppress(OSError):
            fd = os.open(missed_workfolders_file(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(folder + "\n")
        return False


def app_settings_file() -> Path:
    return app_dir() / SETTINGS_FILE


def read_app_settings() -> dict[str, Any]:
    """Content of `settings.yaml`; empty if there is none or it is unreadable."""
    try:
        data = YAML(typ="safe").load(app_settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError, YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def write_app_settings(data: Mapping[str, Any]) -> None:
    """Write `settings.yaml` (0600, atomically)."""
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    buffer = io.StringIO()
    yaml.dump(dict(data), buffer)
    _write_private(app_settings_file(), buffer.getvalue())
