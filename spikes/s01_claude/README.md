# Spike S1 – Claude Agent SDK

Auftrag: [W0-02](../../umsetzung/welle-0/W0-02_Spike_Claude_Auftrag.md) · Ergebnis: [docs/spikes/S1_claude.md](../../docs/spikes/S1_claude.md)

## Frage

Wie wird der Claude-Adapter (W2-01) gebaut? Der Spike beantwortet S1, S1b und S7 aus der [Roadmap](../../docs/05-roadmap.md#phase-0--gerüst-und-spikes):
Thinking, Caching, Tool-Ergebnisse, System-Prompt, Skills, Auth/Umgebung, Sandbox, Freigabe-Callback, Hook-Verbote, Hinweise mitten im Turn und der User-Agent-Weg über `.mcp.json`.

## Voraussetzungen

- `uv sync --group spikes` (bringt `claude-agent-sdk` 0.2.174 mit gebündelter Claude-Code-CLI 2.1.310 und `mcp` 2.3.0)
- Vorhandener Claude-Code-Login auf diesem Mac (Schlüsselbund). Kein Token, kein API-Key nötig.
- Jedes Skript entfernt beim Start Auth- und Session-Variablen aus seiner Umgebung (`common.clean_env()`, gibt nur Namen aus) und schaltet die claude.ai-Connectoren ab (`ENABLE_CLAUDEAI_MCP_SERVERS=false`).
- Grenzen: Haiku 5.5 für Mechanik, Sonnet 5.5 für Verhalten (Fragen 3, 7, 8, 9), `max_budget_usd` ≤ 1 pro Skript, insgesamt ≤ 60 Turns. `common.check_turn_budget()` bricht vor einem Lauf ab, wenn das Limit überschritten würde.

## Ausführung

Alle Befehle vom Repo-Root aus. Arbeitsordner, Mitschnitte und Ausgaben landen in `spikes/s01_claude/tmp/` (von Git ignoriert).

| Frage | Befehl | Modell | Turns |
|---|---|---|---:|
| – (SDK-Typen, `help()`) | `uv run --group spikes python spikes/s01_claude/inspect_sdk.py` | – | 0 |
| 1, 2, 5a | `uv run --group spikes python spikes/s01_claude/q01_solo.py` | Haiku | 5 |
| 3 | `uv run --group spikes python spikes/s01_claude/q03_system_prompt.py` | Sonnet | 11 |
| 4 | `uv run --group spikes python spikes/s01_claude/q04_skills.py` | Haiku | 8 |
| 4 (Isolation) | `uv run --group spikes python spikes/s01_claude/q04b_isolation.py` (legt die Skill-Quelle selbst an) | – | 0 |
| 5 (Connector-Schalter, W0-02.1) | `uv run --group spikes python spikes/s01_claude/q01b_connectors.py` (je Variante 20 s) | – | 0 |
| 6 | `uv run --group spikes python spikes/s01_claude/q06_sandbox.py` | Haiku | 4 |
| 6/7 (acceptEdits) | `uv run --group spikes python spikes/s01_claude/q06b_accept_edits.py` | Haiku | 3 |
| 7 | `uv run --group spikes python spikes/s01_claude/q07_can_use_tool.py` (wartet 2 min) | Sonnet | 3 |
| 8 | `uv run --group spikes python spikes/s01_claude/q08_pretooluse_deny.py` | Sonnet | 3 |
| 9 | `uv run --group spikes python spikes/s01_claude/q09_additional_context.py [--convention]` | Sonnet | 4 / 5 |
| 10 | `uv run --group spikes python spikes/s01_claude/q10_user_agent.py` (nach q04) | Haiku | 5 |

Frage 5b (Profil-Token) und 11 (Anleitung für die Claude-App) sind aus Doku und SDK-Quelltext beantwortet, siehe Ergebnis.

Hilfsskripte:

- `uv run --group spikes python spikes/s01_claude/usage_report.py` – Usage- und Kostentabelle aus `tmp/usage.jsonl`
- `uv run --group spikes python spikes/s01_claude/show_raw.py <lauf> [--full <type>]` – Roh-Events eines Laufs lesbar ausgeben
- `uv run --group spikes python spikes/s01_claude/export_fixtures.py` – ausgewählte Mitschnitte bereinigt nach `tests/fixtures/claude/` exportieren und prüfen

## Dateien

- `common.py` – Umgebung bereinigen, Roh-Mitschnitt (`Recorder` hängt sich an den Subprocess-Transport des SDK), Usage-Ledger, Turn-Limit, kompakte Ausgabe
- `dummy_mcp.py` – stdio-MCP-Server mit `read_messages` und `ping` (`mcp.server.MCPServer`, der Nachfolger von FastMCP)
- `role_explorer.md` – Rollentext für Frage 3
- Ein Durchlauf `q01_solo.py` lief zuerst ohne `ENABLE_CLAUDEAI_MCP_SERVERS=false`. Er ist im Ledger als `q01_solo_connectors` geführt (siehe Ergebnis, Frage 5).
- `q08_pretooluse_deny.py`: Die Pfadprüfung im Hook wurde nach dem Lauf präzisiert (Bash-Befehle mit dem eigenen Arbeitsordner wurden fälschlich mit erfasst). Im Lauf kam nur das Write-Tool vor, das Verhalten dafür ist unverändert.
