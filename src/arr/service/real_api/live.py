"""Area `live`: the live stream of the service (W3-01 §3.4, docs/08-ux-konzept.md §6.6).

`subscribe` hands out subscriptions of the process-wide `EventHub`; the events come from the
`RunWatcher` (`arr.service.watcher`), which this area starts and stops with the service. A
switch of the workfolder gets a new watcher (nothing of the old folder is news), and open
pages go to the start page of the new workfolder (W3-04).
"""

import asyncio
import contextlib
import time

from arr.service.api_client import Subscription
from arr.service.api_models import CountsChangedEvent
from arr.service.real_api.base import ServiceState
from arr.service.real_api.workfolder import counts
from arr.service.watcher import RunWatcher


def show_new_workfolder() -> None:
    """Send every open tab to the start page: what it shows belongs to the old workfolder."""
    from nicegui import Client

    for client in list(Client.instances.values()):
        if client.has_socket_connection:
            client.run_javascript("location.assign('/')")


class LiveArea:
    """`subscribe` and `now` of `ApiClient`, plus the watcher's lifecycle."""

    def __init__(self, state: ServiceState, *, watcher: RunWatcher | None = None) -> None:
        self.state = state
        self.watcher = watcher or RunWatcher(state)
        self._task: asyncio.Task[None] | None = None
        state.on_switch.append(self._follow_switch)

    def subscribe(self, run_id: str | None = None, agents: set[str] | None = None) -> Subscription:
        return self.state.hub.add(run_id, agents)

    @staticmethod
    def now() -> float:
        return time.time()

    async def start(self) -> None:
        """Start the watcher in the current event loop (idempotent)."""
        if self._task is None or self._task.done():
            self.watcher.tick()  # baseline now: no flood of old events on the first pass
            self._task = asyncio.create_task(self.watcher.run(), name="arr-watcher")

    def _follow_switch(self) -> None:
        """A new watcher on the new workfolder; no `await`, see `switch_workfolder`."""
        old = self.watcher
        self.watcher = RunWatcher(
            self.state,
            interval_s=old.interval_s,
            full_every_s=old.full_every_s,
            tick_every_s=old.tick_every_s,
        )
        if self._task is not None and not self._task.done():
            self._task.cancel()
            self.watcher.tick()
            self._task = asyncio.create_task(self.watcher.run(), name="arr-watcher")
        hub = self.state.hub
        hub.publish(
            CountsChangedEvent(
                seq=hub.next_seq(), ts=time.time(), run_id=None, counts=counts(self.state)
            )
        )
        show_new_workfolder()

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
