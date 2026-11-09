"""Low-level extraction helpers: run/paragraph formatting, images, table grids."""

from __future__ import annotations

import posixpath

from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from ..ooxml_extractor.package import DocxPackage, RelInfo
from ..schemas.extraction import (
    ImageRef,
    ParagraphFormatting,
    Run,
    RunFormatting,
    TableStructure,
)

_ALIGN = {0: "left", 1: "center", 2: "right", 3: "justify", 4: "distribute"}
_EXT_CONTENT_TYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".emf": "image/x-emf",
    ".wmf": "image/x-wmf",
    ".svg": "image/svg+xml",
}


def _underline_to_bool(u) -> bool | None:
    if u is None:
        return None
    if isinstance(u, bool):
        return u
    # WD_UNDERLINE enum member — treat any explicit non-NONE value as underlined.
    return "NONE" not in str(u).upper()


def _alignment_str(alignment) -> str | None:
    if alignment is None:
        return None
    try:
        return _ALIGN.get(int(alignment))
    except (ValueError, TypeError):
        return None


def run_formatting(run) -> RunFormatting:
    """Extract character formatting for a run (best-effort, never raises)."""
    f = run.font
    color = None
    try:
        if f.color is not None and f.color.rgb is not None:
            color = str(f.color.rgb)
    except (AttributeError, ValueError):
        color = None
    size = None
    try:
        if f.size is not None:
            size = float(f.size.pt)
    except (AttributeError, ValueError):
        size = None
    highlight = None
    try:
        if f.highlight_color is not None:
            highlight = str(f.highlight_color)
    except (AttributeError, ValueError):
        highlight = None
    return RunFormatting(
        font=f.name,
        size=size,
        bold=f.bold,
        italic=f.italic,
        underline=_underline_to_bool(f.underline),
        color=color,
        highlight=highlight,
    )


def extract_runs(paragraph: Paragraph) -> list[Run]:
    return [
        Run(text=r.text, formatting=run_formatting(r))
        for r in paragraph.runs
        if r.text
    ]


def paragraph_formatting(paragraph: Paragraph) -> ParagraphFormatting:
    style_name = None
    try:
        style_name = paragraph.style.name if paragraph.style is not None else None
    except (AttributeError, KeyError):
        style_name = None

    font = size = bold = italic = underline = color = None
    for r in paragraph.runs:
        if r.text and r.text.strip():
            rf = run_formatting(r)
            font, size, bold, italic, underline, color = (
                rf.font, rf.size, rf.bold, rf.italic, rf.underline, rf.color,
            )
            break

    return ParagraphFormatting(
        style_name=style_name,
        alignment=_alignment_str(paragraph.alignment),
        font=font,
        size=size,
        bold=bold,
        italic=italic,
        underline=underline,
        color=color,
        shading=_paragraph_shading(paragraph),
        borders=_paragraph_borders(paragraph),
    )


def _paragraph_shading(paragraph: Paragraph) -> str | None:
    pPr = paragraph._p.find(qn("w:pPr"))
    if pPr is None:
        return None
    shd = pPr.find(qn("w:shd"))
    if shd is None:
        return None
    fill = shd.get(qn("w:fill"))
    return fill if fill and fill.lower() != "auto" else None


def _paragraph_borders(paragraph: Paragraph) -> dict[str, str]:
    pPr = paragraph._p.find(qn("w:pPr"))
    if pPr is None:
        return {}
    pbdr = pPr.find(qn("w:pBdr"))
    if pbdr is None:
        return {}
    out: dict[str, str] = {}
    for side in ("top", "left", "bottom", "right"):
        el = pbdr.find(qn(f"w:{side}"))
        if el is not None:
            val = el.get(qn("w:val"))
            if val:
                out[side] = val
    return out


