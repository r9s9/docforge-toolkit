"""Fill a template: describe its fields, map a document onto it, render values.

Where the web app asks a model to write field values ("writer" and "compose"),
the fill-template skill has Claude write them into a values file; rendering is
the same deterministic path the app uses after its review screen.
"""

from __future__ import annotations

import base64
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..ai_router.context import build_template_context
from ..ai_router.document import render_content_outline, route_document_structural
from ..ai_router.router import route_structured
from ..schemas.enums import ClassificationType, FieldType
from ..schemas.extraction import DocumentExtraction
from ..schemas.routing import RoutingResult
from ..schemas.template import FieldDefinition
from ..schemas.validation import ValidationReport
from ..structure_normalizer import build_extraction
from .common import ToolkitError, check_docx, read_json
from .package import TemplateVersion
from .render import merge_defaults, parse_docx_blocks, section_toggles

# Below this share of fields mapped, a document isn't "the same kind" and
# structural mapping gives way (upstream services/generation.py uses 0.4).
STRUCTURAL_COVERAGE = 0.4


def value_shape(f: FieldDefinition) -> str:
    """What a value for this field looks like in a values file."""
    if f.field_type == FieldType.IMAGE:
        return "path to a PNG/JPEG file (omit to keep the original picture)"
    if f.field_type == FieldType.BOOLEAN:
        return "true or false"
    if f.classification == ClassificationType.REPEATABLE_BLOCK:
        return 'list of {"title": …, "body": …}, one per repeated item'
    if f.field_type == FieldType.TABLE:
        cols = ", ".join(c.field_name for c in f.columns)
        return f"list of rows, each an object with keys: {cols}"
    if f.classification == ClassificationType.REPEATABLE_SECTION:
        return "list of strings, one per paragraph or point"
    if f.field_type == FieldType.DATE:
        return "date string (YYYY-MM-DD unless the description asks for another format)"
    if f.field_type == FieldType.NUMBER:
        return "number or numeric string"
    if f.field_type == FieldType.ENUM:
        return "one of the allowed_values"
    if f.field_type == FieldType.MULTILINE_TEXT:
        return "text; may use blank lines, '- ' bullets and **bold**"
    return "short text"


def describe(tv: TemplateVersion) -> dict:
    """The template as a document: type, sections, and every field's spec and place."""
    fields = tv.fields()
    intel = tv.intelligence()
    rep = tv.representative()
    context = build_template_context(
        document_type=intel.document_type_guess,
        sections=intel.sections,
        classifications=intel.classifications,
        fields=[f.model_copy(deep=True) for f in fields],
        representative=rep,
    )
    places = context.get("field_places", {})
    out_fields = []
    for f in fields:
        spec: dict = {
            "field_name": f.field_name,
            "label": f.label,
            "type": f.field_type.value,
            "classification": f.classification.value,
            "required": f.required,
            "value_shape": value_shape(f),
            "description": f.description,
        }
        if f.enum_values:
            spec["allowed_values"] = f.enum_values
        if f.columns:
            spec["columns"] = [
                {"field_name": c.field_name, "label": c.label, "type": c.field_type.value,
                 "required": c.required}
                for c in f.columns
            ]
        if f.default not in (None, ""):
            spec["default"] = f.default
        spec.update({k: v for k, v in places.get(f.field_name, {}).items() if k != "order"})
        out_fields.append(spec)

    from ..template_builder.builder import section_toggle_name

    sections = [
        {**s, "toggle": section_toggle_name(s["section_key"])}
        for s in context.get("sections", [])
    ]
    out = {"template": tv.summary(), "document_type": intel.document_type_guess}
    if sections:
        out["sections"] = sections
    out["fields"] = out_fields
    notes = tv.notes().strip()
    if notes:
        out["notes"] = notes
    return out


def read_document(path: str | Path) -> tuple[DocumentExtraction, str]:
    """Extract any .docx and return it with a block-id outline of its content."""
    check_docx(path)
    doc = build_extraction(Path(path), document_id="source", filename=Path(path).name)
    return doc, render_content_outline(doc)


def route_document(tv: TemplateVersion, path: str | Path) -> dict:
    """Map a document of the same kind onto the template's fields, structurally."""
    fields = tv.fields()
    doc, outline = read_document(path)
    rep = tv.representative()
    out: dict = {"source": str(path)}
    if rep is not None:
        intel = tv.intelligence()
        routing = route_document_structural(
            rep, intel.classifications, fields, doc,
            template_id=tv.template_id, version=tv.version,
        )
        kept, rejected = _check_labels(routing, fields, intel.classifications)
        labelled = len(kept["labelled"]) + len(rejected)
        mappable = max(1, len([f for f in fields if f.field_type != FieldType.BOOLEAN]))
        coverage = len(kept["all"]) / mappable
        out["coverage"] = round(coverage, 2)
        # Most labels missing means the alignment paired unrelated paragraphs:
        # this is a different kind of document, whatever the coverage says.
        mostly_wrong = labelled > 0 and len(rejected) * 2 > labelled
        if coverage >= STRUCTURAL_COVERAGE and not mostly_wrong:
            values = {p.field_name: p.value for p in kept["all"] if p.value is not None}
            out["method"] = "structural"
            out["values"] = values
            out["missing_required"] = [
                f.field_name for f in fields if f.required and f.field_name not in values
            ]
            if rejected:
                out["rejected"] = rejected
            if out["missing_required"] or rejected:
                out["outline"] = outline
            return out
        if rejected:
            out["rejected"] = rejected
    # A different kind of document: Claude reads the outline and writes the
    # values itself (where the web app would call its writer model).
    out["method"] = "outline"
    out["note"] = (
        "This document's structure doesn't match the template. Read the outline "
        "and write the values yourself."
    )
    out["outline"] = outline
    return out


