# W3-07 Runner robust: Spike

Wegwerf-Code, wird nie von `arr` importiert. Kein echtes Modell, kein Login: die echte Claude-CLI
(gebündelt mit `claude-agent-sdk`) und der echte Codex-App-Server (`openai-codex` 0.163.0) laufen
über arrs eigene Backends gegen ein Fake-Modell auf 127.0.0.1, in einem Test-`HOME` unter
`/private/tmp` (Labor der Login-Schutz-Tests, `tests/login_guard_kit.py`). `~/.claude` und
`~/.codex` bleiben unberührt.

**Frage:**

1. Wie lange darf ein Werkzeug des Room-Servers (MCP) blockieren, bevor die Claude-CLI bzw. der
   Codex-App-Server abbricht?
2. Läuft ein Hintergrundprozess, den der Agent in seiner Shell startet, über das Ende seines Turns
   hinaus weiter, und auch über einen neuen Turn derselben Session?

**Aufbau:**

- `block_server.py`: MCP-Server mit dem Namen `arr-room` und einem Werkzeug `block(seconds)`. Er
  ersetzt im Spike den Room-Server (gleiche Einbindung, gleiche Freigabe wie die echten
  Room-Werkzeuge).
- `probe.py`: Fake-Modelle, die je Turn ein Skript spielen (Marker `SPIKE-TURN-<k>` im Prompt).
  Bei Codex ruft das Fake-Modell das MCP-Werkzeug so auf wie das echte Modell im Code-Modus: über
  `exec` mit `await tools.mcp__arr_room__block(...)`.

**Ausführung** (je Lauf ein eigenes Labor, parallel möglich):

```bash
uv run python spikes/w3_07/probe.py block claude 65        # Werkzeug blockiert 65 s
uv run python spikes/w3_07/probe.py block codex 65
uv run python spikes/w3_07/probe.py block codex 130 200    # mit tool_timeout_sec = 200
uv run python spikes/w3_07/probe.py background claude      # Hintergrundprozesse
uv run python spikes/w3_07/probe.py background codex
```

Ausgabe: ein JSON-Objekt je Lauf. Die Ergebnisse stehen im Abschlussbericht W3-07.
