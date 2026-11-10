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
from arr.service.real_api.profile_usage import (
    INCOMPLETE,
    LIST_UNREADABLE,
    MISSING,
    NOTE,
    UNREADABLE,
)
from arr.service.state import (
    known_workfolders_file,
    missed_workfolders_file,
    record_workfolder,
    remember_workfolder,
    workfolders_file,
)
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
    assert "allen Workfoldern, in denen ein Run gestartet" in usage.note
    assert "Claude Code oder Codex" in usage.note


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


# --------------------------------------------------------------------------------------------
# W4-00.1 Punkt 1: every workfolder in which a run started, never shortened
# --------------------------------------------------------------------------------------------


def known() -> list[str]:
    return json.loads(known_workfolders_file().read_text("utf-8"))["workfolders"]


def test_record_adds_once_with_mode_0600_and_starts_from_the_recent_list(
    tmp_path: Path, arr_home: Path
) -> None:
    arr_home.mkdir(parents=True)
    remember_workfolder(tmp_path / "alt")  # the service: also recorded
    for _ in range(2):
        assert record_workfolder(tmp_path / "neu")
    assert known() == [str(tmp_path / "alt"), str(tmp_path / "neu")]
    assert known_workfolders_file().stat().st_mode & 0o777 == 0o600


def test_the_list_is_never_shortened(tmp_path: Path, arr_home: Path) -> None:
    arr_home.mkdir(parents=True)
    for n in range(25):
        remember_workfolder(tmp_path / f"wf{n}")
    assert len(known()) == 25
    assert len(json.loads(workfolders_file().read_text())["workfolders"]) == 10, "GUI list stays"


_RECORD = """
import sys
from arr.service.state import record_workfolder
for n in range(int(sys.argv[2])):
    assert record_workfolder(f"{sys.argv[1]}/wf{n}", wait_s=30)
"""


def test_two_starts_at_once_lose_no_entry(tmp_path: Path, arr_home: Path) -> None:
    """Six processes (the service and `arr run`, many times) write at the same moment."""
    import subprocess
    import sys

    arr_home.mkdir(parents=True)
    env = {**os.environ, "ARR_HOME": str(arr_home)}
    procs = [
        subprocess.Popen([sys.executable, "-c", _RECORD, str(tmp_path / f"p{i}"), "15"], env=env)
        for i in range(6)
    ]
    assert [proc.wait(60) for proc in procs] == [0] * 6
    assert sorted(known()) == sorted(str(tmp_path / f"p{i}" / f"wf{n}") for i in range(6)
                                     for n in range(15))  # fmt: skip


