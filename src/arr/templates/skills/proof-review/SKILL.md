---
name: proof-review
description: Lässt einen Beweis oder Beweisentwurf von mehreren unabhängigen Judges (Claude und ChatGPT) prüfen; der Autor arbeitet die Einwände nach, bis die Judges zustimmen oder die Revisionen aufgebraucht sind. Nutzen, wenn ein Beweis vorliegt oder entstehen soll und kritisch geprüft werden muss.
---

# Proof Review (Research-Group)

Ein Autor liefert den Beweis (aus dem Projekt oder neu geschrieben). Judges prüfen ihn unabhängig voneinander, diskutieren danach in `#judges` und fällen ein Urteil (`accept`, `minor`, `major`, `reject`). Ist es nicht `accept`, überarbeitet der Autor den Beweis, und die Prüfung beginnt von vorn.

## Wann sinnvoll

- Ein Beweis oder eine Beweisskizze liegt als Datei im Projekt, oder die Aussage ist klar genug, dass ein Autor ihn schreiben kann.
- Du willst eine unabhängige, kritische Prüfung statt Selbstbestätigung.

Nicht sinnvoll für offene Fragen ohne klare Aussage (nimm `exploration` oder `roundtable`) oder für eine reine Rechnung (nimm `sympy-check`).

## task.md

- die Aussage, genau formuliert, mit allen Voraussetzungen und der Notation
- den Pfad zum Beweis im Projekt, falls es einen gibt (auch als Parameter `artifact`)
- worauf die Judges besonders achten sollen (z. B. Randfälle, eine bestimmte Abschätzung)
- was als Ergebnis zählt: vollständiger Beweis, oder auch ein Gegenbeispiel

## Parameter

| Name | Standard | Bedeutung |
|---|---|---|
| judges | 2 | Anzahl der Judges, abwechselnd Claude und ChatGPT |
| max_revisions | 1 | Überarbeitungen des Autors |
| discussion_rounds | 1 | Runden in `#judges` nach der Prüfung (0: keine) |
| verdict | majority | `majority` (bei Gleichstand das strengere) oder `unanimous` |
| judge_memory | fresh | `fresh`: neue Sitzung je Prüfrunde; `continue`: Sitzung bleibt |
| artifact | – | Pfad zum Beweis im Projekt; leer: der Autor schreibt ihn zuerst |

Agenten: `author` (Claude Opus), `judge-1 …` (Claude Sonnet und ChatGPT im Wechsel; Modell und Profil für ChatGPT kommen aus `arr.yaml`). Limits: 1 h, 10 Turns je Agent, 5 $.

## Ergebnis

`read_run_summary` nennt Urteil, Runden und offene Einwände. Die Gutachten stehen in `read_results(kind="review")`, die Fassungen im Run-Ordner unter `output/artifacts/v<n>/`. Übernimm einen Beweis ins Projekt nur nach Rücksprache mit dem User.
