"""Live events of the service (W3-01 A5, A6): a run of a second process (`arr run`) appears
live and can be stopped from the service; latency from a write in the run folder to the event
is below one second for transcript lines, channel messages and status changes, also under
load; half-written lines, damaged and truncated files do not stop the watcher."""

import asyncio
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from run_kit import say, turns, wait, write_profiles
from service_kit import (
    append,
    hold_lock,
    make_run_folder,
    make_workfolder,
    start_run_process,
    transcript_line,
)

from arr.models import RunStatus, Starter
from arr.room.store import RoomStore
from arr.runs.manager import set_run_status
from arr.service.api_client import Subscription
from arr.service.api_models import (
    AgentTranscriptEvent,
    ApiException,
    ChannelMessageEvent,
    ErrorCode,
    LiveEvent,
    RunAction,
    RunFilter,
    RunStatusEvent,
)
from arr.service.real_api import RealApi

BASE = "http://127.0.0.1:8765"
LATENCY_LIMIT_S = 1.0


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return make_workfolder(tmp_path)


async def wait_event(
    sub: Subscription, matches: Callable[[LiveEvent], bool], within: float = 10.0
) -> tuple[LiveEvent, float]:
    """The first matching event and the time it was seen."""
    end = time.monotonic() + within
    while time.monotonic() < end:
        for event in sub.drain():
            if matches(event):
                return event, time.time()
        await asyncio.sleep(0.01)
    raise AssertionError("event did not arrive")


def is_status(status: RunStatus, run_id: str | None = None) -> Callable[[LiveEvent], bool]:
    return lambda e: (
        isinstance(e, RunStatusEvent)
        and e.run.status is status
        and (run_id is None or e.run_id == run_id)
    )


# --- A5: a run of a second process -----------------------------------------------------------


async def test_run_of_a_second_process_appears_live_and_can_be_stopped(
    tmp_path: Path, wf: Path, arr_home: Path
) -> None:
    (wf / "solo.yaml").write_text(
        "version: 1\nproject: demo\ntask: Los.\ntopology: {kind: solo}\n"
        "defaults: {provider: claude, model: fake-model, auth: fake-auth}\n"
        "agents: [{id: a}]\n",
        encoding="utf-8",
    )
    api = RealApi(wf, base_url=BASE)
    await api.start()
    sub = api.subscribe()
    process = start_run_process(
        tmp_path, arr_home, wf / "solo.yaml", {"a": turns([say("los"), wait(120)])}
    )
    try:
        # the folder appears and turns `running`; the service sees it within a second
        end = time.monotonic() + 60
        seen_running_at = None
        while time.monotonic() < end and seen_running_at is None:
            for folder in (wf / "runs").glob("*/meta.json"):
                try:
                    if json.loads(folder.read_text())["status"] == "running":
                        seen_running_at = time.time()
                except (OSError, ValueError):
                    pass
            await asyncio.sleep(0.01)
        assert seen_running_at is not None, "the run never started"
        event, at = await wait_event(sub, is_status(RunStatus.RUNNING), within=5.0)
        assert at - seen_running_at < LATENCY_LIMIT_S
        run_id = event.run_id
        assert run_id is not None
        page = await api.list_runs(status=RunFilter.ACTIVE)
        assert [s.id for s in page.items] == [run_id] and page.items[0].starter is Starter.CLI
        detail = await api.get_run(run_id)
        assert detail.actions_allowed == [RunAction.STOP]
        with pytest.raises(ApiException) as error:
            await api.run_action(run_id, RunAction.PAUSE)
        assert error.value.error.code is ErrorCode.NOT_ALLOWED and "Terminal" in str(error.value)
        result = await api.run_action(run_id, RunAction.STOP)
        assert "Stopp angefordert" in result.message
        code = await asyncio.to_thread(process.wait, 90)
        assert code == 130
        await wait_event(sub, is_status(RunStatus.STOPPED, run_id), within=5.0)
        assert (await api.get_run(run_id)).run.status is RunStatus.STOPPED
        assert not (wf / "runs" / run_id / "stop.request").exists()
    finally:
        if process.poll() is None:
            process.kill()
        await api.close()


# --- A6: latency -----------------------------------------------------------------------------


async def measure(
    sub: Subscription, write: Callable[[], None], matches: Callable[[LiveEvent], bool]
) -> float:
    started = time.time()
    await asyncio.to_thread(write)
    _event, at = await wait_event(sub, matches, within=5.0)
    return at - started


async def test_latency_below_a_second_for_transcript_message_and_status(wf: Path) -> None:
    run_dir = make_run_folder(wf, status=RunStatus.RUNNING)
    lock = hold_lock(run_dir)
    run_id = run_dir.name
    transcript = run_dir / "agents" / "a" / "transcript.jsonl"
    room = RoomStore(run_dir)
    api = RealApi(wf, base_url=BASE)
    await api.start()
    sub = api.subscribe(run_id, agents={"a"})
    try:
        latencies = []
        for i in range(1, 8):
            latencies.append(
                await measure(
                    sub,
                    lambda i=i: append(
                        transcript,
                        transcript_line(i, "a", run_id, "text", {"text": f"Zeile {i}"}) + "\n",
                    ),
                    lambda e, i=i: isinstance(e, AgentTranscriptEvent) and e.event.seq == i,
                )
            )
            latencies.append(
                await measure(
                    sub,
                    lambda i=i: room.post("main", f"Nachricht {i}") and None,
                    lambda e, i=i: (
                        isinstance(e, ChannelMessageEvent) and e.message.body == f"Nachricht {i}"
                    ),
                )
            )
        latencies.append(
            await measure(
                sub,
                lambda: set_run_status(run_dir, RunStatus.PAUSED) and None,
                is_status(RunStatus.PAUSED),
            )
        )
        latencies.append(
            await measure(
                sub,
                lambda: set_run_status(run_dir, RunStatus.RUNNING) and None,
                is_status(RunStatus.RUNNING),
            )
        )
        assert max(latencies) < LATENCY_LIMIT_S, latencies
        # the transcript page sees the same lines
        page = await api.get_transcript(run_id, "a")
        assert [e.seq for e in page.items] == list(range(1, 8))
    finally:
        await api.close()
        lock.release()


