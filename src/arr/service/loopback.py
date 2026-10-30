"""Holding `[::1]:<port>` for the service (W3-01.2 N1, docs/04-service-gui-und-schnittstellen.md
§1).

Browsers resolve `<random>.localhost` to `::1` and `127.0.0.1` and try `::1` first. NiceGUI
(uvicorn) listens on `127.0.0.1` only, so any other program could sit on `[::1]:<port>` and
receive the token link and the cookie. A socket that is bound but not listening is no cure:
macOS answers such a connection with silence, and browsers wait. So arr listens on
`[::1]:<port>` itself, with a small relay (`LoopbackRelay`) that pipes every connection to
`127.0.0.1:<port>`; the guard sees the same request. The socket is bound before the token
link is handed out and held until the process ends. It is not inheritable, so no child
process (agents, app servers) keeps the port. Machines without IPv6 (`::1` cannot be bound,
e.g. in CI) run without the reservation.

`port_in_use` says whether another program holds the port on either loopback address or on a
wildcard (`0.0.0.0`, `::`), by momentary binds that never listen; arr then refuses to start
there. While arr runs, a foreign bind
on `::1` or `127.0.0.1` fails whatever socket options it uses (no `SO_REUSEPORT` on arr's
sockets). A wildcard bind with `SO_REUSEADDR` may succeed on macOS, but it receives no
loopback connection: the kernel delivers to the specific sockets arr holds.
"""

import asyncio
import contextlib
import errno
import logging
import socket

from arr.errors import ArrError

log = logging.getLogger("arr.service")

NO_IPV6_ERRNOS = frozenset(
    {errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.ENOPROTOOPT}
)
"""Errors that mean „this machine has no `::1`“ (not „the port is taken“)."""
LOOPBACK_V4 = "127.0.0.1"
LOOPBACK_V6 = "::1"
RELAY_CHUNK = 65536


def _socket(family: int) -> socket.socket:
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.set_inheritable(False)
    if family == socket.AF_INET6:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    return sock


PROBES = (
    (socket.AF_INET, LOOPBACK_V4, "127.0.0.1"),
    (socket.AF_INET6, LOOPBACK_V6, "[::1]"),
    (socket.AF_INET, "0.0.0.0", "0.0.0.0 (alle Adressen)"),
    (socket.AF_INET6, "::", "[::] (alle Adressen)"),
)


def port_in_use(port: int) -> str | None:
    """What holds the port, or None if arr can take it.

    Tries a momentary bind with `SO_REUSEADDR` on `127.0.0.1`, `::1`, `0.0.0.0` and `::`
    (never `listen`, closed at once). Such a bind fails exactly when another socket holds
    that very address, whatever its options (arr sets no `SO_REUSEPORT`), and it passes a
    bare `TIME_WAIT` remainder of an earlier arr. The wildcards are probed because macOS lets
    a specific bind pass next to a wildcard holder. Without IPv6 only IPv4 is checked.
    """
    for family, address, label in PROBES:
        sock = _socket(family)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((address, port))
        except OSError as exc:
            if family == socket.AF_INET6 and exc.errno in NO_IPV6_ERRNOS:
                continue
            return label
        finally:
            sock.close()
    return None


class LoopbackRelay:
    """Holds `[::1]:<port>` and relays its connections to `127.0.0.1:<port>`."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._sock: socket.socket | None = None
        self._server: asyncio.base_events.Server | None = None
        self.available = False

    @property
    def bound(self) -> bool:
        return self._sock is not None

    @property
    def socket(self) -> socket.socket | None:
        return self._sock

    def bind(self) -> bool:
        """Take `[::1]:<port>` now; False (and nothing held) when the machine has no IPv6.

        Raises `ArrError` (German) when another program holds the address. `SO_REUSEADDR`
        only lets arr restart past its own `TIME_WAIT` remainders; the exact address stays
        exclusive, and arr never sets `SO_REUSEPORT`.
        """
        if self._sock is not None:
            return True
        sock = _socket(socket.AF_INET6)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((LOOPBACK_V6, self.port))
            sock.listen(64)
        except OSError as exc:
            sock.close()
            if exc.errno in NO_IPV6_ERRNOS:
                log.info("Kein IPv6 auf diesem Rechner: [::1]:%s wird nicht reserviert.", self.port)
                return False
            if exc.errno == errno.EADDRINUSE:
                raise ArrError(
                    f"Der Port {self.port} ist unter [::1] belegt, aber nicht von arr. Wähle mit "
                    "„--port“ einen anderen Port oder beende das Programm, das den Port nutzt."
                ) from None
            raise
        sock.setblocking(False)
        self._sock = sock
        self.available = True
        return True

    async def start(self) -> None:
        """Serve the held socket in the running event loop (idempotent)."""
        if self._sock is None or self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle, sock=self._sock)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(LOOPBACK_V4, self.port)
        except OSError:
            writer.close()
            return
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        for stream in (writer, up_writer):
            with contextlib.suppress(Exception):
                stream.close()

    def close(self) -> None:
        """Release the address (also safe to call twice and after a hard abort)."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await src.read(RELAY_CHUNK)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except (OSError, asyncio.IncompleteReadError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if dst.can_write_eof():
                dst.write_eof()
