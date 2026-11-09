"""In-place compliance fix: put a document's boilerplate back in line with its template.

Adapted from upstream ``services/compliance_fix.py``. It repairs FIXED
(boilerplate) text only: changed paragraphs get the template's wording back,
and removed ones are re-inserted near where they belong. Field values, extra
content and formatting are left exactly as they are.
"""

from __future__ import annotations

import os
import tempfile
from io import BytesIO

from ..common.textutil import similarity
from ..multi_doc_differ import align_to_representative
from ..schemas.classification import ElementClassification
from ..schemas.enums import ClassificationType
from ..schemas.extraction import DocumentExtraction
from ..structure_normalizer import build_extraction, walk_document

FIXED_MATCH_THRESHOLD = 0.9


def _set_paragraph_text(paragraph, text: str) -> None:
    """Replace a paragraph's text, keeping the first run's formatting."""
    runs = paragraph.runs
    if runs:
        runs[0].text = text
        for r in runs[1:]:
            r._element.getparent().remove(r._element)
    else:
        paragraph.add_run(text)


def _insert_after(anchor, text: str):
    """Insert a new paragraph carrying ``text`` immediately after ``anchor``."""
    from docx.oxml import OxmlElement
    from docx.text.paragraph import Paragraph

    new_p = OxmlElement("w:p")
    anchor._p.addnext(new_p)
    para = Paragraph(new_p, anchor._parent)
    para.add_run(text)
    return para


def fix_against(
    rep: DocumentExtraction,
    classifications: list[ElementClassification],
    data: bytes,
    filename: str = "document.docx",
    *,
    strict: bool = False,
) -> tuple[bytes, int]:
    """Return (corrected .docx bytes, number of fixes applied).

    By default a boilerplate paragraph is restored only when it is less than
    90% similar to the template, matching upstream. ``strict`` restores any
    wording change, for small edits judged material ("30 days" to "90 days").
    """
    from docx import Document

    cls_by_node = {c.node_id: c for c in classifications}

    # Extract for alignment, and load the live document for patching.
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        extraction = build_extraction(tmp.name, document_id="fix", filename=filename)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    pydoc = Document(BytesIO(data))
    aligned = align_to_representative(rep, extraction)
    walk = {n.node_id: n for n in walk_document(pydoc)}

    n_fixed = 0
    last_anchor = None  # nearest preceding mapped paragraph (for inserts)

    for node in rep.top_level_elements():
        c = cls_by_node.get(node.node_id)
        doc_el = aligned.get(node.node_id)

        # Track the last paragraph that maps to a template element, so a
        # missing boilerplate paragraph can be re-inserted in the right place.
        if doc_el is not None:
            wn = walk.get(doc_el.node_id)
            if wn is not None and wn.kind == "paragraph":
                last_anchor = wn.obj

        if c is None or c.classification != ClassificationType.FIXED:
            continue
        expected = node.text.strip()
        if not expected:
            continue

        if doc_el is None:
            # Boilerplate paragraph removed from the document: put it back.
            if last_anchor is not None:
                last_anchor = _insert_after(last_anchor, expected)
                n_fixed += 1
            continue

        wn = walk.get(doc_el.node_id)
        if wn is None or wn.kind != "paragraph":
            continue
        para = wn.obj
        current = (para.text or "").strip()
        if strict:
            changed = " ".join(current.split()) != " ".join(expected.split())
        else:
            changed = similarity(current, expected) < FIXED_MATCH_THRESHOLD
        if changed:
            _set_paragraph_text(para, expected)
            n_fixed += 1

    out = BytesIO()
    pydoc.save(out)
    return out.getvalue(), n_fixed
