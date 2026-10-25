"""Load test of the room with several processes (W1-03 A3, docs/spikes/S4_room.md).

5 agent processes, each with its own stdio room server (`python -m arr.room.server`), plus
an external writer process (orchestrator and user via `RoomStore`) that writes at the same
time. Checks: no lost, duplicate or foreign messages, no lock errors, cursors at the end.
Run with `-s` to see the report.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

from arr.models import GroupChatConfig
from arr.room.store import RoomStore

HERE = Path(__file__).parent / "room_load"
AGENTS = [f"agent-{i}" for i in range(5)]
CHANNELS = ["main", "alpha", "beta", "gamma", "secret"]
POSTS_PER_AGENT = 400
MAX_SECONDS = 60


def memberships() -> dict[str, list[str]]:
    """Everybody is in main plus some side channels; nobody is in secret (as in S4)."""
    member = {a: ["main"] for a in AGENTS}
    for i, agent in enumerate(AGENTS):
        if i % 2 == 0:
            member[agent].append("alpha")
        if i % 3 != 0:
            member[agent].append("beta")
        if i % 2 == 1:
            member[agent].append("gamma")
    return member


def percentiles_ms(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def at(q: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))] * 1000, 1)

    return {"n": len(ordered), "p50": at(0.5), "p95": at(0.95), "max": at(1.0)}


def test_five_agent_processes_and_concurrent_external_writer(tmp_path: Path) -> None:
    started = time.perf_counter()
    run_dir = tmp_path / "run"
    signals = tmp_path / "signals"
    signals.mkdir()
    store = RoomStore.create(run_dir)
    member = memberships()
    for channel in CHANNELS:
        members = [a for a in AGENTS if channel in member[a]] or ["nobody"]
        store.create_channel(GroupChatConfig(name=channel, members=members))

    procs: dict[str, tuple[subprocess.Popen, Path]] = {}
    for i, agent in enumerate(AGENTS):
        foreign = ["secret"] + [c for c in CHANNELS if c not in member[agent]][:1]
        cmd = [
            sys.executable, str(HERE / "agent.py"), "--agent", agent, "--run-dir", str(run_dir),
            "--channels", ",".join(member[agent]), "--foreign", ",".join(dict.fromkeys(foreign)),
            "--count", str(POSTS_PER_AGENT), "--seed", str(i), "--signal-dir", str(signals),
            "--out", str(tmp_path / f"{agent}.json"),
        ]  # fmt: skip
        log = tmp_path / f"{agent}.log"
        with log.open("w") as fh:
            procs[agent] = (subprocess.Popen(cmd, stderr=fh), log)
    writer_cmd = [
        sys.executable, str(HERE / "writer.py"), "--run-dir", str(run_dir),
        "--channels", ",".join(CHANNELS), "--agents", ",".join(AGENTS),
        "--signal-dir", str(signals), "--out", str(tmp_path / "writer.json"),
    ]  # fmt: skip
    writer_log = tmp_path / "writer.log"
    with writer_log.open("w") as fh:
        writer = subprocess.Popen(writer_cmd, stderr=fh)

    def check_alive() -> None:
        for agent, (proc, log) in procs.items():
            assert proc.poll() in (None, 0), f"{agent} failed:\n{log.read_text()}"
        assert writer.poll() in (None, 0), writer_log.read_text()
        assert time.perf_counter() - started < MAX_SECONDS, "load test too slow"

    try:
        # Common start: all room servers are connected before anybody writes.
        while not all((signals / f"{a}.ready").exists() for a in AGENTS):
            check_alive()
            time.sleep(0.02)
        startup_s = time.perf_counter() - started
        (signals / "go").touch()
        while not all((signals / f"{a}.posted").exists() for a in AGENTS):
            check_alive()
            time.sleep(0.02)
        assert writer.wait(timeout=MAX_SECONDS) == 0, writer_log.read_text()
        (signals / "all.done").touch()
        for agent, (proc, log) in procs.items():
            assert proc.wait(timeout=MAX_SECONDS) == 0, f"{agent} failed:\n{log.read_text()}"
    finally:
        for proc in [writer, *(p for p, _ in procs.values())]:
            if proc.poll() is None:
                proc.kill()
    duration_s = time.perf_counter() - started

    results = {a: json.loads((tmp_path / f"{a}.json").read_text()) for a in AGENTS}
    ext = json.loads((tmp_path / "writer.json").read_text())
    all_msgs = {name: store.messages(name) for name in CHANNELS}
    by_id = {m.id: m for msgs in all_msgs.values() for m in msgs}

    # Every post of every agent is in the database exactly once, nothing extra.
    agent_rows = [m for m in by_id.values() if m.sender in AGENTS]
    posted = [i for r in results.values() for i in r["posted"]]
    assert sorted(posted) == sorted(m.id for m in agent_rows)
    bodies = [m.body for m in by_id.values()]
    duplicates_in_db = len(bodies) - len(set(bodies))
    expected = len(CHANNELS) + len(AGENTS) * POSTS_PER_AGENT + len(ext["written"])

    lost = duplicates = cursor_bad = 0
    for agent, res in results.items():
        for channel, ids in res["received"].items():
            others = {m.id for m in all_msgs[channel] if m.sender != agent}
            lost += len(others - set(ids))
            duplicates += len(ids) - len(set(ids))
            if store.cursor(agent, channel) != max(others):
                cursor_bad += 1
    intrusions = [m for m in agent_rows if m.sender not in store.channel_config(m.channel).members]
    lock_errors = sum(
        log.read_text().count("database is locked") for _, log in procs.values()
    ) + writer_log.read_text().count("database is locked")
    server_errors = sum(
        line.count(" ERROR ") for _, log in procs.values() for line in log.read_text().splitlines()
    )
    tool_errors = [e for r in results.values() for e in r["errors"]]

    # Overlap: external writes inside the interval in which all agents were posting.
    first = max(min(m.ts for m in agent_rows if m.sender == a) for a in AGENTS)
    last = min(max(m.ts for m in agent_rows if m.sender == a) for a in AGENTS)
    writer_ts = [by_id[i].ts for i in ext["written"]]
    overlapping = sum(first <= ts <= last for ts in writer_ts)

    post_lat = [x for r in results.values() for x in r["latency"]["post_message"]]
    read_lat = [x for r in results.values() for x in r["latency"]["read_messages"]]
    report = {
        "agents": len(AGENTS),
        "posts_per_agent": POSTS_PER_AGENT,
        "messages_in_db": len(by_id),
        "expected_in_db": expected,
        "external_writes": len(ext["written"]),
        "external_writes_while_all_agents_post": overlapping,
        "all_agents_post_window_s": round(last - first, 2),
        "writer_window_s": round(max(writer_ts) - min(writer_ts), 2),
        "lost": lost,
        "duplicate_deliveries": duplicates,
        "duplicate_rows": duplicates_in_db,
        "cursor_mismatches": cursor_bad,
        "tool_errors": len(tool_errors),
        "lock_errors": lock_errors,
        "server_error_lines": server_errors,
        "external_writer_errors": len(ext["errors"]),
        "rows_in_foreign_channels": len(intrusions),
        "membership_rejected": all(
            m["post_rejected"] and m["read_rejected"]
            for r in results.values()
            for m in r["membership"].values()
        ),
        "footers_ok": all(r["footers_ok"] for r in results.values()),
        "startup_s": round(startup_s, 2),
        "duration_s": round(duration_s, 2),
        "latency_ms": {
            "post_message": percentiles_ms(post_lat),
            "read_messages": percentiles_ms(read_lat),
            "external_write": percentiles_ms(ext["latency"]),
        },
    }
    print("\nRoom-Lasttest:", json.dumps(report, indent=2, ensure_ascii=False))

    assert report["messages_in_db"] == expected
    assert tool_errors == [] and ext["errors"] == []
    assert (lost, duplicates, duplicates_in_db, cursor_bad) == (0, 0, 0, 0)
    assert (lock_errors, server_errors, len(intrusions)) == (0, 0, 0)
    assert report["membership_rejected"] and report["footers_ok"]
    assert overlapping >= 20, report
    assert duration_s < MAX_SECONDS
