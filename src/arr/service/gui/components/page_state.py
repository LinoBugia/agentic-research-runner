"""Loading, error and empty states of the W1-08 pages (docs/08 §5 „Zustände“, §8.4, §8.5).

Pages load each area with `load_into`: fast content is sent with the page, slow content
replaces a skeleton that appears after 300 ms, with a sentence about the reason after 3 s
(static/screens.css). A failing request shows a banner where the content would be, and the
rest of the page stays usable.

In the mock, `?zustand=leer|laedt|fehler` forces a state (`state_api`, `MockApi.forced`), so
reviews and the UX acceptance test can see every state of every page.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

from nicegui import background_tasks, ui

from arr.service.api_client import ApiClient
from arr.service.api_models import ApiException
from arr.service.gui.app import STATIC_DIR, STATIC_URL
from arr.service.gui.components.base import button, esc, icon, raw

FORCED_STATES = ("leer", "laedt", "fehler")
"""Values of `?zustand=` the mock understands (docs/08 §5, W1-08)."""


def state_api(api: ApiClient, zustand: str, page: str = "") -> ApiClient:
    """The API a page uses: in the mock with `?zustand=…` a view in that state, else `api`."""
    forced = getattr(api, "forced", None)
    if zustand in FORCED_STATES and forced is not None:
        return forced(zustand, page)
    return api


def use_screen_assets() -> None:
    """Load the styles and scripts of the W1-08 pages, with typeset formulas (G7)."""
    from arr.service.gui.pages.run_detail import KATEX_URL

    version = int(max(p.stat().st_mtime for p in STATIC_DIR.iterdir()))
    ui.add_head_html(
        f'<link rel="stylesheet" href="{STATIC_URL}/screens.css?v={version}">'
        f'<script src="{STATIC_URL}/screens.js?v={version}" defer></script>'
        f'<script src="{STATIC_URL}/math.js?v={version}" data-katex="{KATEX_URL}" defer>'
        "</script>"
    )


def skeleton_html(lines: int = 4, *, shape: str = "lines", reason: str = "") -> str:
    """Skeleton in the shape of the content; shown after 300 ms, `reason` after 3 s (§5).

    `shape`: `lines` (text), `rows` (table rows), `cards` (cards side by side), `kpi`.
    """
    if shape == "rows":
        body = "".join('<div class="skel skel-row"></div>' for _ in range(lines))
    elif shape == "cards":
        body = (
            '<div class="grid-3">'
            + "".join(
                '<div class="card"><div class="skel w40 h20"></div><div class="skel w80"></div>'
                '<div class="skel w60"></div><div class="skel w80"></div></div>'
                for _ in range(lines)
            )
            + "</div>"
        )
    elif shape == "kpi":
        body = (
            '<div class="grid-4">'
            + "".join(
                '<div class="kpi"><div class="skel w40"></div>'
                '<div class="skel w60 h20"></div></div>'
                for _ in range(lines)
            )
            + "</div>"
        )
    else:
        widths = ["w80", "w60", "w80", "w40", "w60", "w80"]
        body = "".join(f'<div class="skel {widths[i % len(widths)]}"></div>' for i in range(lines))
    slow = f'<p class="skel-slow small muted">{esc(reason)}</p>' if reason else ""
    return (
        f'<div class="skel-wrap" aria-busy="true" data-state="laedt">'
        f'<span class="sr-only">Lädt …</span>{body}{slow}</div>'
    )


def error_html(exc: ApiException, *, retry_href: str, retry_label: str = "Neu laden") -> str:
    """Banner after §8.4 where the content would be: what happened (bold), why/what to do,
    actions. An action without its own URL (e.g. „Index neu aufbauen“) reloads the page."""
    err = exc.error
    actions = []
    if err.action is not None:
        actions.append((err.action.label, err.action.url or retry_href))
    if not actions or actions[0][1] != retry_href:
        actions.append((retry_label, retry_href))
    links = "".join(
        f'<a class="btn btn-sm{" primary" if i == 0 else ""}" href="{esc(href)}">{esc(label)}</a>'
        for i, (label, href) in enumerate(actions)
    )
    detail = f" {esc(err.detail)}" if err.detail else ""
    return (
        f'<div class="banner err" role="alert" data-state="fehler">{icon("error")}'
        f'<div class="txt"><b>{esc(err.message)}</b>{detail}'
        f'<div class="btn-row" style="margin-top:8px">{links}</div></div></div>'
    )


def clean_href(path: str, **params: Any) -> str:
    """`path` with `params` (empty values dropped), without `?zustand=` (for „Neu laden“)."""
    query = urlencode({k: v for k, v in params.items() if v not in (None, "", False)})
    return path + (f"?{query}" if query else "")


FAST_SECONDS = 0.25
"""Content that arrives within this time is sent with the page; later content replaces a
skeleton (shown from 300 ms on, docs/08 §5)."""


async def load_into(
    box: ui.element,
    load: Callable[[], Awaitable[Any]],
    render: Callable[[Any], Any],
    *,
    retry_href: str,
    skeleton: str,
) -> None:
    """Fill `box` with `render(await load())`; a slow `load` shows `skeleton` meanwhile.

    A failing request shows the §8.4 banner in `box` instead of the content.
    """
    task = asyncio.ensure_future(load())
    done, _ = await asyncio.wait({task}, timeout=FAST_SECONDS)
    if done:
        await _finish(box, task, render, retry_href)
        return
    with box:
        raw(skeleton)
    background_tasks.create(_finish(box, task, render, retry_href), name="arr-load")


async def _finish(
    box: ui.element, task: "asyncio.Future[Any]", render: Callable[[Any], Any], retry_href: str
) -> None:
    try:
        data = await task
    except ApiException as exc:
        if not box.is_deleted:
            box.clear()
            with box:
                raw(error_html(exc, retry_href=retry_href))
        return
    if box.is_deleted:
        return
    box.clear()
    with box:
        result = render(data)
        if hasattr(result, "__await__"):
            await result


FORCED_ERRORS = {
    "freigaben": (
        "Die Freigabe-Inbox ist nicht lesbar.",
        "arr konnte den Index der Freigaben nicht laden. Lade die Seite neu, das Protokoll im "
        "Run-Ordner ist vollständig.",
        "Neu laden",
    ),
    "runs": (
        "Run-Ordner nicht lesbar: meta.json fehlt in 20261012-0915_collatz_custom_77aa.",
        "Vermutlich wurde der Ordner außerhalb von arr verändert. Der Index lässt sich aus den "
        "Run-Ordnern neu aufbauen.",
        "Index neu aufbauen",
    ),
    "run": (
        "Run-Ordner nicht lesbar: meta.json fehlt.",
        "Vermutlich wurde der Ordner außerhalb von arr verändert. Zeig ihn im Finder oder bau "
        "den Index aus den Run-Ordnern neu auf.",
        "Index neu aufbauen",
    ),
}


def forced_state(
    frame: Any, zustand: str, page: str, *, title: str, path: str, folder: str = ""
) -> bool:
    """For pages built before W1-08 (Freigaben, Runs, Run-Detail): render the forced states
    „lädt“ and „fehler“ of the mock (`?zustand=…`) in place of the content; True if done.

    „leer“ needs no special case, the pages show their own empty state with `state_api`.
    `folder` is the run folder of the run detail (for „Im Finder zeigen“, „Pfad kopieren“).
    """
    if zustand not in ("laedt", "fehler") or not hasattr(frame.api, "forced"):
        return False
    from arr.service.api_models import Action, ErrorCode

    with frame.content:
        raw(f'<div class="page-head"><h1 id="run-title" tabindex="-1">{esc(title)}</h1></div>')
        if zustand == "laedt":
            shape = "cards" if page == "freigaben" else "rows"
            reason = {
                "freigaben": "Die Freigabe-Inbox wird gelesen …",
                "runs": "Die Run-Ordner werden gelesen (31 Runs) …",
                "run": "Die letzten 200 Ereignisse werden geladen …",
            }[page]
            raw(skeleton_html(8 if shape == "rows" else 2, shape=shape, reason=reason))
        else:
            message, detail, label = FORCED_ERRORS[page]
            err = ApiException.of(
                ErrorCode.UNAVAILABLE, message, detail=detail, action=Action(label=label, url="")
            )
            raw(error_html(err, retry_href=path))
            if page == "run" and folder:
                with ui.element("p").classes("btn-row").style("margin-top:8px"):
                    button(
                        "Im Finder zeigen",
                        lambda: frame.toast(
                            f"Im Finder gezeigt: {folder} (im Prototyp nur angezeigt)"
                        ),
                        classes="btn btn-sm",
                    ).mark("show-in-finder")
                    raw(
                        f'<button type="button" class="btn btn-sm" data-copy="{esc(folder)}">'
                        "Pfad kopieren</button>",
                        tag="span",
                    )
    return True
