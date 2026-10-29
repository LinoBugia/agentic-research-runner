# W3-01: Live-Probe des Service (Wegwerf-Code)

**Frage:** Läuft ein kleiner gemischter Run (1 × `claude-haiku-5-5`, 1 × `gpt-6-luna`) im Service genauso zu Ende wie mit `arr run`, kommen die Live-Ereignisse in ≤ 1 s an, lässt sich ein zweiter Run über die API mitten im Lauf stoppen, und bleibt der Secret-Scan sauber (Auftrag W3-01 §3.8, Kriterium A10)?

**Dateien**

- `live_service.py [--workfolder ~/arr-m1] [--port N]` – startet den Service im eigenen Prozess auf dem Test-Workfolder (ohne Browser), startet über `RealApi.start_run` (keine Klicks) einen `roundtable`-Run im Modus `event` mit den Rollen `frager` (Claude) und `antworter` (Codex) aus der M1-Wecken-Probe (W2-05), folgt ihm über `subscribe` bis zum Ende und misst die Latenz der ersten fünf Transkript- und Kanal-Ereignisse. Ein zweiter, gleicher Run wird beim ersten Transkript-Ereignis mit `run_action(stop)` gestoppt. Am Ende: Zählung (Anfragen, Turns, Kosten, Dauer, Latenzen) und Secret-Scan beider Runs. Der Token-Link des Service wird nicht ausgegeben.

**Ausführung** (Voraussetzungen wie bei W2-05: Claude-Login des Macs, angemeldetes Codex-Profil `chatgpt-w2-02a`, Basis-venv `~/.venvs/mathe`, Test-Workfolder `~/arr-m1` mit dem Projekt `collatz-mini` und den Rollen `frager.md`, `antworter.md`):

```bash
uv run python spikes/w3_01/live_service.py
```

Budget je Lauf: `max_budget_usd` 0,30 $, Wandzeit 10 min, 3 Turns je Agent, 2 Weckungen. Ergebnisse stehen im Abschlussbericht W3-01 (lokale Doku).
