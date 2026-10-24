# S4 – Room mit mehreren Prozessen

**Frage:** Hält der Room-Server (MCP stdio + SQLite WAL) mehrere Agenten-Prozesse gleichzeitig aus, ohne Lock-Fehler, verlorene Nachrichten oder falsche Cursor? Wird die Mitgliedschaft erzwungen? Kann der Orchestrator von außen in dieselbe DB schreiben? (Roadmap S4, Auftrag W0-04)

Ergebnis: [docs/spikes/S4_room.md](../../docs/spikes/S4_room.md)

## Dateien

| Datei | Zweck |
|---|---|
| `server.py` | Room-Server mit `mcp` 2.x (`MCPServer`), Tools `list_channels`, `post_message`, `read_messages`. Identität aus `AGENT_ID`, DB aus `ROOM_DB`, `ROOM_TXN=immediate\|deferred` |
| `db.py` | Schema (`channel`, `member`, `msg`, `cursor`) und `connect()` (WAL, `busy_timeout` 30 s) |
| `client.py` | Ein simulierter Agent: MCP-Client ohne LLM, startet seinen eigenen `server.py` über stdio |
| `load.py` | Lasttest: N Agent-Prozesse + externer Schreiber (`orchestrator`), danach Prüfung und Bericht als JSON |
| `results/` | Ausgaben der Messläufe |

## Ausführung

Voraussetzung: `uv sync --group spikes`. Kein Login, kein Modell.

```bash
uv run --group spikes python spikes/s04_room/load.py                         # 5 Agenten, 1000 Nachrichten, 200 vom Orchestrator
uv run --group spikes python spikes/s04_room/load.py --txn deferred          # Gegenprobe: Lese-Transaktion wird zur Schreib-Transaktion
uv run --group spikes python spikes/s04_room/load.py --agents 1 --messages 200 --orchestrator 0   # Basislinie ohne Konkurrenz
```

Die DB entsteht unter `spikes/s04_room/tmp/run-…/` und wird am Ende gelöscht (`--keep` behält sie).
