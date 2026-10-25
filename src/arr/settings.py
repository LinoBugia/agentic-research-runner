"""App settings (`~/.arr/`) and workfolder settings (`arr.yaml`).

See docs/01-architektur.md §2 and docs/02-workfolder-und-runs.md §1.
"""

import os
from pathlib import Path

ARR_HOME_ENV = "ARR_HOME"


def app_dir() -> Path:
    """Return the absolute app directory: `$ARR_HOME` if set, otherwise `~/.arr`.

    A relative `$ARR_HOME` is made absolute against the current directory, so the returned
    path stays valid after a directory change or in a subprocess. The directory is not
    created here.
    """
    override = os.environ.get(ARR_HOME_ENV)
    if override:
        return Path(override).expanduser().absolute()
    return Path.home() / ".arr"
