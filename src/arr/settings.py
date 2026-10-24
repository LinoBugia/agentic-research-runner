"""App settings (`~/.arr/`) and workfolder settings (`arr.yaml`).

See docs/01-architektur.md §2 and docs/02-workfolder-und-runs.md §1.
"""

import os
from pathlib import Path

ARR_HOME_ENV = "ARR_HOME"


def app_dir() -> Path:
    """Return the app directory: `$ARR_HOME` if set, otherwise `~/.arr`.

    The directory is not created here.
    """
    override = os.environ.get(ARR_HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".arr"
