"""„Nachweis je Profil“ over all known workfolders, exact and off the event loop (W4-00).

B15: the record counts in every workfolder of `~/.arr/workfolders.json`, names the workfolder
of each run and every workfolder it could not read. Run folders are hostile: FIFOs, links,
huge and unreadable files must neither block nor lead outside. B13: exact numbers. B8: a cold
record over many large transcripts does not hold the event loop, measured relative to its own
duration. FakeBackends only: the run folders are written by the test.
"""

import asyncio
import json
import os
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from run_kit import write_profiles
from service_kit import make_run_folder, make_workfolder, transcript_line

from arr.models import RunStatus
from arr.service.api_models import ProfileUsage, ProfileUsageFolder, ProfileUsageSum
from arr.service.gui import format as fmt
from arr.service.gui.pages.profile import usage_html
from arr.service.real_api import RealApi
from arr.service.real_api.profile_usage import LIST_UNREADABLE, MISSING, UNREADABLE
from arr.service.state import remember_workfolder, workfolders_file
from arr.usage import AgentUsage, RunUsage, UsageTotal

BASE = "http://127.0.0.1:8765"
LIMIT_S = 10.0
"""Every record here must be done long before this: a FIFO that blocks would hang forever."""


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path) -> Path:
    write_profiles(arr_home, {"fake-auth": "claude_oauth", "fake-key": "claude_api_key"})
    return make_workfolder(tmp_path / "eins")


@pytest.fixture
def other(tmp_path: Path, wf: Path) -> Path:
    folder = make_workfolder(tmp_path / "zwei")
    remember_workfolder(folder)
    remember_workfolder(wf)  # the service remembers its own one first
    return folder


@pytest.fixture
async def api(wf: Path) -> AsyncIterator[RealApi]:
    service = RealApi(wf, base_url=BASE, scan=False)
    yield service
    await service.close()


async def record(api: RealApi, name: str = "fake-auth") -> ProfileUsage:
    return await asyncio.wait_for(api.get_profile_usage(name), LIMIT_S)


def write_usage(run_dir: Path, tokens: int, profile: str = "fake-auth") -> None:
    total = UsageTotal(turns=1)
    total.usage.input = tokens
    usage = RunUsage(
        run=run_dir.name, by_agent={"a": AgentUsage(profile=profile, **total.model_dump())}
    )
    (run_dir / "usage.json").write_text(usage.model_dump_json(), "utf-8")


def turn_end_line(run_dir: Path, tokens: int) -> str:
    data = {"usage": {"input": tokens, "output": 1}, "cost_usd": 0.5, "cost_kind": "estimated"}
    return transcript_line(1, "a", run_dir.name, "turn_end", data)


def transcript(run_dir: Path) -> Path:
    return run_dir / "agents" / "a" / "transcript.jsonl"


def folder(usage: ProfileUsage, path: Path) -> ProfileUsageFolder:
    [found] = [f for f in usage.workfolders if f.path == str(path)]
    return found


# --------------------------------------------------------------------------------------------
# B15: all known workfolders
# --------------------------------------------------------------------------------------------


async def test_two_workfolders_count_and_each_run_names_its_own(
    api: RealApi, wf: Path, other: Path
) -> None:
    here = make_run_folder(wf, status=RunStatus.DONE, run_id="20261101-1000_demo_custom_aaaa")
    there = make_run_folder(other, status=RunStatus.DONE, run_id="20261101-1100_demo_custom_bbbb")
    write_usage(here, 1081)
    write_usage(there, 500)
    usage = await record(api)
    assert usage.total.input == 1581 and usage.total.runs == 2
    rows = {row.run_id: row for row in usage.runs}
    assert rows[here.name].workfolder == str(wf) and rows[here.name].url == f"/runs/{here.name}"
    assert rows[there.name].workfolder == str(other) and rows[there.name].url == ""
    assert [f.path for f in usage.workfolders] == [str(wf), str(other)]
    assert folder(usage, wf).current and not folder(usage, other).current
    assert folder(usage, wf).runs == 1 and folder(usage, other).runs == 1
    assert all(f.problem is None for f in usage.workfolders)
    assert "allen Workfoldern" in usage.note and "Claude Code oder Codex" in usage.note


