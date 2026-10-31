# W3-02: Live-Probe GUI-Runs (Wegwerf-Code)

**Frage:** Läuft ein kleiner gemischter Run (1 × `claude-haiku-5-5`, 1 × `gpt-6-luna`) über `RealApi` bis `done`, erreicht eine Nachricht des Users einen Agenten mit sichtbarem Zustellstatus und kommt die Antwort, lässt sich der Run danach verschlanken, und bleibt der Secret-Scan sauber (Auftrag W3-02 §3.7, Kriterium A8)?

**Datei**

- `live_probe.py [--workfolder ~/arr-m1] [--port N]` – startet den Service im eigenen Prozess (ohne Browser), vergleicht die Vorschau aus `resolve_run` mit der `run.yaml`, stellt nach der ersten Kanal-Nachricht eine Frage an `antworter`, verfolgt Zustellung und Antwort und verschlankt den Run am Ende (zuerst `dry_run`). Der Token-Link des Service wird nicht ausgegeben.

**Ausführung** (Voraussetzungen wie bei W3-01):

```bash
uv run python spikes/w3_02/live_probe.py
```

Budget: ein Lauf, `max_budget_usd` 0,30 $, Wandzeit 10 min, 3 Turns je Agent, 2 Weckungen. Ergebnisse stehen im Abschlussbericht W3-02 (lokale Doku).
