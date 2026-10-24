"""Command line interface (Typer). Later commands attach to `app` as subcommands.

See docs/04-service-gui-und-schnittstellen.md §6.
"""

import platform
from typing import Annotated

import typer

from arr import __version__
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
