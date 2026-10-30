"""Shared state of the `RealApi` areas (W3-01 §3.3, §6).

Every area gets the one `ServiceState` of the process: the workfolder with its settings, the
run store that reads the run folders (`arr.service.real_api.rundir`), the `EventHub` of the
live stream, the registry of the runs this service executes (`RunHandle` with the
`RunManager`), the `ProfileGate`s shared by all runs, and the base URL for `gui_url`s. The
helpers below build the uniform errors (docs/08 §8.4) and the German messages of the areas
that later orders fill in (`unavailable`).

`switch_workfolder` replaces workfolder, settings, store and registry (W3-04), so areas read
them from the state on every use and never keep them. `hub` and `gates` stay: subscribers
hang on the hub, and a profile's concurrency and rate-limit state belong to the process.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arr.errors import ArrError
from arr.models import ProfilesFile, WorkfolderSettings
from arr.runs.launch import Launch, load_profiles
from arr.runs.manager import RunManager
from arr.runs.orchestrator import ProfileGate
from arr.service.api_client import EventHub
from arr.service.api_models import ApiException, ErrorCode
from arr.service.real_api.rundir import RunStore
from arr.workfolder import load_workfolder_settings

log = logging.getLogger("arr.service")


def unavailable(what: str, order: str) -> ApiException:
    """Error of an area a later order fills in (docs/08 §8.4: what, why, what to do)."""
    return ApiException.of(
        ErrorCode.UNAVAILABLE,
        f"{what} gibt es in dieser Fassung von arr noch nicht. Dieser Teil der Oberfläche "
        f"kommt mit Auftrag {order}; bis dahin bleibt die Seite ohne Inhalt.",
    )


def not_found(what: str, ident: str) -> ApiException:
    return ApiException.of(ErrorCode.NOT_FOUND, f"{what} „{ident}“ nicht gefunden.")


def not_allowed(message: str, **fields: Any) -> ApiException:
    return ApiException.of(ErrorCode.NOT_ALLOWED, message, **fields)


def from_arr_error(exc: ArrError, code: ErrorCode = ErrorCode.INVALID) -> ApiException:
    """An `ArrError` of the core as API error (its message is already German)."""
    return ApiException.of(code, exc.message)


@dataclass
class RunHandle:
    """A run this service executes: its launcher task and, once built, its `RunManager`."""

    run_id: str
    run_dir: Path
    launch: Launch
    started: float = field(default_factory=time.time)
    task: asyncio.Task[Any] | None = None
    manager: RunManager | None = None

    @property
    def done(self) -> bool:
        return self.task is not None and self.task.done()


class RunRegistry:
    """Runs of this service by ID (W3-01 §3.5)."""

    def __init__(self) -> None:
        self._handles: dict[str, RunHandle] = {}

    def add(self, handle: RunHandle) -> None:
        self._handles[handle.run_id] = handle

    def get(self, run_id: str) -> RunHandle | None:
        return self._handles.get(run_id)

    def owns(self, run_id: str) -> bool:
        """True while this service executes the run (its task is not over)."""
        handle = self._handles.get(run_id)
        return handle is not None and not handle.done

    def active(self) -> list[RunHandle]:
        """Handles whose task is still running."""
        return [h for h in self._handles.values() if not h.done]

    def all(self) -> list[RunHandle]:
        return list(self._handles.values())

    def __len__(self) -> int:
        return len(self._handles)


class ServiceState:
    """What all areas share; one per service process (see the module docstring)."""

    def __init__(
        self,
        workfolder: str | Path,
        *,
        base_url: str,
        settings: WorkfolderSettings | None = None,
        profiles: ProfilesFile | None = None,
        hub: EventHub | None = None,
        builders: Any = None,
        scan: bool = True,
        secret_reader: Any = None,
        manager_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.workfolder = Path(workfolder).expanduser().absolute()
        self.base_url = base_url.rstrip("/")
        self.settings = settings or load_workfolder_settings(self.workfolder)
        self._profiles = profiles
        self.hub = hub or EventHub()
        self.registry = RunRegistry()
        self.gates: dict[str, ProfileGate] = {}
        self.builders = builders
        self.scan = scan
        self.secret_reader = secret_reader
        self.manager_kwargs = dict(manager_kwargs or {})
        self.store = RunStore(self.workfolder / "runs")
        self.interrupted_at_start: list[str] = []
        self.started = time.time()
        self.on_switch: list[Callable[[], None]] = []

    @staticmethod
    def now() -> float:
        return time.time()

    def gui_url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def run_url(self, run_id: str) -> str:
        return self.gui_url(f"/runs/{run_id}")

    def profiles(self) -> ProfilesFile:
        """`profiles.yaml` (loaded once; `ArrError` with the hint when it is missing)."""
        if self._profiles is None:
            self._profiles = load_profiles()
        return self._profiles

    def profiles_or_empty(self) -> ProfilesFile:
        """`profiles.yaml`, or an empty list when the file does not exist yet (lists)."""
        try:
            return self.profiles()
        except ArrError:
            return ProfilesFile()

    def reload_settings(self) -> WorkfolderSettings:
        """Read `arr.yaml` again (the service keeps its settings otherwise)."""
        self.settings = load_workfolder_settings(self.workfolder)
        self._sync_gates()
        return self.settings

    def _sync_gates(self) -> None:
        """Existing `ProfileGate`s take the limits of the settings; waiters check again."""
        for name, gate in self.gates.items():
            gate.limit = self.settings.concurrency.get(name)
            gate.kick()

    def switch_workfolder(
        self, workfolder: Path, settings: WorkfolderSettings, interrupted: list[str]
    ) -> None:
        """Go over to `workfolder` in one step; the caller makes sure no own run is active.

        `interrupted`: runs of the new workfolder that its recovery changed. Runs through
        without yielding to the event loop, so no area sees a half-switched state.
        """
        self.workfolder = workfolder
        self.settings = settings
        self.store = RunStore(workfolder / "runs")
        self.registry = RunRegistry()
        self.interrupted_at_start = interrupted
        self._sync_gates()
        for listener in self.on_switch:
            listener()
