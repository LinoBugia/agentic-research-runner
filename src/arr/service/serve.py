"""`arr serve`: the local service with the GUI on real data (W3-01 §3.1, docs/04 §1).

One process runs NiceGUI (and so FastAPI and uvicorn) on `127.0.0.1` with `RealApi` on the
workfolder, and holds `[::1]:<port>` itself with a relay to it (`arr.service.loopback`,
W3-01.2), so no other program can sit where browsers connect first. It answers only under its
own fresh hostname `<random>.localhost` (W3-01.1 M1); the whole app sits behind the `Guard`
(token cookie, host and origin checks, `arr.service.guard`). `~/.arr/service.json` (0600)
tells where the service listens; a second `arr serve` finds a running service (process alive,
port answers as arr under that name), opens it in the browser and ends. Ctrl-C or SIGTERM
stops every run of the service with finalizer and secret scan, then the process ends; a
second signal aborts hard like `arr run` (status `interrupted`). At start, runs left active
by a crash become `interrupted` (`RealApi.start`).

The MCP server for Claude Code (`arr.service.user_api`, W4-01) is mounted at `/mcp/` in the
same process, with its own startup task for the session manager (spike S6: mounted apps get no
lifespan) and the DNS-rebinding protection of the `mcp` package on the own name; the `Guard`
lets only the bearer token from `service.json` through there. What the service tells the
terminal also goes to the service log (`~/.arr/logs/service.log`, without the token).

This module knows no Typer: `arr.cli` calls `serve()`.
"""

import asyncio
import contextlib
import http.client
import logging
import os
import signal
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arr.errors import ArrError
from arr.log import TERMINAL_COPY, add_secret
from arr.models import RunStatus
from arr.runs.launch import EXIT_STOPPED, REPEAT_WINDOW_S, find_workfolder
from arr.runs.manager import set_run_status
from arr.runs.orchestrator import OrchestratorLog
from arr.runs.prepare import load_run_meta
from arr.service.guard import SERVICE_HEADER, Guard, new_hostname, new_token, service_host
from arr.service.loopback import LoopbackRelay, port_in_use
from arr.service.real_api import RealApi
from arr.service.state import (
    ServiceInfo,
    read_service_info,
    recent_workfolders,
    remember_workfolder,
    remove_service_info,
    write_service_info,
)
from arr.service.user_api import mount_mcp
from arr.workfolder import is_workfolder

DEFAULT_PORT = 8765
Report = Callable[[str], None]

log = logging.getLogger("arr.service")


def _print(line: str) -> None:
    """Print a line for the user; a closed standard output never stops the service (N3).

    `arr serve | tee …` whose reader is gone makes `print` raise `BrokenPipeError`. Then
    further output goes nowhere, so neither a later line nor the flush at exit can fail.
    """
    try:
        print(line, flush=True)
    except OSError:
        with contextlib.suppress(Exception):
            sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


def _say(report: Report, line: str) -> None:
    """Report without ever raising (a broken reporter must not stop anything, N3).

    The line also goes to the service log; its filter removes the token (W4-01).
    """
    with contextlib.suppress(Exception):
        report(line)
    with contextlib.suppress(Exception):
        log.info("%s", line, extra={TERMINAL_COPY: True})


# --------------------------------------------------------------------------------------------
# Workfolder, port, running service
# --------------------------------------------------------------------------------------------


def resolve_workfolder(given: str | Path | None) -> Path:
    """`--workfolder`, else the last one used, else an `ArrError` that says the way."""
    if given is not None:
        return find_workfolder(given)
    for path in recent_workfolders():
        if is_workfolder(path):
            return path.absolute()
    raise ArrError(
        "Kein Workfolder: arr weiß noch nicht, welchen Ordner es öffnen soll. Starte mit "
        "„arr serve --workfolder <Ordner>“ (einen neuen Workfolder legt „arr init <Ordner>“ "
        "an). arr merkt sich den Ordner für das nächste Mal."
    )


def port_free(port: int) -> bool:
    """True if arr can take the port on `127.0.0.1` and `[::1]` (see `port_in_use`)."""
    return port_in_use(port) is None


