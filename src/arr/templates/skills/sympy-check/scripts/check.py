"""Check an identity or simplify an expression with SymPy (skill `sympy-check`).

Usage:
    python check.py gleich "LHS" "RHS" [--symbol n:integer,positive ...]
    python check.py vereinfache "EXPR" [--symbol x:real ...]

`^` means power. Exit codes: 0 equal (or simplified), 1 not equal, 2 undecided, 3 error
(refused input, unreadable expression, a limit reached, SymPy missing, wrong call).
An expression may start with `-`; options are only `--symbol` and `--`.

An expression is never run as code. The only wall against code is the allow-list: before
SymPy sees an expression, every token must be an ASCII number, an ASCII name, an operator, a
comma or a round bracket, and every called name a known SymPy function. The restricted name
space of the parser is no second wall (an `eval` without builtins can be escaped); it only
keeps the names small.

Limits: CPU time (`CPU_SECONDS`, also memory where the system allows it), and before any
evaluation a size check on a parse that evaluates nothing: no power of numbers above
`MAX_BITS` bits, no factorial-like function of a number above `MAX_ARGUMENT`.
"""

import contextlib
import keyword
import math
import os
import re
import signal
import sys

NOT_EQUAL = 1
UNDECIDED = 2
ERROR = 3
MAX_CHARS = 2000
MAX_DIGITS = 100
"""Longest number literal."""
MAX_BITS = 14_000
"""Largest power of numbers (about 4200 digits, below the 4300 digits Python prints)."""
MAX_ARGUMENT = 1000
"""Largest number argument of factorial-like functions (`BIG_FUNCTIONS`)."""
CPU_SECONDS = 10
MEMORY_BYTES = 512 * 1024 * 1024

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
BIG_FUNCTIONS = {"factorial", "binomial", "gamma", "harmonic", "fibonacci", "catalan", "bernoulli"}
CONSTANTS = {"pi", "E", "I", "oo"}
"""Names SymPy reads as constants: π, Euler's number, the imaginary unit, infinity."""
ASSUMPTIONS = {
    "positive", "negative", "nonnegative", "nonpositive", "nonzero", "zero",
    "integer", "rational", "irrational", "real", "complex", "imaginary", "finite",
    "even", "odd", "prime", "composite", "algebraic", "transcendental",
    "extended_real", "extended_positive", "extended_negative", "extended_nonnegative",
}  # fmt: skip
_TOKEN = re.compile(
    r"\s+|(?P<num>\d+(?:\.\d+)?)|(?P<name>[A-Za-z][A-Za-z0-9_]*)|\*\*|[-+*/^(),!]", re.ASCII
)
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*", re.ASCII)
USAGE = (
    'Aufruf: check.py gleich "A" "B" [--symbol n:integer,positive] | check.py vereinfache "A". '
    "Exit-Code: 0 gleich, 1 nicht gleich, 2 nicht entschieden, 3 Fehler."
)
_PARSER_NAMES = ("Integer", "Float", "Rational", "Symbol", "Add", "Mul", "Pow", "factorial")
"""Names the transformations of `parse_expr` write into the code they generate."""


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
            char = text[pos]
            shown = char if char.isprintable() and not char.isspace() else f"U+{ord(char):04X}"
            raise Refused(
                f"Zeichen „{shown}“ an Stelle {pos + 1} ist nicht erlaubt. Erlaubt sind "
                "Zahlen, Namen (ASCII), + - * / ^ ** !, Kommas und runde Klammern."
            )
        if match.group("num") and len(match.group("num")) > MAX_DIGITS:
            raise Refused(f"Die Zahl an Stelle {pos + 1} hat mehr als {MAX_DIGITS} Ziffern.")
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


