"""Derive FieldDefinitions and ValidationRules from a classification result.

This aggregation turns per-node classifications into the user-facing field list
(field_definitions.json) and the rule set (validation_rules.json) stored in a
template package. The review UI edits these directly.
"""

from __future__ import annotations

from ..common.textutil import short_field_name, slugify_field
from ..schemas.classification import ClassificationResult, ElementClassification
from ..schemas.enums import (
    ClassificationType,
    ElementType,
    FieldType,
    IssueSeverity,
    RuleType,
)
from ..schemas.extraction import DocumentExtraction
from ..schemas.template import FieldDefinition, TableColumn, ValidationRule
from .tags_only import STRUCTURAL_MARK

_NUMBER_HEADER_HINTS = ("qty", "quantity", "amount", "price", "total", "cost", "count", "rate", "no.")
_DATE_HEADER_HINTS = ("date", "due")

# The two parts of a repeated heading+body group. Named here so the builder,
# the writer and the editor all agree on what an item looks like.
BLOCK_TITLE = "title"
BLOCK_BODY = "body"


def BLOCK_COLUMNS() -> list[TableColumn]:
    return [
        TableColumn(field_name=BLOCK_TITLE, label="Heading", field_type=FieldType.TEXT),
        TableColumn(field_name=BLOCK_BODY, label="Content", field_type=FieldType.MULTILINE_TEXT),
    ]


def _column_type(header: str) -> FieldType:
    h = header.lower()
    if any(k in h for k in _DATE_HEADER_HINTS):
        return FieldType.DATE
    if any(k in h for k in _NUMBER_HEADER_HINTS):
        return FieldType.NUMBER
    return FieldType.TEXT


def _table_columns(headers: list[str]) -> list[TableColumn]:
    cols: list[TableColumn] = []
    used: set[str] = set()
    for i, h in enumerate(headers):
        base = short_field_name(h, fallback=f"col{i + 1}")
        name = base
        n = 2
        while name in used:
            name = f"{base}_{n}"
            n += 1
        used.add(name)
        cols.append(
            TableColumn(field_name=name, label=h or f"Column {i + 1}", field_type=_column_type(h))
        )
    return cols


def include_field_name(c: ElementClassification, element) -> str:
    """Boolean toggle field name for an optional node (shared by builder)."""
    base = c.field_name or (element.text if element else "")
    # The classifier may already call the node "include_…"; never say it twice.
    while base.startswith("include_"):
        base = base[len("include_"):]
    return f"include_{short_field_name(base, fallback='section', max_words=2)}"


def _label_from(field_name: str, classification: ElementClassification) -> str:
    if classification.static_prefix:
        lbl = classification.static_prefix.strip().rstrip(":").strip()
        if lbl:
            return lbl
    return field_name.replace("_", " ").title()


_KIND_HINT = {
    ClassificationType.DYNAMIC_DATE: "A date value",
    ClassificationType.DYNAMIC_PERSON: "A person's name",
    ClassificationType.DYNAMIC_NUMBER: "A numeric value",
    ClassificationType.DYNAMIC_ENUM: "One of a fixed set of choices",
    ClassificationType.REPEATABLE_TABLE: "Repeating rows of data",
    ClassificationType.REPEATABLE_SECTION: "Repeating free-text content",
}


def _is_empty_layout_node(e) -> bool:
    """A node that holds no text and no picture — spacing, not content."""
    if e is None:
        return False
    if e.type in (ElementType.TABLE, ElementType.IMAGE) or e.image_ref is not None:
        return False
    return not (e.text or "").strip()


def _fallback_description(
    label: str, classification: ElementClassification, example_text: str = ""
) -> str:
    """A deterministic, still-useful description when the AI left one blank.

    The routing/compose agents lean on ``description`` as their main semantic
    signal for a field beyond its bare name — an empty description silently
    starves them of context. This keeps every field usable even if a
    classification pass (or a heuristic-fallback run) never wrote one.
    """
    hint = _KIND_HINT.get(classification.classification, "The value")
    parts = [f"{hint} for \"{label}\"."]
    if classification.static_prefix:
        parts.append(f"In the document it follows the label \"{classification.static_prefix.strip()}\".")
    if classification.enum_values:
        parts.append(f"Allowed values: {', '.join(classification.enum_values)}.")
    sample = " ".join((example_text or "").split())
    if sample:
        if len(sample) > 120:
            sample = sample[:120].rstrip() + "…"
        parts.append(f"Original example: “{sample}”")
    return " ".join(parts)


