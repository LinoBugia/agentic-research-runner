---
name: roundtable
description: Mehrere Agenten (Claude und ChatGPT) diskutieren eine Frage in Runden in einem gemeinsamen Kanal; am Ende fasst ein Agent Ergebnisse, Vermutungen und Dissens zusammen. Nutzen für eine offene Frage, zu der verschiedene Sichtweisen helfen.
---

# Roundtable (Research-Group)

Diskutanten bearbeiten die Frage gemeinsam im Kanal `#runde`, in höchstens `rounds` Runden. Danach schreibt ein Zusammenfasser, was gesichert ist, was vermutet wird und wo die Diskutanten uneinig sind.

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

Die Höchstwerte sind so gewählt, dass 10 Turns je Agent reichen (ein Turn je Runde, der Zusammenfasser einen mehr). Mehr Turns gehen über `overrides={"limits": {"turns_per_agent": …}}`.

Agenten: `diskutant-1 …` (Claude Sonnet und ChatGPT im Wechsel; Modell und Profil für ChatGPT kommen aus `arr.yaml`), `summarizer` (Claude Haiku). Limits: 45 min, 10 Turns je Agent, 3 $.

## Ergebnis

`read_run_summary` enthält die Zusammenfassung. Den Verlauf liest du mit `read_groupchat(channel="runde")`, die Fazits der Diskutanten mit `read_results`.
