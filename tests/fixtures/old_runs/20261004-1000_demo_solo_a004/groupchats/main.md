# #main

- Taktung: event
- Mitglieder: solver
- Nachrichten: 2

### #1 · 2026-11-23 14:47:08 · orchestrator · note

*angeheftet*

> Steckbrief #main
> Mitglieder:
> - solver
> Taktung: ereignisgesteuert: Wer mit `@name` angesprochen, gefragt oder mit `reply_to` beantwortet wird, wird sofort geweckt. Runden gibt es nur, wenn niemand mehr schreibt.
> Länge einer Nachricht: höchstens 3000 Zeichen.
> Gesprächsregeln:
> 1. Eine Nachricht, ein Gedanke. Setze `kind` korrekt.
> 2. Behauptungen mit Begründung oder Beleg: Datei, Rechnung, Anhang.
> 3. Beantworte Fragen an dich zuerst.
> 4. Wer jemanden fragt, schreibt `@name` (oder `to`); `@alle` fragt alle, `@user` den User (nur er antwortet darauf). Auf eine Antwort wartest du mit `wait_for_messages`, auf den User nie mit `wait_until`: Antwortet er nicht, arbeite mit einer begründeten Annahme weiter und nenne sie in deinem Ergebnis.
> 5. Widerspruch als `objection` mit `reply_to`, sachlich und konkret.
> 6. Nicht wiederholen. Zustimmung nur, wenn sie etwas hinzufügt.
> 7. Lange Inhalte (Beweise, Code, Tabellen) als Anhang, im Text nur die Kernaussage.
> 8. Wer seinen Teil erledigt hat, gibt sein Ergebnis ab (`submit_result` oder Nachricht `result`) oder meldet `done`. Fertig heißt: Du wartest nicht mehr, bleibst aber ansprechbar; jede Nachricht an dich weckt dich. Wartest du auf das Ergebnis eines anderen, warte mit `wait_until(minutes, until_result_from="<name>")`.
> Ende: wenn alle fertig sind (Ergebnis abgegeben oder `done`) und keine Nachricht an einen von ihnen ungelesen ist, nach den Leerlauf-Runden oder wenn es lange ruhig ist.

### #2 · 2026-11-23 14:47:08 · solver · note

> Ich fange an.
