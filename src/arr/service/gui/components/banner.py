"""K13 banner and K14 empty state (docs/08-ux-konzept.md K13, K14, §8.4, §8.5)."""

from typing import Literal

from arr.service.gui.components.base import esc, icon, raw

BannerKind = Literal["err", "warn", "info"]
_ICONS = {"err": "error", "warn": "warning", "info": "info"}


def banner_html(
    kind: BannerKind,
    title: str,
    body: str = "",
    actions: list[tuple[str, str]] | None = None,
    *,
    small: bool = False,
) -> str:
    """A banner: icon, bold title (what happened), text (why, what to do), actions as links.

    Errors are `role="alert"`, the rest `role="status"` (K13). `body` is plain text.
    """
    role = "alert" if kind == "err" else "status"
    links = "".join(
        f'<a class="btn btn-sm" href="{esc(href)}">{esc(label)}</a>'
        for label, href in actions or []
    )
    row = f'<div class="btn-row" style="margin-top:8px">{links}</div>' if links else ""
    cls = f"banner {kind}" + (" banner-sm" if small else "")
    return (
        f'<div class="{cls}" role="{role}">{icon(_ICONS[kind])}'
        f'<div class="txt"><b>{esc(title)}</b>{esc(body)}{row}</div></div>'
    )


def banner(kind: BannerKind, title: str, body: str = "", actions=None, *, small=False):  # noqa: ANN001, ANN201
    """Render `banner_html` into the current container."""
    return raw(banner_html(kind, title, body, actions, small=small))


def empty_state_html(
    symbol: str, title: str, sentence: str, actions: list[tuple[str, str, bool]] | None = None
) -> str:
    """Empty state: what appears here, why it is empty, next step (§8.5, K14).

    `actions` are `(label, href, primary)`.
    """
    links = "".join(
        f'<a class="btn btn-sm{" primary" if primary else ""}" href="{esc(href)}">{esc(label)}</a>'
        for label, href, primary in actions or []
    )
    row = f'<div class="btn-row">{links}</div>' if links else ""
    return (
        f'<div class="empty" role="status"><div class="big">{icon(symbol)}</div>'
        f"<b>{esc(title)}</b>{esc(sentence)}{row}</div>"
    )


def empty_state(symbol: str, title: str, sentence: str, actions=None):  # noqa: ANN001, ANN201
    """Render `empty_state_html` into the current container."""
    return raw(empty_state_html(symbol, title, sentence, actions))
