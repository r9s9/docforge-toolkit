"""Compliance checker — score a document against a template.

Aligns a candidate document's structure to the template's representative
structure and evaluates three dimensions:

  * structure — does FIXED boilerplate appear (and match)?
  * fields    — are required dynamic fields present (and well-formed)?
  * tables    — do repeatable tables exist with the right shape + data?

Produces an overall compliance score (0–100), a grade, per-dimension scores,
and an itemized list of differences with severities.
"""

from __future__ import annotations

from ..common.textutil import sample_label, similarity
from ..multi_doc_differ import align_to_representative
from ..schemas.classification import ElementClassification
from ..schemas.compliance import (
    ComplianceAlignedPair,
    ComplianceDifference,
    ComplianceReport,
    DimensionScore,
)
from ..schemas.enums import ClassificationType, ElementType, FieldType, is_dynamic
from ..schemas.extraction import DocumentExtraction
from ..schemas.template import FieldDefinition, ValidationRule
from ..validator import validate

_FIXED_MATCH_THRESHOLD = 0.9
_HEADER_MATCH_THRESHOLD = 0.8


def _extract_value(text: str, label: str | None) -> str:
    if label and text.startswith(label):
        return text[len(label):].strip()
    if label:
        # tolerate minor label spacing differences
        head = label.rstrip().rstrip(":")
        if head and text.lower().startswith(head.lower()):
            return text.split(":", 1)[-1].strip() if ":" in text else text[len(head):].strip()
    return text.strip()


def check_compliance(
    rep: DocumentExtraction,
    classifications: list[ElementClassification],
    fields: list[FieldDefinition],
    rules: list[ValidationRule],
    doc: DocumentExtraction,
    *,
    template_id: str,
    version: int,
    document_name: str = "",
) -> ComplianceReport:
    cls_by_node = {c.node_id: c for c in classifications}
    field_by_node: dict[str, FieldDefinition] = {}
    for f in fields:
        for nid in f.node_ids:
            field_by_node[nid] = f

    aligned = align_to_representative(rep, doc)

    dims = {
        "structure": DimensionScore(name="structure"),
        "fields": DimensionScore(name="fields"),
        "tables": DimensionScore(name="tables"),
    }
    differences: list[ComplianceDifference] = []
    matched_fields: list[str] = []
    missing_fields: list[str] = []
    alignment: list[ComplianceAlignedPair] = []
    aligned_doc_ids: set[str] = set()

    for node in rep.top_level_elements():
        c = cls_by_node.get(node.node_id)
        if c is None or c.classification in (ClassificationType.AUTO_FIELD, ClassificationType.UNKNOWN):
            continue
        doc_el = aligned.get(node.node_id)
        if doc_el is not None:
            aligned_doc_ids.add(doc_el.node_id)

        if c.classification == ClassificationType.FIXED:
            _score_fixed(node, doc_el, dims["structure"], differences)
        elif c.classification == ClassificationType.REPEATABLE_TABLE:
            _score_table(node, c, field_by_node.get(node.node_id), doc_el, dims["tables"], differences)
        elif is_dynamic(c.classification):
            _score_field(
                node, c, field_by_node.get(node.node_id), rules, doc_el,
                dims["fields"], differences, matched_fields, missing_fields,
            )

        pair = _build_pair(node, c, field_by_node.get(node.node_id), doc_el)
        if pair is not None:
            alignment.append(pair)

    # Doc-only content (present in the upload, not in the template) -> "extra".
    for el in doc.top_level_elements():
        if el.node_id in aligned_doc_ids or not (el.text or "").strip():
            continue
        if el.type == ElementType.TABLE:
            alignment.append(ComplianceAlignedPair(
                node_id=el.node_id, status="extra", severity="info", is_table=True,
                document_headers=(el.table_structure.headers if el.table_structure else []),
                document_text="(table not in template)",
            ))
        else:
            alignment.append(ComplianceAlignedPair(
                node_id=el.node_id, status="extra", severity="info",
                document_text=el.text.strip()[:400],
            ))

    total = sum(d.total for d in dims.values())
    satisfied = sum(d.satisfied for d in dims.values())
    for d in dims.values():
        d.score = round(100.0 * d.satisfied / d.total, 1) if d.total else 100.0
    overall = round(100.0 * satisfied / total, 1) if total else 100.0
    grade = "pass" if overall >= 90 else "warning" if overall >= 70 else "fail"
    fixable = any(d.kind in ("changed_fixed", "missing_fixed") for d in differences)

    return ComplianceReport(
        template_id=template_id,
        version=version,
        document_name=document_name,
        score=overall,
        grade=grade,
        dimensions=list(dims.values()),
        differences=differences,
        matched_fields=matched_fields,
        missing_fields=missing_fields,
        alignment=alignment,
        fixable=fixable,
    )


