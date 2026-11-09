"""Route the *content of an uploaded document* onto a template's fields.

This powers "create a new doc from an existing filled DOCX": we extract all of
the source document's content (paragraphs + tables) and map it into the target
template's fields. The LLM does this best (it understands which paragraph is the
summary, which table is the line-items, etc.); a heuristic fallback handles
labeled lines and matches tables by header similarity.
"""

from __future__ import annotations

import logging

from ..common.textutil import similarity
from ..multi_doc_differ import align_to_representative
from ..schemas.classification import ElementClassification
from ..schemas.enums import ElementType, FieldType
from ..schemas.extraction import DocumentExtraction
from ..schemas.routing import PlacementInstruction, RoutingResult
from ..schemas.template import FieldDefinition
from .router import route_unstructured_heuristic

logger = logging.getLogger("docforge.ai_router.document")


def _extract_value(text: str, label: str | None) -> str:
    """Strip a known label/prefix (e.g. 'Invoice Date: ') to get the value."""
    if label and text.startswith(label):
        return text[len(label):].strip()
    if label:
        head = label.rstrip().rstrip(":")
        if head and text.lower().startswith(head.lower()):
            return text.split(":", 1)[-1].strip() if ":" in text else text[len(head):].strip()
    return text.strip()


def route_document_structural(
    rep: DocumentExtraction,
    classifications: list[ElementClassification],
    fields: list[FieldDefinition],
    doc: DocumentExtraction,
    *,
    template_id: str,
    version: int,
) -> RoutingResult:
    """Map a document that shares the template's structure by aligning it to the
    template's representative and reading each field's value from the matching
    element. This is near-exact when the upload is the same *kind* of document the
    template was built from (including the original source document) — far more
    reliable than fuzzy text routing.
    """
    aligned = align_to_representative(rep, doc)  # {rep_node_id: matched element}
    cls_by_node = {c.node_id: c for c in classifications}
    placements: list[PlacementInstruction] = []

    for f in fields:
        if f.field_type == FieldType.BOOLEAN:
            continue  # include-toggles default to on
        node_id = next((nid for nid in f.node_ids if nid in aligned), None)
        if node_id is None:
            continue
        el = aligned[node_id]
        if f.field_type == FieldType.TABLE:
            ts = el.table_structure
            if ts and len(ts.rows) > 1:
                cols = [c.field_name for c in f.columns]
                rows = [
                    {cols[i]: (r[i] if i < len(r) else "") for i in range(len(cols))}
                    for r in ts.rows[1:]
                    if any((cell or "").strip() for cell in r)
                ]
                if rows:
                    placements.append(PlacementInstruction(
                        field_name=f.field_name, value=rows, confidence=0.9,
                        source_excerpt="(matched table)",
                    ))
            continue
        c = cls_by_node.get(node_id)
        value = _extract_value(el.text or "", c.static_prefix if c else None)
        if value:
            placements.append(PlacementInstruction(
                field_name=f.field_name, value=value, confidence=0.9,
                source_excerpt=(el.text or "")[:80],
            ))

    placed = {p.field_name for p in placements}
    missing = [f.field_name for f in fields if f.required and f.field_name not in placed]
    return RoutingResult(
        template_id=template_id, version=version, placements=placements,
        missing_required=missing, source="structural",
    )


def document_content(doc: DocumentExtraction) -> dict:
    """Extract a routing-friendly view of a document: paragraphs + tables."""
    paragraphs: list[str] = []
    tables: list[dict] = []
    for e in doc.top_level_elements():
        if e.type == ElementType.TABLE and e.table_structure:
            ts = e.table_structure
            rows = ts.rows[1:] if len(ts.rows) > 1 else []
            tables.append({"headers": ts.headers, "rows": rows})
        elif e.text and e.text.strip():
            paragraphs.append(e.text.strip())
    return {"paragraphs": paragraphs, "tables": tables}


def extraction_blocks(doc: DocumentExtraction) -> list[dict]:
    """Ordered preview blocks (paragraphs/headings/tables) from an extraction —
    used to show users the content of an uploaded/checked document."""
    blocks: list[dict] = []
    for e in doc.top_level_elements():
        if e.type == ElementType.TABLE and e.table_structure:
            ts = e.table_structure
            blocks.append(
                {"type": "table", "headers": ts.headers, "rows": ts.rows[1:] if len(ts.rows) > 1 else []}
            )
        elif e.text and e.text.strip():
            kind = "heading" if e.type == ElementType.HEADING else "paragraph"
            blocks.append({"type": kind, "text": e.text, "style": e.style_name or ""})
    return blocks


def render_content_text(content: dict) -> str:
    """Readable flattening of document content for the unstructured LLM router."""
    lines = list(content.get("paragraphs", []))
    for t in content.get("tables", []):
        lines.append("TABLE:")
        lines.append(" | ".join(t.get("headers", [])))
        for r in t.get("rows", []):
            lines.append(" | ".join(r))
    return "\n".join(lines)


# Long paragraphs are truncated in the outline so a big source document can't
# crowd out the template it is being mapped onto; the full text stays one tool
# call away (see ``source_blocks``).
_OUTLINE_PARA_CHARS = 600


def _block_id(index: int) -> str:
    return f"b{index}"


