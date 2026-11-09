"""Check a document against its template, and record Claude's judgement of it.

``check`` is the web app's deterministic backbone (``compliance.checker``):
structural alignment plus a 3-dimension score. Where the app then asks a model
whether each difference is material, the check-document skill has Claude write
verdicts, and ``apply_verdicts`` merges them the same way upstream
``compliance/judge.py`` does, adding the "semantic" dimension.
"""

from __future__ import annotations

from pathlib import Path

from ..common.textutil import similarity
from ..compliance import check_compliance
from ..multi_doc_differ import align_to_representative
from ..schemas.classification import ElementClassification
from ..schemas.compliance import ComplianceDifference, ComplianceReport, DimensionScore
from ..schemas.enums import ClassificationType, ElementType
from ..schemas.extraction import DocumentExtraction
from ..structure_normalizer import build_extraction
from .common import ToolkitError, check_docx
from .package import TemplateVersion

_VALID_SEVERITY = {"error", "warning", "info"}
DIFF_TEXT_CHARS = 200  # upstream judge truncation
MINOR_CHANGE = "changed_fixed_minor"


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def minor_changes(
    rep: DocumentExtraction,
    classifications: list[ElementClassification],
    doc: DocumentExtraction,
) -> list[ComplianceDifference]:
    """Boilerplate whose wording changed only a little.

    The checker counts fixed text as matching at 90% similarity or more, so a
    one-word edit to a long clause ("30 days" to "90 days") scores as a match
    and is never listed. These are reported at "info" severity, leaving the
    deterministic score exactly as upstream computes it, so that Claude can
    judge whether the small change alters meaning.
    """
    cls_by_node = {c.node_id: c for c in classifications}
    aligned = align_to_representative(rep, doc)
    found: list[ComplianceDifference] = []
    for node in rep.top_level_elements():
        c = cls_by_node.get(node.node_id)
        doc_el = aligned.get(node.node_id)
        if c is None or c.classification != ClassificationType.FIXED or doc_el is None:
            continue
        expected, actual = node.text.strip(), (doc_el.text or "").strip()
        if not expected or _norm(expected) == _norm(actual):
            continue
        sim = similarity(actual, expected)
        if sim >= 0.9:  # below this the checker already reports changed_fixed
            found.append(ComplianceDifference(
                kind=MINOR_CHANGE, node_id=node.node_id, severity="info",
                expected=expected[:400], found=actual[:400],
                message=(
                    f"Boilerplate wording differs slightly ({int(sim * 100)}% similar); "
                    "the score counts it as matching. Check whether it changes meaning."
                ),
            ))
    return found


def fixed_text_count(tv: TemplateVersion) -> int:
    """How many paragraphs of fixed wording the template has to check against.

    A "Full template" turns every text into a field, so it has none: a check
    against it can compare structure and formatting, but not wording.
    """
    from .analysis import _in_loop

    rep = tv.representative()
    if rep is None:
        return 0
    classifications = tv.intelligence().classifications
    cls = {c.node_id: c for c in classifications}
    loops = {c.node_id for c in classifications if c.classification == ClassificationType.REPEATABLE_TABLE}
    parent = {e.node_id: e.parent_node_id for e in rep.elements}
    count = 0
    for e in rep.elements:
        if not (e.text or "").strip() or e.type == ElementType.TABLE:
            continue
        if _in_loop(e.node_id, parent, loops):
            continue  # a repeating table's rows are filled, not fixed
        c = cls.get(e.node_id)
        if c is None or c.classification == ClassificationType.FIXED:
            count += 1
    return count


def check(tv: TemplateVersion, path: str | Path) -> ComplianceReport:
    check_docx(path)
    rep = tv.representative()
    if rep is None:
        raise ToolkitError(
            "this template version has no stored structure to compare against",
            hint="Rebuild the template with `build` to enable compliance checks.",
        )
    doc = build_extraction(Path(path), document_id="checked", filename=Path(path).name)
    classifications = tv.intelligence().classifications
    report = check_compliance(
        rep,
        classifications,
        tv.fields(),
        tv.rules(),
        doc,
        template_id=tv.template_id,
        version=tv.version,
        document_name=Path(path).name,
    )
    minor = minor_changes(rep, classifications, doc)
    if minor:
        report.differences.extend(minor)
        changed = {d.node_id for d in minor}
        for pair in report.alignment:
            if pair.node_id in changed and pair.status == "match":
                pair.status = "changed_minor"
                pair.severity = "info"
        # The strict fixer restores these too.
        report.fixable = True
    return report


