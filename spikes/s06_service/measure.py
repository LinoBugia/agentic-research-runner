"""Spike S6: measure the service under load (50 events/s for 60 s into a growing JSONL file).

Starts app.py and writer.py, opens /live in headless Chrome (Playwright), follows the SSE
endpoint in parallel, samples CPU/RSS of the service and the browser, calls REST and MCP
during the stream and takes screenshots for docs/spikes/img/.

    uv run --group spikes --with playwright --with psutil python spikes/s06_service/measure.py
"""

import argparse
import asyncio
import contextlib
import json
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import psutil
from mcp import Client
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
TMP = HERE / "tmp"
IMG = HERE.parent.parent / "docs" / "spikes" / "img"

# Collected in the page: frame intervals, long tasks and when each event line appeared.
INIT_JS = """
window.__m = {frames: [], longtasks: [], seen: {}};
(function () {
  let last = performance.now();
  function tick(t) { window.__m.frames.push(t - last); last = t; requestAnimationFrame(tick); }
  requestAnimationFrame(tick);
  try {
    new PerformanceObserver(l => l.getEntries().forEach(e => window.__m.longtasks.push(e.duration)))
      .observe({type: 'longtask', buffered: true});
  } catch (e) {}
  const attach = setInterval(() => {
    const log = document.querySelector('.nicegui-log');
    if (!log) return;
    clearInterval(attach);
    new MutationObserver(muts => {
      const now = performance.timeOrigin + performance.now();
      for (const m of muts) for (const n of m.addedNodes) {
        const s = (n.textContent || '').match(/^#(\\d+)/);
        if (s && !(s[1] in window.__m.seen)) window.__m.seen[s[1]] = now;
      }
    }).observe(log, {childList: true, subtree: true});
  }, 20);
})();
"""


def pct(values: list[float], q: int) -> float:
    if len(values) < 2:
        return round(values[0], 1) if values else float("nan")
    return round(statistics.quantiles(values, n=100, method="inclusive")[q - 1], 1)


def summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 1),
        "p50": pct(values, 50),
        "p95": pct(values, 95),
        "p99": pct(values, 99),
        "max": round(max(values), 1),
    }


class Sampler(threading.Thread):
    """Samples CPU % and RSS of the service process tree and of the Chrome processes."""

    def __init__(self, service_pid: int) -> None:
        super().__init__(daemon=True)
        self.service = psutil.Process(service_pid)
        self.me = psutil.Process()
        self.phase = "start"
        self.rows: list[dict] = []
        self.stop = threading.Event()

    def _tree(self, root: psutil.Process, name_filter: str | None = None) -> list[psutil.Process]:
        procs = [root, *root.children(recursive=True)]
        if name_filter:
            procs = [p for p in procs if name_filter in p.name().lower()]
        return procs

    def run(self) -> None:
        known: dict[tuple[str, int], psutil.Process] = {}  # one object per (label, pid)
        while not self.stop.wait(1.0):
            row = {"t": time.time(), "phase": self.phase}
            for label, procs in (
                ("service", self._tree(self.service)),
                ("browser", self._tree(self.me, "chrome")),
                ("renderer", self._tree(self.me, "renderer")),
            ):
                cpu = rss = 0.0
                for p in procs:
                    p = known.setdefault((label, p.pid), p)
                    with contextlib.suppress(psutil.Error):
                        cpu += p.cpu_percent(None)
                        rss += p.memory_info().rss
                row[f"{label}_cpu"] = cpu
                row[f"{label}_rss_mb"] = rss / 1024 / 1024
            self.rows.append(row)

    def by_phase(self, phase: str) -> dict:
        rows = [r for r in self.rows if r["phase"] == phase][1:]  # first sample primes cpu_percent
        out = {}
        for k in (
            "service_cpu",
            "service_rss_mb",
            "browser_cpu",
            "browser_rss_mb",
            "renderer_cpu",
            "renderer_rss_mb",
        ):
            vals = [r[k] for r in rows]
            out[k] = (
                {"mean": round(statistics.fmean(vals), 1), "max": round(max(vals), 1)}
                if vals
                else None
            )
        return out


async def follow_sse(url: str, out: list[float], stop: asyncio.Event) -> None:
    async with httpx.AsyncClient(timeout=None) as client, client.stream("GET", url) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                out.append((time.time() - json.loads(line[6:])["ts"]) * 1000)
            if stop.is_set():
                break


