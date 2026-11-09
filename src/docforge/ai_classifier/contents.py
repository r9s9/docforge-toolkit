"""A table of contents follows the headings it lists.

Each entry of a Word table of contents is the text of a heading, copied there
by Word and linked back to it (a hyperlink to the bookmark Word put on the
heading, plus a PAGEREF field for its page number). In a template, an entry is
therefore not content of its own: it is whatever its heading becomes. So an
entry is linked to its heading here and never becomes a field; the template
builder gives it the heading's tag ({{ agenda_title }} in both places), and a
generated document's contents list reads like its headings.

Matching is by the link Word wrote. An entry without one (a contents list typed
by hand, or saved without hyperlinks) is matched by its text, in document
order. A list is linked only when every entry in it finds its heading: a half
linked list would split one Word field between two treatments.
"""

from __future__ import annotations

import re

from ..schemas.classification import ClassificationResult, ElementClassification
from ..schemas.enums import ClassificationType, ElementType
from ..schemas.extraction import DocumentExtraction, NormalizedElement

_CONTENTS_STYLES = ("toc ",)  # "toc 1" … "toc 9" (python-docx style names)
_NUMBER_RE = re.compile(r"^(?:[\dA-Za-z]{1,3}[.)]?)(?:[.\d]+)?\.?$")
_PAGE_RE = re.compile(r"^(?:\d+|[ivxlcdm]+)$", re.IGNORECASE)


def is_contents_entry(e: NormalizedElement) -> bool:
    """A line of a table of contents (not a list of figures or an index)."""
    style = (e.style_name or "").lower()
    if e.header_footer_scope is not None or e.parent_node_id is not None:
        return False
    return style.startswith(_CONTENTS_STYLES) or style == "toc" or (
        "toc" in e.semantic_hints and bool(e.link_target)
    )


def is_entry_number(text: str) -> bool:
    """Whether a contents entry's leading text is its number ("1.1", "A.", "IV")."""
    return bool(_NUMBER_RE.match((text or "").strip()))


def entry_title(text: str) -> str:
    """The heading text inside a contents entry: "1.1\tAgenda\t3" -> "Agenda"."""
    parts = [p.strip() for p in (text or "").split("\t") if p.strip()]
    if len(parts) > 1 and _PAGE_RE.match(parts[-1]):
        parts = parts[:-1]
    if len(parts) > 1 and is_entry_number(parts[0]):
        parts = parts[1:]
    return " ".join(parts)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip().lower()


def _blocks(elements: list[NormalizedElement]) -> list[list[NormalizedElement]]:
    """Consecutive runs of contents entries: one table of contents each."""
    blocks: list[list[NormalizedElement]] = []
    current: list[NormalizedElement] = []
    for e in elements:
        if is_contents_entry(e):
            current.append(e)
        elif current and not (e.text or "").strip():
            continue  # a blank line inside the list does not end it
        else:
            if current:
                blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _resolve(
    block: list[NormalizedElement],
    by_bookmark: dict[str, NormalizedElement],
    headings: list[NormalizedElement],
) -> dict[str, str] | None:
    """entry node -> heading node for a whole block, or None if any is unmatched."""
    links: dict[str, str] = {}
    cursor = 0  # text matches advance in document order: one heading per entry
    for e in block:
        target = by_bookmark.get(e.link_target or "")
        if target is None:
            title = _norm(entry_title(e.text))
            if not title:
                return None
            for i in range(cursor, len(headings)):
                text = _norm(headings[i].text)
                if text == title or text.startswith(title) or title.startswith(text):
                    target, cursor = headings[i], i + 1
                    break
        if target is None or target.node_id == e.node_id:
            return None
        links[e.node_id] = target.node_id
    return links


def link_contents_entries(extraction: DocumentExtraction, result: ClassificationResult) -> set[str]:
    """Link every resolvable contents entry to its heading; return their node ids.

    Linked entries become AUTO_FIELD (content Word derives) with ``links_to``
    set, and lose any field the classifier gave them.
    """
    top = extraction.top_level_elements()
    body = [e for e in top if e.header_footer_scope is None]
    by_bookmark = {b: e for e in body for b in e.bookmarks}
    headings = [e for e in body if e.type == ElementType.HEADING and not is_contents_entry(e)]
    cls_by_node = {c.node_id: c for c in result.classifications}

    linked: set[str] = set()
    for block in _blocks(body):
        links = _resolve(block, by_bookmark, headings)
        if not links:
            continue
        for node_id, target in links.items():
            c = cls_by_node.get(node_id)
            if c is None:
                c = ElementClassification(node_id=node_id, source=result.source or "heuristic")
                result.classifications.append(c)
                cls_by_node[node_id] = c
            c.classification = ClassificationType.AUTO_FIELD
            c.field_name = None
            c.field_type = None
            c.required = False
            c.optional = False
            c.static_prefix = c.static_suffix = None
            c.links_to = target
            c.rationale = "Table of contents entry: follows its heading."
            linked.add(node_id)
    return linked