async def test_latency_under_load_with_a_large_transcript(wf: Path) -> None:
    run_dir = make_run_folder(wf, status=RunStatus.RUNNING)
    lock = hold_lock(run_dir)
    run_id = run_dir.name
    transcript = run_dir / "agents" / "a" / "transcript.jsonl"
    with transcript.open("w", encoding="utf-8") as handle:
        for seq in range(1, 20_001):
            handle.write(transcript_line(seq, "a", run_id, "text", {"text": "x" * 80}) + "\n")
    api = RealApi(wf, base_url=BASE)
    await api.start()
    sub = api.subscribe(run_id, agents={"a"})
    stop = threading.Event()
    counter = {"seq": 20_000}

    def writer() -> None:  # a busy agent: ~200 lines per second
        while not stop.is_set():
            counter["seq"] += 1
            append(
                transcript,
                transcript_line(counter["seq"], "a", run_id, "thinking", {"text": "…"}) + "\n",
            )
            time.sleep(0.005)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        latencies = []
        for i in range(5):
            marker = f"Marke {i}"
            latencies.append(
                await measure(
                    sub,
                    lambda marker=marker, i=i: append(
                        transcript,
                        transcript_line(10**6 + i, "a", run_id, "text", {"text": marker}) + "\n",
                    ),
                    lambda e, marker=marker: (
                        isinstance(e, AgentTranscriptEvent) and e.event.data.get("text") == marker
                    ),
                )
            )
        assert max(latencies) < LATENCY_LIMIT_S, latencies
    finally:
        stop.set()
        thread.join(timeout=5)
        await api.close()
        lock.release()


async def test_half_lines_damaged_and_truncated_files_do_not_stop_the_watcher(wf: Path) -> None:
    run_dir = make_run_folder(wf, status=RunStatus.RUNNING)
    lock = hold_lock(run_dir)
    run_id = run_dir.name
    transcript = run_dir / "agents" / "a" / "transcript.jsonl"
    api = RealApi(wf, base_url=BASE)
    await api.start()
    sub = api.subscribe(run_id, agents={"a"})
    try:
        full = transcript_line(1, "a", run_id, "text", {"text": "halbe Zeile"})
        half = len(full) // 2
        await asyncio.to_thread(append, transcript, full[:half])
        await asyncio.sleep(0.6)
        torn = [e for e in sub.drain() if isinstance(e, AgentTranscriptEvent)]
        assert not torn, "a torn line is not an event"
        await asyncio.to_thread(append, transcript, full[half:] + "\n")
        event, _ = await wait_event(sub, lambda e: isinstance(e, AgentTranscriptEvent))
        assert isinstance(event, AgentTranscriptEvent) and event.event.data["text"] == "halbe Zeile"
        # a damaged line is skipped, the next good one arrives
        await asyncio.to_thread(append, transcript, "{kaputt\n")
        await asyncio.to_thread(
            append, transcript, transcript_line(2, "a", run_id, "text", {"text": "danach"}) + "\n"
        )
        event, _ = await wait_event(sub, lambda e: isinstance(e, AgentTranscriptEvent))
        assert isinstance(event, AgentTranscriptEvent) and event.event.seq == 2
        # the orchestrator log with a damaged line still gives the log view
        append(run_dir / "orchestrator.jsonl", "nicht json\n")
        assert isinstance(await api.get_log(run_id), list)
        # a truncated transcript is indexed afresh
        transcript.write_text("", encoding="utf-8")
        await asyncio.sleep(0.4)
        await asyncio.to_thread(
            append, transcript, transcript_line(1, "a", run_id, "text", {"text": "neu"}) + "\n"
        )
        event, _ = await wait_event(sub, lambda e: isinstance(e, AgentTranscriptEvent))
        assert isinstance(event, AgentTranscriptEvent) and event.event.data["text"] == "neu"
        assert (await api.get_transcript(run_id, "a")).total == 1
        # an unreadable file: no exception, messages still flow
        if os.geteuid() != 0:
            transcript.chmod(0)
            try:
                api.live.watcher.tick()
                room = RoomStore(run_dir)
                await asyncio.to_thread(room.post, "main", "trotzdem")
                await wait_event(sub, lambda e: isinstance(e, ChannelMessageEvent))
            finally:
                transcript.chmod(0o644)
        # a `meta.json` that is half written keeps the last good status
        (run_dir / "meta.json").write_text("{", encoding="utf-8")
        api.live.watcher.tick()
        assert (await api.get_run(run_id)).run.status is RunStatus.RUNNING
        (run_dir / "meta.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": "stopped",
                    "starter": "cli",
                    "project": "demo",
                    "created": time.time() - 10,
                    "started": time.time() - 9,
                    "ended": time.time(),
                    "arr_version": "0",
                }
            ),
            encoding="utf-8",
        )
        await wait_event(sub, is_status(RunStatus.STOPPED))
    finally:
        await api.close()
        lock.release()
