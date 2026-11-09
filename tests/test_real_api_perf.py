"""Tempo of `RealApi` (W3-01 A7): a list of 200 runs that grows linearly (against 20 runs on
the same machine, W3-10.2) and a run detail with 50 000 transcript lines in under a second,
cold (first read) and warm (cached by mtime)."""

import asyncio
import json
import shutil
import time
from pathlib import Path

import pytest
from run_kit import write_profiles
from service_kit import make_run_folder, make_workfolder, transcript_line

from arr.models import RunStatus
from arr.service.real_api import RealApi

BASE = "http://127.0.0.1:8765"
LIMIT_S = 1.0


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return make_workfolder(tmp_path)


def clone_runs(template: Path, count: int) -> list[Path]:
    runs = template.parent
    meta = json.loads((template / "meta.json").read_text())
    out = []
    for i in range(count):
        run_id = f"20261001-{i // 60:02d}{i % 60:02d}_demo_custom_{i:04x}"
        target = runs / run_id
        shutil.copytree(template, target)
        (target / "meta.json").write_text(
            json.dumps({**meta, "run_id": run_id, "created": meta["created"] - i * 60})
        )
        out.append(target)
    return out


RATIO_MAX = 20
"""200 runs may take at most this many times as long as 20: linear is ≈ 10, quadratic ≈ 100."""
CEILING_S = 3.0
"""Backstop only: the CI runner (Ubuntu 26) is about 2.5 times slower than a Mac; the criterion
that does not depend on the machine is `RATIO_MAX` (W3-10.2)."""


def _listed_runs(workfolder: Path, count: int) -> None:
    template = make_run_folder(workfolder, status=RunStatus.DONE, name="Vorlage")
    transcript = template / "agents" / "a" / "transcript.jsonl"
    with transcript.open("w", encoding="utf-8") as handle:
        for seq in range(1, 101):
            handle.write(transcript_line(seq, "a", template.name, "text", {"text": "x"}) + "\n")
    (template / "output" / "summary.md").write_text("# Zusammenfassung\n", "utf-8")
    clone_runs(template, count - 1)


def test_list_of_200_runs_under_a_second(wf: Path, tmp_path: Path) -> None:
    """The list grows linearly: 200 runs against 20 on the same machine, cold (a fresh
    `RealApi`) and warm (cached by mtime), alternately and the best of three, so neither a slow
    runner nor load fails it; a quadratic step still does (W3-10.2, before: under 1 s)."""
    small = make_workfolder(tmp_path / "klein")
    _listed_runs(small, 20)
    _listed_runs(wf, 200)

    async def measure() -> dict[int, tuple[float, float]]:
        times: dict[int, tuple[list[float], list[float]]] = {20: ([], []), 200: ([], [])}
        for _ in range(3):
            for workfolder, count in ((small, 20), (wf, 200)):
                api = RealApi(workfolder, base_url=BASE)
                start = time.perf_counter()
                page = await api.list_runs(limit=200)
                cold = time.perf_counter() - start
                start = time.perf_counter()
                await api.list_runs(limit=200)
                times[count][0].append(cold)
                times[count][1].append(time.perf_counter() - start)
                assert page.total == count
        return {n: (min(colds), min(warms)) for n, (colds, warms) in times.items()}

    best = asyncio.run(measure())
    (cold20, warm20), (cold200, warm200) = best[20], best[200]
    print(
        f"\nlist_runs: 20 Runs kalt {cold20:.3f} s, warm {warm20:.3f} s; "
        f"200 Runs kalt {cold200:.3f} s, warm {warm200:.3f} s"
    )
    assert cold200 <= RATIO_MAX * cold20 and warm200 <= RATIO_MAX * warm20, "nicht linear"
    assert cold200 < CEILING_S and warm200 < CEILING_S


def test_run_detail_with_50000_transcript_lines_under_a_second(wf: Path) -> None:
    run_dir = make_run_folder(wf, status=RunStatus.RUNNING)
    run_id = run_dir.name
    transcript = run_dir / "agents" / "a" / "transcript.jsonl"
    seq = 0
    results = 0
    turn = 0
    with transcript.open("w", encoding="utf-8") as handle:
        while seq < 50_000:
            turn += 1
            seq += 1
            handle.write(
                transcript_line(
                    seq, "a", run_id, "turn_start", {"prompt": "Los", "stage": None}, turn=turn
                )
                + "\n"
            )
            for _ in range(200):
                seq += 1
                if seq % 2:
                    handle.write(
                        transcript_line(seq, "a", run_id, "text", {"text": "x" * 120}, turn=turn)
                        + "\n"
                    )
                    continue
                tool_id = f"toolu_{seq}"
                call = {"id": tool_id, "name": "Bash", "input": {"command": "ls"}}
                handle.write(transcript_line(seq, "a", run_id, "tool_call", call, turn=turn) + "\n")
                seq += 1
                results += 1
                result = {"id": tool_id, "is_error": False, "content": "ok", "blob": None}
                handle.write(
                    transcript_line(seq, "a", run_id, "tool_result", result, turn=turn) + "\n"
                )
            seq += 1
            usage = {"input": 10, "output": 5, "cache_read": None, "cache_write": None}
            end_data = {
                "status": "ok",
                "usage": {**usage, "reasoning": None},
                "cost_usd": 0.001,
                "cost_kind": "estimated",
                "session": None,
                "num_turns": 1,
            }
            handle.write(transcript_line(seq, "a", run_id, "turn_end", end_data, turn=turn) + "\n")
    assert seq >= 50_000

    async def detail(api: RealApi) -> float:
        start = time.perf_counter()
        await api.get_run(run_id)
        await api.list_agents(run_id)
        await api.list_channels(run_id)
        await api.get_results(run_id)
        page = await api.get_transcript(run_id, "a")
        assert len(page.items) == 200 and page.items[-1].seq == seq
        assert page.total == seq - results  # tool results are bundled into their calls
        return time.perf_counter() - start

    async def measure() -> tuple[list[float], float, float]:
        # cold = a fresh RealApi, best of three like the list above (a loaded CI machine took
        # 1.077 s without a change in the code); the limit stays
        colds = []
        for _ in range(3):
            api = RealApi(wf, base_url=BASE)
            colds.append(await detail(api))
        warm = await detail(api)
        start = time.perf_counter()
        older = await api.get_transcript(run_id, "a", from_seq=25_000, limit=200)
        assert older.older is not None and older.older.events > 0
        return colds, warm, time.perf_counter() - start

    colds, warm, page = asyncio.run(measure())
    shown = ", ".join(f"{c:.3f}" for c in colds)
    print(
        f"\nrun detail (50k lines): cold {shown} s (best {min(colds):.3f} s), warm {warm:.3f} s, "
        f"older page {page:.3f} s"
    )
    assert min(colds) < LIMIT_S and warm < LIMIT_S and page < LIMIT_S
