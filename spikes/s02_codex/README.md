# S2 / S2b / S3 – Codex-SDK (`openai-codex`)

Auftrag: [W0-03](../../umsetzung/welle-0/W0-03_Spike_Codex_Auftrag.md) · Ergebnis: [docs/spikes/S2_codex.md](../../docs/spikes/S2_codex.md)

## Frage

Wie wird der Codex-Adapter für das ChatGPT-Abo gebaut (W2-02)? Konkret die Fragen 1–13 aus dem Auftrag: Signaturen, Modelle, Events, Reasoning, Usage, Instructions, MCP, `AGENTS.md`/Skills, Fortsetzen, Freigaben, Sandbox, Hinweise mitten im Turn (`steer`) und getrennte Login-Ordner (`CODEX_HOME`).

## Voraussetzungen

- `uv sync --group spikes` (bringt `openai-codex` 0.163.0 **mit eigener Codex-Binary** `openai-codex-cli-bin` mit, nichts zu installieren)
- vorhandener Codex-Login in `~/.codex` (wird automatisch übernommen, nie gelesen)
- Modell `gpt-6-luna`, Effort `low` (in `common.py`, per `S2_MODEL` / `S2_EFFORT` änderbar)
- Grenze: höchstens 40 Turns insgesamt. Jedes Skript prüft das vorab über `tmp/usage.jsonl` (`check_budget`).
- Agenten arbeiten nur unter `tmp/work/`, alle Ausgaben landen in `tmp/out/` (beides von Git ignoriert)

`common.make_codex()` hält die globale `~/.codex/config.toml` des Users aus den Läufen heraus: Modell, Effort, Service-Tier, `notify`, Plugins, Apps, Computer-/Browser-Use, Multi-Agent, Web-Suche. Außerdem zeichnet es jede JSON-RPC-Zeile roh auf (`RawTap`).

## Ausführung

Alle Befehle aus dem Repo-Wurzelordner:

```bash
# ohne Turns
uv run --group spikes python spikes/s02_codex/q01_signatures.py        # Q1  Signaturen + help()
uv run --group spikes python spikes/s02_codex/q02_models.py            # Q2  Modellliste, Account-Art

# mit Turns (Anzahl in Klammern)
uv run --group spikes python spikes/s02_codex/q03_turn_events.py basic       # Q3, Q5      (1)
uv run --group spikes python spikes/s02_codex/q03_turn_events.py reasoning   # Q3, Q4      (1)
uv run --group spikes python spikes/s02_codex/q04_reasoning.py gpt-6.1-sol medium hard   # Q4 (1)
S2_SUMMARY_CFG=1 uv run --group spikes python spikes/s02_codex/q04_reasoning.py gpt-6.1-sol medium hard  # (1)
uv run --group spikes python spikes/s02_codex/q06_instructions.py      # Q6          (2)
uv run --group spikes python spikes/s02_codex/q07_mcp.py               # Q7          (3)
uv run --group spikes python spikes/s02_codex/q08_context.py           # Q8          (2)
uv run --group spikes python spikes/s02_codex/q09_resume.py start      # Q9          (1)
uv run --group spikes python spikes/s02_codex/q09_resume.py resume     #   neuer Prozess (1)
uv run --group spikes python spikes/s02_codex/q10_approvals.py         # Q10         (2)
uv run --group spikes python spikes/s02_codex/q11_sandbox.py           # Q11         (2)
uv run --group spikes python spikes/s02_codex/q12_steer.py             # Q12         (2)

# Q13 – getrennte Login-Ordner
uv run --group spikes python spikes/s02_codex/q13_codex_home.py probe     # 2 leere CODEX_HOMEs parallel (0)
uv run --group spikes python spikes/s02_codex/q13_codex_home.py login     # Device-Code in Home A, gibt nur URL + Code aus
uv run --group spikes python spikes/s02_codex/q13_codex_home.py parallel  # A und ~/.codex parallel (2)
uv run --group spikes python spikes/s02_codex/q13_codex_home.py cleanup   # Logout in A, Temp-Homes löschen

# Auswertung
uv run --group spikes python spikes/s02_codex/make_fixtures.py   # tests/fixtures/codex/*.jsonl neu + Leck-Scan
uv run --group spikes python spikes/s02_codex/make_fixtures.py --scan-only   # nur Leck-Scan der committeten Fixtures
uv run --group spikes python spikes/s02_codex/usage_report.py    # Usage-Tabelle aller Turns
uv run --group spikes python spikes/s02_codex/show_raw.py tmp/out/q10_user_raw.jsonl --only item/commandExecution/requestApproval
```

## Dateien

| Datei | Zweck |
|---|---|
| `common.py` | Client-Fabrik mit Isolation, `RawTap`, Usage-Log, Turn-Budget, Bereinigung, Fixtures |
| `room_mcp.py` | Mini-stdio-MCP-Server (mcp 2.x `MCPServer`) als Ersatz für `arr-room`: `whoami`, `post` |
| `q01` … `q13` | je eine Frage, siehe oben |
| `make_fixtures.py` | Fixtures aus den Roh-Logs erzeugen und auf Home-Pfad, Tokens, E-Mail, Repo-URL, Skill-Sync-Bucket (`~/.agents/skills/synced/<org-id>_<account-id>/`) und nicht redigierte Account-Felder prüfen |
| `usage_report.py`, `show_raw.py` | Auswertung und Anzeige der Roh-Logs (bereinigt) |

Fixture-Format: eine Zeile pro JSON-RPC-Nachricht `{"t": s, "dir": "in"|"out", "msg": {...}}`. `out` ist SDK → App-Server (Requests, Antworten auf Freigaben), `in` ist App-Server → SDK.
