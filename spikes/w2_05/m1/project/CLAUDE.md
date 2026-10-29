# Projekt: Collatz-Mini

## Ziel und Stand
Kleine Beispielrechnungen zur Collatz-Abbildung T(n) = n/2 für gerades n und T(n) = 3n + 1 für
ungerades n. Stand: Notizen und ein Hilfsskript, noch kein Beweis.

## Notation und Konventionen
- s(n) ist die Zahl der Schritte von n bis zur ersten 1, mit s(1) = 0.
- Python-Hilfsfunktionen liegen in `code/collatz.py` (Funktion `steps(n)`).
- Beweise und Pläne als Markdown-Dateien im Projektordner, kurz und mit Verweis auf die Prüfung.

<!-- arr:user-agent -->
## Arbeiten mit arr
- Group-Skills für Runs liegen unter `.claude/skills/`. Details im Skill `arr`.
- Runs startest du mit `start_run`. Der User muss jeden Run in der arr-GUI freigeben.
- Ergebnisse: `read_run_summary`, dann `read_results`. Übernahme nach `results/` nur nach
  Rücksprache.
<!-- /arr:user-agent -->