def symbol_specs(specs: list[str]) -> list[tuple[str, dict[str, bool]]]:
    """Check `--symbol name:flag,flag` before SymPy: ASCII names, known assumptions."""
    wanted: list[tuple[str, dict[str, bool]]] = []
    for spec in specs:
        name, _, flags = spec.partition(":")
        name = name.strip()
        if not _NAME.fullmatch(name) or "__" in name or keyword.iskeyword(name):
            raise Refused(f"„{name}“ ist kein erlaubter Symbolname (nur ASCII-Buchstaben).")
        assumptions = {}
        for flag in (f.strip() for f in flags.split(",") if f.strip()):
            if flag not in ASSUMPTIONS:
                raise Refused(
                    f"Unbekannte Annahme „{flag}“ bei {name}. Erlaubt sind z. B. positive, "
                    "integer, real, nonnegative."
                )
            assumptions[flag] = True
        wanted.append((name, assumptions))
    return wanted


def _symbols(specs: list[str]) -> dict:
    wanted = symbol_specs(specs)
    if not wanted:
        return {}
    import sympy

    return {name: sympy.Symbol(name, **assumptions) for name, assumptions in wanted}


def _scope(text: str, declared: dict, notes: set[str], *, inert: bool) -> dict:
    """The names `parse_expr` may use; `inert` makes every function an undefined one."""
    import sympy

    called, plain = names_of(text)
    scope = {"__builtins__": {}}
    for name in _PARSER_NAMES:
        scope[name] = getattr(sympy, name)
    for name in called:
        scope[name] = sympy.Function(name) if inert else getattr(sympy, name)
    if inert:
        scope["factorial"] = sympy.Function("factorial")
    for name in plain:
        if name in declared:
            scope[name] = declared[name]
        elif name in CONSTANTS:
            scope[name] = getattr(sympy, name)
            if name in ("I", "E"):
                notes.add(name)
        else:
            scope[name] = sympy.Symbol(name)  # e.g. N, S, beta as a variable
    return scope


def _bits(value) -> float:  # noqa: ANN001 - a SymPy number
    """About how many bits the integer part of |value| has."""
    import sympy

    magnitude = abs(sympy.N(value, 15))
    if not magnitude.is_finite:
        return math.inf
    return float(sympy.log(magnitude + 2, 2).evalf(15))


def check_size(text: str, declared: dict) -> None:
    """Refuse powers of numbers and factorial-like functions that are too large to compute.

    The expression is parsed without evaluating anything (`evaluate=False`, every function
    undefined), then checked from the leaves up, so every number evaluated here is bounded.
    """
    import sympy
    from sympy.parsing.sympy_parser import convert_xor, parse_expr, standard_transformations

    scope = _scope(text, declared, set(), inert=True)
    transformations = (*standard_transformations, convert_xor)
    expr = parse_expr(
        text, local_dict={}, global_dict=scope, transformations=transformations, evaluate=False
    )
    if isinstance(expr, tuple):
        raise Refused("Gib einen Ausdruck an, kein Tupel.")
    for node in sympy.postorder_traversal(expr):
        if node.is_Pow and node.base.is_number and node.exp.is_number:
            exponent = abs(sympy.N(node.exp, 15))
            if not exponent.is_finite or float(exponent) * max(_bits(node.base), 1) > MAX_BITS:
                raise Refused(
                    f"Die Potenz {node} wäre zu groß zum Ausrechnen (mehr als {MAX_BITS} Bit). "
                    "Rechne symbolisch, z. B. mit einer Variablen statt der Zahl."
                )
        name = getattr(node.func, "__name__", "")
        if name in BIG_FUNCTIONS and node.args:
            for argument in node.args:
                if argument.is_number and abs(sympy.N(argument, 15)) > MAX_ARGUMENT:
                    raise Refused(
                        f"{name}(…) mit einem Argument über {MAX_ARGUMENT} ist zu groß zum "
                        "Ausrechnen. Rechne symbolisch, z. B. mit einer Variablen."
                    )


def _parse(text: str, declared: dict, notes: set[str]):  # noqa: ANN202
    from sympy.parsing.sympy_parser import convert_xor, parse_expr, standard_transformations

    check_size(text, declared)
    scope = _scope(text, declared, notes, inert=False)
    transformations = (*standard_transformations, convert_xor)
    return parse_expr(text, local_dict={}, global_dict=scope, transformations=transformations)


