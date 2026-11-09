"""Expand rich field values into real Word paragraphs after rendering.

docxtpl can only put a value *inside a run*, so a multi-paragraph value has
nowhere to go — newlines collapse and the document reads as one block. Instead
of fighting the template engine, :mod:`docforge.assembler.assembler` renders a
short **sentinel** token in place of each rich value and this module walks the
finished document replacing every sentinel with the paragraphs it stands for.

Doing it here, on the rendered output, has two properties that matter:

* **No template change.** Every already-published template keeps working — the
  placeholder is still a plain ``{{ field }}``, so nothing needs republishing.
* **The design is inherited exactly.** Each generated paragraph copies the host
  paragraph's ``w:pPr`` and each run copies the placeholder run's ``w:rPr``, so
  spacing, font, size and colour come from the template, not from us. Emphasis
  is only ever *added* (never switched off), so a template that styles a block
  bold stays bold.
"""

from __future__ import annotations

import copy
import logging
import zipfile
from io import BytesIO

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Twips
from docx.text.paragraph import Paragraph

from ..structure_normalizer import walk_document
from .richtext import RichBlock, RichSpan

logger = logging.getLogger("docforge.assembler.postprocess")

# Private-use codepoints: valid XML, never present in real content, and left
# untouched by Jinja's autoescaping (they are not &, < or >).
_SENTINEL_OPEN = ""
_SENTINEL_CLOSE = ""

# Quarter-inch per list level, matching Word's default list indents.
_INDENT_STEP = 360  # twips

_SETTINGS_PART = "word/settings.xml"


def sentinel(index: int) -> str:
    """The token rendered in place of the ``index``-th rich value."""
    return f"{_SENTINEL_OPEN}DFRICH{index}{_SENTINEL_CLOSE}"


# --- low-level element builders ---------------------------------------------


def _make_run(rPr, span: RichSpan):
    """A ``w:r`` carrying ``span``'s text, cloning the placeholder's formatting."""
    run = OxmlElement("w:r")
    new_rPr = copy.deepcopy(rPr) if rPr is not None else None
    if new_rPr is None and (span.bold or span.italic):
        new_rPr = OxmlElement("w:rPr")
    if new_rPr is not None:
        # Only ever add emphasis: forcing bold off here would strip styling the
        # template itself applied to this run.
        if span.bold:
            new_rPr.get_or_add_b().val = True
        if span.italic:
            new_rPr.get_or_add_i().val = True
        run.append(new_rPr)
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = span.text
    run.append(t)
    return run


def _resolve_bullet_num_id(doc) -> str | None:
    """A numId from the package whose level 0 is a bullet, if one exists.

    Reusing a definition the template already carries means bullets pick up the
    document's own glyph and indents. Only bullets are reused: a *numbered*
    definition keeps a running counter, so borrowing one would make a list start
    at whatever number the template's own list had reached.
    """
    try:
        numbering = doc.part.numbering_part.element
    except (NotImplementedError, KeyError, ValueError, AttributeError):
        return None
    fmt_by_abstract: dict[str, str] = {}
    for abstract in numbering.findall(qn("w:abstractNum")):
        aid = abstract.get(qn("w:abstractNumId"))
        for lvl in abstract.findall(qn("w:lvl")):
            if lvl.get(qn("w:ilvl")) != "0":
                continue
            num_fmt = lvl.find(qn("w:numFmt"))
            if num_fmt is not None and aid is not None:
                fmt_by_abstract[aid] = num_fmt.get(qn("w:val")) or ""
            break
    for num in numbering.findall(qn("w:num")):
        ref = num.find(qn("w:abstractNumId"))
        if ref is None:
            continue
        if fmt_by_abstract.get(ref.get(qn("w:val")) or "") == "bullet":
            return num.get(qn("w:numId"))
    return None


def _apply_list_format(p_el, block: RichBlock, bullet_num_id: str | None) -> bool:
    """Style ``p_el`` as a list item. Returns True when Word numbering was used."""
    pPr = p_el.get_or_add_pPr()
    for existing in pPr.findall(qn("w:numPr")):  # drop inherited list membership
        pPr.remove(existing)
    if block.kind == "bullet" and bullet_num_id:
        num_pr = pPr.get_or_add_numPr()
        num_pr.get_or_add_ilvl().val = block.level
        num_pr.get_or_add_numId().val = int(bullet_num_id)
        return True
    # No usable definition (or a numbered item, which must keep the numbers the
    # author actually wrote): indent it and carry the marker as literal text.
    ind = pPr.get_or_add_ind()
    ind.left = Twips(_INDENT_STEP * (block.level + 1))
    ind.hanging = Twips(_INDENT_STEP)
    return False


def _block_run_elements(block: RichBlock, rPr, *, numbered: bool) -> list:
    """Runs for one block, prefixing a literal marker when Word can't number it."""
    spans = list(block.spans)
    if block.kind != "paragraph" and not numbered and block.marker:
        spans = [RichSpan(f"{block.marker} ")] + spans
    return [_make_run(rPr, s) for s in spans if s.text]


