"""Spike S5: APFS clones vs. copies – duration and actually used space.

Creates ~500 MB of test data in many files under spikes/s05_apfs/tmp/, compares
`cp -c -R`, clonefile(2) via ctypes and shutil.copytree, measures extra space after
changes in a clone, validates the size method from sizes.py against the space freed on
deletion, checks non-APFS targets via disk images, and deletes everything again.

    uv run python spikes/s05_apfs/main.py [--reps 3] [--keep]
"""

import argparse
import ctypes
import errno
import json
import os
import platform
import plistlib
import random
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from sizes import folder_size  # noqa: E402

HERE = Path(__file__).parent
TMP = HERE / "tmp"
MiB = 1024 * 1024

_libc = ctypes.CDLL("libc.dylib", use_errno=True)


# --- helpers -------------------------------------------------------------------------------


def clonefile(src: Path, dst: Path) -> None:
    """clonefile(2): clones a file or a whole directory tree in one call."""
    if _libc.clonefile(os.fsencode(src), os.fsencode(dst), 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(dst))


def fs_type(path: Path) -> str:
    """f_fstypename from statfs(2) (struct statfs, 64-bit inode layout)."""
    buf = ctypes.create_string_buffer(2400)
    if _libc.statfs(os.fsencode(path), buf) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(path))
    return buf.raw[72:88].split(b"\0")[0].decode()


def can_clone(src: Path, dst_parent: Path) -> bool:
    """Cheap pre-check: same device and APFS. The real answer is the errno of clonefile."""
    return src.stat().st_dev == dst_parent.stat().st_dev and fs_type(dst_parent) == "apfs"


def clone_or_copy(src: Path, dst: Path) -> str:
    """What arr would do: try clonefile, fall back to copytree on EXDEV/ENOTSUP."""
    try:
        clonefile(src, dst)
        return "clonefile"
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.ENOTSUP):
            raise
        shutil.copytree(src, dst, symlinks=True)
        return f"copytree (clonefile: {errno.errorcode[exc.errno]})"


def free_bytes(path: Path) -> int:
    """Free space for the volume of path, after sync and a short settle (median of 3)."""
    os.sync()
    time.sleep(1.0)
    samples = []
    for _ in range(3):
        st = os.statvfs(path)
        samples.append(st.f_bavail * st.f_frsize)
        time.sleep(0.2)
    return int(statistics.median(samples))


def df(path: Path) -> str:
    return subprocess.run(["df", "-k", str(path)], capture_output=True, text=True).stdout.strip()


def du_k(path: Path) -> int:
    out = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True).stdout
    return int(out.split()[0]) * 1024


def mib(n: int | None) -> float | None:
    return None if n is None else round(n / MiB, 1)


def make_data(root: Path, small: int, big: int, seed: int = 1) -> None:
    """Many small files (1–16 KiB) plus 1 MiB files, incompressible content."""
    rng = random.Random(seed)
    for i in range(small):
        d = root / f"src{i % 40:02d}" / f"sub{i % 7}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"note{i:05d}.tex").write_bytes(os.urandom(rng.randint(1, 16) * 1024))
    for i in range(big):
        d = root / "data" / f"part{i % 12:02d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"blob{i:04d}.bin").write_bytes(os.urandom(MiB))


def big_files(root: Path, n: int) -> list[Path]:
    return sorted((root / "data").rglob("*.bin"))[:n]


# --- experiments ---------------------------------------------------------------------------


def compare_methods(src: Path, reps: int) -> list[dict]:
    methods = {
        "cp -c -R": lambda s, d: subprocess.run(["cp", "-c", "-R", str(s), str(d)], check=True),
        "clonefile (ctypes)": clonefile,
        "shutil.copytree": lambda s, d: shutil.copytree(s, d, symlinks=True),
        "cp -R (ohne -c)": lambda s, d: subprocess.run(["cp", "-R", str(s), str(d)], check=True),
    }
    rows = []
    for name, fn in methods.items():
        for rep in range(reps):
            dst = TMP / "copy"
            before = free_bytes(TMP)
            t0 = time.perf_counter()
            fn(src, dst)
            dur = time.perf_counter() - t0
            used = before - free_bytes(TMP)
            fsz = folder_size(str(dst))
            rows.append(
                {
                    "method": name,
                    "rep": rep + 1,
                    "seconds": round(dur, 3),
                    "free_delta_mib": mib(used),
                    "du_mib": mib(du_k(dst)),
                    "logical_mib": mib(fsz.logical),
                    "private_mib": mib(fsz.private),
                    "shared_files": fsz.shared_files,
                }
            )
            print(rows[-1], flush=True)
            shutil.rmtree(dst)
    return rows


