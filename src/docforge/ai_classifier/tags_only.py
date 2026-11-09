"""Tags-only mode: guarantee that EVERY text element becomes a fillable field.

In tags-only mode the published template must contain only placeholders — no
original prose survives. The AI passes are instructed to classify everything as
dynamic with meaningful names, but this deterministic post-pass is the safety
net that enforces the invariant regardless of which engine produced the result
(LLM, heuristic, or the heuristic fallback after an AI failure).

Grouping: one field per heading and per table; a run of >= 2 consecutive body
paragraphs / list items under one heading collapses into ONE repeatable-section
field (all nodes share the field name), so an 80-paragraph document yields a
handful of section fields instead of 80 one-off fields.
"""

from __future__ import annotations

import re

from ..common.textutil import short_field_name, value_kind
from ..schemas.classification import ClassificationResult, ElementClassification
from ..schemas.enums import ClassificationType, ElementType, FieldType, is_dynamic, needs_field
from ..schemas.extraction import DocumentExtraction, NormalizedElement
from .heuristic import holds_picture, looks_like_data_table, paragraph_look

# Body text longer than this becomes a multiline field rather than a one-liner.
_MULTILINE_THRESHOLD = 120

_GROUPABLE = (ElementType.PARAGRAPH, ElementType.LIST_ITEM)


def _unique(base: str, used: set[str]) -> str:
    name = base or "field"
    if name not in used:
        return name
    i = 2
    while f"{name}_{i}" in used:
        i += 1
    return f"{name}_{i}"


def _suffixed(where: str, kind: str) -> str:
    """A name for one kind of element in a place: "key_findings_title".

    With nowhere to name it after (content before the first heading), the kind
    alone: "body", not "body_body".
    """
    base = short_field_name(where or "", fallback="")
    return f"{base}_{kind}" if base and base != kind else kind


def _synth_description(section: str, text: str) -> str:
    sample = " ".join(text.split())
    if len(sample) > 120:
        sample = sample[:120].rstrip() + "…"
    where = f'the "{section}" section' if section else "the document"
    return f"Content for {where}. Original example: “{sample}”"


# Short labels that name a part of the document rather than say anything in it.
# They belong to the template's furniture: replacing "TABLE OF CONTENTS" with a
# sentence is never what anyone wanted.
_STRUCTURAL_LABELS = frozenset({
    "table of contents", "contents", "revisions", "revision history", "tables",
    "figures", "appendices", "appendix", "references", "bibliography", "index",
    "glossary", "abbreviations", "definitions", "annexes", "annex",
    "list of tables", "list of figures", "table of figures", "document history",
})
_STRUCTURAL_STYLE_HINTS = ("contents", "toc", "table of figures", "index")
_MAX_LABEL_CHARS = 24


def _is_structural_label(e: NormalizedElement) -> bool:
    """A section label that is part of the document's furniture, not its content."""
    text = " ".join((e.text or "").split())
    if not text or len(text) > _MAX_LABEL_CHARS:
        return False
    if text.strip(":").lower() in _STRUCTURAL_LABELS:
        return True
    style = (e.style_name or "").lower()
    # A very short label in a contents/TOC style is furniture even if we don't
    # know the word — corporate templates invent their own ("VERTEILER").
    return any(h in style for h in _STRUCTURAL_STYLE_HINTS) and len(text.split()) <= 3


# Styles whose whole paragraph is written by Word from the document itself — a
# table of contents, a list of figures, an index. Tagging these is pointless:
# Word rebuilds them on open and overwrites whatever we put there.
_GENERATED_STYLES = ("toc", "table of figures", "index ", "table of authorities")


def is_generated_listing(e: NormalizedElement) -> bool:
    """A paragraph Word writes for itself: a contents entry, a list of figures.

    These are still tagged — full-template mode tags everything — but the
    builder has to strip the field that produced them first, or Word would
    rebuild the list when the document opens and overwrite the tag with it.
    """
    style = (e.style_name or "").lower()
    if style.startswith(_GENERATED_STYLES):
        return True
    if style.startswith("caption"):
        # A caption carries an auto number, but the words after it are the
        # author's — the number is kept and the text becomes a field.
        return False
    return "toc" in e.semantic_hints or "auto_field" in e.semantic_hints


def _is_exempt(e: NormalizedElement, c: ElementClassification | None) -> bool:
    """Nodes that keep their existing classification in tags-only mode.

    Only text no person wrote is exempt: a picture with no words of its own,
    and a table of contents entry linked to its heading (it takes that
    heading's tag; see ``contents.py``). Everything else — including
    the running header, the footer and every label — becomes a field, because
    "full template" has to mean it.
    """
    if (e.image_ref is not None or e.type == ElementType.IMAGE) and not (e.text or "").strip():
        return True  # a picture alone is swapped via its image field, not tagged

    if c is not None and c.links_to:
        return True  # a contents entry: it takes its heading's tag, not its own

    if not (e.text or "").strip() and e.type != ElementType.TABLE:
        return True  # nothing to templatize
    return False


