# agentic research runner (`arr`)

Lokale Bibliothek für Mathe-Projekte plus Agent-Harness mit Web-GUI. Aus einem Claude-Code-Chat heraus startet `arr` **Research-Groups**: mehrere Claude- und ChatGPT-Agenten mit eigenem Arbeitsordner, Rollen und Gruppenchats. Jeder Run wird vollständig protokolliert.

Status: Planung abgeschlossen (Projektauftrag v1.0), Umsetzung beginnt mit Welle 0. Reihenfolge und Stand: [Codeplan](umsetzung/Codeplan.md), Ablauf: [Arbeitsweise](umsetzung/Arbeitsweise.md).

## Dokumentation

| Dokument | Inhalt |
|---|---|
| [00 Projektauftrag](docs/00-projektauftrag.md) | Ziele, Umfang, Entscheidungen, Abnahme |
| [01 Architektur](docs/01-architektur.md) | Komponenten, Backend-Adapter, Orchestrator |
| [02 Workfolder & Runs](docs/02-workfolder-und-runs.md) | Ordnerstruktur, `run.yaml`, Run-Protokoll |
| [03 Skills & Research-Groups](docs/03-skills-und-research-groups.md) | Skill-Format, `group.yaml`, Topologien |
| [04 Service, GUI & Schnittstellen](docs/04-service-gui-und-schnittstellen.md) | MCP-Tools, REST, GUI-Seiten, CLI |
| [05 Roadmap](docs/05-roadmap.md) | Phasen, Arbeitspakete, Spikes |
| [06 Gruppenchats](docs/06-gruppenchats.md) | Kommunikationsmodell der Agenten |
| [07 Berechtigungen & Freigaben](docs/07-berechtigungen-und-freigaben.md) | Sandbox, Regeln, Freigabe-Inbox, Aufsichts-Agent |
| [API-Recherche](docs/mathe-agent-harness.md) | Agent-SDKs (Claude, OpenAI, Codex) |

## Entwicklung

Voraussetzungen: [uv](https://docs.astral.sh/uv/) und Python 3.12.

```bash
uv sync                                   # Umgebung anlegen
uv run arr info                           # CLI ausprobieren
uv run pytest                             # Tests
uv run ruff check && uv run ruff format --check   # Linter und Format
```

Regeln für die Entwicklung (Sprache, Tests, Secrets, Commits): [CLAUDE.md](CLAUDE.md). Spikes: [spikes/README.md](spikes/README.md).
