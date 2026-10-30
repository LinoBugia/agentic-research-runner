# W3-05 Claude-Login und Profile

**Frage:** Kann arr `claude setup-token` ohne Terminal ausführen (E18), und funktionieren Anlegen, Testen und Löschen von Profilen mit echten Logins?

## Ausführung

- `claude_login_probe.py`: Tor. Der User führt es einmal von Hand aus, weil die Anmeldung einen echten Browser braucht:
  ```bash
  uv run python spikes/w3_05/claude_login_probe.py
  ```
  Das Skript zeigt nur, ob ein Token erkannt wurde, und dessen Länge. Es legt das Token unter `arr-login-probe` im Schlüsselbund ab, macht eine Anfrage mit `claude-haiku-5-5` und löscht den Eintrag wieder.