STRUCTURAL_MARK = "[structural]"
GENERATED_MARK = "[generated]"


def _mark_generated(c: ElementClassification) -> None:
    """Flag a field that replaces Word-generated content.

    The builder strips the field code that produced it, so the tag becomes the
    real content instead of something Word overwrites on the next update.
    """
    if GENERATED_MARK not in (c.rationale or ""):
        c.rationale = f"{(c.rationale or '').rstrip()} {GENERATED_MARK}".strip()


def _mark_structural(c: ElementClassification) -> None:
    """Flag a field as a document label so it can default to its own wording."""
    if STRUCTURAL_MARK not in (c.rationale or ""):
        c.rationale = f"{(c.rationale or '').rstrip()} {STRUCTURAL_MARK}".strip()
    c.required = False  # it already has a value; nobody must be made to retype it


def _is_data_table(e: NormalizedElement) -> bool:
    """Whether a table repeats rows of the same kind, or is page layout."""
    return looks_like_data_table(e.table_structure)


def _cell_name(text: str) -> str:
    """Name a layout cell after what it *is*, not after the example's value.

    A cell reading "Document number" names itself; one reading "01.01.2001"
    would give "f_01_01_2001", which tells a later reader nothing. For those,
    the kind of value is the more useful name.
    """
    kind = value_kind(text)
    if kind != "text":
        return kind  # date / number / person — _unique adds the suffix
    return short_field_name(text, fallback="cell", max_words=2)


def _field_table_cells(
    extraction: DocumentExtraction,
    result: ClassificationResult,
    table: NormalizedElement,
    cls_by_node: dict[str, ElementClassification],
    used: set[str],
    section: str,
    only_row: int | None = None,
) -> set[str]:
    """Turn each non-empty cell of a table into its own field.

    ``only_row`` restricts this to one row — used for a data table, where the
    loop owns every row but the header.
    """
    forced: set[str] = set()
    # A cell is named after itself ("status"); only a name another field
    # already took gets the section's first word in front ("rating_status").
    stem = short_field_name(section or table.text or "cell", fallback="cell", max_words=1)
    for e in extraction.elements:
        if e.parent_node_id != table.node_id:
            continue
        if only_row is not None and _cell_row(e) != only_row:
            continue
        text = (e.text or "").strip()
        if not text:
            continue  # spacing, or a cell holding only a picture
        c = cls_by_node.get(e.node_id)
        if c is None:
            c = ElementClassification(node_id=e.node_id, source=result.source or "heuristic")
            result.classifications.append(c)
            cls_by_node[e.node_id] = c
        if c.field_name and is_dynamic(c.classification):
            # The model already named this cell: keep its name, but every text
            # is the tag's alone here, as for paragraphs.
            c.static_prefix = None
            c.static_suffix = None
            continue
        base = _cell_name(text)
        name = base if base not in used else _unique(f"{stem}_{base}", used)
        used.add(name)
        _force_dynamic(
            c,
            classification=ClassificationType.DYNAMIC_TEXT,
            field_type=(
                FieldType.MULTILINE_TEXT if len(text) > _MULTILINE_THRESHOLD else FieldType.TEXT
            ),
            name=name,
            section=section,
            text=text,
        )
        if only_row == 0:
            # Column headings label the rows below them. Editable, but they keep
            # their own wording so a table never loses its headings.
            _mark_structural(c)
        forced.add(e.node_id)
    return forced


def _page_area(e: NormalizedElement) -> str | None:
    """"header" or "footer" for text in a page header/footer, else None (body)."""
    scope = e.header_footer_scope or ""
    if scope.startswith("header"):
        return "header"
    if scope.startswith("footer"):
        return "footer"
    return None


def _cell_row(e: NormalizedElement) -> int | None:
    """Row index of a table-cell paragraph (the normalizer stores 'r0c1')."""
    match = re.match(r"r(\d+)c\d+", e.subtype or "")
    return int(match.group(1)) if match else None


def _force_dynamic(
    c: ElementClassification,
    *,
    classification: ClassificationType,
    field_type: FieldType,
    name: str,
    section: str,
    text: str,
) -> None:
    c.classification = classification
    c.field_name = name
    c.field_type = field_type
    c.required = True
    c.optional = False
    c.static_prefix = None
    c.static_suffix = None
    if not (c.description or "").strip():
        c.description = _synth_description(section, text)
    _mark_tags_only(c)