async def test_a_missing_and_an_unreadable_workfolder_are_named(
    api: RealApi, wf: Path, other: Path, tmp_path: Path
) -> None:
    write_usage(make_run_folder(other, status=RunStatus.DONE), 7)
    gone = tmp_path / "weg"
    locked = make_workfolder(tmp_path / "zu")
    write_usage(make_run_folder(locked, status=RunStatus.DONE), 1000)
    remember_workfolder(gone)
    remember_workfolder(locked)
    (locked / "runs").chmod(0)
    try:
        usage = await record(api)
    finally:
        (locked / "runs").chmod(0o755)
    assert folder(usage, gone).problem == MISSING
    assert folder(usage, locked).problem == UNREADABLE
    assert folder(usage, other).problem is None and usage.total.input == 7
    html = usage_html(usage, time.time())
    assert f"Nicht gezählt: {gone} fehlt." in html
    assert f"Nicht gezählt: {locked} lässt sich nicht lesen." in html


async def test_a_file_as_workfolder_is_named(api: RealApi, wf: Path, tmp_path: Path) -> None:
    file = tmp_path / "datei"
    file.write_text("kein Ordner")
    remember_workfolder(file)
    usage = await record(api)
    assert folder(usage, file).problem == "ist kein Ordner"


@pytest.mark.parametrize("damage", ["broken", "fifo", "folder", "mode"])
async def test_an_unreadable_list_is_named(
    api: RealApi, wf: Path, other: Path, damage: str
) -> None:
    write_usage(make_run_folder(wf, status=RunStatus.DONE), 3)
    path = workfolders_file()
    path.unlink()
    if damage == "broken":
        path.write_text("{ kaputt")
    elif damage == "fifo":
        os.mkfifo(path)
    elif damage == "folder":
        path.mkdir()
    else:
        path.write_text(json.dumps({"workfolders": [str(other)]}))
        path.chmod(0)
    try:
        usage = await record(api)
    finally:
        if damage == "mode":
            path.chmod(0o600)
    assert usage.total.input == 3, "the current workfolder still counts"
    [problem] = [f for f in usage.workfolders if f.problem is not None]
    assert problem.path == str(path) and problem.problem == LIST_UNREADABLE


async def test_no_list_is_no_problem(api: RealApi, wf: Path) -> None:
    usage = await record(api)
    assert [(f.path, f.problem) for f in usage.workfolders] == [(str(wf), None)]


async def test_the_same_workfolder_counts_once(
    api: RealApi, wf: Path, other: Path, tmp_path: Path
) -> None:
    write_usage(make_run_folder(other, status=RunStatus.DONE), 5)
    alias = tmp_path / "alias"
    alias.symlink_to(other)
    remember_workfolder(alias)
    remember_workfolder(wf)
    usage = await record(api)
    assert usage.total.input == 5 and usage.total.runs == 1
    assert len([f for f in usage.workfolders if f.problem is None]) == 2


async def test_a_run_without_usage_json_counts_in_the_other_workfolder(
    api: RealApi, other: Path
) -> None:
    run_dir = make_run_folder(other, status=RunStatus.INTERRUPTED)
    transcript(run_dir).write_text(turn_end_line(run_dir, 40) + "\n")
    [row] = (await record(api)).runs
    assert row.input == 40 and row.aborted and row.workfolder == str(other)


# --------------------------------------------------------------------------------------------
# B15: hostile run folders
# --------------------------------------------------------------------------------------------


def _fifo_blocks_the_old_way(fifo: Path) -> bool:
    """Countertest: reading the FIFO as before (`read_text`) hangs until a writer comes."""
    done = threading.Event()

    def read() -> None:
        fifo.read_text()
        done.set()

    threading.Thread(target=read, daemon=True).start()
    blocked = not done.wait(0.3)
    with open(fifo, "w"):  # a writer lets the reader go
        pass
    done.wait(2)
    return blocked


