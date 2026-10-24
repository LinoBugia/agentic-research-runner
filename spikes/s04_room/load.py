"""Spike S4: load test for the room server with several MCP client processes.

Starts N agent processes (client.py, each with its own server.py over stdio) plus one
external writer that writes directly into the same SQLite file (like the orchestrator).
Afterwards it checks: lock errors, lost or duplicate messages, cursors, membership.

    uv run --group spikes python spikes/s04_room/load.py [--agents 5] [--messages 1000]
"""

import argparse
import json
import random
import shutil
import statistics
import subprocess
import sys
import time
from multiprocessing import Process, Queue
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import connect, init_db  # noqa: E402

HERE = Path(__file__).parent
CHANNELS = ["main", "alpha", "beta", "gamma", "secret"]


def memberships(n: int) -> dict[str, list[str]]:
    """Every agent is in main plus some side channels; nobody is in secret."""
    agents = [f"agent{i}" for i in range(n)]
    member = {a: ["main"] for a in agents}
    for i, a in enumerate(agents):
        if i % 2 == 0:
            member[a].append("alpha")
        if i % 3 != 0:
            member[a].append("beta")
        if i % 2 == 1:
            member[a].append("gamma")
    return member


def external_writer(db: str, count: int, seed: int, queue: Queue) -> None:
    """Write as `orchestrator` directly into the DB, outside MCP."""
    rng = random.Random(seed)
    con = connect(db)
    lat, errors = [], []
    for seq in range(count):
        ch = rng.choice(CHANNELS)
        t0 = time.perf_counter()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO msg(ts,sender,channel,kind,body) VALUES(?,?,?,?,?)",
                (time.time(), "orchestrator", ch, "note", f"orchestrator/{seq}"),
            )
            con.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001 - spike: record every failure
            errors.append(repr(exc))
            if con.in_transaction:
                con.execute("ROLLBACK")
        lat.append(time.perf_counter() - t0)
        time.sleep(rng.uniform(0.005, 0.03))
    con.close()
    queue.put({"latency": lat, "errors": errors})