def _mark_tags_only(c: ElementClassification) -> None:
    """Tag a classification as tags-only-touched (idempotent).

    Used downstream by the description-writing AI pass (to find nodes that
    still need a real, content-grounded blurb).
    """
    if "[tags_only]" not in (c.rationale or ""):
        c.rationale = f"{(c.rationale or '').rstrip()} [tags_only]".strip()


def enforce_tags_only(extraction: DocumentExtraction, result: ClassificationResult) -> set[str]:
    """Mutate ``result`` so every non-exempt text node is a dynamic field.

    Returns the set of node_ids that were force-fielded by this pass (i.e. the
    AI/heuristic engine had left them FIXED/UNKNOWN) — the caller can use this
    to target a follow-up AI description pass at exactly the fields that never
    got the model's attention.
    """
    cls_by_node = {c.node_id: c for c in result.classifications}
    forced: set[str] = set()

    # Ensure every top-level node has a classification entry to mutate.
    for e in extraction.top_level_elements():
        if e.node_id not in cls_by_node:
            c = ElementClassification(node_id=e.node_id, source=result.source or "heuristic")
            result.classifications.append(c)
            cls_by_node[e.node_id] = c

    used: set[str] = {
        c.field_name for c in result.classifications if c.field_name
    }

    # Walk in document order, tracking the current section title and grouping
    # consecutive groupable body nodes.
    section = ""
    pending: list[tuple[NormalizedElement, ElementClassification]] = []
    # Whether the run being collected is Word-generated content (a contents
    # list). Mixing those with ordinary prose would put both in one field.
    pending_generated = [False]

    def flush_pending() -> None:
        """Assign the buffered paragraph/list run to field(s)."""
        nonlocal pending
        if not pending:
            return
        run = pending
        pending = []
        # Nodes the AI already fielded keep their own names; only consecutive
        # *unfielded* nodes are grouped. Split the run accordingly.
        unfielded: list[tuple[NormalizedElement, ElementClassification]] = []

        def flush_unfielded() -> None:
            nonlocal unfielded
            chunk = unfielded
            unfielded = []
            if not chunk:
                return
            if len(chunk) >= 2:
                # One repeatable section: every node shares the field name.
                name = _unique(_suffixed(section, "body"), used)
                used.add(name)
                joined = " ".join(e.text.strip() for e, _ in chunk[:3])
                for _e, c in chunk:
                    _force_dynamic(
                        c,
                        classification=ClassificationType.REPEATABLE_SECTION,
                        field_type=FieldType.MULTILINE_TEXT,
                        name=name,
                        section=section,
                        text=joined,
                    )
                    forced.add(_e.node_id)
            else:
                e, c = chunk[0]
                txt = e.text.strip()
                ftype = (
                    FieldType.MULTILINE_TEXT
                    if len(txt) > _MULTILINE_THRESHOLD
                    else FieldType.TEXT
                )
                name = _unique(_suffixed(section or txt, "body"), used)
                used.add(name)
                _force_dynamic(
                    c,
                    classification=ClassificationType.DYNAMIC_TEXT,
                    field_type=ftype,
                    name=name,
                    section=section,
                    text=txt,
                )
                forced.add(e.node_id)

        for e, c in run:
            if c.field_name and (is_dynamic(c.classification) or c.classification == ClassificationType.REPEATABLE_SECTION):
                flush_unfielded()
                # Already a named dynamic field (AI-directed) — just enforce the
                # no-literal-text invariant.
                c.static_prefix = None
                c.static_suffix = None
                c.required = True
                c.optional = False
                if not (c.description or "").strip():
                    c.description = _synth_description(section, e.text)
                _mark_tags_only(c)
            elif holds_picture(e) or (unfielded and paragraph_look(unfielded[0][0]) != paragraph_look(e)):
                # A repeated paragraph is written with the first one's look, so
                # only paragraphs that look alike share a field: a cover's title
                # and subtitle, or an intro above body text, each keep their own.
                # And a picture in a repeated paragraph would repeat (or vanish)
                # with it, so a paragraph holding one stands alone.
                flush_unfielded()
                unfielded.append((e, c))
                if holds_picture(e):
                    flush_unfielded()
            else:
                unfielded.append((e, c))
        flush_unfielded()

    # Header and footer text is named for where it sits, not after whichever
    # section the body walk happened to end in ("certification_sign_off_3").
    body_section: str | None = None

    for e in extraction.top_level_elements():
        c = cls_by_node[e.node_id]
        area = _page_area(e)
        if area and body_section is None:
            flush_pending()
            body_section, section = section, area
        elif area and section != area:
            flush_pending()
            section = area
        elif not area and body_section is not None:
            flush_pending()
            section, body_section = body_section, None

        if _is_exempt(e, c):
            flush_pending()
            continue

        if _is_structural_label(e):
            # "TABLE OF CONTENTS", "REVISIONS", "FIGURES" name a part of the
            # document. They are still fields — every text is — but they keep
            # their own wording as the default, so an untouched template renders
            # the label rather than whatever prose an AI would invent for it.
            flush_pending()
            text = (e.text or "").strip()
            section = text or section
            if not (c.field_name and is_dynamic(c.classification)):
                name = _unique(_suffixed(text, "label"), used)
                used.add(name)
                _force_dynamic(
                    c,
                    classification=ClassificationType.DYNAMIC_TEXT,
                    field_type=FieldType.TEXT,
                    name=name,
                    section=section,
                    text=text,
                )
                forced.add(e.node_id)
            _mark_structural(c)
            continue

        if e.type == ElementType.HEADING:
            flush_pending()
            section = e.text.strip() or section
            if c.field_name and is_dynamic(c.classification):
                c.static_prefix = None
                c.static_suffix = None
                c.required = True
                c.optional = False
                if not (c.description or "").strip():
                    c.description = _synth_description(section, e.text)
                _mark_tags_only(c)
            else:
                name = _unique(
                    _suffixed(section, "title"), used
                )
                used.add(name)
                _force_dynamic(
                    c,
                    classification=ClassificationType.DYNAMIC_TEXT,
                    field_type=FieldType.TEXT,
                    name=name,
                    section=section,
                    text=e.text.strip(),
                )
                forced.add(e.node_id)
            continue

        if e.type == ElementType.TABLE:
            flush_pending()
            already_looped = (
                c.classification == ClassificationType.REPEATABLE_TABLE and bool(c.field_name)
            )
            if already_looped or _is_data_table(e):
                if not already_looped:
                    name = _unique(
                        _suffixed(section, "rows"), used
                    )
                    used.add(name)
                    headers = e.table_structure.headers if e.table_structure else []
                    _force_dynamic(
                        c,
                        classification=ClassificationType.REPEATABLE_TABLE,
                        field_type=FieldType.TABLE,
                        name=name,
                        section=section,
                        text=", ".join(headers) or "table",
                    )
                    forced.add(e.node_id)
                # The loop replaces the data rows; the header row above it stays
                # put, so its column labels are the one part of a data table
                # that still needs tagging.
                forced.update(
                    _field_table_cells(
                        extraction, result, e, cls_by_node, used, section, only_row=0
                    )
                )
            else:
                # A layout table (a cover block, an address panel). Its shape is
                # part of the design, so it keeps every row and each cell becomes
                # its own field instead of the whole table becoming a loop.
                forced.update(
                    _field_table_cells(extraction, result, e, cls_by_node, used, section)
                )
            continue

        if e.type in _GROUPABLE:
            generated = is_generated_listing(e)
            if generated != pending_generated[0]:
                flush_pending()
                pending_generated[0] = generated
            pending.append((e, c))
            continue

        # Anything else with text (unknown kinds) — treat as a single paragraph.
        pending.append((e, c))
        flush_pending()

    flush_pending()

    # A contents list is now a field like any other; record which ones replaced
    # Word-generated content so the builder can remove the field behind them.
    for e in extraction.top_level_elements():
        c = cls_by_node[e.node_id]
        if c.field_name and is_generated_listing(e):
            _mark_generated(c)

    # Final safety net: guarantee the invariant absolutely. Anything above could
    # in principle miss an edge case (a new ElementType, an unusual walk order);
    # this catches any surviving non-exempt node that still isn't a real field
    # and force-tags it individually, so "every text is tagged" always holds.
    for e in extraction.top_level_elements():
        c = cls_by_node[e.node_id]
        if _is_exempt(e, c):
            continue
        if needs_field(c.classification) and c.field_name:
            continue
        if e.type == ElementType.TABLE:
            continue  # a layout table's cells carry its fields, not the table
        txt = e.text.strip()
        ftype = FieldType.MULTILINE_TEXT if len(txt) > _MULTILINE_THRESHOLD else FieldType.TEXT
        where = _page_area(e) or section or txt
        name = _unique(_suffixed(where, "content"), used)
        used.add(name)
        _force_dynamic(
            c,
            classification=ClassificationType.DYNAMIC_TEXT,
            field_type=ftype,
            name=name,
            section=section,
            text=txt,
        )
        forced.add(e.node_id)

    return forced