async def test_a_fifo_never_blocks_the_record(api: RealApi, other: Path) -> None:
    fifo_meta = make_run_folder(
        other, status=RunStatus.DONE, run_id="20261101-0900_demo_custom_f1f1"
    )
    (fifo_meta / "meta.json").unlink()
    os.mkfifo(fifo_meta / "meta.json")
    fifo_usage = make_run_folder(
        other, status=RunStatus.DONE, run_id="20261101-0901_demo_custom_f2f2"
    )
    os.mkfifo(fifo_usage / "usage.json")
    transcript(fifo_usage).write_text(turn_end_line(fifo_usage, 11) + "\n")
    fifo_yaml = make_run_folder(
        other, status=RunStatus.RUNNING, run_id="20261101-0902_demo_custom_f3f3"
    )
    (fifo_yaml / "run.yaml").unlink()
    os.mkfifo(fifo_yaml / "run.yaml")
    fifo_log = make_run_folder(
        other, status=RunStatus.RUNNING, run_id="20261101-0903_demo_custom_f4f4"
    )
    os.mkfifo(transcript(fifo_log))
    usage = await record(api)
    assert folder(usage, other).unreadable_runs == 1  # the run without a readable meta.json
    rows = {row.run_id: row for row in usage.runs}
    assert rows[fifo_usage.name].input == 11, "summed from the transcript instead"
    assert fifo_yaml.name not in rows, "no run.yaml: no agent known to be on the profile"
    assert rows[fifo_log.name].input == 0
    assert _fifo_blocks_the_old_way(fifo_meta / "meta.json")


async def test_links_are_not_followed(api: RealApi, other: Path, tmp_path: Path) -> None:
    """A link to a run, to `meta.json`, to an agent folder or to a transcript leads nowhere."""
    outside = make_workfolder(tmp_path / "draussen")
    foreign = make_run_folder(
        outside, status=RunStatus.DONE, run_id="20261101-0800_demo_custom_0f0f"
    )
    write_usage(foreign, 100_000)
    transcript(foreign).write_text(turn_end_line(foreign, 100_000) + "\n")
    (other / "runs").mkdir(exist_ok=True)
    (other / "runs" / "20261101-0801_demo_custom_1111").symlink_to(foreign)
    linked_meta = make_run_folder(
        other, status=RunStatus.DONE, run_id="20261101-0802_demo_custom_2222"
    )
    (linked_meta / "meta.json").unlink()
    (linked_meta / "meta.json").symlink_to(foreign / "meta.json")
    linked_agent = make_run_folder(
        other, status=RunStatus.RUNNING, run_id="20261101-0803_demo_custom_3333"
    )
    agent_dir = linked_agent / "agents" / "a"
    os.rename(agent_dir, linked_agent / "agents" / "a.alt")
    agent_dir.symlink_to(foreign / "agents" / "a")
    linked_log = make_run_folder(
        other, status=RunStatus.RUNNING, run_id="20261101-0804_demo_custom_4444"
    )
    transcript(linked_log).symlink_to(transcript(foreign))
    usage = await record(api)
    assert usage.total.input == 0, "nothing of the foreign run counts"
    rows = {row.run_id for row in usage.runs}
    assert rows == {linked_agent.name, linked_log.name}
    assert folder(usage, other).unreadable_runs == 1  # the linked meta.json


async def test_huge_and_unreadable_files(api: RealApi, other: Path) -> None:
    huge = make_run_folder(
        other, status=RunStatus.INTERRUPTED, run_id="20261101-0700_demo_custom_aaaa"
    )
    with transcript(huge).open("wb") as handle:
        handle.write(b'{"kind": "turn_end", "x": "' + b"y" * (3 * 1024 * 1024) + b'"}\n')
        handle.write(turn_end_line(huge, 9).encode() + b"\n")
        handle.write(b"z" * (2 * 1024 * 1024))  # a last line without end
    big_usage = make_run_folder(
        other, status=RunStatus.DONE, run_id="20261101-0701_demo_custom_bbbb"
    )
    with (big_usage / "usage.json").open("wb") as handle:
        handle.truncate(9 * 1024 * 1024)  # sparse, over the limit
    transcript(big_usage).write_text(turn_end_line(big_usage, 4) + "\n")
    locked = make_run_folder(
        other, status=RunStatus.INTERRUPTED, run_id="20261101-0702_demo_custom_cccc"
    )
    transcript(locked).write_text(turn_end_line(locked, 1000) + "\n")
    transcript(locked).chmod(0)
    try:
        usage = await record(api)
    finally:
        transcript(locked).chmod(0o600)
    rows = {row.run_id: row for row in usage.runs}
    assert rows[huge.name].input == 9, "the overlong line is skipped, the next one counts"
    assert rows[big_usage.name].input == 4, "a usage.json over the limit: from the transcript"
    assert rows[locked.name].input == 0