def derive_field_definitions(
    extraction: DocumentExtraction, result: ClassificationResult
) -> list[FieldDefinition]:
    """Build the ordered list of fillable fields from classifications."""
    # Map field_name -> section_key (from section understanding). The model
    # writes section membership from memory, so its names routinely differ in
    # case, punctuation or length from the shortened names we actually keep —
    # match on the normalized name, or every field silently ends up section-less.
    field_section: dict[str, str] = {}
    for s in result.sections:
        for fname in s.field_names:
            field_section[short_field_name(fname, fallback="")] = s.section_key

    def section_for(name: str) -> str | None:
        return field_section.get(short_field_name(name, fallback=""))

    by_id = {e.node_id: e for e in extraction.elements}
    fields: list[FieldDefinition] = []
    for c in result.classifications:
        if not c.field_name:
            continue
        if _is_empty_layout_node(by_id.get(c.node_id)):
            # A blank paragraph is spacing, not content. The model routinely
            # names them ("spacer_1", "standard_spacer") and each one becomes a
            # field nobody can fill and everybody has to scroll past.
            continue
        if c.classification not in (
            ClassificationType.DYNAMIC_TEXT,
            ClassificationType.DYNAMIC_DATE,
            ClassificationType.DYNAMIC_PERSON,
            ClassificationType.DYNAMIC_ENUM,
            ClassificationType.DYNAMIC_NUMBER,
            ClassificationType.REPEATABLE_TABLE,
            ClassificationType.REPEATABLE_SECTION,
            ClassificationType.REPEATABLE_BLOCK,
        ):
            continue

        # Several classifications may share one field name (a grouped repeatable
        # section in tags-only mode) — merge them into one field spanning all nodes.
        existing = next((f for f in fields if f.field_name == c.field_name), None)
        if existing is not None:
            if c.node_id not in existing.node_ids:
                existing.node_ids.append(c.node_id)
            continue

        columns: list[TableColumn] = []
        if c.classification == ClassificationType.REPEATABLE_TABLE:
            el = by_id.get(c.node_id)
            headers = el.table_structure.headers if el and el.table_structure else []
            columns = _table_columns(headers)
        elif c.classification == ClassificationType.REPEATABLE_BLOCK:
            # A repeated heading+body group is a two-column shape, so it reuses
            # the table machinery rather than introducing a parallel one.
            columns = BLOCK_COLUMNS()

        label = _label_from(c.field_name, c)
        example_el = by_id.get(c.node_id)
        example_text = (example_el.text if example_el else "") or ""
        # A document label ("TABLE OF CONTENTS") is editable like everything
        # else, but starts as itself: left alone it renders the label, instead
        # of vanishing or being filled with a sentence.
        default = example_text.strip() if STRUCTURAL_MARK in (c.rationale or "") else None
        fields.append(
            FieldDefinition(
                field_name=c.field_name,
                label=label,
                field_type=c.field_type or FieldType.TEXT,
                classification=c.classification,
                description=(c.description or "").strip()
                or _fallback_description(label, c, example_text),
                required=c.required and not c.optional,
                enum_values=c.enum_values,
                default=default,
                node_ids=[c.node_id],
                section_key=section_for(c.field_name),
                columns=columns,
                confidence=c.confidence,
            )
        )

    # Image fields: each embedded raster picture can become a fillable image
    # (the user uploads a replacement at generation), or be kept exactly as-is
    # (company logos, icons, brand marks). We surface one field per real picture;
    # the review UI defaults these to "keep original" so brand assets are never
    # lost — the user opts a picture into being dynamic. Only true raster pictures
    # qualify (content_type image/*); images embedded in text boxes / OLE objects
    # can't be swapped in place, so they're left untouched.
    used_names = {f.field_name for f in fields}
    image_nodes: set[str] = set()
    image_fields: list[FieldDefinition] = []
    n_img = 0
    for e in extraction.elements:
        ir = e.image_ref
        if not (ir and (ir.content_type or "").startswith("image/")):
            continue
        if e.node_id in image_nodes:
            continue
        n_img += 1
        scope = "header_" if (e.header_footer_scope or "").startswith("header") else (
            "footer_" if (e.header_footer_scope or "").startswith("footer") else ""
        )
        base = slugify_field(f"{scope}image_{n_img}", fallback=f"image_{n_img}")
        name = base
        k = 2
        while name in used_names:
            name = f"{base}_{k}"
            k += 1
        used_names.add(name)
        image_nodes.add(e.node_id)
        image_fields.append(
            FieldDefinition(
                field_name=name,
                label=f"Image {n_img}",
                field_type=FieldType.IMAGE,
                classification=ClassificationType.DYNAMIC_IMAGE,
                description="Replace this picture per document, or keep the original.",
                required=False,
                node_ids=[e.node_id],
                section_key=section_for(name),
                confidence=0.5,
            )
        )
    # A picture-only node is an image field, not a text field — drop the text
    # field the classifier produced for it. A paragraph that holds a picture
    # *and* words keeps both: the logo is swapped through its image field while
    # the caption beside it stays editable as text.
    if image_nodes:
        text_bearing = {
            e.node_id for e in extraction.elements
            if e.node_id in image_nodes and (e.text or "").strip()
        }
        drop = image_nodes - text_bearing
        fields = [f for f in fields if not set(f.node_ids) & drop]
    fields.extend(image_fields)

    # Boolean "include" toggles for optional content (present in some examples
    # but not others). Default True so optional content renders unless turned off.
    used_include: set[str] = set()
    for c in result.classifications:
        if not c.optional or c.classification == ClassificationType.AUTO_FIELD:
            continue
        el = by_id.get(c.node_id)
        name = include_field_name(c, el)
        if name in used_include:
            continue
        used_include.add(name)
        preview = (el.text[:40] if el and el.text else c.field_name) or "section"
        fields.append(
            FieldDefinition(
                field_name=name,
                label=f"Include: {preview}",
                field_type=FieldType.BOOLEAN,
                classification=ClassificationType.FIXED,
                description="Whether to include this optional content.",
                required=False,
                default=True,
                node_ids=[c.node_id],
                section_key=section_for(c.field_name),
                confidence=c.confidence,
            )
        )
    return fields


