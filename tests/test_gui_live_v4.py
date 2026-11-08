"""W3-10 V4: the end of a run and every status change reach an open run detail.

The run is one of a second process (`make_run_folder` + `run.lock`), so the test sets its
status itself and the watcher of the service reports it. Each case has its counter-check: the
same situation without the part that fixes it leaves the page on „Läuft“.

- events published while the page loads wait in the subscription (it is made first);
- a lost event is caught up by the comparison of an open, unsettled run;
- a reconnect of the browser compares at once;
- a redraw that fails is tried again;
- the watcher sends a status it could not build in the next tick.
"""

import asyncio
import logging
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from service_kit import hold_lock, make_run_folder
from test_gui_real import page_html, see
from test_gui_runs_real import make_api, real_user, wf

from arr.errors import ArrError
from arr.models import RunStatus
from arr.room.store import RoomStore
from arr.room.types import AgentStatus
from arr.runs.manager import set_run_status
from arr.service import watcher as watcher_module
from arr.service.api_models import (
    AgentStateEvent,
    ApiException,
    ChannelMessageEvent,
    ErrorCode,
    RunStatusEvent,
)
from arr.service.gui.components.agent_card import AgentCard
from arr.service.gui.pages import run_detail
from arr.service.gui.pages.run_detail import RunDetailPage

__all__ = ["wf"]  # the fixture


@pytest.fixture
def running(wf: Path) -> Iterator[Path]:
    """A running run of another process."""
    folder = make_run_folder(wf, status=RunStatus.RUNNING, agents=["a"])
    lock = hold_lock(folder)
    yield folder
    lock.release()