def modifications(src: Path) -> list[dict]:
    """Change files inside a clone and measure the extra space."""
    clone = TMP / "clone"
    clonefile(src, clone)
    rows = []

    def step(name: str, action) -> None:
        before_free = free_bytes(TMP)
        before = folder_size(str(clone))
        action()
        after_free = free_bytes(TMP)
        after = folder_size(str(clone))
        rows.append(
            {
                "change": name,
                "free_delta_mib": mib(before_free - after_free),
                "private_delta_mib": mib(after.private - before.private),
                "du_delta_mib": mib(after.allocated - before.allocated),
            }
        )
        print(rows[-1], flush=True)

    files = big_files(clone, 60)

    def inplace() -> None:
        for f in files[0:10]:
            with f.open("r+b") as fh:
                fh.seek(512 * 1024)
                fh.write(b"!")

    def append() -> None:
        for f in files[10:20]:
            with f.open("ab") as fh:
                fh.write(os.urandom(64 * 1024))

    def rewrite() -> None:
        for f in files[20:30]:
            f.write_bytes(os.urandom(MiB))

    def atomic_save() -> None:
        for f in files[30:40]:
            tmp = f.with_suffix(".tmp")
            tmp.write_bytes(f.read_bytes()[:-1] + b"?")
            os.replace(tmp, f)

    def new_files() -> None:
        d = clone / "new"
        d.mkdir()
        for i in range(20):
            (d / f"n{i}.bin").write_bytes(os.urandom(MiB))

    def delete() -> None:
        for f in files[40:60]:
            f.unlink()

    step("1 Byte in 10 Dateien à 1 MiB überschrieben (in place)", inplace)
    step("64 KiB an 10 Dateien angehängt", append)
    step("10 Dateien à 1 MiB komplett neu geschrieben", rewrite)
    step("10 Dateien „Editor-Speichern“ (temp + rename, 1 Byte anders)", atomic_save)
    step("20 neue Dateien à 1 MiB", new_files)
    step("20 Dateien à 1 MiB gelöscht", delete)
    return rows


def validate_method(src: Path) -> list[dict]:
    """Run-like folder (base + 3 work clones), predicted vs. freed space on deletion.

    Case A: the source project still exists. Case B: the source project was deleted first."""
    rows = []
    for case in ("A: Quelle bleibt", "B: Quelle vorher gelöscht"):
        project = TMP / "project"
        shutil.copytree(src, project)  # real copy: nothing outside the run shares its blocks
        run = TMP / "run"
        (run / "agents").mkdir(parents=True)
        clonefile(project, run / "base")
        for a in ("a1", "a2", "a3"):
            (run / "agents" / a).mkdir()
            clonefile(project, run / "agents" / a / "work")
            for f in big_files(run / "agents" / a / "work", 5):
                f.write_bytes(os.urandom(MiB))  # 5 MiB changed per agent
            (run / "agents" / a / "transcript.jsonl").write_bytes(os.urandom(2 * MiB))
        if case.startswith("B"):
            shutil.rmtree(project)
        fsz = folder_size(str(run))
        before = free_bytes(TMP)
        shutil.rmtree(run)
        freed = free_bytes(TMP) - before
        rows.append(
            {
                "case": case,
                "files": fsz.files,
                "logical_mib": mib(fsz.logical),
                "du_mib": mib(fsz.allocated),
                "private_mib": mib(fsz.private),
                "actual_mib": mib(fsz.actual),
                "freed_on_delete_mib": mib(freed),
                "measure_seconds": round(fsz.seconds, 2),
            }
        )
        print(rows[-1], flush=True)
        if project.exists():
            shutil.rmtree(project)
    return rows


