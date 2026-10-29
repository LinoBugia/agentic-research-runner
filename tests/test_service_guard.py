"""Protection of the service against other websites and processes (W3-01 A2, W3-01.1 M1 and
G1–G3), against a real server in a child process: own hostname `<random>.localhost`, token
link → host-only cookie and redirect, no cookie → German page, other hosts → no data and no
cookie, foreign origin → refused for every method and the websocket."""

import json
import re
from collections.abc import Iterator

import pytest
from service_kit import ServiceProcess, make_workfolder, start_service
from websockets.exceptions import InvalidStatus

from arr.service.guard import (
    COOKIE,
    HOSTNAME_LENGTH,
    SERVICE_HEADER,
    Guard,
    new_hostname,
    new_token,
)

POLLING = "/_nicegui_ws/socket.io/?EIO=4&transport=polling"


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ServiceProcess]:
    tmp = tmp_path_factory.mktemp("guard")
    arr_home = tmp / "home"
    arr_home.mkdir()
    workfolder = make_workfolder(tmp)
    proc = start_service(tmp, arr_home, workfolder)
    try:
        yield proc
    finally:
        proc.stop()


def cookie_of(service: ServiceProcess) -> str:
    return f"{COOKIE}={service.token}"


# --- hostname -----------------------------------------------------------------------------------


def test_service_has_a_random_localhost_name_in_service_json_and_link(
    service: ServiceProcess,
) -> None:
    info = json.loads((service.arr_home / "service.json").read_text())
    assert info["host"] == service.hostname
    assert re.fullmatch(rf"[a-z0-9]{{{HOSTNAME_LENGTH},}}\.localhost", service.hostname)
    assert info["token"] == service.token and info["port"] == service.port
    assert re.fullmatch(r"[A-Za-z0-9_-]{40,}", service.token)
    link = [line for line in service.output().splitlines() if "token=" in line]
    assert link and f"http://{service.hostname}:{service.port}/?token=" in link[0]
    assert "127.0.0.1" not in link[0]
    assert new_hostname() != new_hostname()
    assert len(new_hostname()) == HOSTNAME_LENGTH + len(".localhost")


# --- without cookie -----------------------------------------------------------------------------


def test_without_cookie_nothing_is_served(service: ServiceProcess) -> None:
    with service.client() as http:
        for path in ["/", "/runs", "/arr-static/arr.js", POLLING]:
            response = http.get(path)
            assert response.status_code == 401, path
            assert response.headers.get(SERVICE_HEADER.decode()) == "refused"
            assert "nicht freigeschaltet" in response.text and "arr" in response.text
            assert "nicegui" not in response.text.lower()
            assert service.token not in response.text


def test_wrong_token_is_refused_and_sets_no_cookie(service: ServiceProcess) -> None:
    with service.client() as http:
        response = http.get("/?token=" + new_token())
        assert response.status_code == 401
        assert "set-cookie" not in response.headers


# --- token link → cookie ------------------------------------------------------------------------


def test_token_link_sets_host_only_cookie_and_redirects_without_token(
    service: ServiceProcess,
) -> None:
    with service.client() as http:
        response = http.get(f"/?token={service.token}")
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        cookie = response.headers["set-cookie"]
        attributes = {part.strip().split("=")[0].lower() for part in cookie.split(";")[1:]}
        assert cookie.startswith(f"{COOKIE}={service.token}")
        assert {"httponly", "samesite", "path", "max-age"} <= attributes
        assert "domain" not in attributes, "host-only: no Domain attribute"
        assert "SameSite=Strict" in cookie and "Path=/" in cookie
        # other parameters survive, the token does not
        response = http.get(f"/runs?status=aktiv&token={service.token}&n=100")
        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith("/runs?") and "token" not in location
        assert set(location.split("?", 1)[1].split("&")) == {"status=aktiv", "n=100"}


def test_token_link_with_non_latin1_path_redirects_encoded(service: ServiceProcess) -> None:
    """W3-01.1 G2: `/€?token=…` used to end in a 500."""
    with service.client() as http:
        response = http.get(f"/%E2%82%AC?token={service.token}")
        assert response.status_code == 303
        assert response.headers["location"] == "/%E2%82%AC"
        assert "set-cookie" in response.headers


def test_with_cookie_pages_and_static_files_load(service: ServiceProcess) -> None:
    with service.client(cookie=True) as http:
        page = http.get("/")
        assert page.status_code == 200
        assert "nicegui" in page.text.lower()
        assert http.get("/arr-static/arr.js").status_code == 200
        assert http.get("/", headers={"Host": service.host.upper()}).status_code == 200


