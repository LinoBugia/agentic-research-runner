"""Command line interface (Typer). Later commands attach to `app` as subcommands.

See docs/04-service-gui-und-schnittstellen.md §6.
"""

import platform
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from arr import __version__
from arr.errors import ArrError
from arr.settings import app_dir

if TYPE_CHECKING:
    from arr.models import AuthProfile

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


auth_app = typer.Typer(
    help="Anmeldungen der Auth-Profile verwalten.",
    no_args_is_help=True,
)
app.add_typer(auth_app, name="auth")


def _codex_profile(name: str) -> "AuthProfile":
    """The profile from `profiles.yaml`, or a new `codex_chatgpt` profile with that name."""
    from arr.models import AuthKind, AuthProfile, ProfilesFile
    from arr.resolve import load_yaml_model

    file = app_dir() / "profiles.yaml"
    if file.exists():
        for profile in load_yaml_model(file, ProfilesFile).profiles:
            if profile.name == name:
                if profile.kind is not AuthKind.CODEX_CHATGPT:
                    raise ArrError(
                        f"Das Profil „{name}“ ist kein ChatGPT-Profil (Art: {profile.kind}). "
                        "Der Codex-Login geht nur für Profile der Art „codex_chatgpt“."
                    )
                return profile
    return AuthProfile(name=name, kind=AuthKind.CODEX_CHATGPT)


@auth_app.command("codex-login")
def codex_login(
    profile: Annotated[str, typer.Argument(help="Name des Auth-Profils, z. B. chatgpt-privat.")],
) -> None:
    """ChatGPT-Login für ein Codex-Profil per Gerätecode (eigener Login-Ordner)."""
    import asyncio

    from pydantic import ValidationError

    from arr.backends.codex_config import codex_home_for
    from arr.backends.codex_login import codex_account, codex_login

    try:
        auth = _codex_profile(profile)
    except ValidationError:
        _fail(f"„{profile}“ ist kein gültiger Profilname (Kleinbuchstaben, Ziffern, Bindestriche).")
    except ArrError as exc:
        _fail(str(exc))

    def show(link: str, code: str) -> None:
        typer.echo("Öffne diesen Link im Browser und gib dort den Code ein:")
        typer.echo(f"  Link: {link}")
        typer.echo(f"  Code: {code}")
        typer.echo("Warte auf die Anmeldung (der Code gilt 15 Minuten, Abbruch mit Strg+C) …")

    typer.echo(f"Login-Ordner: {codex_home_for(auth)}")
    try:
        state = asyncio.run(codex_account(auth))
        if state.logged_in:
            typer.echo(f"Das Profil „{auth.name}“ ist bereits angemeldet: {state.describe()}.")
            return
        result = asyncio.run(codex_login(auth, show=show))
    except KeyboardInterrupt:
        _fail("Abgebrochen. Das Profil bleibt abgemeldet.")
    except Exception as exc:  # noqa: BLE001 - Codex errors are shown, never their details
        _fail(f"Der Login ist fehlgeschlagen ({type(exc).__name__}).")
    if not result.success:
        _fail(f"Der Login ist fehlgeschlagen: {result.error}")
    typer.echo(f"Angemeldet: {result.account.describe()}. Profil „{auth.name}“ ist bereit.")
