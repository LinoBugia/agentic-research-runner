"""Command line interface (Typer). Later commands attach to `app` as subcommands.

See docs/04-service-gui-und-schnittstellen.md §6.
"""

import platform
from pathlib import Path
from typing import Annotated

import typer

from arr import __version__
from arr.errors import ArrError
from arr.settings import app_dir

app = typer.Typer(
    name="arr",
    help="arr – agentic research runner: Research-Groups aus Claude- und ChatGPT-Agenten.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"arr {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Version anzeigen und beenden.",
        ),
    ] = False,
) -> None:
    """arr – agentic research runner."""


@app.command()
def info() -> None:
    """Version, Python, App-Ordner und Plattform anzeigen."""
    typer.echo(f"arr:        {__version__}")
    typer.echo(f"Python:     {platform.python_version()}")
    typer.echo(f"App-Ordner: {app_dir()}")
    typer.echo(f"Plattform:  {platform.platform()}")


def _fail(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


@app.command()
def init(
    path: Annotated[
        Path,
        typer.Argument(help="Ordner des Workfolders (Standard: aktueller Ordner)."),
    ] = Path("."),
) -> None:
    """Workfolder anlegen (vorhandene Dateien bleiben unverändert)."""
    from arr.workfolder import init_workfolder

    try:
        folder = init_workfolder(path)
    except (NotImplementedError, ArrError) as exc:
        _fail(str(exc))
    typer.echo(f"Workfolder bereit: {folder}")


@app.command()
def gui(
    mock: Annotated[
        bool, typer.Option("--mock", help="Mit Beispieldaten statt echtem Service starten.")
    ] = False,
    port: Annotated[int | None, typer.Option(help="Port auf 127.0.0.1.")] = None,
    browser: Annotated[
        bool, typer.Option("--browser/--no-browser", help="Oberfläche im Browser öffnen.")
    ] = True,
) -> None:
    """Oberfläche starten (vorerst nur mit --mock)."""
    from arr.service.gui import run

    try:
        run(mock=mock, port=port, open_browser=browser)
    except (NotImplementedError, ArrError) as exc:
        _fail(str(exc))
