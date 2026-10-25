# Projekt: $projekt

## Ziel und Stand
<Worum geht es in diesem Projekt? Aktueller Stand in wenigen Sätzen.>

## Notation und Konventionen
<Bezeichnungen, Definitionen, Dateinamen und Ordner, die Agenten kennen sollten.>

<!-- arr:user-agent -->
## Arbeiten mit arr
- Für Research-Groups (mehrere Agenten, die unabhängig arbeiten und sich austauschen) gibt es
  Group-Skills unter `.claude/skills/`, z. B. `exploration` für breite Ideensuche oder
  `proof-review` für Beweisprüfung. Details im Skill `arr`.
- Runs startest du mit `start_run`. Der User muss jeden Run in der arr-GUI freigeben.
- Ergebnisse: `read_run_summary`, dann `read_results`. Übernahme nach `results/` nur nach
  Rücksprache.
<!-- /arr:user-agent -->
