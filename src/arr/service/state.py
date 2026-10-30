"""Files of the service in the app folder (`~/.arr/`, docs/04 §1): `service.json`, the list of
recently used workfolders and the app settings.

`service.json` (mode 0600) tells a second `arr serve`, the MCP bridge (W4-01) and the Dock
start (W5-02) where the running service listens and how to open it: hostname
(`<random>.localhost`, W3-01.1 M1), port, token, PID, start time and workfolder. It is
written at start and removed at the end; a stale file (process gone) is overwritten by the
next start. `workfolders.json` remembers the workfolders the service was started with, most
recent first, so `arr serve` without `--workfolder` opens the last one. `settings.yaml` holds
what the settings page keeps for this Mac (docs/02 §1): `open_in`, `appearance` and
`notifications` (event → on/off, keys of `NotificationEvent`; a missing key means on).
"""

import io
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML, YAMLError

from arr.settings import app_dir

SERVICE_FILE = "service.json"
WORKFOLDERS_FILE = "workfolders.json"
SETTINGS_FILE = "settings.yaml"
MAX_RECENT = 10


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
    """Put `path` in front of the recent list (at most `MAX_RECENT` entries)."""
    folder = str(Path(path).expanduser().absolute())
    recent = [str(p) for p in recent_workfolders() if str(p) != folder]
    data = {"version": 1, "workfolders": [folder, *recent][:MAX_RECENT]}
    _write_private(workfolders_file(), json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def app_settings_file() -> Path:
    return app_dir() / SETTINGS_FILE


def read_app_settings() -> dict[str, Any]:
    """Content of `settings.yaml`; empty if there is none or it is unreadable."""
    try:
        data = YAML(typ="safe").load(app_settings_file().read_text(encoding="utf-8"))
    except (OSError, YAMLError):
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
