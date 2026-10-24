"""Spike S6: does `ui.run(native=True)` (pywebview) open its own window on macOS?

Starts app.py --native, waits for a window of the process tree, screenshots it to
docs/spikes/img/s06_native.png, writes events meanwhile and closes everything again.

    uv run --group spikes --with pywebview --with psutil python spikes/s06_service/native_check.py
"""

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil
import Quartz

HERE = Path(__file__).parent
TMP = HERE / "tmp"
IMG = HERE.parent.parent / "docs" / "spikes" / "img"
PORT = 8766


def windows_of(pids: set[int]) -> list[dict]:
    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    return [
        {
            "id": int(w["kCGWindowNumber"]),
            "pid": int(w["kCGWindowOwnerPID"]),
            "owner": str(w.get("kCGWindowOwnerName", "")),
            "title": str(w.get("kCGWindowName", "")),
            "bounds": dict(w["kCGWindowBounds"]),
        }
        for w in infos
        if int(w["kCGWindowOwnerPID"]) in pids and int(w.get("kCGWindowLayer", 1)) == 0
    ]


def main() -> None:
    TMP.mkdir(exist_ok=True)
    IMG.mkdir(parents=True, exist_ok=True)
    jsonl = TMP / "native.jsonl"
    jsonl.write_text("")
    t0 = time.perf_counter()
    log = (TMP / "native.log").open("w")
    app = subprocess.Popen(
        [
            sys.executable,
            str(HERE / "app.py"),
            "--jsonl",
            str(jsonl),
            "--port",
            str(PORT),
            "--native",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    writer = subprocess.Popen(
        [sys.executable, str(HERE / "writer.py"), str(jsonl), "--rate", "50", "--seconds", "20"],
        stdout=subprocess.DEVNULL,
    )
    result: dict = {"window": None}
    try:
        proc = psutil.Process(app.pid)
        while time.perf_counter() - t0 < 30 and result["window"] is None:
            pids = {proc.pid, *(c.pid for c in proc.children(recursive=True))}
            wins = windows_of(pids)
            if wins:
                result["window"] = wins[0]
                result["seconds_to_window"] = round(time.perf_counter() - t0, 2)
            time.sleep(0.2)
        result["process_tree"] = [
            {"pid": p.pid, "name": p.name(), "rss_mb": round(p.memory_info().rss / 2**20, 1)}
            for p in [proc, *proc.children(recursive=True)]
        ]
        if result["window"]:
            time.sleep(6)  # app.py redirects "/" to the live view in native mode
            # The live view in the window subscribes to the tailer: proof it loaded and connected.
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/stats") as resp:
                result["service_stats_after_6s"] = json.loads(resp.read())
            shot = IMG / "s06_native.png"
            cap = subprocess.run(
                ["screencapture", "-o", "-x", "-l", str(result["window"]["id"]), str(shot)],
                capture_output=True,
                text=True,
            )
            result["screencapture_exit"] = cap.returncode
            result["screencapture_stderr"] = cap.stderr.strip()
    finally:
        writer.terminate()
        app.terminate()
        try:
            app.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app.kill()
        log.close()
        result["app_exit"] = app.returncode
        result["log_tail"] = (TMP / "native.log").read_text()[-1500:]
        shutil.rmtree(TMP)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
