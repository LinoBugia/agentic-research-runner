# Spikes

Kleine Wegwerf-Experimente aus Phase 0 ([05 Roadmap](../docs/05-roadmap.md#phase-0--gerüst-und-spikes)). Sie beantworten konkrete Fragen zu SDKs und Infrastruktur, bevor der Harness-Kern gebaut wird.

## Konventionen

- **Ordner:** jeder Spike liegt in `spikes/sNN_name/`, z. B. `spikes/s01_claude_solo/`.
- **README pro Spike:** `spikes/sNN_name/README.md` mit
  - **Frage:** was der Spike klären soll (aus der Roadmap),
  - **Ausführung:** genauer Befehl, Voraussetzungen (Login, Modell, Limits).
- **Ausführung** immer mit der Gruppe `spikes`:
  ```bash
  uv run --group spikes python spikes/s01_claude_solo/main.py
  ```
- **Ergebnisse** als kurzes Markdown nach `docs/spikes/<nr>.md` (z. B. `docs/spikes/s01.md`).
- **Fixtures:** rohe Events, die spätere Tests brauchen, nach `tests/fixtures/<backend>/` (z. B. `tests/fixtures/claude/`). Vorher auf Secrets prüfen.
- **Ausgaben** während der Arbeit nach `spikes/out/` (von Git ignoriert).
- **Wegwerf-Code:** Spike-Code wird **nicht** von `arr` importiert. Was sich bewährt, wird in einem eigenen Auftrag sauber in `src/arr/` neu geschrieben. Trotzdem muss er `uv run ruff check` und `uv run ruff format --check` bestehen, weil die CI das ganze Repo prüft.
- **Keine Secrets** in Spike-Code, Ausgaben oder Fixtures. Logins kommen aus den vorhandenen Anmeldungen bzw. dem Schlüsselbund.