async def test_a_list_that_cannot_be_written_lets_the_run_start_and_makes_it_incomplete(
    api: RealApi, wf: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`record_workfolder` never raises; the record still counts the workfolder (from
    `.missed`) and says „unvollständig“."""
    lone = make_workfolder(tmp_path / "nur-arr-run")
    write_usage(make_run_folder(lone, status=RunStatus.DONE), 77)
    known_workfolders_file().parent.mkdir(parents=True, exist_ok=True)
    known_workfolders_file().mkdir()  # cannot be replaced by a file
    assert record_workfolder(lone) is False
    assert "ließ sich nicht in known_workfolders.json vermerken" in caplog.text
    assert missed_workfolders_file().read_text().splitlines() == [str(lone)]
    assert missed_workfolders_file().stat().st_mode & 0o777 == 0o600
    usage = await record(api)
    assert usage.total.input == 77
    problems = {f.problem for f in usage.workfolders if f.problem is not None}
    assert problems == {LIST_UNREADABLE, INCOMPLETE}


async def test_the_record_counts_known_and_missed_and_says_incomplete(
    api: RealApi, wf: Path, tmp_path: Path, arr_home: Path
) -> None:
    only_run = make_workfolder(tmp_path / "nur-arr-run")
    write_usage(make_run_folder(only_run, status=RunStatus.DONE), 77)
    assert record_workfolder(only_run), "never opened by the service, only `arr run`"
    usage = await record(api)
    assert usage.total.input == 77 and folder(usage, only_run).runs == 1
    assert all(f.problem is None for f in usage.workfolders)
    lost = make_workfolder(tmp_path / "verloren")
    write_usage(make_run_folder(lost, status=RunStatus.DONE), 3)
    with missed_workfolders_file().open("w") as handle:
        handle.write(f"{lost}\n")
    usage = await record(api)
    assert usage.total.input == 80, "a missed workfolder still counts"
    [problem] = [f for f in usage.workfolders if f.problem is not None]
    assert problem.problem == INCOMPLETE and "unvollständig" in problem.problem
    assert INCOMPLETE in usage_html(usage, time.time())


def test_the_note_names_every_way_of_starting() -> None:
    assert "in denen ein Run gestartet" in NOTE and "„arr run“" in NOTE
    assert "zuletzt" not in NOTE


# --------------------------------------------------------------------------------------------
# W4-00.1 B2, B3, B6, B8, B9
# --------------------------------------------------------------------------------------------


async def test_b2_one_runs_folder_counts_once(api: RealApi, other: Path, tmp_path: Path) -> None:
    write_usage(make_run_folder(other, status=RunStatus.DONE), 100)
    twin = tmp_path / "zwilling"
    twin.mkdir()
    (twin / "arr.yaml").write_text("version: 1\n")
    (twin / "runs").symlink_to(other / "runs")
    remember_workfolder(twin)
    usage = await record(api)
    assert usage.total.input == 100 and usage.total.runs == 1
    assert folder(usage, twin).problem == f"gleicher runs-Ordner wie {other.name}, schon gezählt"
    assert f"gleicher runs-Ordner wie {other.name}" in usage_html(usage, time.time())


@pytest.mark.parametrize("which", ["recent", "known"])
async def test_b3_the_lists_may_be_links_of_the_user(
    api: RealApi, wf: Path, other: Path, tmp_path: Path, which: str
) -> None:
    write_usage(make_run_folder(other, status=RunStatus.DONE), 9)
    path = workfolders_file() if which == "recent" else known_workfolders_file()
    if which == "known":
        workfolders_file().unlink()
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    target = dotfiles / path.name
    os.replace(path, target)
    path.symlink_to(target)
    usage = await record(api)
    assert usage.total.input == 9 and all(f.problem is None for f in usage.workfolders)
    if which == "known":
        assert record_workfolder(tmp_path / "noch-einer")
        assert path.is_symlink(), "the user's link stays a link"
        assert str(tmp_path / "noch-einer") in json.loads(target.read_text())["workfolders"]


async def test_b3_a_fifo_as_list_still_does_not_block(api: RealApi, wf: Path) -> None:
    workfolders_file().parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(known_workfolders_file())
    usage = await record(api)
    assert [f.problem for f in usage.workfolders if f.problem] == [LIST_UNREADABLE]


def test_b6_no_empty_paragraph_when_nothing_could_be_counted() -> None:
    empty = ProfileUsageSum(label="x")
    usage = ProfileUsage(
        profile="p",
        today=empty,
        week=empty,
        total=empty,
        note="n",
        workfolders=[ProfileUsageFolder(path="/weg", name="weg", problem=MISSING)],
    )
    html = usage_html(usage, time.time())
    assert '<p class="small muted" style="margin-top:8px"></p>' not in html
    assert "Nicht gezählt: /weg fehlt." in html


async def test_b8_paths_and_names_are_escaped(api: RealApi, wf: Path, tmp_path: Path) -> None:
    odd = make_workfolder(tmp_path / 'b<&"x')
    write_usage(make_run_folder(odd, status=RunStatus.DONE), 5)
    remember_workfolder(odd)
    html = usage_html(await record(api), time.time())
    assert "b&lt;&amp;&quot;x" in html and 'b<&"x' not in html


async def test_b9_the_cache_sees_a_run_that_ends_between_two_records(
    api: RealApi, wf: Path
) -> None:
    """`meta.json` and `usage.json` change in place (same inode): new numbers, not the cache."""
    run_dir = make_run_folder(wf, status=RunStatus.RUNNING)
    transcript(run_dir).write_text(turn_end_line(run_dir, 40) + "\n")
    [row] = (await record(api)).runs
    assert row.active and row.input == 40
    meta = run_dir / "meta.json"
    inode = meta.stat().st_ino
    meta.write_text(meta.read_text().replace('"running"', '"done"'))
    write_usage(run_dir, 1000)
    usage_file = run_dir / "usage.json"
    [row] = (await record(api)).runs
    assert meta.stat().st_ino == inode and not row.active and row.input == 1000
    usage_inode = usage_file.stat().st_ino
    write_usage(run_dir, 1234)
    assert usage_file.stat().st_ino == usage_inode
    assert (await record(api)).total.input == 1234


async def test_b9_a_relative_path_in_a_list_is_not_counted(
    api: RealApi, wf: Path, other: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_usage(make_run_folder(other, status=RunStatus.DONE), 6)
    monkeypatch.chdir(other.parent)
    data = {"version": 1, "workfolders": [str(wf), other.name]}
    workfolders_file().write_text(json.dumps(data))
    known_workfolders_file().unlink()
    usage = await record(api)
    assert usage.total.input == 0 and [f.path for f in usage.workfolders] == [str(wf)]


def test_the_first_record_writes_also_for_a_workfolder_the_gui_knew(
    tmp_path: Path, arr_home: Path
) -> None:
    """Found in the browser check: the service's own workfolder is in the recent list already;
    the first record must still create the list (seeded with the recent ones)."""
    arr_home.mkdir(parents=True)
    data = {"version": 1, "workfolders": [str(tmp_path / "a"), str(tmp_path / "b")]}
    workfolders_file().write_text(json.dumps(data))
    assert record_workfolder(tmp_path / "a")
    assert known() == [str(tmp_path / "b"), str(tmp_path / "a")]


def test_remember_keeps_the_users_link_of_the_recent_list(tmp_path: Path, arr_home: Path) -> None:
    arr_home.mkdir(parents=True)
    target = tmp_path / "dotfiles" / "workfolders.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"version": 1, "workfolders": []}))
    workfolders_file().symlink_to(target)
    remember_workfolder(tmp_path / "wf")
    assert workfolders_file().is_symlink()
    assert json.loads(target.read_text())["workfolders"] == [str(tmp_path / "wf")]