def derive_validation_rules(fields: list[FieldDefinition]) -> list[ValidationRule]:
    """Generate a baseline rule set from field definitions (spec §13)."""
    rules: list[ValidationRule] = []

    def add(field_name, rtype, params=None, message=None, severity=IssueSeverity.ERROR):
        rules.append(
            ValidationRule(
                rule_id=f"{field_name}:{rtype.value}",
                rule_type=rtype,
                field_name=field_name,
                params=params or {},
                message=message,
                severity=severity,
            )
        )

    for f in fields:
        if f.required:
            add(f.field_name, RuleType.REQUIRED, message=f"'{f.label}' is required")

        if f.field_type == FieldType.DATE:
            add(
                f.field_name,
                RuleType.DATE_FORMAT,
                params={"formats": ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%B %d, %Y"]},
                message=f"'{f.label}' should be a valid date",
                severity=IssueSeverity.WARNING,
            )
        elif f.field_type == FieldType.NUMBER:
            add(
                f.field_name,
                RuleType.NUMERIC_FORMAT,
                message=f"'{f.label}' should be numeric",
                severity=IssueSeverity.WARNING,
            )
        elif f.field_type == FieldType.ENUM and f.enum_values:
            add(
                f.field_name,
                RuleType.ENUM,
                params={"allowed": f.enum_values},
                message=f"'{f.label}' must be one of: {', '.join(f.enum_values)}",
            )
        elif f.field_type == FieldType.TABLE:
            required_cols = [c.field_name for c in f.columns if c.required]
            add(
                f.field_name,
                RuleType.TABLE_SCHEMA,
                params={
                    "columns": [c.field_name for c in f.columns],
                    "required_columns": required_cols,
                },
                message=f"Rows of '{f.label}' must match the column schema",
                severity=IssueSeverity.WARNING,
            )

    return rules
