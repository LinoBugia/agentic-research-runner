"""Spike S6: one process with NiceGUI (on FastAPI), REST, SSE, a live JSONL view, a table page
and a mounted MCP Streamable-HTTP app.

    uv run --group spikes python spikes/s06_service/app.py --jsonl <file> [--port 8765] [--native]

Pages: /live (follows the JSONL file), /table. API: /api/runs, /api/stats,
/api/runs/demo/events (SSE). MCP: /mcp/ (tool list_runs).
"""

import argparse
import asyncio
import json
import random
import time
from pathlib import Path

from fastapi.responses import StreamingResponse
from mcp.server.mcpserver import MCPServer
from nicegui import app, ui

ARGS: argparse.Namespace


# --- one tailer per process, fan-out to all subscribers -------------------------------------


class Tailer:
    """Polls a growing JSONL file and hands complete lines to subscriber queues."""

    def __init__(self, path: Path, interval: float = 0.05) -> None:
        self.path = path
        self.interval = interval
        self.subscribers: set[asyncio.Queue] = set()
        self.events = 0
        self.started = time.time()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    async def run(self) -> None:
        pos = 0
        rest = b""
        while True:
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size < pos:  # file was truncated or replaced
                pos, rest = 0, b""
            if size > pos:
                with self.path.open("rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read(size - pos)
                pos = size
                *lines, rest = (rest + chunk).split(b"\n")
                for line in lines:
                    if not line.strip():
                        continue
                    ev = json.loads(line)
                    self.events += 1
                    for q in list(self.subscribers):
                        if not q.full():
                            q.put_nowait(ev)
            await asyncio.sleep(self.interval)


TAILER: Tailer


# --- REST and SSE ----------------------------------------------------------------------------

RUNS = [
    {
        "id": f"20261024-{1000 + i:04d}_collatz_exploration_{i:04x}",
        "project": random.Random(i).choice(["collatz", "goldbach", "ramsey"]),
        "skill": random.Random(i + 1).choice(["exploration", "review", "roundtable"]),
        "status": random.Random(i + 2).choice(["done", "running", "interrupted"]),
        "cost": round(random.Random(i + 3).uniform(0.1, 12), 2),
        "size_mb": round(random.Random(i + 4).uniform(1, 900), 1),
    }
    for i in range(500)
]


@app.get("/api/runs")
def api_runs() -> list[dict]:
    return RUNS


@app.get("/api/stats")
def api_stats() -> dict:
    return {"events": TAILER.events, "subscribers": len(TAILER.subscribers)}


@app.get("/api/runs/demo/events")
async def api_events() -> StreamingResponse:
    q = TAILER.subscribe()

    async def stream():
        try:
            while True:
                ev = await q.get()
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        finally:
            TAILER.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


# --- MCP Streamable HTTP in the same app -------------------------------------------------------

mcp = MCPServer("arr")


@mcp.tool()
def list_runs(limit: int = 5) -> str:
    """List the most recent runs."""
    return "\n".join(r["id"] for r in RUNS[:limit])


app.mount("/mcp", mcp.streamable_http_app(streamable_http_path="/"))


async def _mcp_session_manager() -> None:
    # Mounted sub-apps get no lifespan, so the session manager runs as a startup task.
    async with mcp.session_manager.run():
        await asyncio.Event().wait()


# --- pages -----------------------------------------------------------------------------------


@ui.page("/live")
def live_page() -> None:
    q = TAILER.subscribe()
    ui.context.client.on_delete(lambda: TAILER.unsubscribe(q))
    shown = {"n": 0, "lag": 0.0}

    with ui.row().classes("items-center gap-6"):
        ui.label("Live: transcript.jsonl").classes("text-xl font-bold")
        count = ui.label("0 Events")
        lag = ui.label("Verzögerung: –")
        clicks = ui.label("Klicks: 0")
        ui.button(
            "Klick-Test",
            on_click=lambda: clicks.set_text(f"Klicks: {int(clicks.text.split()[-1]) + 1}"),
        )
    log = ui.log(max_lines=500).classes("w-full h-[70vh] font-mono text-xs")

    def flush() -> None:
        batch = []
        while not q.empty() and len(batch) < 200:
            batch.append(q.get_nowait())
        if not batch:
            return
        now = time.time()
        for ev in batch:
            stamp = (
                time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
                + f".{int(ev['ts'] * 1000) % 1000:03d}"
            )
            log.push(
                f"#{ev['seq']:05d} {stamp} {ev['agent']:<10} {ev['type']:<14} {ev['text'][:90]}"
            )
        shown["n"] += len(batch)
        shown["lag"] = (now - batch[-1]["ts"]) * 1000
        count.set_text(f"{shown['n']} Events")
        lag.set_text(f"Verzögerung: {shown['lag']:.0f} ms")

    ui.timer(0.1, flush)


@ui.page("/table")
def table_page() -> None:
    ui.label("Runs").classes("text-xl font-bold")
    columns = [
        {"name": k, "label": label, "field": k, "sortable": True, "align": "left"}
        for k, label in [
            ("id", "Run"),
            ("project", "Projekt"),
            ("skill", "Skill"),
            ("status", "Status"),
            ("cost", "Kosten $"),
            ("size_mb", "Größe MB"),
        ]
    ]
    search = ui.input("Filter")
    table = ui.table(columns=columns, rows=RUNS, row_key="id", pagination=25).classes("w-full")
    search.bind_value(table, "filter")


@ui.page("/")
def index() -> None:
    if ARGS.native:  # the native window always opens "/"
        ui.navigate.to("/live")
    ui.link("Live-Ansicht", "/live")
    ui.link("Tabelle", "/table")


def _start_background() -> None:
    asyncio.get_running_loop().create_task(TAILER.run())
    asyncio.get_running_loop().create_task(_mcp_session_manager())


if __name__ in {"__main__", "__mp_main__"}:
    p = argparse.ArgumentParser()
    p.add_argument("--jsonl", required=True)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--native", action="store_true")
    ARGS = p.parse_args()
    TAILER = Tailer(Path(ARGS.jsonl))
    app.on_startup(_start_background)
    ui.run(
        host="127.0.0.1",
        port=ARGS.port,
        reload=False,
        show=False,
        native=ARGS.native,
        window_size=(1200, 800) if ARGS.native else None,
        title="arr – Spike S6",
        language="de",
        show_welcome_message=False,
    )
