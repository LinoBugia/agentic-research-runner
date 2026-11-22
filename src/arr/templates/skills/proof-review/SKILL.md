---
name: proof-review
description: Lässt einen Beweis oder Beweisentwurf von mehreren unabhängigen Judges (Claude und ChatGPT) prüfen; der Autor arbeitet die Einwände nach, bis die Judges zustimmen oder die Revisionen aufgebraucht sind. Nutzen, wenn ein Beweis vorliegt oder entstehen soll und kritisch geprüft werden muss.
---

# Proof Review (Research-Group)

Ein Autor liefert den Beweis (aus dem Projekt oder neu geschrieben). Judges prüfen ihn unabhängig voneinander, besprechen ihre Gutachten danach in `#judges` und geben ein Urteil ab (`accept`, `minor`, `major`, `reject`). Ist das Urteil nicht `accept`, überarbeitet der Autor den Beweis, und die Prüfung beginnt von vorn.

## Ablauf in Phasen

| Phase | Wer | Abgabe | Weiter |
|---|---|---|---|
| fassung | author | `artifact` „beweis“ | entfällt, wenn `artifact` den Beweis aus dem Projekt nennt |
| pruefung | judge-1 … (isoliert, neue Sitzung je Durchlauf bei `judge_memory: reset`) | `review` | weiter zu diskussion |
| diskussion | judge-1 … in `#judges` (anonym) | `review` | Urteil nach `verdict`: accept → Ende; minor, major → revision; reject → Ende |
| revision | author, höchstens `max_revisions` Mal | `artifact` „beweis“ (neue Fassung) | zurück zu pruefung; danach Ende |

Mit `discussion_rounds: 0` gibt es keine Diskussion: Dann zählt das Urteil der Prüfung (Phase `pruefung-allein`). Mit einem Judge und Diskussion bekommt der Judge in `diskussion` einen Turn allein.

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

| Name | Standard | Bereich | Bedeutung |
|---|---|---|---|
| judges | 2 | 1–5 | Anzahl der Judges, abwechselnd Claude und ChatGPT |
| max_revisions | 1 | 0–2 | Überarbeitungen des Autors |
| discussion_rounds | 1 | 0–1 | Runden in `#judges` nach der Prüfung (0: keine) |
| verdict | majority | | `majority` (bei Gleichstand das strengere) oder `unanimous` |
| judge_memory | reset | | `reset`: neue Sitzung und frischer Arbeitsordner je Prüfung; `keep`: beides bleibt |
| artifact | – | | Pfad zum Beweis im Projekt; leer: der Autor schreibt ihn zuerst |

Die Höchstwerte sind so gewählt, dass 12 Turns je Agent auch im ungünstigsten Fall reichen (ein Judge braucht höchstens 3 Durchläufe × 4 Turns: Prüfung und Diskussion, je mit Nachfrage). Brauchen die Agenten mehr Turns, geht das über `overrides={"limits": {"turns_per_agent": 16}}`.

Agenten: `author` (Claude Opus), `judge-1 …` (Claude Sonnet und ChatGPT im Wechsel; Modell und Profil für ChatGPT kommen aus `arr.yaml`). Limits: 1 h, 12 Turns je Agent, 5 $.

## Ergebnis

`read_run_summary(run_id)` nennt die Abgaben mit ihren Kurzfassungen. Im Run-Ordner liegen die Fassungen des Beweises unter `output/beweis/v<n>/` (ab der zweiten mit `diff.patch`), die Gutachten unter `output/submissions/` (je Abgabe ein Ordner `NNN-<phase>-<judge>/`). Übernimm einen Beweis ins Projekt nur nach Rücksprache mit dem User.
