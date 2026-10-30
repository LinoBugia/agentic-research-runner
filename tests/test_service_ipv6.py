"""`[::1]:<port>` belongs to arr (W3-01.2 N1), against a real server: foreign binds fail while
the service runs, requests over `::1` reach the service through the relay (HTTP and
websocket), the service refuses to start when `::1` or a wildcard is taken, the held socket
is not inheritable and is released at the end."""

import errno
import http.client
import select
import socket
import statistics
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from run_kit import write_profiles
from service_kit import (
    ServiceProcess,
    free_port,
    ipv6_available,
    make_workfolder,
    start_service,
    wait_for,
)

from arr.errors import ArrError
from arr.service.guard import COOKIE, SERVICE_HEADER
from arr.service.loopback import LoopbackRelay, port_in_use
from arr.service.serve import serve

pytestmark = pytest.mark.skipif(not ipv6_available(), reason="kein IPv6 (::1) auf dem Rechner")

VARIANTS = {
    "plain": (),
    "reuseaddr": (socket.SO_REUSEADDR,),
    "reuseport": (socket.SO_REUSEPORT,),
    "both": (socket.SO_REUSEADDR, socket.SO_REUSEPORT),
}


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ServiceProcess]:
    tmp = tmp_path_factory.mktemp("ipv6")
    arr_home = tmp / "home"
    arr_home.mkdir()
    proc = start_service(tmp, arr_home, make_workfolder(tmp))
    try:
        yield proc
    finally:
        proc.stop()


def foreign_socket(family: int, address: str, port: int, options: tuple[int, ...]) -> socket.socket:
    """Bind and listen like another program would; raises OSError when arr holds the port."""
    sock = socket.socket(family, socket.SOCK_STREAM)
    for option in options:
        sock.setsockopt(socket.SOL_SOCKET, option, 1)
    if family == socket.AF_INET6:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1 if address == "::1" else 0)
    try:
        sock.bind((address, port))
        sock.listen(5)
    except OSError:
        sock.close()
        raise
    return sock


def http_get(address: str, port: int, host: str, headers: dict[str, str] | None = None) -> Any:
    con = http.client.HTTPConnection(address, port, timeout=10)
    try:
        con.request("GET", "/", headers={"Host": host, **(headers or {})})
        response = con.getresponse()
        response.read()
        return response
    finally:
        con.close()


# --- foreign binds while arr runs -----------------------------------------------------------------


def test_foreign_binds_on_loopback_fail_while_the_service_runs(service: ServiceProcess) -> None:
    for name, options in VARIANTS.items():
        with pytest.raises(OSError) as error:
            foreign_socket(socket.AF_INET6, "::1", service.port, options)
        assert error.value.errno == errno.EADDRINUSE, name
    for name, options in VARIANTS.items():
        with pytest.raises(OSError) as error:
            foreign_socket(socket.AF_INET, "127.0.0.1", service.port, options)
        assert error.value.errno == errno.EADDRINUSE, name


def test_wildcard_binds_fail_or_receive_no_loopback_connection(service: ServiceProcess) -> None:
    """Plain wildcard binds fail. With `SO_REUSEADDR`/`SO_REUSEPORT` macOS lets a wildcard
    bind through, but the kernel delivers loopback connections to arr's specific sockets."""
    for family, address in ((socket.AF_INET6, "::"), (socket.AF_INET, "0.0.0.0")):
        for name, options in VARIANTS.items():
            try:
                foreign = foreign_socket(family, address, service.port, options)
            except OSError as error:
                assert error.errno == errno.EADDRINUSE, (address, name)
                continue
            try:
                for target in ("::1", "127.0.0.1"):
                    response = http_get(target, service.port, service.host)
                    assert response.status == 401, (address, name, target)
                    assert response.getheader(SERVICE_HEADER.decode()) == "refused"
                    readable, _, _ = select.select([foreign], [], [], 0.3)
                    assert not readable, f"{address} {name}: Fremder bekam die Verbindung"
            finally:
                foreign.close()


# --- the relay ------------------------------------------------------------------------------------


