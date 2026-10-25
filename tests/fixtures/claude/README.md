# Fixtures: Claude Agent SDK (roh)

Rohe JSON-Nachrichten zwischen `claude-agent-sdk` 0.2.174 und der gebündelten Claude-Code-CLI 2.1.310, mitgeschnitten im Spike S1 ([docs/spikes/S1_claude.md](../../../docs/spikes/S1_claude.md)).

## Format

Eine Zeile pro Nachricht:

```json
{"dir": "in", "t": 1.234, "msg": { ... }}
```

- `dir`: `in` = CLI → SDK (stdout der CLI), `out` = SDK → CLI (stdin der CLI)
- `t`: Sekunden seit der ersten Nachricht der Datei
- `msg`: die Nachricht unverändert, bis auf die Bereinigung

Nachrichten mit `dir: "in"` und einem `type` außer `control_request`/`control_response` lassen sich mit `claude_agent_sdk._internal.message_parser.parse_message()` in SDK-Objekte (`AssistantMessage`, `UserMessage`, `ResultMessage`, `SystemMessage`, `RateLimitEvent`) umwandeln. Für Replay-Tests kann ein eigener `Transport` die `in`-Zeilen ausliefern und die `out`-Zeilen als Erwartung prüfen.

## Bereinigung

Home-Pfad → `~` (auch im kodierten Projektschlüssel `-Users-…`), Account-Block der Initialize-Antwort → Platzhalter `user@example.com`, keine Tokens. Prüfung: `export_fixtures.py` meldet für jede Datei `ok`.

## Dateien

| Datei | Inhalt |
|---|---|
| `solo_three_turns_thinking_tools.jsonl` | Haiku, 3 Queries in einer Session: ThinkingBlocks (`display="summarized"`), Write + Bash, Tool-Ergebnisse mit `tool_use_result`, PostToolUse-Hook-Callbacks, Usage mit Cache-Read ab Query 2 |
| `sandbox_violation_and_network_request.jsonl` | Haiku, Sandbox an: Bash-Schreiben außerhalb blockiert, `allowWrite`-Ordner erlaubt, Netzwerk als `can_use_tool`-Anfrage `SandboxNetworkAccess` (abgelehnt), Write außerhalb → `can_use_tool` (abgelehnt), `permission_denials` im Result |
| `can_use_tool_deny_after_wait.jsonl` | Sonnet, `Bash(python3 *)` per Regel erlaubt, `curl` → `can_use_tool`, Antwort nach 120 s: Ablehnung mit Begründung |
| `pretooluse_deny_with_reason.jsonl` | Sonnet, PreToolUse-Hook verbietet Write außerhalb trotz Allow-Regel, Begründung im Tool-Ergebnis, Agent weicht in den Arbeitsordner aus |
| `posttooluse_hint_and_mcp_tool.jsonl` | Sonnet, PostToolUse mit `additionalContext` (Chat-Hinweis), Agent ruft daraufhin das MCP-Tool `mcp__room__read_messages` auf |
| `project_mcp_json_and_skill.jsonl` | Haiku, Projektordner mit `.mcp.json` (stdio-Server) und Symlink-Skill, `setting_sources=["project"]`: MCP-Tool `ping` und Skill `arr-probe` genutzt |
