"""Child process that runs `capture_token` and is then killed by the test.

Arguments: the fake CLI, the deadline and the alarm grace in seconds.
"""

import asyncio
import sys
from pathlib import Path

from arr.service import login


async def main() -> None:
    login.ALARM_GRACE_S = int(sys.argv[3])
    await login.capture_token(
        lambda token: None, cli=Path(sys.argv[1]), timeout_s=float(sys.argv[2])
    )


if __name__ == "__main__":
    asyncio.run(main())
