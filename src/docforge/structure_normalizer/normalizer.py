"""structure_normalizer — build a normalized DocumentExtraction from a .docx.

This is the single entry point used by ingestion. It combines python-docx's
object model (robust paragraph/table/run access) with DocxPackage (media hashes,
relationships, numbering) to produce the renderer-agnostic schema in §7.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from ..common.textutil import semantic_hints
from ..ooxml_extractor.numbering import NumberingResolver
from ..ooxml_extractor.package import DocxPackage
from ..schemas.enums import ElementType
from ..schemas.extraction import (
    DocumentExtraction,
    DocumentSection,
    NormalizedElement,
    NumberingInfo,
)
from .text import (
    extract_runs,
    extract_table_structure,
    paragraph_formatting,
    paragraph_images,
)
from .walk import WalkNode, walk_document


def _safe_xpath(obj) -> str:
    try:
        el = obj._element
        return el.getroottree().getpath(el)
    except Exception:  # pragma: no cover - defensive
        return ""


def _heading_subtype(style_name: str | None) -> str | None:
    if not style_name:
        return None
    s = style_name.lower()
    if s.startswith("heading"):
        digits = "".join(ch for ch in s if ch.isdigit())
        return digits or "1"
    if s in ("title", "subtitle"):
        return s
    return None


def _numbering_info(paragraph: Paragraph, resolver: NumberingResolver) -> NumberingInfo | None:
    pPr = paragraph._p.find(qn("w:pPr"))
    if pPr is None:
        return None
    numPr = pPr.find(qn("w:numPr"))
    if numPr is None:
        return None
    ilvl_el = numPr.find(qn("w:ilvl"))
    numid_el = numPr.find(qn("w:numId"))
    level = 0
    if ilvl_el is not None and ilvl_el.get(qn("w:val")):
        try:
            level = int(ilvl_el.get(qn("w:val")))
        except ValueError:
            level = 0
    num_id = numid_el.get(qn("w:val")) if numid_el is not None else None
    return NumberingInfo(
        num_id=num_id,
        level=level,
        is_ordered=resolver.is_ordered(num_id, level),
    )


class _FieldSpanTracker:
    """Tracks a Word "complex field" (``w:fldChar`` begin/separate/end) across
    the multiple PARAGRAPHS it can span.

    A generated Table of Contents is the common case: Word puts the
    begin/instrText marker in only the FIRST paragraph, then each visible
    entry ("1.1 Agenda & Topics ... 3") is its OWN paragraph with no field
    marker of its own, until a final ``end`` marker closes the field. Checking
    each paragraph in isolation (the previous behaviour) only recognized that
    first line as auto-field/TOC content — every other entry looked like
    ordinary fixed text and was eligible for tags-only to convert into a
    static placeholder, which would break the TOC (Word regenerates its
    content from the real headings whenever fields are updated/printed).
    """

    def __init__(self) -> None:
        self.open = False
        self.is_toc = False

    def reset(self) -> None:
        self.open = False
        self.is_toc = False

    def process(self, paragraph: Paragraph) -> tuple[bool, bool]:
        """Return (has_word_field, is_toc) for ``paragraph``, updating span state."""
        p = paragraph._p
        was_open, was_toc = self.open, self.is_toc

        simple = p.find(".//" + qn("w:fldSimple"))
        instr = p.findall(".//" + qn("w:instrText"))
        fld_chars = p.findall(".//" + qn("w:fldChar"))
        begin = any(fc.get(qn("w:fldCharType")) == "begin" for fc in fld_chars)
        end = any(fc.get(qn("w:fldCharType")) == "end" for fc in fld_chars)

        this_field = simple is not None or bool(instr) or bool(fld_chars)
        this_toc = (simple is not None and "TOC" in (simple.get(qn("w:instr")) or "")) or any(
            it.text and "TOC" in it.text for it in instr
        )

        has_field = this_field or was_open
        is_toc = this_toc or (was_open and was_toc)

        if begin:
            self.open = True
        if this_toc:
            self.is_toc = True
        if end:
            self.open = False
            self.is_toc = False

        return has_field, is_toc


def _build_sections(doc) -> list[DocumentSection]:
    sections: list[DocumentSection] = []
    for i, sec in enumerate(doc.sections):
        def tw(length):
            return int(length.twips) if length is not None else None

        margins = {
            k: v
            for k, v in {
                "top": tw(sec.top_margin),
                "bottom": tw(sec.bottom_margin),
                "left": tw(sec.left_margin),
                "right": tw(sec.right_margin),
                "header": tw(sec.header_distance),
                "footer": tw(sec.footer_distance),
            }.items()
            if v is not None
        }
        sections.append(
            DocumentSection(
                section_index=i,
                page_width=tw(sec.page_width),
                page_height=tw(sec.page_height),
                margins=margins,
            )
        )
    return sections


def _element_from_walknode(
    wn: WalkNode,
    pkg: DocxPackage,
    doc_rels,
    resolver: NumberingResolver,
    field_tracker: _FieldSpanTracker,
) -> NormalizedElement:
    if wn.kind == "table":
        field_tracker.reset()  # a field never legitimately spans into/out of a table
        table = wn.obj
        ts = extract_table_structure(table)
        subtype = f"r{wn.row}c{wn.col}" if wn.row is not None else None
        return NormalizedElement(
            node_id=wn.node_id,
            parent_node_id=wn.parent_node_id,
            xpath=_safe_xpath(table),
            type=ElementType.TABLE,
            subtype=subtype,
            text="",
            table_structure=ts,
            position_index=wn.position_index,
            header_footer_scope=wn.scope,
            section_index=wn.section_index,
            semantic_hints=["table"],
        )

    # paragraph
    para: Paragraph = wn.obj
    text = para.text or ""
    fmt = paragraph_formatting(para)
    images = paragraph_images(para, pkg, doc_rels)
    numbering = _numbering_info(para, resolver)
    has_field, is_toc = field_tracker.process(para)

    # Decide element type.
    heading_sub = _heading_subtype(fmt.style_name)
    if heading_sub is not None:
        etype = ElementType.HEADING
        subtype = heading_sub
    elif numbering is not None:
        etype = ElementType.LIST_ITEM
        subtype = "ordered" if numbering.is_ordered else "bullet"
    elif images and not text.strip():
        etype = ElementType.IMAGE
        subtype = None
    elif has_field and not text.strip():
        etype = ElementType.FIELD
        subtype = "toc" if is_toc else "field"
    else:
        etype = ElementType.PARAGRAPH
        subtype = f"r{wn.row}c{wn.col}" if wn.row is not None else None

    hints = semantic_hints(text)
    if images:
        hints.append("image")
    if has_field:
        hints.append("auto_field")
    if is_toc:
        hints.append("toc")
    if wn.row is not None:
        hints.append("in_table")

    bookmarks, link_target = _links(para)

    return NormalizedElement(
        node_id=wn.node_id,
        parent_node_id=wn.parent_node_id,
        xpath=_safe_xpath(para),
        type=etype,
        subtype=subtype,
        style_name=fmt.style_name,
        text=text,
        runs=extract_runs(para),
        image_ref=images[0] if images else None,
        numbering_info=numbering,
        position_index=wn.position_index,
        header_footer_scope=wn.scope,
        section_index=wn.section_index,
        formatting=fmt,
        semantic_hints=hints,
        bookmarks=bookmarks,
        link_target=link_target,
    )


_PAGEREF_RE = re.compile(r"PAGEREF\s+(\S+)", re.IGNORECASE)


def _links(para: Paragraph) -> tuple[list[str], str | None]:
    """The bookmarks in a paragraph, and the bookmark it links to (if any).

    A contents entry links to its heading twice over: a hyperlink to the
    heading's bookmark, and a PAGEREF field for its page number.
    """
    p = para._p
    names = [
        name
        for b in p.iter(qn("w:bookmarkStart"))
        if (name := b.get(qn("w:name"))) and name != "_GoBack"
    ]
    target = None
    for h in p.iter(qn("w:hyperlink")):
        if anchor := h.get(qn("w:anchor")):
            target = anchor
            break
    if target is None:
        instr = "".join(i.text or "" for i in p.iter(qn("w:instrText")))
        if m := _PAGEREF_RE.search(instr):
            target = m.group(1)
    return names, target


def build_extraction(
    file_path: str | Path,
    document_id: str,
    filename: str | None = None,
) -> DocumentExtraction:
    """Parse a .docx file into the normalized DocumentExtraction schema."""
    file_path = Path(file_path)
    filename = filename or file_path.name

    pkg = DocxPackage.from_path(file_path)
    doc_part = pkg.main_document_name()
    doc_rels = pkg.rels_for(doc_part)
    resolver = NumberingResolver.from_package(pkg, doc_part)

    doc = Document(str(file_path))
    nodes = walk_document(doc)

    # One field-span tracker per (scope, parent_node_id, cell) context, so an
    # open field never leaks across a header/footer/table-cell boundary.
    trackers: dict[tuple, _FieldSpanTracker] = {}
    elements: list[NormalizedElement] = []
    for wn in nodes:
        key = (wn.scope, wn.parent_node_id, wn.row, wn.col)
        tracker = trackers.setdefault(key, _FieldSpanTracker())
        elements.append(_element_from_walknode(wn, pkg, doc_rels, resolver, tracker))
    sections = _build_sections(doc)

    # Stable content hash over (type, text) — used to detect duplicate uploads.
    hasher = hashlib.sha256()
    for el in elements:
        hasher.update(f"{el.type.value}:{el.text}\n".encode("utf-8", "ignore"))
    content_hash = hasher.hexdigest()

    return DocumentExtraction(
        document_id=document_id,
        filename=filename,
        page_count=pkg.page_count(),
        content_hash=content_hash,
        sections=sections,
        elements=elements,
    )