def _starts_with_label(text: str, label: str) -> bool:
    head = label.strip().rstrip(":").strip().lower()
    return not head or (text or "").strip().lower().startswith(head)


def _check_labels(routing: RoutingResult, fields: list[FieldDefinition], classifications) -> tuple[dict, list[dict]]:
    """Keep structural placements whose source paragraph carries the field's label.

    Upstream's extractor returns a whole paragraph when the expected label
    ("Invoice Date: ") isn't there, so a misaligned paragraph would pass as a
    value. A labelled field whose source lacks the label is rejected instead.
    """
    cls_by_node = {c.node_id: c for c in classifications}
    by_name = {f.field_name: f for f in fields}
    kept_all, kept_labelled, rejected = [], [], []
    for p in routing.placements:
        f = by_name.get(p.field_name)
        label = None
        for nid in (f.node_ids if f else []):
            c = cls_by_node.get(nid)
            if c is not None and c.static_prefix and c.static_prefix.strip():
                label = c.static_prefix
                break
        if label is None:
            kept_all.append(p)
        elif _starts_with_label(p.source_excerpt, label):
            kept_all.append(p)
            kept_labelled.append(p)
        else:
            rejected.append({
                "field_name": p.field_name, "expected_label": label.strip(),
                "found": p.source_excerpt,
            })
    return {"all": kept_all, "labelled": kept_labelled}, rejected


# --------------------------------------------------------------------------
# Values and rendering
# --------------------------------------------------------------------------


@dataclass
class Values:
    data: dict
    skipped_sections: list[dict]
    drafted: list[str]
    missing: list[str]


def parse_values(obj) -> Values:
    """Accept the plugin's values format, upstream's writer format, or a flat dict.

    - {"values": {...}, "drafted": [...], "skip_sections": [...], "missing": [...]}
    - {"placements": [{"field_name", "value", ...}], "skipped_sections": [...]}
    - {"field_name": value, ...}
    """
    if not isinstance(obj, dict):
        raise ToolkitError("values must be a JSON object")
    skipped_raw: list = []
    drafted: list[str] = []
    missing: list[str] = []
    if "placements" in obj and isinstance(obj["placements"], list):
        data = {
            p["field_name"]: p.get("value")
            for p in obj["placements"] if isinstance(p, dict) and p.get("field_name")
        }
        drafted = [p["field_name"] for p in obj["placements"] if isinstance(p, dict) and p.get("ai_drafted")]
        skipped_raw = obj.get("skipped_sections") or []
        missing = list(obj.get("missing_required") or [])
    elif "values" in obj and isinstance(obj["values"], dict):
        data = dict(obj["values"])
        skipped_raw = obj.get("skip_sections") or obj.get("skipped_sections") or []
        drafted = [str(x) for x in obj.get("drafted") or []]
        missing = [str(x) for x in obj.get("missing") or []]
    else:
        data = dict(obj)
    skipped = []
    for s in skipped_raw:
        if isinstance(s, str):
            skipped.append({"section_key": s, "reason": ""})
        elif isinstance(s, dict) and s.get("section_key"):
            skipped.append({"section_key": str(s["section_key"]), "reason": str(s.get("reason", ""))})
    return Values(data=data, skipped_sections=skipped, drafted=drafted, missing=missing)


def load_values(path: str | Path) -> Values:
    p = Path(path)
    return parse_values(read_json(p))


def _resolve_images(fields: list[FieldDefinition], data: dict, base_dir: Path | None) -> None:
    """Image values may name a file; the assembler wants bytes or base64."""
    for f in fields:
        if f.field_type != FieldType.IMAGE:
            continue
        value = data.get(f.field_name)
        if not isinstance(value, str) or not value.strip() or value.startswith("data:"):
            continue
        candidate = Path(value)
        if not candidate.is_absolute() and base_dir is not None:
            candidate = base_dir / candidate
        if candidate.is_file():
            data[f.field_name] = base64.b64encode(candidate.read_bytes()).decode("ascii")


@dataclass
class Rendered:
    docx: bytes
    context: dict
    routing: RoutingResult
    validation: ValidationReport | None
    values: Values


