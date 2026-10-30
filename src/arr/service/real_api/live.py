"""Area `live`: the live stream of the service (W3-01 §3.4, docs/08-ux-konzept.md §6.6).

`subscribe` hands out subscriptions of the process-wide `EventHub`; the events come from the
`RunWatcher` (`arr.service.watcher`), which this area starts and stops with the service.
"""

import asyncio
import contextlib
import time

from arr.service.api_client import Subscription
from arr.service.real_api.base import ServiceState
from arr.service.watcher import RunWatcher


class LiveArea:
    """`subscribe` and `now` of `ApiClient`, plus the watcher's lifecycle."""

    def __init__(self, state: ServiceState, *, watcher: RunWatcher | None = None) -> None:
        self.state = state
        self.watcher = watcher or RunWatcher(state)
        self._task: asyncio.Task[None] | None = None

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

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
