# S6 – Service-Stack

**Frage:** Tragen NiceGUI (auf FastAPI), REST, eine Live-Ansicht einer wachsenden JSONL-Datei und eine Tabelle in **einem** Prozess? Bleibt die Oberfläche flüssig, wie viel CPU/Speicher, WebSocket oder SSE? Funktioniert `native=True`? Lässt sich die Streamable-HTTP-App von MCP einhängen? (Roadmap S6, Auftrag W0-04)

Ergebnis: [docs/spikes/S6_service.md](../../docs/spikes/S6_service.md)

## Dateien

| Datei | Zweck |
|---|---|
| `app.py` | Der Service: NiceGUI-Seiten `/live` und `/table`, REST `/api/runs` und `/api/stats`, SSE `/api/runs/demo/events`, MCP unter `/mcp/` (Tool `list_runs`) |
| `writer.py` | Schreibt Transkript-ähnliche Events in eine JSONL-Datei (Standard 50/s über 60 s, Flush nach jedem Event) |
| `measure.py` | Messung: startet Service und Writer, öffnet `/live` in Headless-Chrome (Playwright), liest parallel SSE, misst CPU/RSS, Klick-, REST- und MCP-Latenzen, macht die Screenshots in `docs/spikes/img/` |
| `native_check.py` | Startet `app.py --native` (pywebview) und prüft, ob ein eigenes Fenster entsteht und die Live-Ansicht darin verbunden ist |
| `results/` | Messwerte als JSON |

## Ausführung

Voraussetzung: `uv sync --group spikes`, Google Chrome installiert (für die Messung). `playwright`, `psutil` und `pywebview` sind nicht in der Gruppe `spikes` und werden per `--with` nur für den Lauf geladen (`pyproject.toml` bleibt unverändert).

```bash
uv run --group spikes python spikes/s06_service/app.py --jsonl /pfad/zu/events.jsonl   # Service von Hand, http://127.0.0.1:8765/live
uv run --group spikes --with playwright --with psutil python spikes/s06_service/measure.py                      # 50 Events/s, 60 s
uv run --group spikes --with playwright --with psutil python spikes/s06_service/measure.py --rate 200 --seconds 30
uv run --group spikes --with pywebview --with psutil python spikes/s06_service/native_check.py                  # öffnet kurz ein Fenster
```

Temporäre Dateien liegen unter `spikes/s06_service/tmp/` und werden am Ende gelöscht (`--keep` behält sie).
