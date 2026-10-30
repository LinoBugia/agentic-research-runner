"""Child process of the two-process tests: `arr run <run.yaml>` with FakeBackend builders,
real signals (Ctrl-C through the process group) and a real `RunLock`. Scripts come from the
JSON file in `ARR_TEST_SCRIPTS`; exit code like `arr run` (0 done, 130 stopped, 1 else)."""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from run_kit import FAST, fake_builders  # noqa: E402

import arr.auth  # noqa: E402
from arr.runs.launch import exit_code, plan_launch, run_launch  # noqa: E402


def main() -> int:
    arr.auth._keyring_get = lambda service, account: None  # type: ignore[attr-defined]
    scripts = json.loads(Path(os.environ["ARR_TEST_SCRIPTS"]).read_text("utf-8"))
    launch = plan_launch(sys.argv[1])
    result = asyncio.run(
        run_launch(
            launch,
            decider=None,
            report=lambda line: print(line, flush=True),
            builders=fake_builders(scripts),
            interval_s=0.05,
            stop_poll_s=0.1,
            pacing=FAST,
        )
    )
    return exit_code(result.status)


if __name__ == "__main__":
    sys.exit(main())