def apply_verdicts(report: ComplianceReport, verdicts: dict | list) -> int:
    """Merge material/benign verdicts into the report. Returns how many were applied.

    ``verdicts`` is {"verdicts": [{"index", "material", "severity", "rationale"}],
    "summary": "..."} or just the list.
    """
    items = verdicts.get("verdicts", []) if isinstance(verdicts, dict) else verdicts
    if not isinstance(items, list):
        raise ToolkitError("verdicts must be a list (or {\"verdicts\": [...]})")
    material = judged = 0
    for v in items:
        if not isinstance(v, dict):
            continue
        try:
            index = int(v.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(report.differences):
            continue
        judged += 1
        d = report.differences[index]
        severity = str(v.get("severity", "")).lower()
        if severity in _VALID_SEVERITY:
            d.severity = severity
        rationale = str(v.get("rationale") or "").strip()
        if rationale:
            d.message = f"{d.message}. {rationale}" if d.message else rationale
        if v.get("material"):
            material += 1
    total = judged or len(report.differences)
    score = round(100.0 * (1 - material / total), 1) if total else 100.0
    report.dimensions = [d for d in report.dimensions if d.name != "semantic"]
    report.dimensions.append(
        DimensionScore(name="semantic", satisfied=float(total - material), total=float(total), score=score)
    )
    return judged


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= DIFF_TEXT_CHARS else text[:DIFF_TEXT_CHARS] + "…"


def summary(report: ComplianceReport, *, limit: int = 60) -> dict:
    """The parts Claude needs to judge the differences, without the full alignment."""
    diffs = [
        {
            "index": i,
            "kind": d.kind,
            "severity": d.severity,
            "field_name": d.field_name,
            "expected": _clip(d.expected),
            "found": _clip(d.found),
            "message": d.message,
        }
        for i, d in enumerate(report.differences[:limit])
    ]
    out = {
        "document": report.document_name,
        "template": {"id": report.template_id, "version": report.version},
        "score": report.score,
        "grade": report.grade,
        "dimensions": [
            {"name": d.name, "score": d.score, "satisfied": d.satisfied, "total": d.total}
            for d in report.dimensions
        ],
        "fixable": report.fixable,
        "matched_fields": len(report.matched_fields),
        "missing_fields": report.missing_fields,
        "differences_total": len(report.differences),
        "differences": diffs,
    }
    if len(report.differences) > limit:
        out["note"] = f"showing the first {limit} differences; the full list is in the report file"
    return out


def _cell(text: str, limit: int = 120) -> str:
    text = " ".join((text or "").split()).replace("|", "\\|")
    return text if len(text) <= limit else text[:limit] + "…"


def markdown(report: ComplianceReport, template_name: str = "") -> str:
    """A readable report: score, dimensions, differences, and a side-by-side view."""
    title = f"Compliance report: {report.document_name or 'document'}"
    lines = [f"# {title}", ""]
    if template_name:
        lines.append(f"Checked against **{template_name}** (version {report.version}).")
        lines.append("")
    lines.append(f"**Score: {report.score:.0f} / 100** ({report.grade})")
    lines.append("")
    lines.append("| Dimension | Score | Satisfied |")
    lines.append("|---|---|---|")
    for d in report.dimensions:
        lines.append(f"| {d.name} | {d.score:.0f} | {d.satisfied:g} of {d.total:g} |")
    lines.append("")
    if report.differences:
        lines.append(f"## Differences ({len(report.differences)})")
        lines.append("")
        lines.append("| # | Severity | Kind | Field | Expected | Found | Note |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, d in enumerate(report.differences):
            lines.append(
                f"| {i} | {d.severity} | {d.kind} | {d.field_name or ''} | "
                f"{_cell(d.expected)} | {_cell(d.found)} | {_cell(d.message, 200)} |"
            )
        lines.append("")
    else:
        lines.append("No differences found.")
        lines.append("")
    if report.fixable:
        lines.append(
            "Some boilerplate was changed or removed; DocForge can restore it "
            "without touching the field values."
        )
        lines.append("")
    changed = [a for a in report.alignment if a.status not in ("match", "field", "table")]
    if changed:
        lines.append("## Side by side (changed parts only)")
        lines.append("")
        lines.append("| Status | Template | Document |")
        lines.append("|---|---|---|")
        for a in changed:
            expected = a.template_sample_text or a.template_text
            lines.append(f"| {a.status} | {_cell(expected)} | {_cell(a.document_text)} |")
        lines.append("")
    return "\n".join(lines)