def stats_ms(values: list[float]) -> dict:
    if len(values) < 2:
        return {"n": len(values), "values": [round(x * 1000, 2) for x in values]}
    v = sorted(values)
    q = statistics.quantiles(v, n=100, method="inclusive")
    return {
        "n": len(v),
        "mean": round(statistics.fmean(v) * 1000, 2),
        "p50": round(q[49] * 1000, 2),
        "p95": round(q[94] * 1000, 2),
        "p99": round(q[98] * 1000, 2),
        "max": round(v[-1] * 1000, 2),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--agents", type=int, default=5)
    p.add_argument("--messages", type=int, default=1000, help="total messages from all agents")
    p.add_argument("--orchestrator", type=int, default=200, help="messages from external writer")
    p.add_argument("--txn", choices=["immediate", "deferred"], default="immediate")
    p.add_argument("--keep", action="store_true", help="keep the tmp folder")
    args = p.parse_args()

    work = HERE / "tmp" / f"run-{args.txn}-{int(time.time())}"
    work.mkdir(parents=True)
    db = str(work / "room.sqlite")
    init_db(db)
    member = memberships(args.agents)
    con = connect(db)
    con.executemany("INSERT INTO channel VALUES(?,?,?)", [(c, time.time(), "{}") for c in CHANNELS])
    rows = [(c, a) for a in member for c in member[a]]
    con.executemany("INSERT INTO member VALUES(?,?,NULL)", rows)
    con.close()

    per_agent = args.messages // args.agents
    t0 = time.perf_counter()
    procs = {}
    for i, (agent, chans) in enumerate(member.items()):
        foreign = ["secret"] + [c for c in CHANNELS if c not in chans and c != "secret"][:1]
        cmd = [
            sys.executable, str(HERE / "client.py"),
            "--agent", agent, "--db", db, "--channels", ",".join(chans),
            "--foreign", ",".join(foreign), "--count", str(per_agent), "--seed", str(i),
            "--txn", args.txn, "--signal-dir", str(work), "--out", str(work / f"{agent}.json"),
        ]  # fmt: skip
        log = open(work / f"{agent}.log", "w")  # noqa: SIM115 - closed after wait()
        procs[agent] = (subprocess.Popen(cmd, stderr=log), log)

    queue: Queue = Queue()
    writer = Process(target=external_writer, args=(db, args.orchestrator, 99, queue))
    writer.start()
    ext = queue.get()
    writer.join()

    while not all((work / f"{a}.posted").exists() for a in member):
        if any(pr.poll() not in (None, 0) for pr, _ in procs.values()):
            sys.exit("a client process failed, see logs in " + str(work))
        time.sleep(0.05)
    t_posted = time.perf_counter() - t0
    (work / "all.done").touch()
    for pr, log in procs.values():
        pr.wait()
        log.close()
    t_total = time.perf_counter() - t0

    results = {a: json.loads((work / f"{a}.json").read_text()) for a in member}
    con = connect(db)
    total = con.execute("SELECT count(*) FROM msg").fetchone()[0]
    by_sender = dict(con.execute("SELECT sender, count(*) FROM msg GROUP BY sender").fetchall())
    intrusions = con.execute(
        "SELECT count(*) FROM msg m WHERE sender != 'orchestrator' AND NOT EXISTS "
        "(SELECT 1 FROM member WHERE channel=m.channel AND agent=m.sender)"
    ).fetchone()[0]

    lost = dup = cursor_bad = 0
    for agent, res in results.items():
        for ch, ids in res["received"].items():
            expected = {r[0] for r in con.execute("SELECT id FROM msg WHERE channel=?", (ch,))}
            lost += len(expected - set(ids))
            dup += len(ids) - len(set(ids))
            cur = con.execute(
                "SELECT last_id FROM cursor WHERE agent=? AND channel=?", (agent, ch)
            ).fetchone()
            if expected and (cur is None or cur[0] != max(expected)):
                cursor_bad += 1
    con.close()

    lock_lines = sum((work / f"{a}.log").read_text().count("database is locked") for a in member)
    agent_errors = [e for r in results.values() for e in r["errors"]]
    membership_ok = all(
        m["post_rejected"] and m["read_rejected"]
        for r in results.values()
        for m in r["membership"].values()
    )
    sample = next(iter(results.values()))["membership"]["secret"]
    load_span = max(r["load_window"][1] for r in results.values()) - min(
        r["load_window"][0] for r in results.values()
    )
    post_lat = [x for r in results.values() for x in r["latency"]["post_message"]]
    read_lat = [x for r in results.values() for x in r["latency"]["read_messages"]]
    report = {
        "config": vars(args) | {"per_agent": per_agent},
        "wall_s": {"posting_done": round(t_posted, 2), "total": round(t_total, 2)},
        "load_phase_s": round(load_span, 2),
        "throughput_posts_per_s": round(per_agent * args.agents / load_span, 1),
        "messages_in_db": total,
        "messages_by_sender": by_sender,
        "expected_in_db": per_agent * args.agents + args.orchestrator,
        "agent_tool_errors": len(agent_errors),
        "agent_tool_error_samples": agent_errors[:3],
        "lock_errors_in_server_logs": lock_lines,
        "external_writer_errors": len(ext["errors"]),
        "lost_messages": lost,
        "duplicate_deliveries": dup,
        "cursor_mismatches": cursor_bad,
        "membership_rejected_all": membership_ok,
        "membership_sample": sample,
        "rows_written_to_foreign_channels": intrusions,
        "startup_s": stats_ms([r["startup_s"] for r in results.values()]),
        "latency_ms": {
            "post_message": stats_ms(post_lat),
            "read_messages": stats_ms(read_lat),
            "external_write": stats_ms(ext["latency"]),
        },
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not args.keep:
        shutil.rmtree(work)


if __name__ == "__main__":
    main()
