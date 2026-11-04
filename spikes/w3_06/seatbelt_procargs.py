"""W3-06 §12, new gate: can a Seatbelt rule block `KERN_PROCARGS2`? Measures, changes nothing.

A stand-in process carries a marker in its environment. A second process enters a rule with
`sandbox_init` after its start (a rule at `sandbox-exec` time stops Python before it runs), then
tries the controls and the read. The controls show that the rule really holds in that process.
Only exit codes and flags are printed.

    uv run python spikes/w3_06/seatbelt_procargs.py
"""

import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

READER = r"""
import ctypes, json, os, sys
pid, marker, rule = int(sys.argv[1]), sys.argv[2].encode(), sys.argv[3].encode()
libc = ctypes.CDLL(None, use_errno=True)
err = ctypes.c_char_p()
libc.sandbox_init.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p)]
if libc.sandbox_init(rule, 0, ctypes.byref(err)) != 0:
    print(json.dumps({"rule": "refused"}))
    sys.exit(0)


def by_name(name):
    value, size = ctypes.c_int(0), ctypes.c_size_t(4)
    return libc.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0)


def by_number(*numbers):
    value, size = (ctypes.c_char * 4096)(), ctypes.c_size_t(4096)
    mib = (ctypes.c_int * len(numbers))(*numbers)
    return libc.sysctl(mib, len(numbers), value, ctypes.byref(size), None, 0)


def can_open():
    try:
        os.close(os.open("/etc/hosts", os.O_RDONLY))
        return True
    except OSError:
        return False


buffer = ctypes.create_string_buffer(1 << 20)
mib = (ctypes.c_int * 3)(1, 49, pid)
size = ctypes.c_size_t(len(buffer))
code = libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0)
print(json.dumps({
    "hw.ncpu by name": by_name(b"hw.ncpu"),
    "hw.ncpu by number": by_number(6, 3),
    "kern.ostype by number": by_number(1, 1),
    "/etc/hosts readable": can_open(),
    "PROCARGS2 code": code,
    "PROCARGS2 marker found": code == 0 and marker in buffer.raw[: size.value],
}))
"""

RULES = {
    "allow default": "(version 1)(allow default)",
    "deny sysctl-read": "(version 1)(allow default)(deny sysctl-read)",
    "deny name kern.procargs2": (
        '(version 1)(allow default)(deny sysctl-read (sysctl-name "kern.procargs2"))'
    ),
    "deny prefix kern.": (
        '(version 1)(allow default)(deny sysctl-read (sysctl-name-prefix "kern."))'
    ),
    "deny regex .*": '(version 1)(allow default)(deny sysctl-read (sysctl-name-regex #".*"))',
    "deny process-info*": "(version 1)(allow default)(deny process-info*)",
    "deny default, nothing allowed": "(version 1)(deny default)",
}


def main() -> None:
    marker = "ARR-GUARD-SEATBELT-" + secrets.token_hex(8)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={"GUARD_MARKER": marker, "PATH": "/usr/bin:/bin"},
    )
    time.sleep(0.5)
    reader = Path("/private/tmp") / f"arr-seatbelt-{secrets.token_hex(4)}.py"
    reader.write_text(READER)
    try:
        for name, rule in RULES.items():
            done = subprocess.run(
                [sys.executable, "-I", str(reader), str(holder.pid), marker, rule],
                capture_output=True,
                text=True,
                timeout=30,
            )
            print(f"{name:32} {done.stdout.strip() or json.dumps({'exit': done.returncode})}")
    finally:
        holder.kill()
        reader.unlink()


if __name__ == "__main__":
    main()
