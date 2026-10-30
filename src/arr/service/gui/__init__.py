"""Local web GUI on NiceGUI (docs/04-service-gui-und-schnittstellen.md §4, docs/08-ux-konzept.md).

`run(mock=True)` serves the clickable prototype on sample data (W1-06); the service with real
data is `arr serve` (`arr.service.serve`, W3-01).
"""

from arr.errors import ArrError

DEFAULT_PORT = 8765


def run(*, mock: bool = False, port: int | None = None, open_browser: bool = True) -> None:
    """Start the GUI on 127.0.0.1 and open it in the browser; blocks until stopped.

    `mock=True` uses `MockApi` with its simulated live stream. Without `mock` this raises
    `ArrError`: real data come from the service, `arr serve`.
    """
    if not mock:
        raise ArrError(
            "Die Oberfläche mit echten Daten startet „arr serve“ (mit --workfolder, falls arr "
            "den Workfolder noch nicht kennt). Beispieldaten zeigt „arr gui --mock“."
        )
    from nicegui import app, ui

    from arr.service.gui.app import setup
    from arr.service.mock_api import MockApi

    port = port or DEFAULT_PORT
    api = MockApi(base_url=f"http://127.0.0.1:{port}")
    setup(api)
    app.on_startup(api.start_simulation)
    print(f"arr läuft auf http://127.0.0.1:{port} (Beispieldaten). Beenden mit Ctrl+C.", flush=True)
    ui.run(
        host="127.0.0.1",
        port=port,
        title="arr",
        language="de",
        reload=False,
        show=open_browser,
        show_welcome_message=False,
        tailwind=False,
        favicon="✋",
        reconnect_timeout=10.0,
    )
