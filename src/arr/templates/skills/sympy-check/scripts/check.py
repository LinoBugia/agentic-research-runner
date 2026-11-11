"""Check an identity or simplify an expression with SymPy (skill `sympy-check`).

Usage:
    python check.py gleich "LHS" "RHS" [--symbol n:integer,positive ...]
    python check.py vereinfache "EXPR" [--symbol x:real ...]

`^` means power. Exit code: 0 equal (or simplified), 1 not equal, 2 undecided or error.

An expression is never run as code: before SymPy sees it, every token must be a number, a
name, an operator, a comma or a round bracket, and every called name a known SymPy function
(allow-list). SymPy then parses it with only these names in scope, without builtins.
"""

import argparse
import keyword
import re
import sys

USAGE_ERROR = 2
MAX_CHARS = 2000

FUNCTIONS = {
    "sin", "cos", "tan", "cot", "sec", "csc", "asin", "acos", "atan", "atan2", "acot",
    "sinh", "cosh", "tanh", "coth", "asinh", "acosh", "atanh",
    "exp", "log", "ln", "sqrt", "root", "cbrt", "Abs", "sign", "floor", "ceiling", "frac",
    "re", "im", "arg", "conjugate", "Min", "Max", "Mod", "gcd", "lcm",
    "factorial", "binomial", "gamma", "beta", "zeta", "harmonic", "fibonacci", "catalan",
    "bernoulli", "Rational", "Sum", "Product", "summation", "product",
    "diff", "integrate", "limit", "series", "simplify", "expand", "factor", "cancel",
    "together", "apart", "trigsimp", "powsimp", "combsimp", "gammasimp",
}  # fmt: skip
"""Names that may be called, e.g. `binomial(n, 2)`."""
CONSTANTS = {"pi", "E", "I", "oo"}
"""Names SymPy reads as constants: π, Euler's number, the imaginary unit, infinity."""
ASSUMPTIONS = {
    "positive", "negative", "nonnegative", "nonpositive", "nonzero", "zero",
    "integer", "rational", "irrational", "real", "complex", "imaginary", "finite",
    "even", "odd", "prime", "composite", "algebraic", "transcendental",
    "extended_real", "extended_positive", "extended_negative", "extended_nonnegative",
}  # fmt: skip
_TOKEN = re.compile(r"\s+|(?P<num>\d+(?:\.\d+)?)|(?P<name>[A-Za-z][A-Za-z0-9_]*)|\*\*|[-+*/^(),!]")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


class Refused(ValueError):
    """An input that is not allowed; the message says why (German)."""


def names_of(text: str) -> tuple[set[str], set[str]]:
    """Check `text` against the allow-list; return (called names, other names)."""
    if len(text) > MAX_CHARS:
        raise Refused(f"Der Ausdruck ist zu lang (höchstens {MAX_CHARS} Zeichen).")
    called: set[str] = set()
    plain: set[str] = set()
    pos = 0
    tokens: list[tuple[str, str]] = []
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if match is None:
            raise Refused(
                f"Zeichen „{text[pos]}“ an Stelle {pos + 1} ist nicht erlaubt. Erlaubt sind "
                "Zahlen, Namen, + - * / ^ ** !, Kommas und runde Klammern."
            )
        kind = "name" if match.group("name") else "other"
        if not match.group(0).isspace():
            tokens.append((kind, match.group(0)))
        pos = match.end()
    for index, (kind, token) in enumerate(tokens):
        if kind != "name":
            continue
        if "__" in token or token.startswith("_") or keyword.iskeyword(token):
            raise Refused(f"Der Name „{token}“ ist nicht erlaubt.")
        is_call = index + 1 < len(tokens) and tokens[index + 1][1] == "("
        if is_call:
            if token not in FUNCTIONS:
                raise Refused(
                    f"„{token}(…)“ ist keine bekannte Funktion. Erlaubt sind z. B. sin, exp, "
                    "log, sqrt, binomial, factorial, Sum."
                )
            called.add(token)
        else:
            plain.add(token)
    return called, plain