def render(
    tv: TemplateVersion,
    values: Values,
    *,
    defaults: dict | None = None,
    skip_validation: bool = False,
    base_dir: Path | None = None,
) -> Rendered:
    """Render one document, exactly as the web app does after its review screen."""
    from ..assembler import assemble
    from ..validator import validate

    fields = tv.fields()
    rules = tv.rules()
    data = dict(values.data)
    _resolve_images(fields, data, base_dir)
    routing = route_structured(fields, data, tv.template_id, tv.version)
    routing.skipped_sections = values.skipped_sections
    routing.source = "claude"
    context = merge_defaults(defaults or {}, routing.to_context())
    context.update(section_toggles(fields, routing, context))
    report = None if skip_validation else validate(context, fields, rules)
    docx = assemble(tv.template_bytes(), context, fields)
    return Rendered(docx=docx, context=context, routing=routing, validation=report, values=values)


def _is_empty(value) -> bool:
    return value is None or value == "" or value == []


def render_summary(tv: TemplateVersion, r: Rendered, *, output: Path, blocks: bool = False) -> dict:
    fields = tv.fields()
    filled, empty = [], []
    for f in fields:
        if f.field_type in (FieldType.BOOLEAN, FieldType.IMAGE):
            continue
        value = r.context.get(f.field_name)
        if _is_empty(value) and f.default in (None, ""):
            empty.append(f.field_name)
        else:
            filled.append(f.field_name)
    out: dict = {
        "output": str(output),
        "template": {"name": tv.name, "version": tv.version},
        "filled": filled,
        "empty": empty,
        "missing_required": r.routing.missing_required,
    }
    unknown = [u.removeprefix("unknown key: ") for u in r.routing.unmapped_content]
    if unknown:
        out["unknown_keys"] = unknown
    if r.values.drafted:
        out["drafted"] = r.values.drafted
    if r.values.skipped_sections:
        out["skipped_sections"] = r.values.skipped_sections
    if r.validation is not None:
        out["validation"] = {
            "status": r.validation.status.value,
            "issues": [
                {"field_name": i.field_name, "severity": i.severity.value, "message": i.message}
                for i in r.validation.issues
            ],
        }
    if blocks:
        out["blocks"] = parse_docx_blocks(r.docx)
    return out


# --------------------------------------------------------------------------
# Batch
# --------------------------------------------------------------------------


def _cell(value: str):
    """A CSV cell holding JSON (a list of rows, a list of points) is parsed."""
    s = value.strip()
    if s[:1] in ("[", "{"):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return value
    return value


def read_rows(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        raise ToolkitError(f"file not found: {p}")
    suffix = p.suffix.lower()
    if suffix == ".csv":
        with open(p, newline="", encoding="utf-8-sig") as fh:
            return [{k: _cell(v or "") for k, v in row.items() if k} for row in csv.DictReader(fh)]
    if suffix == ".jsonl":
        rows = []
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ToolkitError(f"{p.name} line {n}: invalid JSON: {exc}") from exc
        return rows
    data = read_json(p)
    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        data = data["rows"]
    if not isinstance(data, list):
        raise ToolkitError(f"{p.name}: expected a list of rows (or {{\"rows\": [...]}})")
    return data


def _row_name(pattern: str | None, row: dict, index: int, template_id: str) -> str:
    if pattern:
        flat = {k: v for k, v in row.items() if not isinstance(v, (list, dict))}
        try:
            text = pattern.format_map(_Blank(flat))
        except (ValueError, IndexError) as exc:
            raise ToolkitError(f"--name-pattern is not a valid pattern: {exc}") from exc
        slug = _FILENAME_JUNK.sub("-", text.lower()).strip("-")[:80].strip("-")
        if slug:
            return slug
    return f"{template_id}-{index:03d}"


_FILENAME_JUNK = re.compile(r"[^a-z0-9]+")


class _Blank(dict):
    def __missing__(self, key):
        return ""


def render_batch(
    tv: TemplateVersion,
    rows: list[dict],
    out_dir: str | Path,
    *,
    defaults: dict | None = None,
    name_pattern: str | None = None,
    base_dir: Path | None = None,
) -> dict:
    """Render one document per row (structured data: deterministic, no AI)."""
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    results = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            results.append({"row": i, "error": "row is not an object"})
            continue
        stem = _row_name(name_pattern, row, i, tv.template_id)
        base, n = stem, 2
        while stem in used:
            stem, n = f"{base}-{n}", n + 1
        used.add(stem)
        try:
            r = render(tv, parse_values(row), defaults=defaults, base_dir=base_dir)
        except ToolkitError as exc:
            results.append({"row": i, "error": str(exc)})
            continue
        path = dest / f"{stem}.docx"
        path.write_bytes(r.docx)
        item = {"row": i, "output": str(path)}
        if r.routing.missing_required:
            item["missing_required"] = r.routing.missing_required
        if r.validation is not None and r.validation.status.value != "pass":
            item["validation"] = r.validation.status.value
            item["issues"] = [i.message for i in r.validation.issues][:5]
        results.append(item)
    ok = sum(1 for x in results if "output" in x)
    return {"rendered": ok, "failed": len(results) - ok, "out_dir": str(dest), "documents": results}