def _new_paragraph(host_p_el, block: RichBlock, rPr, bullet_num_id: str | None):
    """Build a sibling paragraph for ``block``, inheriting the host's ``w:pPr``."""
    p_el = OxmlElement("w:p")
    host_pPr = host_p_el.find(qn("w:pPr"))
    if host_pPr is not None:
        pPr = copy.deepcopy(host_pPr)
        # A section break belongs to exactly one paragraph — copying it would
        # duplicate the page/section it ends.
        for sect in pPr.findall(qn("w:sectPr")):
            pPr.remove(sect)
        p_el.append(pPr)
    numbered = False
    if block.kind != "paragraph":
        numbered = _apply_list_format(p_el, block, bullet_num_id)
    for run_el in _block_run_elements(block, rPr, numbered=numbered):
        p_el.append(run_el)
    return p_el


# --- sentinel expansion ------------------------------------------------------


def _find_in_run(paragraph: Paragraph, token: str):
    """The run containing ``token`` and its offset within that run."""
    for run in paragraph.runs:
        idx = (run.text or "").find(token)
        if idx != -1:
            return run, idx
    return None, -1


def _expand(
    paragraph: Paragraph,
    token: str,
    blocks: list[RichBlock],
    anchor_el,
    bullet_num_id: str | None,
):
    """Replace ``token`` with ``blocks``; returns the element to insert after next."""
    run, offset = _find_in_run(paragraph, token)
    if run is None:
        return anchor_el
    rPr = run._element.find(qn("w:rPr"))
    text = run.text or ""
    prefix, suffix = text[:offset], text[offset + len(token) :]
    first = blocks[0] if blocks else RichBlock()

    # The first block continues the host paragraph, so a static label written by
    # the template ("Summary: ") keeps its value on the same line. A list marker
    # is only added when nothing precedes the value on that line.
    numbered = False
    if first.kind != "paragraph" and not prefix:
        numbered = _apply_list_format(paragraph._p, first, bullet_num_id)
    new_runs = _block_run_elements(first, rPr, numbered=numbered or bool(prefix))
    if suffix:
        new_runs.append(_make_run(rPr, RichSpan(suffix)))

    run.text = prefix
    last_el = run._element
    for run_el in new_runs:
        last_el.addnext(run_el)
        last_el = run_el
    if not prefix:
        run._element.getparent().remove(run._element)

    # Remaining blocks become siblings, in order, after whatever we last placed.
    if anchor_el is None:
        anchor_el = paragraph._p
    for block in blocks[1:]:
        p_el = _new_paragraph(paragraph._p, block, rPr, bullet_num_id)
        anchor_el.addnext(p_el)
        anchor_el = p_el
    return anchor_el


def _flatten(paragraph: Paragraph, token: str, blocks: list[RichBlock]) -> None:
    """Last-resort: drop the token, keeping the text, when it spans runs."""
    plain = "\n".join(b.text for b in blocks)
    for run in paragraph.runs:
        if token in (run.text or ""):
            run.text = (run.text or "").replace(token, plain)
            return
    logger.warning("rich sentinel %r vanished before expansion", token)


def request_field_update(docx_bytes: bytes) -> bytes:
    """Ask Word to rebuild its fields when the document is opened.

    A table of contents, a list of figures and page references are Word *fields*:
    the template carries the field code plus the text it last produced. Filling
    the template changes the headings but not that cached text, so a generated
    document would open showing the example's contents — the right mechanism
    pointing at the wrong document. Setting ``w:updateFields`` makes Word
    refresh them on open, which is the only place that can recompute page
    numbers anyway.
    """
    try:
        with zipfile.ZipFile(BytesIO(docx_bytes)) as source:
            names = source.namelist()
            if _SETTINGS_PART not in names:
                return docx_bytes
            settings = source.read(_SETTINGS_PART).decode("utf-8")
            if "updateFields" in settings:
                return docx_bytes
            opening = settings.find("<w:settings")
            marker = settings.find(">", opening) if opening != -1 else -1
            if marker == -1:
                return docx_bytes
            patched = (
                settings[: marker + 1]
                + '<w:updateFields w:val="true"/>'
                + settings[marker + 1 :]
            )
            out = BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dest:
                for item in source.infolist():
                    data = patched.encode("utf-8") if item.filename == _SETTINGS_PART else source.read(item.filename)
                    dest.writestr(item, data)
            return out.getvalue()
    except Exception:  # never fail a generation over a convenience flag
        logger.debug("could not set updateFields", exc_info=True)
        return docx_bytes


def apply_rich_values(docx_bytes: bytes, rich_map: dict[str, list[RichBlock]]) -> bytes:
    """Replace every sentinel in ``docx_bytes`` with its rendered blocks."""
    if not rich_map:
        return docx_bytes
    doc = Document(BytesIO(docx_bytes))
    bullet_num_id = _resolve_bullet_num_id(doc)
    for node in walk_document(doc):
        if node.kind != "paragraph":
            continue
        paragraph: Paragraph = node.obj  # type: ignore[assignment]
        anchor_el = paragraph._p
        # A paragraph normally holds one placeholder; the loop keeps ordering
        # correct in the rare case a template puts two rich fields on one line.
        for _ in range(len(rich_map)):
            text = paragraph.text or ""
            token = next((t for t in rich_map if t in text), None)
            if token is None:
                break
            found, _offset = _find_in_run(paragraph, token)
            if found is None:  # split across runs — should not happen via docxtpl
                _flatten(paragraph, token, rich_map[token])
                break
            anchor_el = _expand(paragraph, token, rich_map[token], anchor_el, bullet_num_id)
    out = BytesIO()
    doc.save(out)
    return out.getvalue()