def _symbols(specs: list[str]) -> dict:
    """Symbols of `--symbol name:flag,flag`; names and flags are checked before SymPy."""
    wanted: list[tuple[str, dict[str, bool]]] = []
    for spec in specs:
        name, _, flags = spec.partition(":")
        name = name.strip()
        if not _NAME.fullmatch(name) or "__" in name or keyword.iskeyword(name):
            raise Refused(f"„{name}“ ist kein erlaubter Symbolname.")
        assumptions = {}
        for flag in (f.strip() for f in flags.split(",") if f.strip()):
            if flag not in ASSUMPTIONS:
                raise Refused(
                    f"Unbekannte Annahme „{flag}“ bei {name}. Erlaubt sind z. B. positive, "
                    "integer, real, nonnegative."
                )
            assumptions[flag] = True
        wanted.append((name, assumptions))
    if not wanted:
        return {}
    import sympy

    return {name: sympy.Symbol(name, **assumptions) for name, assumptions in wanted}


def _parse(text: str, declared: dict, notes: set[str]):
    import sympy
    from sympy.parsing.sympy_parser import (
        convert_xor,
        parse_expr,
        standard_transformations,
    )

    called, plain = names_of(text)
    scope = {"__builtins__": {}}
    for name in ("Integer", "Float", "Rational", "Symbol", "Function", "factorial"):
        scope[name] = getattr(sympy, name)
    for name in called:
        scope[name] = getattr(sympy, name)
    for name in plain:
        if name in declared:
            scope[name] = declared[name]
        elif name in CONSTANTS:
            scope[name] = getattr(sympy, name)
            if name in ("I", "E"):
                notes.add(name)
        else:
            scope[name] = sympy.Symbol(name)  # e.g. N, S, beta as a variable
    transformations = (*standard_transformations, convert_xor)
    return parse_expr(text, local_dict={}, global_dict=scope, transformations=transformations)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rechnungen mit SymPy nachprüfen.")
    parser.add_argument("aufgabe", choices=["gleich", "vereinfache"])
    parser.add_argument("ausdruck", nargs="+", help="ein Ausdruck, bei „gleich“ zwei")
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Symbol mit Annahmen, z. B. n:integer,positive",
    )
    args = parser.parse_args(argv)
    wanted = 2 if args.aufgabe == "gleich" else 1
    if len(args.ausdruck) != wanted:
        print(f"„{args.aufgabe}“ braucht {wanted} Ausdruck/Ausdrücke.")
        return USAGE_ERROR
    try:
        for text in args.ausdruck:
            names_of(text)  # refuse before SymPy is even imported
    except Refused as exc:
        print(f"Abgelehnt: {exc}")
        return USAGE_ERROR
    try:
        import sympy
    except ImportError:
        print(
            "SymPy fehlt in dieser Python-Umgebung. Bitte um eine Freigabe für "
            "„pip install sympy“ in der Run-Umgebung."
        )
        return USAGE_ERROR
    notes: set[str] = set()
    try:
        declared = _symbols(args.symbol)
        exprs = [_parse(text, declared, notes) for text in args.ausdruck]
    except Refused as exc:
        print(f"Abgelehnt: {exc}")
        return USAGE_ERROR
    except Exception as exc:  # noqa: BLE001 - any parse error is reported, not raised
        print(f"Ausdruck nicht lesbar: {exc}")
        return USAGE_ERROR
    for name in sorted(notes):
        meaning = "die imaginäre Einheit" if name == "I" else "die eulersche Zahl"
        print(f"Hinweis: {name} ist {meaning}. Als Variable: --symbol {name}")
    if args.aufgabe == "vereinfache":
        print(f"vereinfacht: {sympy.simplify(exprs[0])}")
        return 0
    difference = sympy.simplify(exprs[0] - exprs[1])
    if difference == 0:
        print("gleich (Differenz vereinfacht zu 0)")
        return 0
    decided = difference.equals(0)
    if decided is True:
        print("gleich (Differenz ist 0, nach equals)")
        return 0
    if decided is False:
        print(f"nicht gleich: Differenz = {difference}")
        return 1
    print(f"nicht entschieden: Differenz = {difference}")
    return USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())