@pytest.fixture
def pages(monkeypatch: pytest.MonkeyPatch) -> list[RunDetailPage]:
    """Every run detail page the test opens."""
    made: list[RunDetailPage] = []
    original = RunDetailPage.__init__

    def init(self: RunDetailPage, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        made.append(self)

    monkeypatch.setattr(RunDetailPage, "__init__", init)
    return made


def head(user: Any) -> str:
    """The status badge in the head of the run detail."""
    with user.client:
        try:
            return str(next(iter(user.find(marker="run-status").elements)).content)
        except AssertionError:
            return ""  # the page is still being built


async def head_shows(user: Any, word: str, within: float = 3.0) -> bool:
    end = time.monotonic() + within
    while time.monotonic() < end:
        if word in head(user):
            return True
        await asyncio.sleep(0.05)
    return False


def no_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the live stream counts (`raising=False`: the module before W3-10 has no such)."""
    monkeypatch.setattr(run_detail, "RECONCILE_SECONDS", 3600.0, raising=False)


def lose_events(api: Any, monkeypatch: pytest.MonkeyPatch, run_id: str) -> list[Any]:
    """Status and agent events of the run never reach a subscriber (what V4 looked like)."""
    hub = api.state.hub
    publish = hub.publish
    lost: list[Any] = []

    def filtered(event: Any) -> None:
        if isinstance(event, RunStatusEvent | AgentStateEvent) and event.run_id == run_id:
            lost.append(event)
            return
        publish(event)

    monkeypatch.setattr(hub, "publish", filtered)
    return lost


async def until(check: Any, within: float = 5.0) -> None:
    end = time.monotonic() + within
    while time.monotonic() < end:
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("not reached")


# --- while the page loads ---------------------------------------------------------------------


async def test_a_status_change_while_the_page_loads_arrives(
    running: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run ends while the page reads it; without the subscription made first the page
    stayed on „Läuft“ (NB3 under load, V4 after a start from „Neuer Run“)."""
    no_comparison(monkeypatch)
    api = make_api(running.parent.parent)
    watch = api.subscribe(running.name)
    original = RunDetailPage._scan_questions

    async def slow(self: RunDetailPage) -> None:
        set_run_status(running, RunStatus.DONE)  # the page is half built
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if any(
                isinstance(e, RunStatusEvent) and e.run.status is RunStatus.DONE
                for e in watch.drain()
            ):
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("the watcher did not report the end")
        await original(self)

    monkeypatch.setattr(RunDetailPage, "_scan_questions", slow)
    async with real_user(api) as user:
        await user.open(f"/runs/{running.name}")
        assert await head_shows(user, "Fertig"), head(user)


# --- a lost event ------------------------------------------------------------------------------


@pytest.mark.parametrize("compare", [True, False], ids=["comparison", "counter-check"])
async def test_a_lost_end_is_caught_up_by_the_comparison(
    running: Path, monkeypatch: pytest.MonkeyPatch, compare: bool
) -> None:
    if compare:
        monkeypatch.setattr(run_detail, "RECONCILE_SECONDS", 0.3)
    else:
        no_comparison(monkeypatch)
    api = make_api(running.parent.parent)
    async with real_user(api) as user:
        await user.open(f"/runs/{running.name}")
        assert await head_shows(user, "Läuft")
        lost = lose_events(api, monkeypatch, running.name)
        set_run_status(running, RunStatus.DONE)
        await until(lambda: any(isinstance(e, RunStatusEvent) for e in lost))
        assert await head_shows(user, "Fertig", within=2.0) is compare, head(user)


async def test_a_reconnect_compares_at_once(
    running: Path, monkeypatch: pytest.MonkeyPatch, pages: list[RunDetailPage]
) -> None:
    no_comparison(monkeypatch)
    api = make_api(running.parent.parent)
    async with real_user(api) as user:
        await user.open(f"/runs/{running.name}")
        assert await head_shows(user, "Läuft")
        lost = lose_events(api, monkeypatch, running.name)
        set_run_status(running, RunStatus.STOPPED)
        await until(lambda: any(isinstance(e, RunStatusEvent) for e in lost))
        assert not await head_shows(user, "Gestoppt", within=1.0), "counter-check: no event"
        # what NiceGUI does when the socket of the tab comes back (after sleep, Wi-Fi …)
        for handler in user.client.connect_handlers:
            user.client.safe_invoke(handler)
        assert await head_shows(user, "Gestoppt"), head(user)
        assert pages[-1].shown_status is RunStatus.STOPPED


# --- a failed redraw ---------------------------------------------------------------------------


async def test_a_failed_redraw_is_tried_again(
    running: Path, monkeypatch: pytest.MonkeyPatch, pages: list[RunDetailPage]
) -> None:
    no_comparison(monkeypatch)
    monkeypatch.setattr(run_detail, "RETRY_SECONDS", 0.1, raising=False)
    api = make_api(running.parent.parent)
    real = api.list_agents
    calls: list[str] = []

    async def flaky(run_id: str) -> Any:
        status = (await api.get_run(run_id)).run.status
        if status is RunStatus.DONE and not calls:
            calls.append("failed")
            raise ApiException.of(ErrorCode.INTERNAL, "Kurz nicht lesbar.")
        calls.append("ok")
        return await real(run_id)

    async with real_user(api) as user:
        await user.open(f"/runs/{running.name}")
        assert await head_shows(user, "Läuft")
        monkeypatch.setattr(api, "list_agents", flaky)
        set_run_status(running, RunStatus.DONE)
        assert await head_shows(user, "Fertig"), head(user)
        await until(lambda: pages[-1].shown_status is RunStatus.DONE)
        assert calls[0] == "failed" and "ok" in calls, calls


# --- the watcher -------------------------------------------------------------------------------


async def test_the_watcher_sends_a_status_it_could_not_build_in_the_next_tick(
    running: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = make_api(running.parent.parent)
    watcher = api.live.watcher
    sub = api.subscribe(running.name)
    watcher.tick()  # baseline
    real = watcher_module.run_summary
    failures: list[bool] = []

    def once(*args: Any, **kwargs: Any) -> Any:
        if not failures:
            failures.append(True)
            raise ArrError("meta.json wird gerade geschrieben")
        return real(*args, **kwargs)

    monkeypatch.setattr(watcher_module, "run_summary", once)
    set_run_status(running, RunStatus.DONE)
    watcher.tick(time.time() + 10)  # a full pass
    statuses = [e for e in sub.drain() if isinstance(e, RunStatusEvent)]
    assert statuses == [] and failures, "counter-check: the first try failed"
    watcher.tick(time.time() + 20)
    statuses = [e for e in sub.drain() if isinstance(e, RunStatusEvent)]
    assert [(e.run.status, e.previous) for e in statuses] == [(RunStatus.DONE, RunStatus.RUNNING)]
    watcher.tick(time.time() + 30)
    assert not [e for e in sub.drain() if isinstance(e, RunStatusEvent)], "sent once"


# --- W3-10.1 point 3: a lost agent state ------------------------------------------------------


def card(user: Any, agent_id: str) -> str:
    with user.client:
        try:
            return str(next(iter(user.find(marker=f"agent-{agent_id}").elements)).content)
        except AssertionError:
            return ""


async def card_shows(user: Any, agent_id: str, word: str, within: float = 2.0) -> bool:
    end = time.monotonic() + within
    while time.monotonic() < end:
        if word in card(user, agent_id):
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.parametrize("compare", [True, False], ids=["comparison", "counter-check"])
async def test_a_lost_agent_state_is_caught_up_by_the_comparison(
    running: Path, monkeypatch: pytest.MonkeyPatch, compare: bool
) -> None:
    """NB3's picture: the card misses the event that changes it; the comparison draws it."""
    if compare:
        monkeypatch.setattr(run_detail, "RECONCILE_SECONDS", 0.3)
    else:
        no_comparison(monkeypatch)
    api = make_api(running.parent.parent)
    async with real_user(api) as user:
        await user.open(f"/runs/{running.name}")
        assert await card_shows(user, "a", "wartet"), card(user, "a")
        lost = lose_events(api, monkeypatch, running.name)
        RoomStore(running).set_agent_status("a", AgentStatus.WORKING)
        await until(lambda: any(isinstance(e, AgentStateEvent) for e in lost))
        assert await card_shows(user, "a", "arbeitet") is compare, card(user, "a")


# --- W3-10.1 point 4: the run folder vanishes -------------------------------------------------


async def test_a_vanished_run_folder_shows_the_banner_and_stops_comparing(
    wf: Path,
    monkeypatch: pytest.MonkeyPatch,
    pages: list[RunDetailPage],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(run_detail, "RECONCILE_SECONDS", 0.2)
    caplog.set_level(logging.INFO, logger="arr.service.gui")
    folder = make_run_folder(wf, status=RunStatus.RUNNING, agents=["a"])
    lock = hold_lock(folder)
    api = make_api(wf)
    async with real_user(api) as user:
        await user.open(f"/runs/{folder.name}")
        assert await head_shows(user, "Läuft")
        lock.release()
        shutil.rmtree(folder)
        html = await see(user, f"Run-Ordner nicht lesbar: {folder.name}.", retries=100)
        assert "Zu den Runs" in html
        page = pages[-1]
        await asyncio.sleep(1.2)  # six periods of the comparison
        assert not page.reconcile_timer.active, "the comparison stops"
        gone = [r for r in caplog.records if "nicht mehr lesbar" in r.getMessage()]
        failed = [r for r in caplog.records if "Abgleich" in r.getMessage()]
        assert len(gone) == 1 and gone[0].exc_info and not failed, caplog.text


# --- W3-10.1 point 8: resync of the watcher, catch-up of messages ------------------------------


async def test_a_failed_pass_of_the_watcher_resends_agents_and_run(
    running: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = make_api(running.parent.parent)
    watcher = api.live.watcher
    sub = api.subscribe(running.name)
    watcher.tick()  # baseline
    RoomStore(running).set_agent_status("a", AgentStatus.WORKING)
    real = watcher_module.agent_states
    calls: list[int] = []

    def once(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Datenbank kurz gesperrt")
        return real(*args, **kwargs)

    monkeypatch.setattr(watcher_module, "agent_states", once)
    watcher.tick()  # the cursors move on, then the pass fails
    events = sub.drain()
    assert calls and not [e for e in events if isinstance(e, AgentStateEvent)], "counter-check"
    watcher.tick()  # nothing new in the files: only the resync sends
    events = sub.drain()
    agents = [e.agent for e in events if isinstance(e, AgentStateEvent)]
    assert [(a.id, a.state.value) for a in agents] == [("a", "working")]
    assert any(isinstance(e, RunStatusEvent) for e in events)
    watcher.tick()
    assert not [e for e in sub.drain() if isinstance(e, AgentStateEvent)], "once"


@pytest.mark.parametrize("compare", [True, False], ids=["comparison", "counter-check"])
async def test_a_lost_message_is_caught_up_by_the_comparison(
    running: Path, monkeypatch: pytest.MonkeyPatch, compare: bool
) -> None:
    if compare:
        monkeypatch.setattr(run_detail, "RECONCILE_SECONDS", 0.3)
    else:
        no_comparison(monkeypatch)
    api = make_api(running.parent.parent)
    hub = api.state.hub
    publish = hub.publish
    lost: list[Any] = []

    def without_messages(event: Any) -> None:
        if isinstance(event, ChannelMessageEvent) and event.run_id == running.name:
            lost.append(event)
            return
        publish(event)

    async with real_user(api) as user:
        await user.open(f"/runs/{running.name}?kanal=main")
        assert await head_shows(user, "Läuft")
        monkeypatch.setattr(hub, "publish", without_messages)
        RoomStore(running).post("main", "Nachricht-ohne-Ereignis")
        await until(lambda: bool(lost))
        found = False
        for _ in range(40):
            found = "Nachricht-ohne-Ereignis" in page_html(user)
            if found:
                break
            await asyncio.sleep(0.05)
        assert found is compare


async def test_a_card_that_missed_a_draw_is_drawn_again(
    running: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page knew the new state (`self.agents`) but the card never drew it: the next pass
    compares with the card, not with `self.agents` (W3-10.1 point 3)."""
    no_comparison(monkeypatch)
    monkeypatch.setattr(run_detail, "RETRY_SECONDS", 0.1)
    api = make_api(running.parent.parent)
    real = AgentCard.update
    failures: list[str] = []

    def flaky(self: AgentCard, agent: Any, **kwargs: Any) -> None:
        if agent.state.value == "working" and not failures:
            failures.append(agent.id)
            raise RuntimeError("Element kurz nicht zeichenbar")
        real(self, agent, **kwargs)

    async with real_user(api) as user:
        await user.open(f"/runs/{running.name}")
        assert await card_shows(user, "a", "wartet")
        monkeypatch.setattr(AgentCard, "update", flaky)
        RoomStore(running).set_agent_status("a", AgentStatus.WORKING)
        assert await card_shows(user, "a", "arbeitet", within=3.0), card(user, "a")
        assert failures == ["a"], "the first draw failed"
