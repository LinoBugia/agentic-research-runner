---
name: exploration
description: Erkundet mehrere Ansätze zu einer offenen Frage parallel (Claude und ChatGPT) und bewertet sie in einer Rangliste. Nutzen, wenn unklar ist, welcher Weg trägt, ob eine Vermutung stimmt oder wie man ein Problem überhaupt angeht.
---

# Exploration (Research-Group)

Ein Ideator schlägt verschiedene Ansätze vor, oder du gibst sie vor. Explorer erkunden je einen Ansatz in wenigen Arbeitsrunden, Ranker bewerten die Ergebnisse (Kriterien `tragfaehigkeit`, `belege`, `aufwand`, Skala 1–10), und ein Zusammenfasser schreibt, was gesichert ist und was als Nächstes lohnt.

## Wann sinnvoll

- Die Frage ist offen: Man weiß noch nicht, welcher Ansatz trägt oder ob die Aussage stimmt.
- Du willst mehrere Wege vergleichen, statt dich früh auf einen festzulegen.

Nicht sinnvoll, wenn der Weg schon klar ist (nimm `proof`) oder ein Beweis nur geprüft werden soll (nimm `proof-review`).

## task.md

- die Frage, genau formuliert, mit Notation und bekanntem Stand
- was ein gutes Ergebnis wäre (Beweisweg, Gegenbeispiel, Datenlage, Literatur)
- Ansätze, die schon versucht wurden oder ausgeschlossen sind
- eigene Ansätze gibst du als Parameter `ideas` mit

## Parameter

| Name | Standard | Bedeutung |
|---|---|---|
| ideas | leer | vorgegebene Ansätze (Liste von Texten); leer: der Ideator schlägt welche vor |
| n_ideas | 3 | Anzahl vorgeschlagener Ansätze |
| turns_per_idea | 2 | Arbeitsrunden je Ansatz |
| exchange | none | `none`: getrennt; `digest`: Auszüge der anderen; `channel`: Austausch in `#werkstatt` |
| n_explorers | 2 | Anzahl der Explorer, abwechselnd Claude und ChatGPT; mehr Ansätze als Explorer werden reihum verteilt |
| n_rankers | 1 | Anzahl der Ranker (0: keine Rangliste) |

Agenten: `ideator` und `ranker-1 …` (Claude Sonnet), `explorer-1 …` (Claude Sonnet und ChatGPT im Wechsel; Modell und Profil für ChatGPT kommen aus `arr.yaml`), `summarizer` (Claude Haiku). Limits: 1 h, 12 Turns je Agent, 5 $.

## Ergebnis

`read_run_summary` enthält die Rangliste und die Zusammenfassung. Die Stände der Explorer stehen in `read_results(kind="exploration")`, die Ansätze in `read_results(kind="idea")`. Was davon ins Projekt geht, entscheidet der User.
