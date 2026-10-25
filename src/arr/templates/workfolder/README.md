# arr-Workfolder

Dieser Ordner gehört zu **arr** (agentic research runner). Alles, was arr braucht, liegt
hier. Du kannst mehrere Workfolder haben, z. B. „privat“ und „Uni“.

```
arr.yaml        Einstellungen dieses Workfolders (Standards, Limits, Aufräumregeln) – keine Secrets
README.md       diese Datei
projects/       deine Projekte, je ein Ordner mit CLAUDE.md
skills/         Skills im SKILL.md-Format; Ordner mit group.yaml sind Research-Groups
runs/           ein Ordner pro Run: Konfiguration, Protokolle, Ergebnisse
.arr/           Index und Logs (lassen sich aus den Run-Ordnern neu aufbauen)
```

## Gut zu wissen

- **Der Run-Ordner ist die Wahrheit.** Er enthält Aufgabe, Konfiguration, Transkripte,
  Gruppenchats und Ergebnisse. Der Index in `.arr/` wird daraus neu aufgebaut.
- **Arbeitskopien:** Jeder Agent arbeitet in einer eigenen Kopie des Projekts
  (`runs/<run-id>/agents/<agent>/work/`). Dein Projekt bleibt unverändert. Ergebnisse
  übernimmst du bewusst selbst.
- **Keine Secrets in diesem Ordner.** Zugangsdaten liegen im macOS-Schlüsselbund.
- Liegt der Workfolder auf demselben Volume wie die Projekte (APFS), sind die Kopien fast
  gratis. Auf einem anderen Volume kopiert arr echt und weist darauf hin.

## Nächste Schritte

1. Lege ein Projekt unter `projects/` an (Ordner mit einer `CLAUDE.md`).
2. Lege eigene Skills unter `skills/` ab.
3. Starte die Oberfläche mit `arr gui`.
