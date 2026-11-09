# Codeplan – Ablauf

> Einzige Datei aus `umsetzung/` im Git (Workflow 01 v1.2). Codeplan, Aufträge, Berichte und Reviews liegen nur lokal im Haupt-Ordner.

Wellen und Aufträge mit ihren Abhängigkeiten. Pfeile zeigen, was worauf aufbaut; Aufträge untereinander ohne Pfeil laufen parallel.

```text
Welle 0   W0-01 Gerüst ─┬─► W0-02 Spike Claude ──┐
                        ├─► W0-03 Spike Codex ───┤
                        └─► W0-04 Spike Infra ───┤
          W0-05 UX-Konzept (startet sofort) ─────┴──► M0 + UX-Abnahme 1
                                │
Welle 1   W1-01 Kern-Schnittstellen ─┬─► W1-02 Workfolder & RunPreparer
                                     ├─► W1-03 Room (Gruppenchats)
                                     ├─► W1-04 Events, FakeBackend, Usage
                                     └─► W1-05 PolicyEngine
          W1-06 API-Vertrag + Gerüst ──► W1-07 Run-Detail-MVP ──► W1-08 übrige Screens ──► UX-Abnahme 2
                                │
Welle 2   W2-01 ClaudeBackend ─┐
          W2-02 CodexBackend  ─┼──► W2-05 CLI + Live-E2E ──► M1
          W2-03 RunManager + Chat-Taktung ─► W2-04 Topologien + Finalizer ─┘
                                │
Welle 3   W3-00 Codex-E19 Variante 2 ──► (vor W3-04 und W3-05)
          W3-01 Service ─┬─► W3-02 GUI: Runs live, Neuer Run ─────┐
                         ├─► W3-03 Freigabe-Inbox end-to-end ─────┤
                         ├─► W3-04 Workfolder, Projekte (+W3-00) ─┼──► UX-Abnahme 3
                         └─► W3-05 Auth-Profile (+W3-00) ─────────┘
                                │
Welle 4   W4-00 Pflicht-Überträge ─┬─► W4-01 MCP-Bridge + Run-Start-Freigabe ─► W4-04 Aufsichts-Agent ─┐
          W4-02 Skills-Seite ──────┤                                                                ├─► W4-06 E2E ─► M2
                                   └─► W4-03 exploration, review, moderiert ─► W4-05 Start-Skills ──┘
                                │
Welle 5   W5-00 Spike eigener macOS-Benutzer ─► W5-03 Robustheit + Härtung ─┐
          W5-01 Usage & Speicher ───────────────────────────────────────────┼─► W5-04 Usability-Test + Feinschliff ─► M3 = v1
          W5-02 Onboarding ─────────────────────────────────────────────────┘
```

Welle 4: W4-01 und W4-03 starten, wenn W4-00 und W4-02 gemerged sind; W4-05 braucht W4-02 und W4-03.