def _build_pair(node, c, field, doc_el) -> ComplianceAlignedPair | None:
    """Build one side-by-side row (template element vs aligned doc element)."""
    cls = c.classification
    if cls == ClassificationType.FIXED:
        expected = node.text.strip()
        if not expected:
            return None
        if doc_el is None:
            return ComplianceAlignedPair(
                node_id=node.node_id, classification=cls.value, status="missing",
                severity="error", template_text=expected,
            )
        sim = similarity(doc_el.text.strip(), expected)
        status = "match" if sim >= _FIXED_MATCH_THRESHOLD else "changed"
        return ComplianceAlignedPair(
            node_id=node.node_id, classification=cls.value, status=status,
            severity="info" if status == "match" else "warning",
            template_text=expected, document_text=doc_el.text.strip(),
        )

    if cls == ClassificationType.REPEATABLE_TABLE:
        name = (field.field_name if field else c.field_name) or node.node_id
        exp_headers = node.table_structure.headers if node.table_structure else []
        if doc_el is None or doc_el.type != ElementType.TABLE or doc_el.table_structure is None:
            return ComplianceAlignedPair(
                node_id=node.node_id, classification=cls.value, status="missing_table",
                severity="error", field_name=name, is_table=True, template_headers=exp_headers,
            )
        ts = doc_el.table_structure
        sim = similarity(" | ".join(ts.headers).lower(), " | ".join(exp_headers).lower())
        status = "table" if sim >= _HEADER_MATCH_THRESHOLD else "table_changed"
        return ComplianceAlignedPair(
            node_id=node.node_id, classification=cls.value, status=status,
            severity="info" if status == "table" else "warning", field_name=name, is_table=True,
            template_headers=exp_headers, document_headers=ts.headers,
            document_text=f"{max(0, ts.n_rows - 1)} data row(s)",
        )

    if is_dynamic(cls):
        name = (field.field_name if field else c.field_name) or node.node_id
        required = field.required if field else c.required
        value = _extract_value(doc_el.text, c.static_prefix) if doc_el is not None else ""
        status = "field_missing" if (required and not value) else "field"
        # Image fields keep the original picture in the labelled view: no «Label».
        sample = (
            sample_label(field.label, field.field_name)
            if field is not None and field.field_type != FieldType.IMAGE
            else ""
        )
        return ComplianceAlignedPair(
            node_id=node.node_id, classification=cls.value, status=status,
            severity="error" if status == "field_missing" else "info",
            field_name=name, template_text=node.text.strip(), template_sample_text=sample,
            document_text=(doc_el.text.strip() if doc_el is not None else ""),
        )
    return None


def _score_fixed(node, doc_el, dim: DimensionScore, diffs: list) -> None:
    expected = node.text.strip()
    if not expected:
        return
    dim.total += 1.0
    if doc_el is None:
        diffs.append(ComplianceDifference(
            kind="missing_fixed", node_id=node.node_id, severity="error",
            expected=expected[:120], message=f"Expected fixed content is missing: “{expected[:60]}”",
        ))
        return
    sim = similarity(doc_el.text.strip(), expected)
    if sim >= _FIXED_MATCH_THRESHOLD:
        dim.satisfied += 1.0
    else:
        dim.satisfied += 0.3  # present but altered
        diffs.append(ComplianceDifference(
            kind="changed_fixed", node_id=node.node_id, severity="warning",
            expected=expected[:120], found=doc_el.text.strip()[:120],
            message=f"Fixed content was modified (similarity {int(sim * 100)}%).",
        ))


def _score_field(node, c, field, rules, doc_el, dim: DimensionScore, diffs, matched, missing) -> None:
    required = field.required if field else c.required
    name = (field.field_name if field else c.field_name) or node.node_id
    weight = 2.0 if required else 1.0
    dim.total += weight
    label = c.static_prefix

    value = _extract_value(doc_el.text, label) if doc_el is not None else ""
    if not value:
        if required:
            missing.append(name)
            diffs.append(ComplianceDifference(
                kind="missing_field", node_id=node.node_id, field_name=name, severity="error",
                message=f"Required field “{name}” has no value.",
            ))
        else:
            dim.satisfied += weight  # optional + absent = no penalty
            diffs.append(ComplianceDifference(
                kind="missing_field", node_id=node.node_id, field_name=name, severity="info",
                message=f"Optional field “{name}” is empty.",
            ))
        return

    matched.append(name)
    dim.satisfied += weight

    # Format check (date/number/enum/regex), if we have a field + its rules.
    if field is not None:
        field_rules = [r for r in rules if r.field_name == name and r.rule_type.value != "required"]
        if field_rules:
            report = validate({name: value}, [field], field_rules)
            if report.issues:
                dim.satisfied -= weight * 0.5
                diffs.append(ComplianceDifference(
                    kind="format", node_id=node.node_id, field_name=name, severity="warning",
                    found=value[:80], message=report.issues[0].message,
                ))


def _score_table(node, c, field, doc_el, dim: DimensionScore, diffs) -> None:
    name = (field.field_name if field else c.field_name) or node.node_id
    dim.total += 2.0
    if doc_el is None or doc_el.type != ElementType.TABLE or doc_el.table_structure is None:
        diffs.append(ComplianceDifference(
            kind="missing_table", node_id=node.node_id, field_name=name, severity="error",
            message=f"Expected table “{name}” is missing.",
        ))
        return

    ts = doc_el.table_structure
    exp_headers = node.table_structure.headers if node.table_structure else []
    header_sim = similarity(" | ".join(ts.headers).lower(), " | ".join(exp_headers).lower())

    if ts.n_rows < 2:
        dim.satisfied += 1.0
        diffs.append(ComplianceDifference(
            kind="table_shape", node_id=node.node_id, field_name=name, severity="warning",
            message=f"Table “{name}” has no data rows.",
        ))
        return

    if header_sim >= _HEADER_MATCH_THRESHOLD:
        dim.satisfied += 2.0
    else:
        dim.satisfied += 1.0
        diffs.append(ComplianceDifference(
            kind="table_shape", node_id=node.node_id, field_name=name, severity="warning",
            expected=", ".join(exp_headers), found=", ".join(ts.headers),
            message=f"Table “{name}” columns differ from the template.",
        ))