def answers_as_arr(port: int, hostname: str, timeout_s: float = 1.0) -> bool:
    """True if the arr service with this hostname answers on the port.

    Asked without a cookie under its own `Host`, a real arr always answers 401 with the
    header `x-arr-service` (W3-01.1 G3). Any other answer, e.g. 200 from some other program
    on the port or 403 from an arr with another name, means „not this service“.
    """
    try:
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_s)
        try:
            con.request("GET", "/", headers={"Host": service_host(hostname, port)})
            response = con.getresponse()
            response.read()
            marked = response.getheader(SERVICE_HEADER.decode("ascii")) is not None
            return marked and response.status == 401
        finally:
            con.close()
    except OSError:
        return False


def running_service() -> ServiceInfo | None:
    """The service of `service.json`, if its process lives and its port answers as that arr.

    A stale file (process gone, or another program on the port) counts as no service, so the
    token link never goes to anything but arr.
    """
    info = read_service_info()
    if info is None or not info.alive() or info.pid == os.getpid():
        return None
    if not answers_as_arr(info.port, info.host):
        return None
    return info


# --------------------------------------------------------------------------------------------
# Stopping: Ctrl-C and SIGTERM
# --------------------------------------------------------------------------------------------


class ServiceStop:
    """Ctrl-C of the service: the first stops all runs and ends, the second aborts hard.

    Mirrors `StopControl` of `arr run` (docs/02 §7): signals within `REPEAT_WINDOW_S` are the
    same Ctrl-C (process group + `uv run`). The first signal stops every run of the service
    (`RealApi.stop_runs`: approvals cancelled, turns interrupted, finalizer, secret scan) and
    then shuts the server down. A second one marks the service's active runs `interrupted`
    (`previous: hard_abort`) and leaves the process with `EXIT_STOPPED`.
    """

    def __init__(
        self,
        api: RealApi,
        shutdown: Callable[[], Any],
        report: Report = _print,
        *,
        hard_exit: Callable[[int], Any] = os._exit,
        clock: Callable[[], float] = time.monotonic,
        repeat_window_s: float = REPEAT_WINDOW_S,
        relay: LoopbackRelay | None = None,
    ) -> None:
        self.api = api
        self.shutdown = shutdown
        self.report = report
        self.hard_exit = hard_exit
        self.clock = clock
        self.repeat_window_s = repeat_window_s
        self.relay = relay
        self.interrupts = 0
        self._last: float | None = None
        self._task: asyncio.Task[None] | None = None

    def on_signal(self) -> None:
        now = self.clock()
        if self._last is not None and now - self._last < self.repeat_window_s:
            return
        self._last = now
        self.interrupts += 1
        if self.interrupts > 1:
            self.hard_abort()
            return
        active = len(self.api.state.registry.active())
        if active:
            _say(
                self.report,
                f"Strg-C: {active} Run{'s' if active != 1 else ''} des Service "
                f"{'werden' if active != 1 else 'wird'} gestoppt (Finalizer und Secret-Scan "
                "laufen), dann endet arr. Noch einmal Strg-C (nach einer Sekunde) bricht hart ab.",
            )
        else:
            _say(self.report, "Strg-C: arr wird beendet.")
        self._task = asyncio.get_running_loop().create_task(self._graceful())

    async def _graceful(self) -> None:
        try:
            await self.api.stop_runs()
        except Exception:
            log.exception("Stoppen der Runs fehlgeschlagen")
        self.shutdown()

    def hard_abort(self) -> None:
        _say(
            self.report,
            "Zweites Strg-C: harter Abbruch. Laufende Runs bleiben unvollständig, ihr Status "
            "wird „unterbrochen“ (interrupted).",
        )
        for handle in self.api.state.registry.active():
            with contextlib.suppress(Exception):
                if not load_run_meta(handle.run_dir).status.is_final:
                    set_run_status(handle.run_dir, RunStatus.INTERRUPTED)
                    OrchestratorLog(handle.run_dir).write(
                        "status", status=RunStatus.INTERRUPTED.value, previous="hard_abort"
                    )
        with contextlib.suppress(Exception):
            remove_service_info(os.getpid())
        if self.relay is not None:
            self.relay.close()  # the process end would release it too; be explicit
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                stream.flush()
        self.hard_exit(EXIT_STOPPED)


# --------------------------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------------------------


