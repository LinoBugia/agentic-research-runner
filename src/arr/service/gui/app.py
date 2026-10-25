"""Assembles the GUI: static files, shared head, pages (docs/04 §4, docs/08 §4.1).

`setup(api)` is used by `arr.service.gui.run` and by the tests (NiceGUI user simulation);
pages fetch the client with `get_api()`.
"""

from pathlib import Path

from nicegui import app, ui

from arr.service.api_client import ApiClient

STATIC_DIR = Path(__file__).parent / "static"
STATIC_URL = "/arr-static"
_API: ApiClient | None = None


def get_api() -> ApiClient:
    """The `ApiClient` of this process (set by `setup`)."""
    if _API is None:
        raise RuntimeError("GUI not set up: call arr.service.gui.app.setup(api) first")
    return _API


def setup(api: ApiClient) -> None:
    """Register static files, the shared head and all pages for `api`."""
    global _API
    _API = api
    app.add_static_files(STATIC_URL, STATIC_DIR)
    version = int(max(p.stat().st_mtime for p in STATIC_DIR.iterdir()))
    ui.add_head_html(
        f'<link rel="stylesheet" href="{STATIC_URL}/arr.css?v={version}">'
        f'<script src="{STATIC_URL}/arr.js?v={version}" defer></script>'
        '<meta name="color-scheme" content="light dark">',
        shared=True,
    )
    from arr.service.gui.pages import freigaben, placeholders, run_detail, runs

    freigaben.register()
    runs.register()
    run_detail.register()
    placeholders.register()
