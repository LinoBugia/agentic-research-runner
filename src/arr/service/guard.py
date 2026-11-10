"""Protection of the service against other websites and processes (W3-01 §3.2, W3-01.1 M1,
docs/01-architektur.md §8).

`Guard` is a pure ASGI middleware around the whole NiceGUI app, so it also covers the static
files, the `/_nicegui/…` endpoints and the Socket.IO websocket under `/_nicegui_ws/`:

- **Own hostname:** every start of the service gets a fresh name `<random>.localhost`
  (`new_hostname`, 20 characters `[a-z0-9]` from `secrets`). Browsers resolve `*.localhost`
  to the loopback address (RFC 6761). Cookies are scoped by host and not by port
  (RFC 6265 §8.5), so the cookie below reaches no other server on `127.0.0.1`: nobody else
  knows the name.
- **Token → cookie:** the browser is opened once with `http://<random>.localhost:<port>/?token=…`.
  A request with the right token gets the host-only cookie `arr_session` (`HttpOnly`,
  `SameSite=Strict`, `Path=/`, no `Domain`) and is redirected to the same URL without the
  token. Every other request needs that cookie; without it the answer is a German page that
  says how to open arr (status 401), for the websocket a refused handshake.
- **Host check** (DNS rebinding): only `<random>.localhost:<port>` gets data (status 403
  otherwise, with the German page and without a cookie). A token link on `127.0.0.1:<port>`
  or `localhost:<port>` only redirects to the own hostname.
- **Origin check:** a request that carries an `Origin` header is refused unless it is the
  own origin, for every method and for the websocket (status 403; so no CORS answer of
  Engine.IO ever reaches another site). Requests without `Origin` may only read (`GET`,
  `HEAD`, `OPTIONS`); everything else needs the own origin.

**`/mcp/` (W4-01):** the MCP server for Claude Code (`arr.service.user_api`) sits below
`/mcp`. There the cookie counts for nothing: only `Authorization: Bearer <token>` opens it,
which `arr mcp` reads from `service.json` (a browser never sends it by itself). Host and origin
are checked as strictly as for the GUI (DNS rebinding: only the own name; any `Origin` but the
own one is refused). Everything else, a websocket included, gets 403 without data. The GUI
checks above stay exactly as they are.

Tokens are compared in constant time (`hmac.compare_digest`) and never logged.
"""

import hmac
import secrets
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode

COOKIE = "arr_session"
TOKEN_PARAM = "token"
COOKIE_MAX_AGE_S = 30 * 24 * 3600
"""The cookie lives this long in the browser; it is only good for one start of the service
(each start has a new token and a new hostname), so an old one simply stops working."""
SERVICE_HEADER = b"x-arr-service"
"""Response header on every refusal, so `arr serve` can tell a running service on the port
from some other program."""
HOSTNAME_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"
HOSTNAME_LENGTH = 20
HOSTNAME_SUFFIX = ".localhost"
LOOPBACK_NAMES = ("127.0.0.1", "localhost")
"""Names a token link may use by mistake; the guard only redirects them to the own name."""

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MCP_PATH = "/mcp"
"""Mount point of the MCP server for Claude Code (W4-01)."""
BEARER = "bearer "

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def new_token() -> str:
    """A fresh access token (256 bits, URL safe)."""
    return secrets.token_urlsafe(32)


def new_hostname() -> str:
    """A fresh hostname `<random>.localhost` that no other program knows (W3-01.1 M1)."""
    name = "".join(secrets.choice(HOSTNAME_CHARS) for _ in range(HOSTNAME_LENGTH))
    return name + HOSTNAME_SUFFIX


def service_host(hostname: str, port: int) -> str:
    """The only `Host` the service answers: `<hostname>:<port>`."""
    return f"{hostname}:{port}".lower()