def render_content_outline(doc: DocumentExtraction, *, max_para_chars: int = _OUTLINE_PARA_CHARS) -> str:
    """Render an uploaded document as an indented outline that keeps its shape.

    Flattening a source document to a pool of lines throws away exactly the
    signal that says where its content belongs: which heading it sat under, what
    was a list, what was a table. Preserving the hierarchy lets the model map
    structure to structure — "their Background section is this template's
    Overview" — instead of guessing from word overlap.
    """
    lines: list[str] = []
    for index, element in enumerate(doc.top_level_elements()):
        if element.type == ElementType.TABLE and element.table_structure:
            ts = element.table_structure
            lines.append("TABLE: " + " | ".join(ts.headers))
            for row in (ts.rows[1:] if len(ts.rows) > 1 else []):
                lines.append("  " + " | ".join(row))
            continue
        text = (element.text or "").strip()
        if not text:
            continue
        if len(text) > max_para_chars:
            text = (
                text[:max_para_chars].rstrip()
                + f"… [truncated — get_source_block {_block_id(index)}]"
            )
        if element.type == ElementType.HEADING:
            level = element.subtype if (element.subtype or "").isdigit() else "1"
            lines.append("")
            lines.append("#" * min(6, int(level)) + " " + text)
        elif element.type == ElementType.LIST_ITEM:
            depth = (element.numbering_info.level if element.numbering_info else 0) or 0
            marker = "1." if element.subtype == "ordered" else "-"
            lines.append("  " * depth + f"{marker} {text}")
        else:
            lines.append(text)
    return "\n".join(lines).strip()


def source_blocks(doc: DocumentExtraction) -> dict[str, str]:
    """Full, untruncated text per outline block id (backs ``get_source_block``)."""
    blocks: dict[str, str] = {}
    for index, element in enumerate(doc.top_level_elements()):
        if element.type == ElementType.TABLE and element.table_structure:
            ts = element.table_structure
            rows = [" | ".join(r) for r in ts.rows]
            blocks[_block_id(index)] = "\n".join([" | ".join(ts.headers), *rows])
        elif (element.text or "").strip():
            blocks[_block_id(index)] = element.text.strip()
    return blocks


def _header_match(headers: list[str], labels: list[str]) -> float:
    return similarity(" | ".join(headers).lower(), " | ".join(labels).lower())


def _map_table_rows(table: dict, columns) -> list[dict]:
    """Map a content table's rows to column field_names (header-aware, positional fallback)."""
    headers = [h.lower() for h in table.get("headers", [])]
    col_to_idx: dict[str, int | None] = {}
    for col in columns:
        target = (col.label or col.field_name).lower()
        best_i, best_s = None, 0.0
        for i, h in enumerate(headers):
            s = similarity(h, target)
            if s > best_s:
                best_i, best_s = i, s
        col_to_idx[col.field_name] = best_i if best_s >= 0.5 else None

    out: list[dict] = []
    for r in table.get("rows", []):
        row: dict[str, str] = {}
        for ci, col in enumerate(columns):
            idx = col_to_idx.get(col.field_name)
            if idx is None:
                idx = ci
            row[col.field_name] = r[idx] if idx < len(r) else ""
        out.append(row)
    return out


def route_document_heuristic(
    fields: list[FieldDefinition], content: dict, template_id: str, version: int
) -> RoutingResult:
    # 1) Labeled lines + free-text prose via the standard unstructured router.
    base = route_unstructured_heuristic(
        fields, "\n".join(content.get("paragraphs", [])), template_id, version
    )
    placed = {p.field_name for p in base.placements}

    # 2) Map each unplaced table field to the best-matching content table.
    extra: list[PlacementInstruction] = []
    used: set[int] = set()
    table_fields = [f for f in fields if f.field_type == FieldType.TABLE and f.field_name not in placed]
    tables = content.get("tables", [])
    for tf in table_fields:
        labels = [c.label or c.field_name for c in tf.columns]
        best_i, best_s = -1, 0.0
        for i, t in enumerate(tables):
            if i in used:
                continue
            s = _header_match(t.get("headers", []), labels)
            if s > best_s:
                best_i, best_s = i, s
        if best_i >= 0 and best_s >= 0.3:
            used.add(best_i)
            extra.append(
                PlacementInstruction(
                    field_name=tf.field_name,
                    value=_map_table_rows(tables[best_i], tf.columns),
                    confidence=0.6,
                    source_excerpt="(table matched from document)",
                )
            )

    placements = base.placements + extra
    placed = {p.field_name for p in placements}
    missing = [f.field_name for f in fields if f.required and f.field_name not in placed]
    return RoutingResult(
        template_id=template_id,
        version=version,
        placements=placements,
        missing_required=missing,
        ambiguous_fields=base.ambiguous_fields,
        unmapped_content=base.unmapped_content,
        source="heuristic",
    )


def route_document_content(
    fields: list[FieldDefinition],
    content: dict,
    *,
    template_id: str,
    version: int,
    client=None,
    template_context: dict | None = None,
    source_doc: DocumentExtraction | None = None,
    learned_hints: str = "",
) -> RoutingResult:
    """Map extracted document content onto template fields, deterministically.

    Plugin build: Claude does the AI mapping itself in the fill-template skill,
    so this is upstream's no-model path. The signature is kept for parity.
    """
    return route_document_heuristic(fields, content, template_id, version)
