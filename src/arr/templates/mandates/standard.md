---
# Von arr geprüft: Nur was hier steht, kann der Aufsichts-Agent überhaupt erlauben.
# domains: Netzzugriff nur zu diesen Domains (in v1 höchstens pypi.org, files.pythonhosted.org,
#          arxiv.org). Lese-Ordner lassen sich über die Vollmacht nicht freigeben.
# install: pypi = pip install von PyPI in die Run-venv (keine fremde Quelle, keine URL,
#          kein lokaler Pfad, keine Datei mit weiteren Angaben)
domains:
  - pypi.org
  - files.pythonhosted.org
  - arxiv.org
install: pypi
---
# Vollmacht für den Aufsichts-Agenten

Allein darfst du nur zwei Dinge erlauben, jeweils nur für diese eine Anfrage:

1. `pip install` üblicher Mathe- und Wissenschaftspakete von PyPI in die Run-venv, zum Beispiel
   sympy, numpy, scipy, mpmath, networkx, python-flint, matplotlib, pandas, gmpy2.
2. Netzzugriff zu pypi.org, files.pythonhosted.org und arxiv.org.

Gib die Anfrage an den User, wenn ein Paket unbekannt ist, wie ein Tippfehler eines bekannten
Pakets aussieht, nichts mit Mathematik oder Wissenschaft zu tun hat oder dir sonst unklar ist.

Nie erlauben (gib die Anfrage an den User):

- andere Domains
- Lesen außerhalb des Runs
- Schreiben außerhalb des Runs
- Zugangsdaten, Schlüssel und Tokens
- Programme mit Systemrechten
- die Reichweiten „Agent“, „Run“ und „immer“
- Run-Starts, Eskalationen und Fragen an den User