def non_apfs(src_full: Path) -> list[dict]:
    """Targets on HFS+, ExFAT and a second APFS volume (disk images, no sudo needed)."""
    small = TMP / "small"
    shutil.copytree(src_full / "src00", small / "src00")
    rows = []
    for fs, label in (("HFS+", "arrS5hfs"), ("ExFAT", "arrS5exfat"), ("APFS", "arrS5apfs")):
        img = TMP / f"{label}.dmg"
        subprocess.run(
            ["hdiutil", "create", "-size", "200m", "-fs", fs, "-volname", label, str(img)],
            check=True,
            capture_output=True,
        )
        # ExFAT (FSKit) refuses a custom -mountpoint, so let hdiutil pick one under /Volumes.
        out = subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-plist", str(img)],
            check=True,
            capture_output=True,
        ).stdout
        entities = plistlib.loads(out)["system-entities"]
        mnt = Path(next(e["mount-point"] for e in entities if "mount-point" in e))
        try:
            res: dict = {"target_fs": fs_type(mnt), "can_clone_precheck": can_clone(small, mnt)}
            try:
                clonefile(small, mnt / "c1")
                res["clonefile"] = "ok"
            except OSError as exc:
                res["clonefile"] = f"{errno.errorcode[exc.errno]}: {exc.strerror}"
            p = subprocess.run(
                ["cp", "-c", "-R", str(small), str(mnt / "c2")], capture_output=True, text=True
            )
            res["cp_-c_exit"] = p.returncode
            res["cp_-c_stderr"] = p.stderr.strip()[:200]
            res["cp_-c_copied_files"] = sum(1 for _ in (mnt / "c2").rglob("*") if _.is_file())
            res["clone_or_copy"] = clone_or_copy(small, mnt / "c3")
            # Same-volume clone on the target volume itself.
            try:
                clonefile(mnt / "c3", mnt / "c4")
                res["clonefile_within_target"] = "ok"
            except OSError as exc:
                res["clonefile_within_target"] = errno.errorcode[exc.errno]
            rows.append({"fs": fs} | res)
            print(rows[-1], flush=True)
        finally:
            subprocess.run(["hdiutil", "detach", str(mnt)], check=True, capture_output=True)
            img.unlink()
    shutil.rmtree(small)
    return rows


def noise(samples: int = 6) -> dict:
    vals = []
    for _ in range(samples):
        vals.append(free_bytes(TMP))
    return {
        "samples": samples,
        "min_max_spread_mib": mib(max(vals) - min(vals)),
        "stdev_mib": mib(int(statistics.pstdev(vals))),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--small", type=int, default=4000, help="number of small files")
    p.add_argument("--big", type=int, default=480, help="number of 1 MiB files")
    p.add_argument("--keep", action="store_true")
    args = p.parse_args()

    TMP.mkdir(exist_ok=True)
    report: dict = {
        "machine": {"macos": platform.mac_ver()[0], "python": platform.python_version()},
        "volume_fs": fs_type(TMP),
        "df_before": df(TMP),
        "free_before_mib": mib(free_bytes(TMP)),
    }
    report["noise_idle"] = noise()
    src = TMP / "src"
    before = free_bytes(TMP)
    t0 = time.perf_counter()
    make_data(src, args.small, args.big)
    report["testdata"] = asdict(folder_size(str(src))) | {
        "create_seconds": round(time.perf_counter() - t0, 1),
        "free_delta_mib": mib(before - free_bytes(TMP)),
    }
    print("testdata", report["testdata"], flush=True)
    report["methods"] = compare_methods(src, args.reps)
    report["modifications"] = modifications(src)
    shutil.rmtree(TMP / "clone")
    report["validation"] = validate_method(src)
    report["non_apfs"] = non_apfs(src)
    if not args.keep:
        shutil.rmtree(TMP)
    report["df_after"] = df(HERE)
    report["free_after_mib"] = mib(free_bytes(HERE))
    report["tmp_exists_after"] = TMP.exists()
    out = HERE / "results" / "s05_results.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
