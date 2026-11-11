"""The program an agent could write to reach the arr service (W4-01 A8, standard library only).

The harness puts it into the sandbox of a backend and runs it there. It tries to read
`~/.arr/service.json` (open, cat, a link, a copy) and to reach the port of the service (IPv4,
IPv6, an HTTP request to `/mcp/`). Only `reached` flags leave it, never a content.

    python3 service_lockout_probe.py plan.json
"""

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path


def _read_ways(path: str, scratch: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}

    def attempt(name: str, read) -> None:  # noqa: ANN001
        try:
            out[name] = read()
        except (OSError, subprocess.SubprocessError):
            out[name] = b""

    def by_open() -> bytes:
        with open(path, "rb") as file:
            return file.read()

    def by_cat() -> bytes:
        return subprocess.run(["/bin/cat", path], capture_output=True, timeout=10).stdout

    def by_link() -> bytes:
        link = scratch / "link.json"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(path)
        return link.read_bytes()

    def by_copy() -> bytes:
        copy = scratch / "copy.json"
        shutil.copyfile(path, copy)
        return copy.read_bytes()

    for name, read in (
        ("open", by_open),
        ("cat", by_cat),
        ("Symlink", by_link),
        ("Kopie", by_copy),
    ):
        attempt(name, read)
    return out


def _connect(family: int, address: str, port: int) -> bool:
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect((address, port))
            return True
    except OSError:
        return False


def _http(port: int, host: str) -> bool:
    request = (
        f"POST /mcp/ HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
        "Content-Length: 2\r\nConnection: close\r\n\r\n{}"
    ).encode()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
            sock.sendall(request)
            return sock.recv(64).startswith(b"HTTP/")
    except OSError:
        return False


def main() -> int:
    plan = json.loads(Path(sys.argv[1]).read_text())
    scratch = Path(plan["scratch"])
    scratch.mkdir(parents=True, exist_ok=True)
    marker = plan["marker"].encode()
    rows = []
    for way, content in _read_ways(plan["service_json"], scratch).items():
        rows.append({"way": f"service.json: {way}", "reached": marker in content})
    control = _read_ways(plan["control"], scratch)
    rows += [{"way": f"Kontrolle: {w}", "reached": marker in c} for w, c in control.items()]
    rows.append(
        {"way": "Port IPv4", "reached": _connect(socket.AF_INET, "127.0.0.1", plan["port"])}
    )
    rows.append({"way": "Port IPv6", "reached": _connect(socket.AF_INET6, "::1", plan["port"])})
    rows.append({"way": "HTTP /mcp/", "reached": _http(plan["port"], plan["host"])})
    print(json.dumps(rows))
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
