"""Protection of the service against other websites and processes (W3-01 §3.2, docs/01 §8).

`Guard` is a pure ASGI middleware around the whole NiceGUI app, so it also covers the static
files, the `/_nicegui/…` endpoints and the Socket.IO websocket under `/_nicegui_ws/`:

- **Token → cookie:** the browser is opened once with `/?token=…`. A request with the right
  token gets the cookie `arr_session` (`HttpOnly`, `SameSite=Strict`) and is redirected to the
  same URL without the token. Every other request needs that cookie; without it the answer is
  a German page that says how to open arr (status 401), for the websocket a refused handshake.
- **Host check** (DNS rebinding): only `127.0.0.1:<port>` and `localhost:<port>` (status 403).
- **Origin check** for the websocket and for every request that is not a plain read
  (`GET`, `HEAD`, `OPTIONS`): only the service's own origin, so no other website in the same
  browser can trigger an action (status 403).

Tokens are compared in constant time (`hmac.compare_digest`) and never logged.
"""

import hmac
import secrets
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import parse_qsl, urlencode

COOKIE = "arr_session"
TOKEN_PARAM = "token"
COOKIE_MAX_AGE_S = 30 * 24 * 3600
"""The cookie lives this long in the browser; it is only good for one start of the service
(each start has a new token), so an old one simply stops working."""
SERVICE_HEADER = b"x-arr-service"
"""Response header on every refusal, so `arr serve` can tell a running service on the port
from some other program."""

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def new_token() -> str:
    """A fresh access token (256 bits, URL safe)."""
    return secrets.token_urlsafe(32)


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


def allowed_origins(port: int) -> frozenset[str]:
    return frozenset({f"http://{host}" for host in allowed_hosts(port)})


def refusal_page() -> str:
    """The German page shown without a valid cookie (docs/08 §8.4: no command to type)."""
    return (
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>arr öffnen</title><style>body{font:16px/1.5 -apple-system,system-ui,sans-serif;"
        "max-width:36rem;margin:4rem auto;padding:0 1.5rem;color:#222}h1{font-size:1.4rem}"
        "@media(prefers-color-scheme:dark){body{background:#1c1c1e;color:#eee}}</style></head>"
        "<body><h1>Diese Seite gehört zu arr, ist aber nicht freigeschaltet.</h1>"
        "<p>arr öffnet seine Oberfläche nur über den Link, den es beim Start selbst erzeugt. "
        "So kann keine andere Webseite und kein anderes Programm auf deine Runs zugreifen.</p>"
        "<p><b>So öffnest du arr:</b> Starte arr neu, zum Beispiel über das arr-Symbol im Dock "
        "oder mit „arr serve“ im Terminal. Die Oberfläche öffnet sich dann von selbst in "
        "einem neuen Tab.</p>"
        "<p>Läuft arr schon, schließe diesen Tab und nimm den Tab, den arr geöffnet hat.</p>"
        "</body></html>"
    )


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return None


def _cookie_value(scope: Scope, name: str) -> str | None:
    header = _header(scope, b"cookie")
    if not header:
        return None
    for part in header.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.strip() == name:
            return value.strip()
    return None


class Guard:
    """ASGI middleware: cookie, host and origin checks (see the module docstring)."""

    def __init__(self, app: ASGIApp, *, token: str, port: int) -> None:
        self.app = app
        self._token = token
        self.port = port
        self.hosts = allowed_hosts(port)
        self.origins = allowed_origins(port)
        self._page = refusal_page().encode("utf-8")

    # -- checks ------------------------------------------------------------------------------

    def token_ok(self, candidate: str | None) -> bool:
        """Constant-time comparison with the service token."""
        if not candidate:
            return False
        return hmac.compare_digest(candidate.encode("utf-8"), self._token.encode("utf-8"))

    def host_ok(self, scope: Scope) -> bool:
        host = _header(scope, b"host")
        return host is not None and host.lower() in self.hosts

    def origin_ok(self, scope: Scope) -> bool:
        origin = _header(scope, b"origin")
        return origin is not None and origin.lower() in self.origins

    def cookie_ok(self, scope: Scope) -> bool:
        return self.token_ok(_cookie_value(scope, COOKIE))

    # -- ASGI --------------------------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind == "websocket":
            await self._websocket(scope, receive, send)
        elif kind == "http":
            await self._http(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (self.host_ok(scope) and self.cookie_ok(scope) and self.origin_ok(scope)):
            # Refusing before `accept` makes the server answer the handshake with 403.
            await receive()
            await send({"type": "websocket.close", "code": 1008, "reason": "arr: nicht erlaubt"})
            return
        await self.app(scope, receive, send)

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.host_ok(scope):
            await self._refuse(send, 403, b"text/plain; charset=utf-8", b"arr: falscher Host")
            return
        method = str(scope.get("method", "GET")).upper()
        query = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1")))
        if method in ("GET", "HEAD") and TOKEN_PARAM in query:
            await self._login(scope, send, query)
            return
        if not self.cookie_ok(scope):
            await self._refuse(send, 401, b"text/html; charset=utf-8", self._page)
            return
        if method not in READ_METHODS and not self.origin_ok(scope):
            await self._refuse(send, 403, b"text/plain; charset=utf-8", b"arr: fremder Origin")
            return
        await self.app(scope, receive, send)

    async def _login(self, scope: Scope, send: Send, query: dict[str, str]) -> None:
        """`?token=…`: set the cookie and redirect to the URL without the token."""
        if not self.token_ok(query.get(TOKEN_PARAM)):
            await self._refuse(send, 401, b"text/html; charset=utf-8", self._page)
            return
        rest = urlencode([(k, v) for k, v in query.items() if k != TOKEN_PARAM])
        path = scope.get("path") or "/"
        location = path + (f"?{rest}" if rest else "")
        cookie = (
            f"{COOKIE}={self._token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={COOKIE_MAX_AGE_S}"
        )
        await send(
            {
                "type": "http.response.start",
                "status": 303,
                "headers": [
                    (b"location", location.encode("latin-1")),
                    (b"set-cookie", cookie.encode("latin-1")),
                    (b"cache-control", b"no-store"),
                    (b"content-length", b"0"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def _refuse(self, send: Send, status: int, content_type: bytes, body: bytes) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", content_type),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    (b"cache-control", b"no-store"),
                    (SERVICE_HEADER, b"refused"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