def service_origin(hostname: str, port: int) -> str:
    """The only origin the service accepts."""
    return f"http://{service_host(hostname, port)}"


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
    """ASGI middleware: hostname, cookie and origin checks (see the module docstring)."""

    def __init__(self, app: ASGIApp, *, token: str, port: int, hostname: str) -> None:
        self.app = app
        self._token = token
        self.port = port
        self.hostname = hostname.lower()
        self.host = service_host(hostname, port)
        self.origin = service_origin(hostname, port)
        self.loopback_hosts = frozenset(f"{name}:{port}" for name in LOOPBACK_NAMES)
        self._page = refusal_page().encode("utf-8")

    # -- checks ------------------------------------------------------------------------------

    def token_ok(self, candidate: str | None) -> bool:
        """Constant-time comparison with the service token."""
        if not candidate:
            return False
        return hmac.compare_digest(candidate.encode("utf-8"), self._token.encode("utf-8"))

    def host_ok(self, scope: Scope) -> bool:
        host = _header(scope, b"host")
        return host is not None and host.lower() == self.host

    def origin_ok(self, scope: Scope) -> bool:
        origin = _header(scope, b"origin")
        return origin is not None and origin.lower() == self.origin

    def origin_foreign(self, scope: Scope) -> bool:
        """True for a request that names another origin (also `null`)."""
        origin = _header(scope, b"origin")
        return origin is not None and origin.lower() != self.origin

    def cookie_ok(self, scope: Scope) -> bool:
        return self.token_ok(_cookie_value(scope, COOKIE))

    def bearer_ok(self, scope: Scope) -> bool:
        """`Authorization: Bearer <token>` (only for `/mcp/`, W4-01)."""
        header = _header(scope, b"authorization") or ""
        if not header.lower().startswith(BEARER):
            return False
        return self.token_ok(header[len(BEARER) :].strip())

    @staticmethod
    def is_mcp(scope: Scope) -> bool:
        path = str(scope.get("path") or "")
        return path == MCP_PATH or path.startswith(MCP_PATH + "/")

    # -- ASGI --------------------------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind in ("http", "websocket") and self.is_mcp(scope):
            await self._mcp(scope, receive, send)
        elif kind == "websocket":
            await self._websocket(scope, receive, send)
        elif kind == "http":
            await self._http(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    async def _mcp(self, scope: Scope, receive: Receive, send: Send) -> None:
        """`/mcp/`: own host, no foreign origin, the bearer token; never a cookie (W4-01)."""
        allowed = (
            scope.get("type") == "http"
            and self.host_ok(scope)
            and not self.origin_foreign(scope)
            and self.bearer_ok(scope)
        )
        if allowed:
            await self.app(scope, receive, send)
            return
        if scope.get("type") == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1008, "reason": "arr: nicht erlaubt"})
            return
        await self._refuse(send, 403, b"text/plain; charset=utf-8", b"arr: nicht erlaubt")

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (self.host_ok(scope) and self.cookie_ok(scope) and self.origin_ok(scope)):
            # Refusing before `accept` makes the server answer the handshake with 403.
            await receive()
            await send({"type": "websocket.close", "code": 1008, "reason": "arr: nicht erlaubt"})
            return
        await self.app(scope, receive, send)

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        method = str(scope.get("method", "GET")).upper()
        query = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1")))
        if not self.host_ok(scope):
            await self._wrong_host(scope, send, method, query)
            return
        if self.origin_foreign(scope):
            await self._refuse(send, 403, b"text/plain; charset=utf-8", b"arr: fremder Origin")
            return
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

    async def _wrong_host(self, scope: Scope, send: Send, method: str, query: dict) -> None:
        """No data and no cookie for another host; a token link on `127.0.0.1`/`localhost`
        is sent on to the own hostname (the token stays in the link for that one hop)."""
        host = (_header(scope, b"host") or "").lower()
        if (
            host in self.loopback_hosts
            and method in ("GET", "HEAD")
            and self.token_ok(query.get(TOKEN_PARAM))
        ):
            location = self.origin + self._location(scope, query, keep_token=True)
            await self._redirect(send, location, cookie=None)
            return
        await self._refuse(send, 403, b"text/html; charset=utf-8", self._page)

    async def _login(self, scope: Scope, send: Send, query: dict[str, str]) -> None:
        """`?token=…` on the own host: set the cookie and redirect without the token."""
        if not self.token_ok(query.get(TOKEN_PARAM)):
            await self._refuse(send, 401, b"text/html; charset=utf-8", self._page)
            return
        cookie = (
            f"{COOKIE}={self._token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={COOKIE_MAX_AGE_S}"
        )
        await self._redirect(send, self._location(scope, query, keep_token=False), cookie=cookie)

    @staticmethod
    def _location(scope: Scope, query: dict[str, str], *, keep_token: bool) -> str:
        """Path and query of the request, percent-encoded (the path arrives decoded)."""
        items = [(k, v) for k, v in query.items() if keep_token or k != TOKEN_PARAM]
        rest = urlencode(items)
        path = quote(scope.get("path") or "/")
        return path + (f"?{rest}" if rest else "")

    async def _redirect(self, send: Send, location: str, *, cookie: str | None) -> None:
        headers = [
            (b"location", location.encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"content-length", b"0"),
        ]
        if cookie is not None:
            headers.append((b"set-cookie", cookie.encode("latin-1")))
        await send({"type": "http.response.start", "status": 303, "headers": headers})
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
