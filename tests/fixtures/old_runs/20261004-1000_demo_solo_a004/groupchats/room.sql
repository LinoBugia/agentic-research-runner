BEGIN TRANSACTION;
CREATE TABLE agent_report(agent TEXT PRIMARY KEY, status TEXT, ts REAL);
CREATE TABLE agent_state(agent TEXT PRIMARY KEY, status TEXT, wakeups INTEGER,
    note TEXT, ts REAL);
INSERT INTO "agent_state" VALUES('solver','done',0,'result',1.79544162893734799979e+09);
CREATE TABLE agent_wait(agent TEXT, kind TEXT, since REAL, until REAL,
    file TEXT, result_from TEXT, PRIMARY KEY (agent, kind));
CREATE TABLE assignment(agent TEXT, turn INTEGER, stage TEXT, prompt TEXT,
    context TEXT, ts REAL);
INSERT INTO "assignment" VALUES('solver',1,NULL,'## Aufgabe

Zeige, dass Lemma A für alle n ≡ 1 mod 4 gilt.

Gib dein Ergebnis mit `submit_result` ab, sobald es steht.','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt."}',1.79544162892922187779e+09);
CREATE TABLE channel(name TEXT PRIMARY KEY, created REAL, config TEXT,
    state TEXT NOT NULL DEFAULT 'open');
INSERT INTO "channel" VALUES('main',1.79544162892027685529e+09,'{"name":"main","purpose":null,"members":["solver"],"mode":"event","order":"parallel","moderator":null,"open_from":null,"anonymous":false,"wake_on":["mention","question"],"debounce_s":20.0,"max_wakeups_per_agent":12,"quiet_after_s":300.0,"idle_after_s":30.0,"max_message_chars":3000,"summary_every":15}','open');
CREATE TABLE cursor(agent TEXT, channel TEXT, last_id INTEGER,
    PRIMARY KEY (agent, channel));
CREATE TABLE delegation(id INTEGER PRIMARY KEY, ts REAL, stage TEXT,
    attempt INTEGER, number INTEGER, lead TEXT, agent TEXT, format TEXT, task TEXT,
    status TEXT NOT NULL DEFAULT 'queued', named INTEGER NOT NULL DEFAULT 0,
    submission_id INTEGER, started REAL, ended REAL);
CREATE TABLE delegation_closed(stage TEXT, attempt INTEGER, ts REAL,
    PRIMARY KEY (stage, attempt));
CREATE TABLE floor(id INTEGER PRIMARY KEY, ts REAL, channel TEXT,
    moderator TEXT, agent TEXT, prompt TEXT, msg_id INTEGER, handled INTEGER DEFAULT 0);
CREATE TABLE member(channel TEXT, agent TEXT, alias TEXT,
    PRIMARY KEY (channel, agent));
INSERT INTO "member" VALUES('main','solver',NULL);
CREATE TABLE msg(id INTEGER PRIMARY KEY, ts REAL, sender TEXT, channel TEXT,
    kind TEXT, body TEXT, to_agents TEXT, reply_to INTEGER, refs TEXT, pinned INTEGER,
    open INTEGER, via TEXT, delivery TEXT);
INSERT INTO "msg" VALUES(1,1.79544162892029586231e+09,'orchestrator','main','note','Steckbrief #main
Mitglieder:
- solver
Taktung: ereignisgesteuert: Wer mit `@name` angesprochen, gefragt oder mit `reply_to` beantwortet wird, wird sofort geweckt. Runden gibt es nur, wenn niemand mehr schreibt.
Länge einer Nachricht: höchstens 3000 Zeichen.
Gesprächsregeln:
1. Eine Nachricht, ein Gedanke. Setze `kind` korrekt.
2. Behauptungen mit Begründung oder Beleg: Datei, Rechnung, Anhang.
3. Beantworte Fragen an dich zuerst.
4. Wer jemanden fragt, schreibt `@name` (oder `to`); `@alle` fragt alle, `@user` den User (nur er antwortet darauf). Auf eine Antwort wartest du mit `wait_for_messages`, auf den User nie mit `wait_until`: Antwortet er nicht, arbeite mit einer begründeten Annahme weiter und nenne sie in deinem Ergebnis.
5. Widerspruch als `objection` mit `reply_to`, sachlich und konkret.
6. Nicht wiederholen. Zustimmung nur, wenn sie etwas hinzufügt.
7. Lange Inhalte (Beweise, Code, Tabellen) als Anhang, im Text nur die Kernaussage.
8. Wer seinen Teil erledigt hat, gibt sein Ergebnis ab (`submit_result` oder Nachricht `result`) oder meldet `done`. Fertig heißt: Du wartest nicht mehr, bleibst aber ansprechbar; jede Nachricht an dich weckt dich. Wartest du auf das Ergebnis eines anderen, warte mit `wait_until(minutes, until_result_from="<name>")`.
Ende: wenn alle fertig sind (Ergebnis abgegeben oder `done`) und keine Nachricht an einen von ihnen ungelesen ist, nach den Leerlauf-Runden oder wenn es lange ruhig ist.','[]',NULL,'[]',1,0,NULL,NULL);
INSERT INTO "msg" VALUES(2,1.79544162893256409319e+09,'solver','main','note','Ich fange an.','[]',NULL,'[]',0,0,NULL,NULL);
CREATE TABLE note_turn(agent TEXT PRIMARY KEY, since REAL);
CREATE TABLE poll(proposal_id INTEGER PRIMARY KEY, channel TEXT, rule TEXT,
    called_by TEXT, ts REAL);
CREATE TABLE rating(id INTEGER PRIMARY KEY, ts REAL, rater TEXT,
    result_id INTEGER, score REAL, criteria TEXT, comment TEXT);
CREATE TABLE result(id INTEGER PRIMARY KEY, ts REAL, agent TEXT, stage TEXT,
    kind TEXT, title TEXT, body TEXT, path TEXT, attempt INTEGER);
INSERT INTO "result" VALUES(1,1.79544162893364411814e+09,'solver',NULL,'proof','Beweis','Lemma A gilt.',NULL,1);
CREATE TABLE sealed_result(result_id INTEGER PRIMARY KEY, stage TEXT);
CREATE TABLE sealed_stage(stage TEXT PRIMARY KEY, channel TEXT, ts REAL);
CREATE TABLE sealed_submission(submission_id INTEGER PRIMARY KEY, stage TEXT);
CREATE TABLE submission(id INTEGER PRIMARY KEY, ts REAL, agent TEXT,
    shown_as TEXT, stage TEXT, attempt INTEGER, action TEXT, format TEXT, name TEXT,
    version INTEGER, status TEXT, summary TEXT, data TEXT, path TEXT, final_path TEXT);
CREATE TABLE vote(proposal_id INTEGER, agent TEXT, choice TEXT, reason TEXT,
    ts REAL, PRIMARY KEY (proposal_id, agent));
CREATE TABLE wait_guard(agent TEXT PRIMARY KEY, waited REAL NOT NULL DEFAULT 0,
    seen INTEGER, last_end TEXT, yield_to TEXT, end_turn TEXT,
    until_streak INTEGER NOT NULL DEFAULT 0);
INSERT INTO "wait_guard" VALUES('solver',0.0,NULL,NULL,NULL,NULL,0);
CREATE INDEX msg_channel ON msg(channel, id);
CREATE INDEX msg_reply ON msg(reply_to);
CREATE UNIQUE INDEX rating_once ON rating(rater, result_id);
CREATE UNIQUE INDEX submission_version ON submission(name, version);
CREATE UNIQUE INDEX delegation_number ON delegation(stage, attempt, number);
COMMIT;
