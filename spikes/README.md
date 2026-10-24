# Spikes

Kleine Wegwerf-Experimente aus Welle 0 ([Codeplan](../umsetzung/Codeplan.md), Fragen in [05 Roadmap](../docs/05-roadmap.md#phase-0--gerüst-und-spikes)). Sie beantworten konkrete Fragen zu SDKs und Infrastruktur, bevor der Harness-Kern gebaut wird. **Verbindlich sind die Namen und Pfade aus dem jeweiligen Spike-Auftrag.**

## Konventionen

- **Ordner:** jeder Spike liegt in `spikes/sNN_name/`, z. B. `spikes/s01_claude/`.
- **README pro Spike:** `spikes/sNN_name/README.md` mit
  - **Frage:** was der Spike klären soll (aus der Roadmap),
  - **Ausführung:** genauer Befehl, Voraussetzungen (Login, Modell, Limits).
- **Ausführung** immer mit der Gruppe `spikes`:
  ```bash
  uv run --group spikes python spikes/s01_claude/main.py
  ```
- **Ergebnisse** als Markdown nach `docs/spikes/`, Dateiname laut Spike-Auftrag (z. B. `docs/spikes/S1_claude.md`).
- **Fixtures:** rohe Events, die spätere Tests brauchen, nach `tests/fixtures/<backend>/` (z. B. `tests/fixtures/claude/`). Vorher auf Secrets prüfen.
- **Temporäre Dateien und Ausgaben** während der Arbeit nach `spikes/<spike>/tmp/` oder `spikes/out/` (beides von Git ignoriert, `.gitignore` muss dafür nicht geändert werden).
- **Wegwerf-Code:** Spike-Code wird **nicht** von `arr` importiert. Was sich bewährt, wird in einem eigenen Auftrag sauber in `src/arr/` neu geschrieben. Trotzdem muss er `uv run ruff check` und `uv run ruff format --check` bestehen, weil die CI das ganze Repo prüft.
- **Keine Secrets** in Spike-Code, Ausgaben oder Fixtures. Logins kommen aus den vorhandenen Anmeldungen bzw. dem Schlüsselbund.
