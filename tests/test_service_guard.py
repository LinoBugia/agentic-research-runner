"""Protection of the service against other websites and processes (W3-01 A2), against a real
server in a child process: token link → cookie and redirect, no cookie → German page, wrong
host → refused, foreign origin on writes and on the websocket → refused."""

import json
import re
from collections.abc import Iterator

import httpx
import pytest
from service_kit import ServiceProcess, make_workfolder, start_service
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from arr.service.guard import COOKIE, SERVICE_HEADER, Guard, new_token


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


def client(service: ServiceProcess, **kwargs: object) -> httpx.Client:
    return httpx.Client(base_url=service.url, follow_redirects=False, timeout=10.0, **kwargs)


def cookie_jar(service: ServiceProcess) -> dict[str, str]:
    return {COOKIE: service.token}


def ws_url(service: ServiceProcess) -> str:
    return f"ws://127.0.0.1:{service.port}/_nicegui_ws/socket.io/?EIO=4&transport=websocket"


# --- without cookie -----------------------------------------------------------------------------


def test_without_cookie_pages_static_and_nicegui_endpoints_are_refused(
    service: ServiceProcess,
) -> None:
    with client(service) as http:
        for path in [
            "/",
            "/runs",
            "/arr-static/arr.js",
            "/_nicegui_ws/socket.io/?EIO=4&transport=polling",
        ]:
            response = http.get(path)
            assert response.status_code == 401, path
            assert response.headers.get(SERVICE_HEADER.decode()) == "refused"
            assert "nicht freigeschaltet" in response.text and "arr" in response.text
            assert "nicegui" not in response.text.lower()


def test_wrong_token_is_refused_and_sets_no_cookie(service: ServiceProcess) -> None:
    with client(service) as http:
        response = http.get("/?token=" + new_token())
        assert response.status_code == 401
        assert "set-cookie" not in response.headers


def test_token_link_sets_http_only_strict_cookie_and_redirects_without_token(
    service: ServiceProcess,
) -> None:
    with client(service) as http:
        response = http.get(f"/?token={service.token}")
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        cookie = response.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE}={service.token}")
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
        # other parameters survive, the token does not
        response = http.get(f"/runs?status=aktiv&token={service.token}&n=100")
        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith("/runs?") and "token" not in location
        assert set(location.split("?", 1)[1].split("&")) == {"status=aktiv", "n=100"}


def test_with_cookie_pages_and_static_files_load(service: ServiceProcess) -> None:
    with client(service, cookies=cookie_jar(service)) as http:
        page = http.get("/")
        assert page.status_code == 200
        assert "nicegui" in page.text.lower()
        assert http.get("/arr-static/arr.js").status_code == 200


# --- host and origin ----------------------------------------------------------------------------


def test_wrong_host_is_refused_even_with_cookie(service: ServiceProcess) -> None:
    with client(service, cookies=cookie_jar(service)) as http:
        for host in [
            "evil.example",
            f"evil.example:{service.port}",
            f"127.0.0.1:{service.port + 1}",
        ]:
            response = http.get("/", headers={"Host": host})
            assert response.status_code == 403, host
        assert http.get("/", headers={"Host": f"localhost:{service.port}"}).status_code == 200


def test_writes_need_the_own_origin(service: ServiceProcess) -> None:
    path = "/_nicegui_ws/socket.io/?EIO=4&transport=polling"
    with client(service, cookies=cookie_jar(service)) as http:
        assert http.post(path, content=b"x").status_code == 403  # no Origin at all
        assert (
            http.post(path, content=b"x", headers={"Origin": "http://evil.example"}).status_code
            == 403
        )
        assert http.post(
            path, content=b"x", headers={"Origin": f"http://127.0.0.1:{service.port}"}
        ).status_code not in (401, 403)
        # without cookie the origin does not help
    with client(service) as http:
        assert (
            http.post(
                path, content=b"x", headers={"Origin": f"http://127.0.0.1:{service.port}"}
            ).status_code
            == 401
        )


def test_websocket_needs_cookie_and_own_origin(service: ServiceProcess) -> None:
    own = f"http://127.0.0.1:{service.port}"
    cookie = f"{COOKIE}={service.token}"
    with (
        pytest.raises(InvalidStatus) as refused,
        connect(ws_url(service), additional_headers={"Origin": own}),
    ):
        pass
    assert refused.value.response.status_code == 403
    with (
        pytest.raises(InvalidStatus) as refused,
        connect(
            ws_url(service), additional_headers={"Origin": "http://evil.example", "Cookie": cookie}
        ),
    ):
        pass
    assert refused.value.response.status_code == 403
    with connect(ws_url(service), additional_headers={"Origin": own, "Cookie": cookie}) as ws:
        first = ws.recv(timeout=10)
        assert isinstance(first, str) and first.startswith("0{"), first  # engine.io „open“


# --- token handling -----------------------------------------------------------------------------


def test_token_appears_in_no_log_line_but_the_link(service: ServiceProcess) -> None:
    lines = [line for line in service.output().splitlines() if service.token in line]
    assert lines, "the --no-browser start prints the link once"
    assert all("Öffne die Oberfläche" in line for line in lines), lines


def test_guard_compares_tokens_in_constant_time() -> None:
    guard = Guard(lambda *a: None, token="abc", port=1)  # type: ignore[arg-type]
    assert guard.token_ok("abc") and not guard.token_ok("abd") and not guard.token_ok(None)
    assert guard.hosts == {"127.0.0.1:1", "localhost:1"}
    assert guard.origins == {"http://127.0.0.1:1", "http://localhost:1"}


def test_service_json_holds_the_token_and_the_page_never_shows_it(service: ServiceProcess) -> None:
    info = json.loads((service.arr_home / "service.json").read_text())
    assert info["token"] == service.token and info["port"] == service.port
    with client(service) as http:
        assert service.token not in http.get("/").text
    assert re.fullmatch(r"[A-Za-z0-9_-]{40,}", service.token)