def paragraph_images(
    paragraph: Paragraph, pkg: DocxPackage, doc_rels: dict[str, RelInfo]
) -> list[ImageRef]:
    """Find embedded images referenced by a paragraph's drawings."""
    p = paragraph._p
    refs: list[ImageRef] = []
    # Inline extent (EMU) — attach to the first image if present.
    ext = p.find(".//" + qn("wp:extent"))
    width_emu = height_emu = None
    if ext is not None:
        try:
            width_emu = int(ext.get("cx")) if ext.get("cx") else None
            height_emu = int(ext.get("cy")) if ext.get("cy") else None
        except (ValueError, TypeError):
            pass

    for blip in p.findall(".//" + qn("a:blip")):
        embed = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
        if not embed:
            continue
        rel = doc_rels.get(embed)
        target = rel.target if rel else None
        fname = posixpath.basename(target) if target else None
        ext_lower = ("." + fname.rsplit(".", 1)[-1].lower()) if fname and "." in fname else ""
        refs.append(
            ImageRef(
                relationship_id=embed,
                file_name=fname,
                hash=pkg.part_hash(target) if target else None,
                width_emu=width_emu,
                height_emu=height_emu,
                content_type=_EXT_CONTENT_TYPE.get(ext_lower),
            )
        )
        # Only the first image gets the extent; reset so subsequent ones are unset.
        width_emu = height_emu = None
    return refs


def extract_table_structure(table: Table) -> TableStructure:
    """Build a normalized grid for a table (text + merge + width metadata)."""
    rows_text: list[list[str]] = []
    merged: list[dict] = []
    seen_cells: set[int] = set()

    for r, row in enumerate(table.rows):
        row_vals: list[str] = []
        for c, cell in enumerate(row.cells):
            row_vals.append(cell.text.strip())
            key = id(cell._tc)
            # Record span info once per physical cell.
            if key not in seen_cells:
                seen_cells.add(key)
                span = _cell_span(cell)
                if span and (span[0] > 1 or span[1] > 1):
                    merged.append({"row": r, "col": c, "rowspan": span[0], "colspan": span[1]})
        rows_text.append(row_vals)

    n_rows = len(rows_text)
    n_cols = max((len(rw) for rw in rows_text), default=0)
    headers = rows_text[0] if rows_text else []
    return TableStructure(
        n_rows=n_rows,
        n_cols=n_cols,
        headers=headers,
        rows=rows_text,
        merged_cells=merged,
        col_widths=_col_widths(table, n_cols),
    )


def _cell_span(cell) -> tuple[int, int] | None:
    """Return (rowspan, colspan) for a cell from gridSpan / vMerge, best-effort."""
    tc = cell._tc
    tcPr = tc.find(qn("w:tcPr"))
    colspan = 1
    rowspan = 1
    if tcPr is not None:
        grid = tcPr.find(qn("w:gridSpan"))
        if grid is not None and grid.get(qn("w:val")):
            try:
                colspan = int(grid.get(qn("w:val")))
            except ValueError:
                colspan = 1
        # vMerge "restart" begins a vertical span; counting its length is costly,
        # so we just flag presence (rowspan>=2) for the review UI.
        vmerge = tcPr.find(qn("w:vMerge"))
        if vmerge is not None and (vmerge.get(qn("w:val")) in (None, "restart")):
            rowspan = 2
    return (rowspan, colspan)


def _col_widths(table: Table, n_cols: int) -> list[int | None]:
    """Read column widths from the tblGrid (twips), padded to n_cols."""
    tbl = table._tbl
    grid = tbl.find(qn("w:tblGrid"))
    widths: list[int | None] = []
    if grid is not None:
        for gcol in grid.findall(qn("w:gridCol")):
            w = gcol.get(qn("w:w"))
            try:
                widths.append(int(w) if w else None)
            except ValueError:
                widths.append(None)
    # Pad/trim to expected column count.
    if len(widths) < n_cols:
        widths += [None] * (n_cols - len(widths))
    return widths[:n_cols] if n_cols else widths