class ServiceRuntime:
    """Startup and shutdown of one service process (hooked into NiceGUI's app events)."""

    def __init__(
        self,
        api: RealApi,
        info: ServiceInfo,
        *,
        open_browser: bool,
        report: Report = _print,
        relay: LoopbackRelay | None = None,
    ) -> None:
        self.api = api
        self.info = info
        self.open_browser = open_browser
        self.report = report
        self.relay = relay
        self.stop: ServiceStop | None = None
        self._remove_signals: Callable[[], None] = lambda: None

    async def startup(self) -> None:
        from nicegui import app

        if self.relay is not None:
            await self.relay.start()
        self.info.pid = os.getpid()
        self.info.started = time.time()
        write_service_info(self.info)
        remember_workfolder(self.info.workfolder)
        await self.api.start()
        for run_id in self.api.state.interrupted_at_start:
            _say(
                self.report,
                f"Run {run_id} war beim letzten Mal noch aktiv und gilt jetzt als unterbrochen "
                "(interrupted).",
            )
        self.stop = ServiceStop(self.api, app.shutdown, self.report, relay=self.relay)
        self._remove_signals = self._install_signals(self.stop)
        _say(self.report, f"arr läuft auf {self.info.url} (Workfolder {self.info.workfolder}).")
        if self.open_browser:
            url = self.info.open_url
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    @staticmethod
    def _install_signals(stop: ServiceStop) -> Callable[[], None]:
        loop = asyncio.get_running_loop()
        signals = (signal.SIGINT, signal.SIGTERM)
        try:
            for sig in signals:
                loop.add_signal_handler(sig, stop.on_signal)
        except (ValueError, RuntimeError, NotImplementedError):
            return lambda: None

        def remove() -> None:
            for sig in signals:
                with contextlib.suppress(Exception):
                    loop.remove_signal_handler(sig)

        return remove

    async def shutdown(self) -> None:
        try:
            if self.api.state.registry.active():
                await self.api.stop_runs()
            await self.api.close()
        finally:
            self._remove_signals()
            if self.relay is not None:
                self.relay.close()
            remove_service_info(os.getpid())
            _say(self.report, "arr ist beendet.")


def serve(
    *,
    workfolder: str | Path | None = None,
    port: int | None = None,
    open_browser: bool = True,
    report: Report = _print,
) -> None:
    """Start the service with the GUI; blocks until it is stopped (docs/04 §1).

    Raises `ArrError` (German) when no workfolder is known or the port is taken by another
    program. If an arr service already runs, opens it in the browser and returns.
    """
    root = resolve_workfolder(workfolder)
    port = port or DEFAULT_PORT
    running = running_service()
    if running is not None:
        since = time.strftime("%H:%M", time.localtime(running.started))
        report(
            f"arr läuft schon auf {running.url} (Workfolder {running.workfolder}, seit {since})."
        )
        if open_browser:
            webbrowser.open(running.open_url)
            report("Die Oberfläche wurde im Browser geöffnet.")
        else:
            report(f"Öffne die Oberfläche mit diesem Link: {running.open_url}")
        return
    taken = port_in_use(port)
    if taken is not None:
        raise ArrError(
            f"Der Port {port} ist unter {taken} belegt, aber nicht von arr. Wähle mit „--port“ "
            "einen anderen Port oder beende das Programm, das den Port nutzt."
        )
    relay = LoopbackRelay(port)  # [::1]:<port> is arr's before the token link exists (W3-01.2)
    if not relay.bind():
        _say(report, f"Kein IPv6 auf diesem Rechner: arr reserviert [::1]:{port} nicht.")
    token = new_token()
    add_secret(token)  # never in the service log (W4-01)
    hostname = new_hostname()
    info = ServiceInfo(host=hostname, port=port, token=token, pid=os.getpid(), workfolder=str(root))
    api = RealApi(root, base_url=info.url)
    runtime = ServiceRuntime(api, info, open_browser=open_browser, report=report, relay=relay)

    from nicegui import app, ui

    from arr.service.gui.app import setup

    setup(api)
    mcp = mount_mcp(app, api, hostname=hostname, port=port)
    app.add_middleware(Guard, token=token, port=port, hostname=hostname)
    app.on_startup(runtime.startup)
    app.on_startup(mcp.start)
    app.on_shutdown(mcp.stop)
    app.on_shutdown(runtime.shutdown)
    _say(report, f"arr startet auf {info.url} mit Workfolder {root}. Beenden mit Strg+C.")
    if not open_browser:
        _say(
            report, f"Öffne die Oberfläche mit diesem Link (gilt für diesen Start): {info.open_url}"
        )
    try:
        ui.run(
            host="127.0.0.1",
            port=port,
            title="arr",
            language="de",
            reload=False,
            show=False,
            show_welcome_message=False,
            tailwind=False,
            favicon="✋",
            reconnect_timeout=10.0,
            uvicorn_logging_level="warning",
        )
    finally:
        relay.close()
