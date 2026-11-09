"""What the generation AI is told about the *document* it is filling.

Routing used to see a flat list of fields — name, type, description — with no
idea which part of the document each one belonged to, what came before it, or
what the section it sits in is for. That is the difference between filling in a
form and writing a document, and it is why generated content could be perfectly
valid per field yet read wrong in place.

All of the missing signal already exists: analysis records a
:class:`SectionUnderstanding` per section (its purpose and the content expected
there) and the representative document keeps the original element order. This
module assembles both into a compact payload the prompts can carry.

Templates published before fields carried a ``section_key`` are handled too:
:func:`resolve_section_keys` reconstructs the grouping at generation time, so
old templates get the benefit without being republished.
"""

from __future__ import annotations

from ..common.textutil import short_field_name, similarity, slugify_field
from ..schemas.classification import ElementClassification, SectionUnderstanding
from ..schemas.enums import ElementType
from ..schemas.extraction import DocumentExtraction
from ..schemas.template import FieldDefinition

# How close a section's remembered field name has to be to a real one before we
# treat them as the same field. High enough that unrelated fields never merge.
_NAME_MATCH_THRESHOLD = 0.8

# Section purposes are prose written by the model; keep them useful but bounded
# so a long analysis can't crowd out the content being placed.
_MAX_PURPOSE_CHARS = 300


def resolve_section_keys(
    fields: list[FieldDefinition], sections: list[SectionUnderstanding]
) -> None:
    """Fill in missing ``section_key``s in place, best-effort.

    Three passes, each only touching fields still unassigned: exact slug match,
    then fuzzy name match, then — for anything still loose — the section that
    claimed the nearest earlier field, since fields are in document order.
    """
    if not sections:
        return
    unassigned = [f for f in fields if not f.section_key]
    if not unassigned:
        return

    by_slug: dict[str, str] = {}
    for s in sections:
        for name in s.field_names:
            by_slug[short_field_name(name, fallback="")] = s.section_key
    for f in unassigned:
        f.section_key = by_slug.get(short_field_name(f.field_name, fallback=""))

    still = [f for f in fields if not f.section_key]
    for f in still:
        best_key, best_score = None, 0.0
        for s in sections:
            for name in s.field_names:
                score = similarity(
                    slugify_field(name, fallback=""), slugify_field(f.field_name, fallback="")
                )
                if score > best_score:
                    best_key, best_score = s.section_key, score
        if best_score >= _NAME_MATCH_THRESHOLD:
            f.section_key = best_key

    # Anything left over joins whichever section the previous field belongs to:
    # a field with no remembered home almost always sits under the same heading
    # as the one before it.
    carried: str | None = None
    for f in fields:
        if f.section_key:
            carried = f.section_key
        elif carried:
            f.section_key = carried


def _nearest_heading(rep: DocumentExtraction, node_id: str) -> str:
    """Text of the closest heading above ``node_id`` in the original document."""
    elements = rep.top_level_elements()
    index = next((i for i, e in enumerate(elements) if e.node_id == node_id), None)
    if index is None:
        return ""
    for element in reversed(elements[:index]):
        if element.type == ElementType.HEADING and (element.text or "").strip():
            return element.text.strip()
    return ""


def _order_index(rep: DocumentExtraction | None, field: FieldDefinition) -> int:
    """Where this field first appears in the document (for stable ordering)."""
    if rep is None or not field.node_ids:
        return 10_000
    positions = {e.node_id: i for i, e in enumerate(rep.top_level_elements())}
    return min((positions.get(nid, 10_000) for nid in field.node_ids), default=10_000)


def build_template_context(
    *,
    document_type: str = "",
    sections: list[SectionUnderstanding] | None = None,
    classifications: list[ElementClassification] | None = None,
    fields: list[FieldDefinition] | None = None,
    representative: DocumentExtraction | None = None,
) -> dict:
    """Describe the template as a document: its type, sections and field places.

    Returns ``{}`` when there is nothing worth saying, so callers can pass the
    result straight through and prompts stay unchanged for bare templates.
    """
    sections = sections or []
    fields = fields or []
    if not fields:
        return {}

    resolve_section_keys(fields, sections)

    cls_by_field: dict[str, ElementClassification] = {}
    for c in classifications or []:
        if c.field_name and c.field_name not in cls_by_field:
            cls_by_field[c.field_name] = c

    positions = (
        {e.node_id: i for i, e in enumerate(representative.top_level_elements())}
        if representative is not None
        else {}
    )

    places: dict[str, dict] = {}
    for f in fields:
        entry: dict = {}
        if f.section_key:
            entry["section"] = f.section_key
        if positions:
            entry["order"] = _order_index(representative, f)
            if f.node_ids:
                heading = _nearest_heading(representative, f.node_ids[0])
                if heading:
                    entry["under_heading"] = heading
        c = cls_by_field.get(f.field_name)
        if c is not None:
            if c.static_prefix:
                entry["label_before"] = c.static_prefix
            if c.static_suffix:
                entry["label_after"] = c.static_suffix
        if entry:
            places[f.field_name] = entry

    section_payload = [
        {
            "section_key": s.section_key,
            "title": s.title,
            "purpose": (s.purpose or "")[:_MAX_PURPOSE_CHARS],
            "expected_content": (s.expected_content or "")[:_MAX_PURPOSE_CHARS],
            "fields": [f.field_name for f in fields if f.section_key == s.section_key],
        }
        for s in sections
    ]

    context: dict = {}
    if document_type:
        context["document_type"] = document_type
    if section_payload:
        context["sections"] = section_payload
    if places:
        context["field_places"] = places
    return context
