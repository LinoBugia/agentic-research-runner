"""German formatting of numbers, times and units for the GUI (docs/08-ux-konzept.md §8.7).

All functions are pure; times are Unix seconds shown in local time.
"""

import math
from datetime import datetime

from arr.backends.base import CostKind


def decimal(value: float, places: int = 1) -> str:
    """German decimal: `6.42` → `6,42` (no thousands separator below 10 000)."""
    text = f"{value:,.{places}f}"
    return text.replace(",", " ").replace(".", ",").replace(" ", " ")


def integer(value: int) -> str:
    """Whole number with a narrow space as thousands separator: `9882` → `9 882`."""
    return f"{value:,}".replace(",", " ")


def money(usd: float | None, kind: CostKind | None = None, *, note: bool = False) -> str:
    """`18,40 $` (billed), `≈ 6,42 $` (estimated, subscription), `–` (unknown).

    With `note=True` an estimate gets „(rechnerisch)“ for its first appearance on a page.
    """
    if usd is None:
        return "–"
    text = f"{decimal(usd, 2)} $"
    if kind is CostKind.ESTIMATED:
        text = f"≈ {text}" + (" (rechnerisch)" if note else "")
    return text


def budget(usd: float | None) -> str:
    """Budget limit without decimals when whole: `25 $`, `12,50 $`."""
    if usd is None:
        return "ohne Grenze"
    return f"{decimal(usd, 0 if float(usd).is_integer() else 2)} $"


def tokens(count: int | None) -> str:
    """`412 k`, `1,9 M`, `950`, `–`."""
    if count is None:
        return "–"
    if count >= 1_000_000:
        return f"{decimal(count / 1_000_000, 1)} M"
    if count >= 1_000:
        return f"{count // 1000} k"
    return str(count)


def exact(count: int | None) -> str:
    """Exact number with a thousands dot, for the record per profile (W4-00 B13): `1.081`."""
    if count is None:
        return "–"
    return f"{count:,}".replace(",", ".")


def size(num_bytes: int | None) -> str:
    """Decimal units: `0 B`, `38 MB`, `2,3 GB`, `1,2 TB`."""
    if num_bytes is None:
        return "–"
    if num_bytes < 1000:
        return f"{num_bytes} B"
    if num_bytes < 1_000_000:
        return f"{round(num_bytes / 1000)} kB"
    if num_bytes < 1_000_000_000:
        return f"{round(num_bytes / 1_000_000)} MB"
    if num_bytes < 1_000_000_000_000:
        return f"{decimal(num_bytes / 1_000_000_000, 1)} GB"
    return f"{decimal(num_bytes / 1_000_000_000_000, 1)} TB"


def duration_hm(seconds: float | None) -> str:
    """Run durations in tables: `1:52 h`, `0:04 h`, `–`."""
    if seconds is None:
        return "–"
    minutes = int(seconds // 60)
    return f"{minutes // 60}:{minutes % 60:02d} h"


def duration(seconds: float | None) -> str:
    """Durations in text: `1:52 h`, `22 min`, `1 min 40 s`, `38 s`."""
    if seconds is None:
        return "–"
    seconds = int(seconds)
    if seconds >= 3600:
        return duration_hm(seconds)
    if seconds >= 600:
        return f"{seconds // 60} min"
    if seconds >= 60:
        rest = seconds % 60
        return f"{seconds // 60} min" + (f" {rest} s" if rest else "")
    return f"{seconds} s"


def limit_hours(seconds: int | None) -> str:
    """A time limit: `3 h`, `90 min`, `ohne Grenze`."""
    if seconds is None:
        return "ohne Grenze"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    return f"{seconds // 60} min"


def clock(ts: float) -> str:
    """`15:24`."""
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def clock_s(ts: float) -> str:
    """`15:11:40` (transcripts)."""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def when(ts: float | None, now: float) -> str:
    """`heute 15:21`, `gestern 11:05`, `22.10. 11:05`, `28.09.2025` (other year)."""
    if ts is None:
        return "–"
    day = datetime.fromtimestamp(ts)
    today = datetime.fromtimestamp(now).date()
    delta = (today - day.date()).days
    if delta == 0:
        return f"heute {day:%H:%M}"
    if delta == 1:
        return f"gestern {day:%H:%M}"
    if day.year == today.year:
        return f"{day:%d.%m.} {day:%H:%M}"
    return f"{day:%d.%m.%Y}"


def relative(ts: float, now: float) -> str | None:
    """`vor 3 min`, `gerade eben`; `None` from one hour on (then use absolute times)."""
    age = now - ts
    if age < 0 or age >= 3600:
        return None
    if age < 60:
        return "gerade eben"
    return f"vor {int(age // 60)} min"


def deadline(ts: float, now: float) -> str:
    """`läuft in 14 min ab (15:36)`; seconds in the last 2 minutes (same as arr.js)."""
    left = ts - now
    at = clock(ts)
    if left <= 0:
        return f"Frist abgelaufen ({at})"
    if left < 120:
        minutes, secs = int(left // 60), int(left % 60)
        return f"läuft in {f'{minutes} min ' if minutes else ''}{secs} s ab ({at})"
    if left < 3600:
        return f"läuft in {math.ceil(left / 60)} min ab ({at})"
    total = math.ceil(left / 60)
    return f"läuft in {total // 60}:{total % 60:02d} h ab ({at})"


def score(value: float | None) -> str:
    """Score with one decimal: `8,5`, `–`."""
    return "–" if value is None else decimal(value, 1)


def threshold(value: float | None) -> str:
    """Pass mark without needless decimals: `7`, `7,5` („bestanden ab 7“, §8.7)."""
    if value is None:
        return "–"
    return str(int(value)) if float(value).is_integer() else decimal(value, 1)


def percent(ratio: float | None) -> str:
    """Whole percent: `82 %`."""
    return "–" if ratio is None else f"{round(ratio * 100)} %"


def join_de(items: list[str]) -> str:
    """`a`, `a und b`, `a, b und c`."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " und " + items[-1]