def test_requests_over_ipv6_reach_the_service_through_the_relay(service: ServiceProcess) -> None:
    response = http_get("::1", service.port, service.host)
    assert response.status == 401 and response.getheader(SERVICE_HEADER.decode()) == "refused"
    assert http_get("::1", service.port, "evil.example").status == 403
    page = http_get("::1", service.port, service.host, {"Cookie": f"{COOKIE}={service.token}"})
    assert page.status == 200
    # the websocket (what the GUI lives on) also works over ::1
    from websockets.sync.client import connect

    sock = socket.create_connection(("::1", service.port), timeout=10)
    uri = f"ws://{service.host}/_nicegui_ws/socket.io/?EIO=4&transport=websocket"
    headers = {"Origin": service.url, "Cookie": f"{COOKIE}={service.token}"}
    with connect(uri, sock=sock, additional_headers=headers, open_timeout=10) as ws:
        first = ws.recv(timeout=10)
        assert isinstance(first, str) and first.startswith("0{"), first


def test_relay_adds_little_delay(service: ServiceProcess) -> None:
    def timed(address: str) -> float:
        start = time.perf_counter()
        http_get(address, service.port, service.host)
        return time.perf_counter() - start

    for address in ("::1", "127.0.0.1"):
        timed(address)  # warm up
    via_v6 = statistics.median(timed("::1") for _ in range(7))
    via_v4 = statistics.median(timed("127.0.0.1") for _ in range(7))
    print(f"\nGET / via ::1 (Relay) {via_v6 * 1000:.1f} ms, via 127.0.0.1 {via_v4 * 1000:.1f} ms")
    assert via_v6 < 0.25, "ein Aufruf über das Relay darf nicht spürbar langsamer sein"


# --- start refused when taken, release at the end -------------------------------------------------


@pytest.fixture
def home(arr_home: Path) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    return arr_home


@pytest.mark.parametrize(
    ("family", "address", "v6only"),
    [(socket.AF_INET6, "::1", 1), (socket.AF_INET6, "::", 1), (socket.AF_INET, "0.0.0.0", None)],
    ids=["::1", "::", "0.0.0.0"],
)
def test_service_does_not_start_when_the_port_is_taken_elsewhere(
    tmp_path: Path, home: Path, family: int, address: str, v6only: int | None
) -> None:
    port = free_port()
    holder = socket.socket(family, socket.SOCK_STREAM)
    if v6only is not None:
        holder.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
    holder.bind((address, port))
    holder.listen(1)
    try:
        assert port_in_use(port) is not None
        with pytest.raises(ArrError) as error:
            serve(
                workfolder=make_workfolder(tmp_path),
                port=port,
                open_browser=False,
                report=lambda line: None,
            )
        assert str(port) in str(error.value) and "--port" in str(error.value)
    finally:
        holder.close()


def test_held_socket_is_exclusive_not_inheritable_and_released_on_close() -> None:
    port = free_port()
    relay = LoopbackRelay(port)
    assert relay.bind() is True and relay.bound
    try:
        assert relay.socket is not None and relay.socket.get_inheritable() is False
        assert relay.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) == 0
        with pytest.raises(ArrError) as error:
            LoopbackRelay(port).bind()
        assert "[::1]" in str(error.value) and "--port" in str(error.value)
        assert port_in_use(port) == "[::1]"
    finally:
        relay.close()
    assert port_in_use(port) is None
    again = LoopbackRelay(port)
    assert again.bind() is True
    again.close()


def test_without_ipv6_the_reservation_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    real_socket = socket.socket

    class NoIpv6(real_socket):  # type: ignore[misc,valid-type]
        def bind(self, address: object) -> None:
            if self.family == socket.AF_INET6:
                raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
            super().bind(address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "socket", NoIpv6)
    relay = LoopbackRelay(free_port())
    assert relay.bind() is False and not relay.bound
    relay.close()


def test_service_releases_the_ipv6_port_at_the_end(tmp_path: Path, home: Path) -> None:
    workfolder = make_workfolder(tmp_path)
    service = start_service(tmp_path, home, workfolder)
    port = service.port
    try:
        assert port_in_use(port) is not None
    finally:
        service.stop()
    wait_for(lambda: port_in_use(port) is None, timeout=10, what="[::1] frei nach dem Ende")
