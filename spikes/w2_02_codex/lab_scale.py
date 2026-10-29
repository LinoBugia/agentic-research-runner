"""Offline: time per command for permission profiles of growing size (W2-02.1 M1, no model).

Codex hands the profile to `sandbox-exec` with every command, and Seatbelt compiles it each
time. Deny entries of realistic length (about as long as sibling paths in a workfolder below
`~/Documents/…`) are added to a small base profile; `/usr/bin/true` runs 5 times per size.

    uv run python spikes/w2_02_codex/lab_scale.py [n …]      # LENGTH=160 for longer paths
"""

import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

from codex_cli_bin import bundled_codex_path

SBX = Path(__file__).resolve().parent / "tmp" / "sbx_scale"
RUNS = 5
LENGTH = int(os.environ.get("LENGTH", "115"))


def entries(n: int, work: Path) -> list[tuple[str, str]]:
    base = [(":root", "read"), (str(work), "write"), (str(Path.home()), "deny")]
    # paths need not exist; `LENGTH` characters like `~/…/runs/20261029-0838_demo_custom_7e0d`
    runs = "/Users/someone/Documents/wf/runs"
    fill = "x" * max(0, LENGTH - len(runs) - 25)
    return base + [(f"{runs}/20261029-{i:04d}_demo_{fill}_{i:04x}", "deny") for i in range(n)]


def measure(n: int) -> tuple[float, int, int]:
    home = SBX / f"home_{n}"
    work = SBX / "work"
    for folder in (home, work):
        folder.mkdir(parents=True, exist_ok=True)
    rows = entries(n, work)
    lines = ['default_permissions = "arr"', "[permissions.arr.filesystem]"]
    lines += [f'"{k}" = "{v}"' for k, v in rows]
    (home / "config.toml").write_text("\n".join(lines) + "\n")
    times = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        proc = subprocess.run(
            [str(bundled_codex_path()), "sandbox", "-P", "arr", "-C", str(work)]
            + ["--", "/usr/bin/true"],
            env={**os.environ, "CODEX_HOME": str(home)},
            capture_output=True,
            text=True,
        )
        times.append(time.perf_counter() - t0)
        if proc.returncode != 0:
            print(f"  n={n}: Fehler {proc.stderr.strip()[-120:]}")
            break
    avg = sum(len(k) for k, _ in rows[3:]) // max(1, n)
    return statistics.median(times), len(rows), avg


def main() -> None:
    sizes = [int(a) for a in sys.argv[1:]] or [0, 250, 400, 500, 750, 1000]
    if SBX.exists():
        shutil.rmtree(SBX)
    print("Einträge  Ø Länge  Median je Befehl")
    for n in sizes:
        seconds, total, avg = measure(n)
        print(f"{total:>8}  {avg:>7}  {seconds:>6.2f} s")


if __name__ == "__main__":
    main()
