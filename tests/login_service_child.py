"""Child process: `arr serve` with a fake Claude CLI and a login running from the start.

Environment as in `service_child`, plus `ARR_TEST_CLI` (the fake CLI) and `ARR_TEST_PROFILE`.
"""

import os
from pathlib import Path

from nicegui import app

import arr.auth
from arr.log import setup_logging
from arr.service import login
from arr.service.gui.app import get_api
from arr.service.serve import serve


def main() -> None:
    arr.auth._keyring_get = lambda service, account: None  # type: ignore[attr-defined]
    login.claude_cli = lambda: Path(os.environ["ARR_TEST_CLI"])  # type: ignore[assignment]

    async def start() -> None:
        await get_api().start_login(os.environ["ARR_TEST_PROFILE"])

    app.on_startup(start)
    setup_logging("INFO")
    serve(
        workfolder=os.environ["ARR_TEST_WORKFOLDER"],
        port=int(os.environ["ARR_TEST_PORT"]),
        open_browser=False,
    )


if __name__ == "__main__":
    main()
