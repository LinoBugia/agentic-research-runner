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


WorkfolderOption = Annotated[
    Path | None,
    typer.Option(
        "--workfolder",
        "-w",
        help="Workfolder (Standard: vom aktuellen Ordner aufwärts bis zur arr.yaml).",
        show_default=False,
    ),
]


@app.command()
def run(
    run_file: Annotated[
        Path, typer.Argument(help="Die run.yaml des Runs.", show_default=False, metavar="RUN_YAML")
    ],
    param: Annotated[
        list[str] | None,
        typer.Option(
            "--param",
            "-p",
            help="Parameter der Topologie als name=wert, mehrfach möglich.",
            show_default=False,
        ),
    ] = None,
    workfolder: WorkfolderOption = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Auch Info-Meldungen der Backends zeigen.")
    ] = False,
) -> None:
    """Run aus einer run.yaml starten. Freigaben fragt arr hier im Terminal nach.

    Strg-C stoppt den Run sauber (Finalizer läuft), ein zweites Strg-C bricht hart ab.
    Exit-Code 0 bei „done“, 130 bei „stopped“, sonst 1.
    """
    import asyncio

    from arr.log import setup_logging
    from arr.permissions.inbox import TerminalDecider
    from arr.runs.launch import exit_code, parse_params, plan_launch, run_launch

    setup_logging("INFO" if verbose else "WARNING")
    try:
        launch = plan_launch(run_file, params=parse_params(param or []), workfolder=workfolder)
    except ArrError as exc:
        _fail(str(exc))
    typer.echo(f"Workfolder: {launch.workfolder}")
    try:
        result = asyncio.run(run_launch(launch, decider=TerminalDecider(), report=typer.echo))
    except ArrError as exc:
        _fail(str(exc))
    except KeyboardInterrupt:
        _fail("Abgebrochen.")
    raise typer.Exit(code=exit_code(result.status))


runs_app = typer.Typer(help="Runs des Workfolders anzeigen und steuern.", no_args_is_help=True)
app.add_typer(runs_app, name="runs")

RunIdArgument = Annotated[
    str, typer.Argument(help="Run-ID, siehe „arr runs list“.", show_default=False)
]


def _runs_workfolder(given: Path | None) -> Path:
    from arr.runs.launch import find_workfolder

    try:
        return find_workfolder(given)
    except ArrError as exc:
        _fail(str(exc))
    raise AssertionError("unreachable")


def _run_folder(workfolder: Path | None, run_id: str) -> Path:
    from arr.runs.inspect import run_folder

    try:
        return run_folder(_runs_workfolder(workfolder), run_id)
    except ArrError as exc:
        _fail(str(exc))
    raise AssertionError("unreachable")


@runs_app.command("list")
def runs_list(workfolder: WorkfolderOption = None) -> None:
    """Runs des Workfolders auflisten, neueste zuerst."""
    from arr.runs.inspect import format_runs, list_runs

    root = _runs_workfolder(workfolder)
    typer.echo(format_runs(root, list_runs(root)))


@runs_app.command("status")
def runs_status(run_id: RunIdArgument, workfolder: WorkfolderOption = None) -> None:
    """Status, Grund, letzter Checkpoint, Turns, Usage und offene Freigaben eines Runs."""
    from arr.runs.inspect import format_status, run_status

    folder = _run_folder(workfolder, run_id)
    try:
        typer.echo(format_status(run_status(folder)))
    except (OSError, ValueError, ArrError) as exc:
        _fail(f"Der Run-Ordner {folder} ist nicht lesbar: {exc}")


@runs_app.command("stop")
def runs_stop(run_id: RunIdArgument, workfolder: WorkfolderOption = None) -> None:
    """Laufenden Run stoppen: legt eine Stopp-Anforderung ab, die der ausführende „arr run“-Prozess
    innerhalb weniger Sekunden umsetzt (laufende Turns unterbrochen, danach der Finalizer)."""
    from arr.runs.launch import request_stop

    folder = _run_folder(workfolder, run_id)
    try:
        new = request_stop(folder)
    except ArrError as exc:
        _fail(str(exc))
    if new:
        typer.echo(
            f"Stopp angefordert für {run_id}. Er wirkt, solange ein arr-run-Prozess den Run "
            "ausführt: der stoppt ihn innerhalb weniger Sekunden, danach läuft der Finalizer."
        )
    else:
        typer.echo(f"Für {run_id} liegt schon eine Stopp-Anforderung vor.")


@runs_app.command("scan")
def runs_scan(run_id: RunIdArgument, workfolder: WorkfolderOption = None) -> None:
    """Run-Ordner auf Secrets prüfen (Werte der Auth-Profile und bekannte Muster)."""
    from arr.models import ResolvedRun
    from arr.resolve import load_yaml_model
    from arr.runs.launch import load_profiles
    from arr.runs.secret_scan import format_report, profile_secrets, record_scan, scan_run_dir

    folder = _run_folder(workfolder, run_id)
    secrets: dict[str, str] = {}
    try:
        names = {a.auth for a in load_yaml_model(folder / "run.yaml", ResolvedRun).agents}
        profiles = [p for p in load_profiles().profiles if p.name in names]
        secrets = profile_secrets(profiles)
    except ArrError as exc:
        typer.echo(f"Hinweis: Nur Muster werden geprüft, keine Profil-Werte ({exc.message})")
    report = scan_run_dir(folder, secrets)
    record_scan(folder, report)
    typer.echo(format_report(report))
    if not report.clean:
        raise typer.Exit(code=1)


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
