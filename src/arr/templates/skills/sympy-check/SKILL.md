---
name: sympy-check
description: Rechnungen, Identitäten und Umformungen symbolisch mit SymPy nachprüfen, in der Python-Umgebung des Runs. Nutzen, bevor eine Rechnung in einem Beweis, Gutachten oder Ergebnis als richtig gilt.
---

# SymPy-Check

Prüfe jede Rechnung, auf die sich ein Schritt stützt, statt sie nur im Kopf nachzuvollziehen. Das Skript oder die Ausgabe ist dann ein Beleg.

## Schnell: das Skript dieses Skills

Das Skript liegt im Ordner dieses Skills unter `scripts/check.py`. Starte es mit dem `python` der Run-Umgebung:

```bash
python <skill-ordner>/scripts/check.py gleich "(n+1)^2" "n^2 + 2*n + 1"
python <skill-ordner>/scripts/check.py gleich "sqrt(x^2)" "x" --symbol x:positive
python <skill-ordner>/scripts/check.py vereinfache "sin(x)^2 + cos(x)^2"
```

- `gleich A B`: prüft, ob `A - B` zu 0 vereinfacht. Ausgabe `gleich`, `nicht gleich` (mit Differenz) oder `nicht entschieden`.
- `vereinfache A`: gibt die vereinfachte Form aus.
- **Exit-Codes:** 0 gleich bzw. vereinfacht, 1 nicht gleich, 2 nicht entschieden, 3 Fehler (abgelehnt, nicht lesbar, Grenze erreicht, SymPy fehlt). Nur 1 heißt „nicht gleich“.
- Ein Ausdruck darf mit `-` beginnen (`gleich -x "(-1)*x"`); sicher geht es mit `--` vor den Ausdrücken.
- **Grenzen:** höchstens 10 s CPU-Zeit; Potenzen von Zahlen nur bis etwa 4200 Stellen, `factorial`, `binomial` und Verwandte nur bis zum Argument 1000, Zahlen höchstens 100 Ziffern. Größeres rechnest du symbolisch mit einer Variablen.
- Dezimalzahlen sind Näherungen: `2^0.5` ist nicht gleich `sqrt(2)`. Schreib Brüche (`1/2`) statt `0.5`.
- `--symbol name:annahme,annahme` setzt Annahmen, z. B. `n:integer,positive`. Ohne Annahmen gelten Symbole als komplex; manche Identitäten gelten dann nicht. Eine unbekannte Annahme (z. B. `positiv`) ist ein Fehler.
- `^` ist die Potenz.
- **Konstanten:** `I` ist die imaginäre Einheit, `E` die eulersche Zahl, `pi` und `oo` (unendlich). Meinst du `I` oder `E` als Variable, lege sie mit `--symbol I` an. Andere Namen wie `N`, `S`, `beta`, `gamma` sind Variablen, solange du sie nicht aufrufst.
- **Nur Rechenausdrücke:** Erlaubt sind Zahlen, Namen, `+ - * / ^ ** !`, Kommas, runde Klammern und bekannte Funktionen (`sin`, `exp`, `log`, `sqrt`, `binomial`, `factorial`, `Sum` …). Alles andere lehnt das Skript ab, bevor SymPy die Eingabe sieht; ein Ausdruck wird nie als Python-Code ausgeführt.

## Gründlicher: ein eigenes Skript

Für Summen, Grenzwerte, Reihen, Ungleichungen in kleinen Fällen oder Gegenbeispielsuche schreibst du ein kurzes Skript in deinen Arbeitsordner, z. B. `pruefung/lemma2.py`, und legst es deinem Ergebnis bei.

```python
import sympy as sp
n, k = sp.symbols("n k", integer=True, positive=True)
print(sp.simplify(sp.summation(k, (k, 1, n)) - n * (n + 1) / 2))  # 0
```

## Ehrlich bleiben

- `nicht entschieden` heißt nicht „gleich“. Prüfe dann numerisch an mehreren Stellen und schreib, dass es nur numerisch geprüft ist.
- Eine Rechnung für endlich viele Fälle beweist keine Aussage für alle n. Nenne den geprüften Bereich.
- Fehlt SymPy, frag nach einer Installation (`pip install sympy` in der Run-Umgebung); das braucht eine Freigabe.
