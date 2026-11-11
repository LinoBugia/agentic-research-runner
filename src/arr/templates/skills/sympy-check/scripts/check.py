"""Check an identity or simplify an expression with SymPy (skill `sympy-check`).

Usage:
    python check.py gleich "LHS" "RHS" [--symbol n:integer,positive ...]
    python check.py vereinfache "EXPR" [--symbol x:real ...]

`^` means power. Exit code: 0 equal (or simplified), 1 not equal, 2 undecided or error.
"""

import argparse
import sys

USAGE_ERROR = 2


def _symbols(specs: list[str]) -> dict:
    import sympy

    found = {}
    for spec in specs:
        name, _, flags = spec.partition(":")
        assumptions = {flag.strip(): True for flag in flags.split(",") if flag.strip()}
        found[name.strip()] = sympy.Symbol(name.strip(), **assumptions)
    return found


def _parse(text: str, names: dict):
    from sympy.parsing.sympy_parser import (
        convert_xor,
        parse_expr,
        standard_transformations,
    )

    return parse_expr(
        text, local_dict=names, transformations=(*standard_transformations, convert_xor)
    )


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
    try:
        import sympy
    except ImportError:
        print(
            "SymPy fehlt in dieser Python-Umgebung. Bitte um eine Freigabe für "
            "„pip install sympy“ in der Run-Umgebung."
        )
        return USAGE_ERROR
    wanted = 2 if args.aufgabe == "gleich" else 1
    if len(args.ausdruck) != wanted:
        print(f"„{args.aufgabe}“ braucht {wanted} Ausdruck/Ausdrücke.")
        return USAGE_ERROR
    try:
        names = _symbols(args.symbol)
        exprs = [_parse(text, names) for text in args.ausdruck]
    except Exception as exc:  # noqa: BLE001 - any parse error is reported, not raised
        print(f"Ausdruck nicht lesbar: {exc}")
        return USAGE_ERROR
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
