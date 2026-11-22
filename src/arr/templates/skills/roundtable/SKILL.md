---
name: roundtable
description: Mehrere Agenten (Claude und ChatGPT) diskutieren eine Frage in Runden in einem gemeinsamen Kanal; am Ende fasst ein Agent Ergebnisse, Vermutungen und Dissens zusammen. Nutzen für eine offene Frage, zu der verschiedene Sichtweisen helfen.
---

# Roundtable (Research-Group)

Diskutanten bearbeiten die Frage gemeinsam im Kanal `#runde`, in höchstens `rounds` Runden, und geben am Ende je ein Fazit ab. Danach schreibt ein Zusammenfasser, was gesichert ist, was vermutet wird und wo die Diskutanten uneinig sind.

## Ablauf in Phasen

| Phase | Wer | Abgabe | Weiter |
|---|---|---|---|
| diskussion | diskutant-1 … in `#runde`, `rounds` Runden | `text` (Fazit) je Diskutant | → zusammenfassung |
| zusammenfassung | summarizer, liest den Verlauf von `#runde` | `text` | Ende; entfällt mit `summarizer: keine` |

Der Zusammenfasser ist Mitglied von `#runde`, diskutiert aber nicht mit.

## Wann sinnvoll

- Eine offene Frage, bei der Austausch hilft: Einschätzungen, Gegenbeispiele, Literatur, Ideen für einen Ansatz.
- Ein schnelles Meinungsbild, bevor du einen aufwändigeren Run startest.

Nicht sinnvoll, wenn Ansätze unabhängig und gründlich erkundet werden sollen (nimm `exploration`) oder ein Beweis geprüft werden soll (nimm `proof-review`).

## task.md

- die Frage, genau formuliert, mit Notation und bekanntem Stand
- worüber diskutiert werden soll und was nicht
- was am Ende vorliegen soll (Einschätzung, Liste von Ansätzen, Gegenbeispiel)

## Parameter

| Name | Standard | Bereich | Bedeutung |
|---|---|---|---|
| rounds | 3 | 1–6 | Anzahl der Diskussionsrunden |
| n_diskutanten | 2 | 2–5 | Anzahl der Diskutanten, abwechselnd Claude und ChatGPT |
| summarizer | summarizer | | `summarizer`: Zusammenfassung am Ende; `keine`: ohne |

Die Höchstwerte sind so gewählt, dass 10 Turns je Agent reichen (ein Turn je Runde und eine Nachfrage). Mehr Turns gehen über `overrides={"limits": {"turns_per_agent": …}}`.

Agenten: `diskutant-1 …` (Claude Sonnet und ChatGPT im Wechsel; Modell und Profil für ChatGPT kommen aus `arr.yaml`), `summarizer` (Claude Haiku). Limits: 45 min, 10 Turns je Agent, 3 $.

## Ergebnis

`read_run_summary(run_id)` nennt die Abgaben mit ihren Kurzfassungen. Den Verlauf liest du mit `read_groupchat(run_id, channel="runde")`. Im Run-Ordner liegen die Fazits und die Zusammenfassung unter `output/submissions/` (je Abgabe ein Ordner `NNN-<phase>-<agent>/`).