async def main(args: argparse.Namespace, app: subprocess.Popen, jsonl: Path) -> dict:
    base = f"http://127.0.0.1:{args.port}"
    report: dict = {"config": vars(args)}
    async with httpx.AsyncClient() as http:
        for _ in range(100):
            with contextlib.suppress(httpx.HTTPError):
                if (await http.get(f"{base}/api/stats")).status_code == 200:
                    break
            await asyncio.sleep(0.2)
        sampler = Sampler(app.pid)
        sampler.start()
        sampler.phase = "idle"
        await asyncio.sleep(args.idle)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel="chrome", headless=True)
            ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
            await ctx.add_init_script(INIT_JS)
            page = await ctx.new_page()
            t0 = time.perf_counter()
            await page.goto(f"{base}/live")
            await page.wait_for_selector(".nicegui-log")
            report["live_page_load_ms"] = round((time.perf_counter() - t0) * 1000)
            sampler.phase = "browser_idle"
            await asyncio.sleep(args.idle)

            sse_lat: list[float] = []
            stop = asyncio.Event()
            sse_task = asyncio.create_task(
                follow_sse(f"{base}/api/runs/demo/events", sse_lat, stop)
            )
            await asyncio.sleep(0.5)

            sampler.phase = "stream"
            writer = await asyncio.create_subprocess_exec(
                sys.executable, str(HERE / "writer.py"), str(jsonl),
                "--rate", str(args.rate), "--seconds", str(args.seconds),
                stdout=asyncio.subprocess.PIPE,
            )  # fmt: skip
            click_ms, rest_ms, mcp_ms = [], [], []
            t_start = time.time()
            shot_done = table_done = False
            async with Client(f"{base}/mcp/") as mcp_client:
                while writer.returncode is None and time.time() - t_start < args.seconds + 10:
                    # Click round trip: button -> server handler -> label update in the DOM.
                    label = page.get_by_text("Klicks:")
                    before = await label.inner_text()
                    t = time.perf_counter()
                    await page.get_by_role("button", name="Klick-Test").click()
                    await page.wait_for_function(
                        "b => !document.body.innerText.includes(b)", arg=before, timeout=5000
                    )
                    click_ms.append((time.perf_counter() - t) * 1000)
                    t = time.perf_counter()
                    await http.get(f"{base}/api/runs")
                    rest_ms.append((time.perf_counter() - t) * 1000)
                    t = time.perf_counter()
                    await mcp_client.call_tool("list_runs", {"limit": 5})
                    mcp_ms.append((time.perf_counter() - t) * 1000)
                    elapsed = time.time() - t_start
                    if not shot_done and elapsed > args.seconds / 2:
                        await page.screenshot(path=str(IMG / "s06_live.png"))
                        shot_done = True
                    if not table_done and elapsed > args.seconds * 0.7:
                        tpage = await ctx.new_page()
                        t = time.perf_counter()
                        await tpage.goto(f"{base}/table")
                        await tpage.wait_for_selector("tbody tr")
                        report["table_page_load_ms"] = round((time.perf_counter() - t) * 1000)
                        t = time.perf_counter()
                        await tpage.get_by_text("Kosten $").click()
                        await asyncio.sleep(0.3)
                        await tpage.get_by_label("Filter").fill("goldbach")
                        await tpage.wait_for_timeout(500)
                        report["table_sort_filter_ms_incl_waits"] = round(
                            (time.perf_counter() - t) * 1000
                        )
                        await tpage.screenshot(path=str(IMG / "s06_table.png"))
                        await tpage.close()
                        table_done = True
                    await asyncio.sleep(1.0)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(writer.wait(), 0.01)
            writer_out = (await writer.stdout.read()).decode()
            report["writer"] = json.loads(writer_out) if writer_out else None
            await asyncio.sleep(2)  # let the last events arrive
            sampler.phase = "after"
            stop.set()
            await asyncio.sleep(1.5)
            sse_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sse_task

            m = await page.evaluate("window.__m")
            await browser.close()
        sampler.stop.set()
        sampler.join()

    ts_by_seq = {}
    for line in (await asyncio.to_thread(jsonl.read_text)).splitlines():
        ev = json.loads(line)
        ts_by_seq[ev["seq"]] = ev["ts"] * 1000
    ui_lat = [m["seen"][s] - ts_by_seq[int(s)] for s in m["seen"] if int(s) in ts_by_seq]
    frames = m["frames"][5:]
    report |= {
        "events_written": len(ts_by_seq),
        "events_shown_in_ui": len(m["seen"]),
        "events_via_sse": len(sse_lat),
        "latency_ms": {
            "ui_event_to_dom": summary(ui_lat),
            "sse_event_to_client": summary(sse_lat),
            "click_round_trip": summary(click_ms),
            "rest_get_runs_500_rows": summary(rest_ms),
            "mcp_call_tool": summary(mcp_ms),
        },
        "frames": {
            "count": len(frames),
            "fps_mean": round(1000 / statistics.fmean(frames), 1) if frames else None,
            "interval_ms": summary(frames),
            "frames_over_50ms": sum(1 for f in frames if f > 50),
        },
        "longtasks": {"count": len(m["longtasks"]), "total_ms": round(sum(m["longtasks"]))},
        "resources": {p: sampler.by_phase(p) for p in ("idle", "browser_idle", "stream")},
    }
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--rate", type=float, default=50)
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--idle", type=float, default=8)
    p.add_argument("--keep", action="store_true")
    args = p.parse_args()
    TMP.mkdir(exist_ok=True)
    IMG.mkdir(parents=True, exist_ok=True)
    jsonl = TMP / "events.jsonl"
    jsonl.write_text("")
    app_log = (TMP / "app.log").open("w")
    app = subprocess.Popen(
        [sys.executable, str(HERE / "app.py"), "--jsonl", str(jsonl), "--port", str(args.port)],
        stdout=app_log,
        stderr=subprocess.STDOUT,
    )
    try:
        result = asyncio.run(main(args, app, jsonl))
    finally:
        app.terminate()
        app.wait(timeout=10)
        app_log.close()
    result["app_log_tail"] = (TMP / "app.log").read_text()[-1500:]
    out = HERE / "results" / f"s06_measure_{int(args.rate)}eps.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not args.keep:
        shutil.rmtree(TMP)