def split_args(argv: list[str]) -> tuple[str, list[str], list[str]]:
    """(task, expressions, symbol specs); an expression may start with `-` (W4-05.2)."""
    if not argv:
        raise Refused("Aufgabe und Ausdruck fehlen. " + USAGE)
    task, rest = argv[0], argv[1:]
    if task not in ("gleich", "vereinfache"):
        raise Refused(f"Unbekannte Aufgabe „{task}“. Erlaubt sind gleich und vereinfache.")
    expressions: list[str] = []
    specs: list[str] = []
    index = 0
    only_expressions = False
    while index < len(rest):
        item = rest[index]
        if not only_expressions and item == "--":
            only_expressions = True
        elif not only_expressions and item == "--symbol":
            if index + 1 >= len(rest):
                raise Refused("Nach --symbol fehlt das Symbol, z. B. --symbol n:integer.")
            specs.append(rest[index + 1])
            index += 1
        elif not only_expressions and item.startswith("--symbol="):
            specs.append(item.removeprefix("--symbol="))
        else:
            expressions.append(item)
        index += 1
    wanted = 2 if task == "gleich" else 1
    if len(expressions) != wanted:
        raise Refused(f"„{task}“ braucht {wanted} Ausdruck/Ausdrücke, gefunden {len(expressions)}.")
    return task, expressions, specs


def _limit_resources() -> None:
    """CPU time for this process, and memory where the system allows it (not on macOS)."""
    import resource

    def out_of_time(signum: int, frame: object) -> None:
        print(
            f"Fehler: Die Rechnung dauerte länger als {CPU_SECONDS} s CPU-Zeit und wurde beendet."
        )
        sys.stdout.flush()
        os._exit(ERROR)

    signal.signal(signal.SIGXCPU, out_of_time)
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS + 2))
    # macOS refuses address space limits; there the size check before evaluating still holds
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    try:
        task, expressions, specs = split_args(args)
        for text in expressions:
            names_of(text)  # refuse before SymPy is even imported
        symbol_specs(specs)
    except Refused as exc:
        print(f"Abgelehnt: {exc}")
        return ERROR
    try:
        import sympy
    except ImportError:
        print(
            "Fehler: SymPy fehlt in dieser Python-Umgebung. Bitte um eine Freigabe für "
            "„pip install sympy“ in der Run-Umgebung."
        )
        return ERROR
    _limit_resources()
    notes: set[str] = set()
    try:
        declared = _symbols(specs)
        exprs = [_parse(text, declared, notes) for text in expressions]
        if any(isinstance(e, tuple) for e in exprs):
            raise Refused("Gib einen Ausdruck an, kein Tupel.")
    except Refused as exc:
        print(f"Abgelehnt: {exc}")
        return ERROR
    except MemoryError:
        print("Fehler: Der Ausdruck braucht zu viel Speicher.")
        return ERROR
    except Exception as exc:  # noqa: BLE001 - any parse error is reported, not raised
        print(f"Fehler: Ausdruck nicht lesbar ({type(exc).__name__}: {str(exc)[:200]}).")
        return ERROR
    for name in sorted(notes):
        meaning = "die imaginäre Einheit" if name == "I" else "die eulersche Zahl"
        print(f"Hinweis: {name} ist {meaning}. Als Variable: --symbol {name}")
    try:
        if task == "vereinfache":
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
            return NOT_EQUAL
        print(f"nicht entschieden: Differenz = {difference}")
        return UNDECIDED
    except MemoryError:
        print("Fehler: Die Rechnung braucht zu viel Speicher.")
        return ERROR
    except Exception as exc:  # noqa: BLE001 - SymPy failures are reported, not raised
        print(f"Fehler beim Rechnen ({type(exc).__name__}: {str(exc)[:200]}).")
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
