---
name: latex-writeup
description: Ergebnisse (Sätze, Beweise, Rechnungen) sauber und kompilierbar in LaTeX festhalten, nach den Konventionen des Projekts. Nutzen, wenn ein Ergebnis als .tex-Datei abgegeben oder ins Projekt übernommen werden soll.
---

# LaTeX-Writeup

## Konventionen zuerst

- Sieh im Projekt nach, bevor du schreibst: vorhandene `.tex`-Dateien, eine Präambel (`preamble.tex`, `macros.tex`), Notation in `CLAUDE.md` oder `notation.md`. Übernimm deren Makros, Umgebungen und Schreibweisen.
- Gibt es nichts, nimm die Vorlage unten und `amsthm`-Umgebungen.

## Aufbau

- Jede Aussage in einer Umgebung (`satz`, `lemma`, `definition`, `bemerkung`), jeder Beweis in `proof`.
- Lemmata vor dem Satz, der sie benutzt. Verweise mit `\label` und `\ref`, keine Nummern von Hand.
- Voraussetzungen stehen in der Aussage, nicht erst im Beweis.
- Offene Stellen markierst du sichtbar, z. B. `\textbf{Lücke:} …`, statt sie wegzulassen.
- Rechnungen, die ein Skript geprüft hat, bekommen eine Fußnote mit dem Dateinamen des Skripts.

## Vorlage

```latex
\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[ngerman]{babel}
\usepackage{amsmath,amssymb,amsthm}
\newtheorem{satz}{Satz}
\newtheorem{lemma}[satz]{Lemma}
\theoremstyle{definition}
\newtheorem{definition}[satz]{Definition}
\theoremstyle{remark}
\newtheorem{bemerkung}[satz]{Bemerkung}
\begin{document}
\begin{lemma}\label{lem:hilf} … \end{lemma}
\begin{proof} … \end{proof}
\begin{satz}\label{satz:haupt} … \end{satz}
\begin{proof} Nach Lemma~\ref{lem:hilf} … \end{proof}
\end{document}
```

## Prüfen

- Kompiliere, wenn `pdflatex` oder `latexmk` verfügbar ist: `latexmk -pdf -interaction=nonstopmode datei.tex`. Fehlt beides, sag das in deinem Ergebnis; installiere nichts ohne Freigabe.
- Warnungen zu undefinierten Referenzen behebst du, bevor du abgibst.
- Gib die `.tex`-Datei ab, nicht nur das PDF.
