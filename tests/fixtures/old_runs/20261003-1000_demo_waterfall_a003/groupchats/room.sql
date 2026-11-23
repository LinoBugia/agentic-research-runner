BEGIN TRANSACTION;
CREATE TABLE agent_report(agent TEXT PRIMARY KEY, status TEXT, ts REAL);
CREATE TABLE agent_state(agent TEXT PRIMARY KEY, status TEXT, wakeups INTEGER,
    note TEXT, ts REAL);
INSERT INTO "agent_state" VALUES('p','done',0,'result',1.79544162887520508675e+09);
INSERT INTO "agent_state" VALUES('q','idle',0,'',1.79544162890039178392e+09);
INSERT INTO "agent_state" VALUES('r','done',0,'result',1.79544162889236087465e+09);
CREATE TABLE agent_wait(agent TEXT, kind TEXT, since REAL, until REAL,
    file TEXT, result_from TEXT, PRIMARY KEY (agent, kind));
CREATE TABLE assignment(agent TEXT, turn INTEGER, stage TEXT, prompt TEXT,
    context TEXT, ts REAL);
INSERT INTO "assignment" VALUES('p',1,'skizze','Stufe „skizze“, Versuch 1. Den Auftrag mit Aufgabe und Ergebnissen der Vorstufen findest du mit `get_assignment()`. Gib dein Ergebnis mit `submit_result` ab.','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "stage": "skizze", "attempt": 1, "produces": null, "accepted": []}',1.79544162885130909046e+09);
INSERT INTO "assignment" VALUES('q',1,'skizze','Bewerte das Ergebnis #1 der Stufe „skizze“ mit `submit_rating` (Score und Kommentar, Scores je Kriterium der Rubrik). Ergebnis, Aufgabe und Rubrik findest du mit `get_assignment()`.','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "stage": "skizze", "attempt": 1, "result_id": 1, "result_ids": [1], "result": {"id": 1, "stage": "skizze", "title": "S", "path": null, "body": "Inhalt"}, "accepted": [], "scale": [1.0, 10.0], "criteria": ["korrektheit", "klarheit"]}',1.79544162885940110641e+09);
INSERT INTO "assignment" VALUES('p',2,'skizze','Stufe „skizze“, Versuch 2. Den Auftrag mit Aufgabe und Ergebnissen der Vorstufen findest du mit `get_assignment()`. Das ist eine Nacharbeit: Die Ratings deines letzten Versuchs stehen dort unter `rework`. Gib dein Ergebnis mit `submit_result` ab.','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "stage": "skizze", "attempt": 2, "produces": null, "accepted": [], "rework": {"result": 1, "score": 3.0, "pass_score": 6.0, "ratings": [{"rater": "q", "score": 3.0, "criteria": {}, "comment": "passt"}]}}',1.79544162886884812735e+09);
INSERT INTO "assignment" VALUES('q',2,'skizze','Bewerte das Ergebnis #2 der Stufe „skizze“ mit `submit_rating` (Score und Kommentar, Scores je Kriterium der Rubrik). Ergebnis, Aufgabe und Rubrik findest du mit `get_assignment()`.','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "stage": "skizze", "attempt": 2, "result_id": 2, "result_ids": [2], "result": {"id": 2, "stage": "skizze", "title": "S", "path": null, "body": "Inhalt"}, "accepted": [], "scale": [1.0, 10.0], "criteria": ["korrektheit", "klarheit"]}',1.79544162887622178459e+09);
INSERT INTO "assignment" VALUES('r',1,'beweis','Stufe „beweis“, Versuch 1. Den Auftrag mit Aufgabe und Ergebnissen der Vorstufen findest du mit `get_assignment()`. Gib dein Ergebnis mit `submit_result` ab.','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "stage": "beweis", "attempt": 1, "produces": null, "accepted": [{"id": 2, "stage": "skizze", "title": "S", "path": null, "body": "Inhalt"}]}',1.79544162888576313008e+09);
INSERT INTO "assignment" VALUES('q',3,'beweis','Bewerte das Ergebnis #3 der Stufe „beweis“ mit `submit_rating` (Score und Kommentar, Scores je Kriterium der Rubrik). Ergebnis, Aufgabe und Rubrik findest du mit `get_assignment()`.','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "stage": "beweis", "attempt": 1, "result_id": 3, "result_ids": [3], "result": {"id": 3, "stage": "beweis", "title": "B", "path": null, "body": "Inhalt"}, "accepted": [{"id": 2, "stage": "skizze", "title": "S", "path": null, "body": "Inhalt"}], "scale": [1.0, 10.0], "criteria": ["korrektheit", "klarheit"]}',1.79544162889339808941e+09);
CREATE TABLE channel(name TEXT PRIMARY KEY, created REAL, config TEXT,
    state TEXT NOT NULL DEFAULT 'open');
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
CREATE TABLE msg(id INTEGER PRIMARY KEY, ts REAL, sender TEXT, channel TEXT,
    kind TEXT, body TEXT, to_agents TEXT, reply_to INTEGER, refs TEXT, pinned INTEGER,
    open INTEGER, via TEXT, delivery TEXT);
CREATE TABLE note_turn(agent TEXT PRIMARY KEY, since REAL);
CREATE TABLE poll(proposal_id INTEGER PRIMARY KEY, channel TEXT, rule TEXT,
    called_by TEXT, ts REAL);
CREATE TABLE rating(id INTEGER PRIMARY KEY, ts REAL, rater TEXT,
    result_id INTEGER, score REAL, criteria TEXT, comment TEXT);
INSERT INTO "rating" VALUES(1,1.7954416288630559606e+09,'q',1,3.0,'{}','passt');
INSERT INTO "rating" VALUES(2,1.79544162887963114627e+09,'q',2,7.0,'{}','passt');
INSERT INTO "rating" VALUES(3,1.79544162889735829935e+09,'q',3,8.0,'{"korrektheit": 8, "klarheit": 7}','gut');
CREATE TABLE result(id INTEGER PRIMARY KEY, ts REAL, agent TEXT, stage TEXT,
    kind TEXT, title TEXT, body TEXT, path TEXT, attempt INTEGER);
INSERT INTO "result" VALUES(1,1795441628.8551,'p','skizze','note','S','Inhalt',NULL,1);
INSERT INTO "result" VALUES(2,1.79544162887226299574e+09,'p','skizze','note','S','Inhalt',NULL,2);
INSERT INTO "result" VALUES(3,1795441628.88932,'r','beweis','note','B','Inhalt',NULL,1);
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
INSERT INTO "wait_guard" VALUES('p',0.0,NULL,NULL,NULL,NULL,0);
INSERT INTO "wait_guard" VALUES('q',0.0,NULL,NULL,NULL,NULL,0);
INSERT INTO "wait_guard" VALUES('r',0.0,NULL,NULL,NULL,NULL,0);
CREATE INDEX msg_channel ON msg(channel, id);
CREATE INDEX msg_reply ON msg(reply_to);
CREATE UNIQUE INDEX rating_once ON rating(rater, result_id);
CREATE UNIQUE INDEX submission_version ON submission(name, version);
CREATE UNIQUE INDEX delegation_number ON delegation(stage, attempt, number);
COMMIT;