# --------------------------------------------------------------------------------------------
# B13: exact numbers
# --------------------------------------------------------------------------------------------


def test_exact_numbers_with_a_thousands_dot() -> None:
    assert [fmt.exact(n) for n in (None, 0, 999, 1081, 1581, 1_234_567)] == [
        "–",
        "0",
        "999",
        "1.081",
        "1.581",
        "1.234.567",
    ]
    assert fmt.tokens(1081) == "1 k", "other pages stay as they are"


def test_the_record_shows_1081_and_1581_not_twice_1_k() -> None:
    def period(label: str, tokens: int) -> ProfileUsageSum:
        return ProfileUsageSum(label=label, runs=1, input=tokens, output=tokens)

    usage = ProfileUsage(
        profile="fake-auth",
        today=period("Heute", 1081),
        week=period("7 Tage", 1581),
        total=period("Gesamt", 1581),
        note="x",
    )
    html = usage_html(usage, time.time())
    assert '<td class="num">1.081</td>' in html and '<td class="num">1.581</td>' in html
    assert "1 k" not in html


# --------------------------------------------------------------------------------------------
# B8: the cold record does not hold the event loop
# --------------------------------------------------------------------------------------------


RUNS_COLD = 60
LINES = 2500
"""Per transcript ≈ 1 MB: tool lines and every tenth a `turn_end`."""


def _cold_runs(workfolder: Path) -> None:
    import shutil

    template = make_run_folder(workfolder, status=RunStatus.INTERRUPTED)
    filler = transcript_line(1, "a", template.name, "tool_result", {"text": "x" * 380})
    end = turn_end_line(template, 1)
    lines = [end if n % 10 == 0 else filler for n in range(LINES)]
    transcript(template).write_text("\n".join(lines) + "\n")
    meta = json.loads((template / "meta.json").read_text())
    for number in range(RUNS_COLD - 1):
        run_id = f"20261001-{number // 60:02d}{number % 60:02d}_demo_custom_{number:04x}"
        target = workfolder / "runs" / run_id
        shutil.copytree(template, target)
        (target / "meta.json").write_text(json.dumps({**meta, "run_id": run_id}))


async def _longest_gap(call: object) -> tuple[float, float]:
    """(longest pause of a 5 ms ticker, duration) while `call` runs on the loop."""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.02)
    started = time.perf_counter()
    await call  # type: ignore[misc]
    duration = time.perf_counter() - started
    stop.set()
    await task
    return max(gaps), duration


async def test_the_cold_record_does_not_hold_the_event_loop(wf: Path) -> None:
    """Relative, not against a fixed time (lesson of W3-10.2): the loop's longest pause must
    stay a small part of the record's duration. The countertest runs the same cold record on
    the loop, as before W4-00: then the pause is the whole duration."""
    _cold_runs(wf)
    threaded = RealApi(wf, base_url=BASE, scan=False)
    gap, duration = await _longest_gap(threaded.get_profile_usage("fake-auth"))
    assert (await threaded.get_profile_usage("fake-auth")).total.turns == RUNS_COLD * LINES // 10
    blocking = RealApi(wf, base_url=BASE, scan=False)

    async def on_the_loop() -> None:
        blocking.profile_usage.get("fake-auth")

    old_gap, old_duration = await _longest_gap(on_the_loop())
    assert old_gap >= 0.8 * old_duration, "the countertest must see the blocked loop"
    assert duration >= 0.05, f"too little work to measure ({duration:.3f} s)"
    assert gap <= 0.25 * duration, f"longest pause {gap:.3f} s of {duration:.3f} s"
    await threaded.close()
    await blocking.close()
