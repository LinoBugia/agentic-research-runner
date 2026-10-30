# W3-03: Live-Probe der Freigabe-Inbox (Wegwerf-Code)

**Frage:** Warten Claude und Codex in einem Service-Run auf die Entscheidung aus der Inbox, kommt eine Entscheidung nach mindestens 5 Minuten noch an, läuft die Frist von 20 Minuten sauber ab, und arbeiten die Agenten bei Freigabe und Ablehnung weiter (Auftrag W3-03 §3.4, §3.8, Kriterium A10)?

**Dateien**

- `live_approvals.py [--workfolder ~/arr-m1] [--wait 305] [--port N]` – startet den Service im eigenen Prozess (ohne Browser) und zwei Läufe gleichzeitig mit je 1 × `claude-haiku-5-5` und 1 × `gpt-6-luna`. Lauf 1: Installation, dann `curl`; die erste Anfrage je Agent wird nach mindestens 5 Minuten über `decide_approval` erlaubt, die nächste abgelehnt. Lauf 2: ein `curl`, niemand entscheidet, die Frist von 20 Minuten läuft ab. Am Ende: Zählung, Zeilen aus `approvals.jsonl`, Secret-Scans. Der Token-Link wird nicht ausgegeben.

**Ausführung** (Voraussetzungen wie bei W3-01: Claude-Login des Macs, Profil `chatgpt-w2-02a`, Test-Workfolder `~/arr-m1`):

```bash
uv run python spikes/w3_03/live_approvals.py
```

Budget: je Lauf `max_budget_usd` 0,30 $, zusammen ≤ 30 min Wandzeit, ein Wachhund stoppt bei 20 Modell-Anfragen je Backend. Es erscheinen echte macOS-Mitteilungen. Ergebnisse stehen im Abschlussbericht W3-03 (lokale Doku).
