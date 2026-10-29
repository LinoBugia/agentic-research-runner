# arr – Entwicklung dieses Repos

`arr` (agentic research runner) ist eine lokale Bibliothek für Mathe-Projekte plus Agent-Harness mit Web-GUI. Aus einem Claude-Code-Chat heraus startet arr Research-Groups aus Claude- und ChatGPT-Agenten mit eigenen Arbeitsordnern, Rollen und Gruppenchats.

- **Doku liegt nur lokal** im Haupt-Ordner des Repos, nicht im Git (`docs/`, `umsetzung/`; im Git nur [umsetzung/Codeplan_Ablauf.md](umsetzung/Codeplan_Ablauf.md)). In Worktrees und Klonen fehlt sie: Pfade daher immer im Haupt-Ordner lesen.
- Planung und Entwurf: [docs/](docs/) – Einstieg [00 Projektauftrag](docs/00-projektauftrag.md), [01 Architektur](docs/01-architektur.md)
- Umsetzung: [umsetzung/](umsetzung/) – [Codeplan](umsetzung/Codeplan.md), [Arbeitsweise](umsetzung/Arbeitsweise.md)

## Befehle

```bash
uv sync                      # Umgebung inkl. dev-Gruppe
uv run pytest                # Tests (ohne live)
uv run ruff check            # Linter
uv run ruff format           # Formatieren (CI prüft mit --check)
uv run arr                   # CLI, z. B. uv run arr info
```

Vor jedem Commit: `uv run pytest && uv run ruff check && uv run ruff format --check`

## Regeln

- **Sprache:** Code, Bezeichner, Docstrings und Kommentare auf **Englisch**. Doku, Berichte und **alle Texte der Oberfläche** (CLI-Hilfe, GUI, Meldungen an den User) auf **Deutsch**.
- **Arbeitsweise:** Code-Agenten folgen [umsetzung/Arbeitsweise.md](umsetzung/Arbeitsweise.md) – genau ein Auftrag, eigener Branch, nie in `main` mergen, nie pushen.
- **Commits** enden mit der Zeile:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- **Secrets:** nie in Dateien, Logs oder Commits. Zugangsdaten nur über den macOS-Schlüsselbund (`keyring`) bzw. vorhandene Logins.
- **Tests:** FakeBackend statt echter Modelle. Echte Modelle nur mit Marker `@pytest.mark.live` (standardmäßig abgewählt, Start mit `uv run pytest -m live`), nur-macOS-Tests mit `@pytest.mark.macos`. Neue Module bekommen Tests.
- **Pfade:** keine absoluten Nutzerpfade im Code. App-Daten über `arr.settings.app_dir()` (`~/.arr`, in Tests per `ARR_HOME` bzw. Fixture `arr_home`), Projektdaten über den Workfolder.

## Struktur

- `src/arr/` – Paket nach [01 §2](docs/01-architektur.md#2-paketstruktur-vorschlag); Einstieg CLI: `arr.cli:app` (Typer), neue Befehle als Unterbefehle anhängen. Logging: `arr.log.setup_logging()`.
- `src/arr/templates/` – Vorlagen für Workfolder, Projekte, Vollmachten, Start-Skills (Paketdaten, gelesen über `importlib.resources`)
- `tests/` – pytest, `asyncio_mode = "auto"`
- `spikes/` – Wegwerf-Code, siehe [spikes/README.md](spikes/README.md); wird nie von `arr` importiert
- Laufzeit-Abhängigkeiten bewusst klein halten. Seit Welle 2 sind `claude-agent-sdk`, `openai-codex` (exakt gepinnt, Änderung nur über den Lead) und `keyring` Laufzeit-Abhängigkeiten, neben `mcp` und NiceGUI. Neue Abhängigkeiten nur nach Rücksprache mit dem Lead (gemeinsame `uv.lock`).
