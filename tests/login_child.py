"""Child process that runs `capture_token` and is then killed by the test.

Arguments: the CLI, the deadline, the alarm grace in seconds and optionally `interactive`:
the plain start of the CLI, for the real CLI, which then neither logs in nor opens a browser.
"""

import asyncio
import sys
from pathlib import Path

from arr.service import login


async def main() -> None:
    login.ALARM_GRACE_S = int(sys.argv[3])
    args = () if sys.argv[4:] == ["interactive"] else ("setup-token",)
    await login.capture_token(
        lambda token: None, cli=Path(sys.argv[1]), timeout_s=float(sys.argv[2]), args=args
    )


if __name__ == "__main__":
    asyncio.run(main())