# --- host ---------------------------------------------------------------------------------------


def test_other_hosts_get_no_data_and_no_cookie(service: ServiceProcess) -> None:
    """W3-01.1 M1: `127.0.0.1`, `localhost`, other names, other ports → 403 without cookie."""
    port = service.port
    hosts = [
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"anders.localhost:{port}",
        f"{service.hostname}:{port + 1}",
        "evil.example",
        f"evil.example:{port}",
        service.hostname,  # without port
    ]
    for host in hosts:
        with service.client(cookie=True, host=host) as http:
            response = http.get("/")
            assert response.status_code == 403, host
            assert "nicegui" not in response.text.lower()
            assert "set-cookie" not in response.headers
            # a token link on a wrong host sets no cookie either
            response = http.get(f"/?token={service.token}")
            assert "set-cookie" not in response.headers, host
            if host in (f"127.0.0.1:{port}", f"localhost:{port}"):
                # … but is sent on to the own name, token kept for that one hop
                assert response.status_code == 303, host
                assert response.headers["location"] == f"{service.url}/?token={service.token}"
            else:
                assert response.status_code == 403, host
        with pytest.raises(InvalidStatus) as refused:
            service.ws_connect({"Origin": service.url, "Cookie": cookie_of(service)}, host=host)
        assert refused.value.response.status_code == 403, host


# --- origin -------------------------------------------------------------------------------------


def test_foreign_origin_is_refused_for_every_method(service: ServiceProcess) -> None:
    """W3-01.1 G1: also GET (Socket.IO polling) with a foreign origin, and `null`."""
    foreign = [
        "http://evil.example",
        f"http://127.0.0.1:{service.port}",
        f"http://localhost:{service.port}",
        f"http://anders.localhost:{service.port}",
        f"http://{service.hostname}:{service.port + 1}",
        f"https://{service.host}",
        "null",
    ]
    with service.client(cookie=True) as http:
        for origin in foreign:
            for method, path in [("GET", POLLING), ("GET", "/"), ("POST", POLLING)]:
                response = http.request(method, path, headers={"Origin": origin}, content=b"x")
                assert response.status_code == 403, (method, origin)
                assert "access-control-allow-origin" not in response.headers, origin
        assert http.post(POLLING, content=b"x").status_code == 403  # write without Origin
        for method in ["PUT", "DELETE", "PATCH"]:
            assert http.request(method, "/").status_code == 403, method
        own = http.post(POLLING, content=b"x", headers={"Origin": service.url})
        assert own.status_code not in (401, 403)
        assert http.get(POLLING, headers={"Origin": service.url}).status_code == 200
    with service.client() as http:  # without cookie the origin does not help
        assert http.post(POLLING, content=b"x", headers={"Origin": service.url}).status_code == 401


def test_websocket_needs_cookie_and_own_origin(service: ServiceProcess) -> None:
    cookie = cookie_of(service)
    refusals = [
        {"Origin": service.url},  # no cookie
        {"Cookie": cookie},  # no origin
        {"Cookie": cookie, "Origin": "null"},
        {"Cookie": cookie, "Origin": "http://evil.example"},
        {"Cookie": cookie, "Origin": f"http://127.0.0.1:{service.port}"},
        {"Cookie": cookie, "Origin": f"http://localhost:{service.port}"},
        {"Cookie": f"{COOKIE}=x", "Origin": service.url},
    ]
    for headers in refusals:
        with pytest.raises(InvalidStatus) as refused:
            service.ws_connect(headers)
        assert refused.value.response.status_code == 403, headers
    with service.ws_connect({"Origin": service.url, "Cookie": cookie}) as ws:
        first = ws.recv(timeout=10)
        assert isinstance(first, str) and first.startswith("0{"), first  # engine.io „open“


# --- token handling -----------------------------------------------------------------------------


def test_token_appears_in_no_log_line_but_the_link(service: ServiceProcess) -> None:
    lines = [line for line in service.output().splitlines() if service.token in line]
    assert lines, "the --no-browser start prints the link once"
    assert all("Öffne die Oberfläche" in line for line in lines), lines


def test_guard_compares_tokens_in_constant_time() -> None:
    guard = Guard(lambda *a: None, token="abc", port=1, hostname="abc123.localhost")  # type: ignore[arg-type]
    assert guard.token_ok("abc") and not guard.token_ok("abd") and not guard.token_ok(None)
    assert guard.host == "abc123.localhost:1" and guard.origin == "http://abc123.localhost:1"
    assert guard.loopback_hosts == {"127.0.0.1:1", "localhost:1"}
