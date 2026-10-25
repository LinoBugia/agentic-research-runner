"""Data table as HTML (docs/08-ux-konzept.md §5.3, §7.2): header, group rows, clickable rows.

Rendered in one piece for speed (hundreds of rows, Ü4); live cells carry an `id` so pages can
update them with `arr.setText` without re-rendering the table.
"""

from dataclasses import dataclass, field

from arr.service.gui.components.base import esc


@dataclass(frozen=True)
class Column:
    """A table column: German header, alignment, optional `title` (tooltip) and CSS class."""

    label: str
    align: str = "left"
    title: str | None = None
    css: str = ""
    sr_label: str | None = None


@dataclass
class Row:
    """A row: HTML cells (already escaped), optional link target and CSS class."""

    cells: list[str]
    href: str | None = None
    css: str = ""
    row_id: str | None = None


@dataclass
class Group:
    """A group of rows with a heading row, e.g. „Aktiv · 3“; `label=None` means no heading."""

    label: str | None
    rows: list[Row] = field(default_factory=list)


def table_html(columns: list[Column], groups: list[Group], *, classes: str = "tbl") -> str:
    """Render the table; rows with `href` open it on click (arr.js), links stay keyboard paths."""
    head = []
    for col in columns:
        attrs = ""
        if col.title:
            attrs += f' title="{esc(col.title)}"'
        cls = " ".join(c for c in [col.css, "right" if col.align == "right" else ""] if c)
        if cls:
            attrs += f' class="{esc(cls)}"'
        label = esc(col.label)
        if col.sr_label:
            label = (
                f'<span aria-hidden="true">{label}</span>'
                f'<span class="sr-only">{esc(col.sr_label)}</span>'
            )
        head.append(f'<th scope="col"{attrs}>{label}</th>')
    body = []
    for group in groups:
        if group.label is not None:
            body.append(
                f'<tr class="group"><td colspan="{len(columns)}">{esc(group.label)}</td></tr>'
            )
        for row in group.rows:
            attrs = ""
            if row.href:
                attrs += f' data-href="{esc(row.href)}"'
            if row.css:
                attrs += f' class="{esc(row.css)}"'
            if row.row_id:
                attrs += f' id="{esc(row.row_id)}"'
            cells = []
            for col, cell in zip(columns, row.cells, strict=True):
                cls = " ".join(c for c in [col.css, "right" if col.align == "right" else ""] if c)
                cells.append(f'<td class="{cls}">{cell}</td>' if cls else f"<td>{cell}</td>")
            body.append(f"<tr{attrs}>{''.join(cells)}</tr>")
    return (
        f'<table class="{esc(classes)}"><thead><tr>{"".join(head)}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table>"
    )
