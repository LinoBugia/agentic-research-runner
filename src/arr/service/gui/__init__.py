"""Pages of the local web GUI.

See docs/04-service-gui-und-schnittstellen.md §4.
"""


def run(*, mock: bool = False, port: int | None = None, open_browser: bool = True) -> None:
    """Start the GUI on 127.0.0.1 and open it in the browser; blocks until stopped.

    `mock=True` uses the mock data (W1-06), otherwise the real service (W3-01). `port=None`
    picks the default port. Placeholder until W1-06; raises `NotImplementedError` with a
    German message.
    """
    raise NotImplementedError("Die GUI („arr gui --mock“) kommt mit Auftrag W1-06.")
