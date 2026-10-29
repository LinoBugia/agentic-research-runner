"""Child process of the service tests: `arr serve` with FakeBackend builders.

Environment: `ARR_HOME`, `ARR_TEST_PORT`, `ARR_TEST_WORKFOLDER`, optional `ARR_TEST_SCRIPTS`
(JSON file: FakeBackend scripts by agent ID) and `ARR_TEST_AUTOSTART` (JSON file: a run
input that is started right after the service is up). Run with the tests folder on the path
(`run_kit`), see `service_kit.start_service`.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from run_kit import FAST, fake_builders  # noqa: E402

import arr.auth  # noqa: E402
from arr.runs import launch as launch_module  # noqa: E402


def main() -> None:
    arr.auth._keyring_get = lambda service, account: None  # type: ignore[attr-defined]
    scripts_file = os.environ.get("ARR_TEST_SCRIPTS")
    if scripts_file:
        scripts = json.loads(Path(scripts_file).read_text("utf-8"))
        launch_module.BACKEND_BUILDERS = fake_builders(scripts)  # type: ignore[assignment]
    autostart = os.environ.get("ARR_TEST_AUTOSTART")
    if autostart:
        from nicegui import app

        from arr.service.gui.app import get_api

        run_input = json.loads(Path(autostart).read_text("utf-8"))

        async def start() -> None:
            api = get_api()
            api.state.manager_kwargs.setdefault("pacing", FAST)  # type: ignore[attr-defined]
            await api.start_run(run_input)

        app.on_startup(start)

    from arr.log import setup_logging
    from arr.service.serve import serve

    setup_logging("INFO")
    serve(
        workfolder=os.environ["ARR_TEST_WORKFOLDER"],
        port=int(os.environ["ARR_TEST_PORT"]),
        open_browser=False,
    )


if __name__ == "__main__":
    main()
