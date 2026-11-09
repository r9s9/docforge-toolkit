"""Make a template: analyze examples, show Claude the evidence, apply its decisions.

The flow mirrors the DocForge web app. ``analyze`` reads the examples, compares
them, and runs the deterministic classifier for a first proposal. Claude then
reviews the proposal: where upstream calls a model for the "understand" and
"critique" passes, the skill has Claude read ``outline``/``node``/``evidence``
and write ``decisions.json``. ``build`` applies those decisions exactly as the
app's publish step applies a reviewer's edits, and writes a template package.
"""

from __future__ import annotations

import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..ai.tools import classify_tools
from ..ai_classifier import classify, derive_field_definitions, derive_validation_rules
from ..ai_classifier.tags_only import enforce_tags_only
from ..multi_doc_differ import diff_documents, pick_representative
from ..schemas.classification import (
    ClassificationResult,
    ElementClassification,
    SectionUnderstanding,
)
from ..schemas.diff import DiffRunResult
from ..schemas.enums import (
    ClassificationType,
    ElementType,
    FieldType,
    default_field_type,
    needs_field,
)
from ..schemas.extraction import DocumentExtraction
from ..schemas.template import (
    FieldDefinition,
    ReviewSnapshot,
    TemplateIntelligence,
    ValidationRule,
)
from ..structure_normalizer import build_extraction
from .common import MAX_EXAMPLES, ToolkitError, check_docx, read_json, safe_filename, write_json
from .package import PackageWriter, TemplateVersion
from .samples import sample_context

WORKSPACE = "workspace.json"
PROPOSAL = "proposal.json"
DIFF = "diff.json"
DECISIONS = "decisions.json"

# "full" is the web app's "Full template" (tags-only) mode.
MODES = {"smart": "smart", "full": "tags_only"}

TEXT_CHARS = 200  # upstream ai/prompts._node_payload truncation
SAMPLE_CHARS = 80

# Field names: snake_case Jinja identifiers that can't collide with the
# builder's own template variables.
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_RESERVED = {
    "and", "caller", "elif", "else", "endfor", "endif", "false", "for", "if", "in",
    "is", "item", "kwargs", "loop", "none", "not", "or", "range", "self", "super",
    "true", "varargs",
}
_DECISION_KEYS = {
    "node_id", "classification", "field_name", "field_type", "description", "required",
    "optional", "static_prefix", "static_suffix", "enum_values", "confidence", "rationale",
}
_FIELD_OVERRIDE_KEYS = {"label", "description", "required", "default", "enum_values", "columns"}
_COLUMN_OVERRIDE_KEYS = {"field_name", "label", "field_type", "required"}


def needs_review(c: ElementClassification) -> bool:
    """The nodes upstream's self-critique pass re-examines (ai_classifier/llm.py)."""
    if c.confidence < 0.6:
        return True
    if c.classification == ClassificationType.UNKNOWN:
        return True
    if c.field_name and not (c.description or "").strip():
        return True
    return False


def drop_loop_owned(rep: DocumentExtraction, result: ClassificationResult) -> list[str]:
    """Cells inside a repeating table belong to the table's loop, not to fields.

    The heuristic classifies paragraphs inside table cells too, and "Full
    template" mode turns any classified text into a field. The builder never
    renders a field inside a looped table (the loop owns its cells), so those
    would be phantom fields: listed, but never filled. Layout tables are not
    affected: a cell in a fixed table can still be a field.
    """
    loops = _loop_tables(result)
    if not loops:
        return []
    parent = {e.node_id: e.parent_node_id for e in rep.elements}
    dropped = []
    for c in result.classifications:
        if c.field_name and _in_loop(c.node_id, parent, loops):
            dropped.append(c.field_name)
            c.classification = ClassificationType.FIXED
            c.field_name = None
            c.field_type = None
    return dropped


def _loop_tables(result: ClassificationResult) -> set[str]:
    return {
        c.node_id for c in result.classifications
        if c.classification == ClassificationType.REPEATABLE_TABLE
    }


def _in_loop(node_id: str, parent: dict[str, str | None], loops: set[str]) -> bool:
    seen: set[str] = set()
    p = parent.get(node_id)
    while p and p not in seen:
        if p in loops:
            return True
        seen.add(p)
        p = parent.get(p)
    return False


def tag_every_cell(rep: DocumentExtraction, result: ClassificationResult) -> list[str]:
    """"Full template" also turns the text in nested tables into fields.

    Upstream's tags-only pass gives each cell of a layout table its own field,
    but only one level down. A table inside a cell, such as the date, document
    number and revision strip at the foot of a cover block, kept its text
    fixed. Its cells get fields the same way here. Cells in a repeating table
    stay with that table's loop.
    """
    from ..ai_classifier.tags_only import (
        _MULTILINE_THRESHOLD,
        _cell_name,
        _force_dynamic,
        _is_exempt,
        _unique,
    )

    parent = {e.node_id: e.parent_node_id for e in rep.elements}
    loops = _loop_tables(result)
    by_node = {c.node_id: c for c in result.classifications}
    used = {c.field_name for c in result.classifications if c.field_name}
    tagged = []
    for e in rep.elements:
        if e.parent_node_id is None or e.type == ElementType.TABLE:
            continue
        if _in_loop(e.node_id, parent, loops):
            continue
        c = by_node.get(e.node_id)
        if _is_exempt(e, c):
            continue
        if c is not None and c.field_name and needs_field(c.classification):
            continue
        if c is None:
            c = ElementClassification(node_id=e.node_id, source=result.source or "heuristic")
            result.classifications.append(c)
            by_node[e.node_id] = c
        text = (e.text or "").strip()
        name = _unique(_cell_name(text), used)
        used.add(name)
        _force_dynamic(
            c,
            classification=ClassificationType.DYNAMIC_TEXT,
            field_type=(
                FieldType.MULTILINE_TEXT if len(text) > _MULTILINE_THRESHOLD else FieldType.TEXT
            ),
            name=name,
            section="",
            text=text,
        )
        tagged.append(name)
    return tagged


def keep_page_fields(rep: DocumentExtraction, result: ClassificationResult) -> list[str]:
    """Leave page numbers and other header/footer Word fields alone.

    "Full template" mode treats any paragraph holding a Word field as a
    generated listing, like a table of contents, and the builder then strips
    the field so the tag isn't overwritten. That is right for a contents list
    in the body, but in a footer it deletes the page number. Header and footer
    paragraphs marked that way go back to being Word auto-fields.
    """
    from ..ai_classifier.tags_only import GENERATED_MARK

    scope = {e.node_id: e.header_footer_scope for e in rep.elements}
    restored = []
    for c in result.classifications:
        if GENERATED_MARK in (c.rationale or "") and scope.get(c.node_id):
            restored.append(c.field_name or c.node_id)
            c.classification = ClassificationType.AUTO_FIELD
            c.field_name = None
            c.field_type = None
            c.rationale = (c.rationale or "").replace(GENERATED_MARK, "").strip()
    return restored


# --------------------------------------------------------------------------
# Workspace
# --------------------------------------------------------------------------


@dataclass
class Workspace:
    root: Path
    meta: dict
    extractions: list[DocumentExtraction]
    diff: DiffRunResult | None
    proposal: ClassificationResult

    @property
    def rep_index(self) -> int:
        return int(self.meta["representative"])

    @property
    def rep(self) -> DocumentExtraction:
        return self.extractions[self.rep_index]

    @property
    def rep_source(self) -> Path:
        return self.root / self.meta["documents"][self.rep_index]["source"]

    @property
    def mode(self) -> str:
        return self.meta["mode"]


def load_workspace(path: str | Path) -> Workspace:
    root = Path(path)
    if not (root / WORKSPACE).is_file():
        raise ToolkitError(
            f"{root} is not an analysis workspace",
            hint="Run `analyze <examples…> --work <folder>` first.",
        )
    meta = read_json(root / WORKSPACE)
    extractions = [
        DocumentExtraction.model_validate(read_json(root / d["extraction"]))
        for d in meta["documents"]
    ]
    diff = DiffRunResult.model_validate(read_json(root / DIFF)) if (root / DIFF).is_file() else None
    proposal = ClassificationResult.model_validate(read_json(root / PROPOSAL))
    return Workspace(root=root, meta=meta, extractions=extractions, diff=diff, proposal=proposal)


def analyze(files: list[str], work: str | Path, mode: str = "full") -> dict:
    """Read, compare and classify 1–5 example documents into a workspace."""
    if mode not in MODES:
        raise ToolkitError(f"unknown mode {mode!r}; use one of {sorted(MODES)}")
    if not files:
        raise ToolkitError("give at least one example .docx")
    if len(files) > MAX_EXAMPLES:
        raise ToolkitError(f"at most {MAX_EXAMPLES} example documents per template")
    for f in files:
        check_docx(f)

    root = Path(work)
    if root.exists():
        if not root.is_dir():
            raise ToolkitError(f"{root} exists and is not a folder")
        if any(root.iterdir()) and not (root / WORKSPACE).is_file():
            raise ToolkitError(f"{root} is not empty; choose a new folder for the workspace")
        # Re-analysis: node ids may change, so earlier decisions move to decisions.previous.json.
        old = root / DECISIONS
        if old.is_file():
            old.replace(root / "decisions.previous.json")
        for sub in ("sources", "extractions"):
            shutil.rmtree(root / sub, ignore_errors=True)
        (root / DIFF).unlink(missing_ok=True)
    (root / "sources").mkdir(parents=True, exist_ok=True)
    (root / "extractions").mkdir(parents=True, exist_ok=True)

    extractions: list[DocumentExtraction] = []
    docs: list[dict] = []
    for i, f in enumerate(files, start=1):
        src = Path(f)
        dest = root / "sources" / f"{i}-{safe_filename(src.name, 'example.docx')}"
        shutil.copyfile(src, dest)
        ex = build_extraction(dest, document_id=f"doc{i}", filename=src.name)
        write_json(root / "extractions" / f"{i}.json", ex.model_dump(mode="json"))
        extractions.append(ex)
        docs.append({
            "file": src.name,
            "source": f"sources/{dest.name}",
            "extraction": f"extractions/{i}.json",
            "elements": len(ex.top_level_elements()),
        })

    diff = diff_documents(extractions) if len(extractions) >= 2 else None
    rep_index = pick_representative(extractions) if len(extractions) >= 2 else 0
    proposal = classify(extractions[rep_index], diff, mode=MODES[mode])
    drop_loop_owned(extractions[rep_index], proposal)
    keep_page_fields(extractions[rep_index], proposal)
    if mode == "full":
        tag_every_cell(extractions[rep_index], proposal)

    meta = {
        "format": "docforge-workspace",
        "format_version": 1,
        "mode": mode,
        "documents": docs,
        "representative": rep_index,
    }
    write_json(root / WORKSPACE, meta)
    write_json(root / PROPOSAL, proposal.model_dump(mode="json"))
    if diff is not None:
        write_json(root / DIFF, diff.model_dump(mode="json"))
    if not (root / DECISIONS).is_file():
        write_json(root / DECISIONS, {
            "document_type": proposal.document_type_guess,
            "sections": [],
            "classifications": [],
            "fields": {},
            "image_fields": [],
        })
    ws = Workspace(root=root, meta=meta, extractions=extractions, diff=diff, proposal=proposal)
    return summarize(ws)


def _field_line(f: FieldDefinition, by_id: dict) -> dict:
    line: dict = {
        "field_name": f.field_name,
        "type": f.field_type.value,
        "classification": f.classification.value,
        "label": f.label,
        "nodes": f.node_ids,
    }
    if f.columns:
        line["columns"] = [c.field_name for c in f.columns]
    el = by_id.get(f.node_ids[0]) if f.node_ids else None
    example = " ".join((el.text if el else "").split())
    if example:
        line["example"] = example[:SAMPLE_CHARS] + ("…" if len(example) > SAMPLE_CHARS else "")
    if f.confidence < 0.6:
        line["low_confidence"] = True
    return line


def summarize(ws: Workspace, result: ClassificationResult | None = None) -> dict:
    """The proposal at a glance: what varies, what's fixed, what to review."""
    result = result or ws.proposal
    rep = ws.rep
    by_id = {e.node_id: e for e in rep.elements}
    fields = derive_field_definitions(rep, result.model_copy(deep=True))
    counts = Counter(c.classification.value for c in result.classifications)
    # Only body-level elements: cells inside a table belong to the table's own
    # decision, and upstream's review pass never sees them either.
    top_level = {e.node_id for e in rep.top_level_elements()}
    review = [
        c.node_id for c in result.classifications
        if needs_review(c) and c.node_id in top_level
    ]
    return {
        "workspace": str(ws.root),
        "mode": ws.mode,
        "documents": [
            {"file": d["file"], "elements": d["elements"], "representative": i == ws.rep_index}
            for i, d in enumerate(ws.meta["documents"])
        ],
        "document_type_guess": result.document_type_guess,
        "classified_nodes": dict(sorted(counts.items())),
        "proposed_fields": [
            _field_line(f, by_id) for f in fields if f.field_type != FieldType.IMAGE
        ],
        "pictures": [
            {"field_name": f.field_name, "node": f.node_ids[0]}
            for f in fields if f.field_type == FieldType.IMAGE
        ],
        "needs_review": {"count": len(review), "node_ids": review[:40]},
        "decisions_file": str(ws.root / DECISIONS),
    }


# --------------------------------------------------------------------------
# Evidence views (mirror upstream ai/tools.classify_tools)
# --------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…"


def _node_row(e, c, nd, text_chars: int) -> dict:
    row: dict = {"node_id": e.node_id, "type": e.type.value}
    if e.parent_node_id:
        # Inside a table cell: which table, and which cell ("r0c1").
        row["in_table"] = e.parent_node_id
        if e.subtype:
            row["cell"] = e.subtype
    if e.style_name:
        row["style"] = e.style_name
    row["text"] = _clip(e.text or "", text_chars)
    if e.semantic_hints:
        row["hints"] = e.semantic_hints
    if e.type == ElementType.TABLE and e.table_structure:
        row["table_headers"] = e.table_structure.headers
        row["n_rows"] = e.table_structure.n_rows
    if c is not None:
        prop: dict = {"classification": c.classification.value}
        if c.field_name:
            prop["field_name"] = c.field_name
            prop["field_type"] = c.field_type.value if c.field_type else None
        if c.static_prefix:
            prop["static_prefix"] = c.static_prefix
        if c.static_suffix:
            prop["static_suffix"] = c.static_suffix
        if c.optional:
            prop["optional"] = True
        if c.links_to:
            prop["links_to"] = c.links_to
        prop["confidence"] = round(c.confidence, 2)
        if needs_review(c):
            prop["review"] = True
        row["proposal"] = prop
    if nd is not None:
        ev = {
            "status": nd.status.value,
            "samples": [s[:SAMPLE_CHARS] for s in nd.sample_texts[:4]],
            "detected_kind": nd.detected_kind,
            "static_prefix": nd.static_prefix,
            "row_count_variable": nd.row_count_variable,
            "optional": nd.is_optional or None,
            "confidence": round(nd.confidence, 2),
        }
        row["evidence"] = {k: v for k, v in ev.items() if v not in (None, [], "")}
    return row


def outline(
    ws: Workspace, *, offset: int = 0, limit: int = 80, only: str = "all",
    text_chars: int = TEXT_CHARS,
) -> dict:
    """The representative document in order, with the proposal and diff evidence.

    The text inside table cells is listed right after its table, marked with
    ``in_table`` and ``cell``, so the text of a cover block or another layout
    table can be classified one paragraph at a time. Cells of a table proposed
    as a loop are left out: the loop fills them.
    """
    if only not in ("all", "review", "fields"):
        raise ToolkitError("--only must be one of: all, review, fields")
    cls = {c.node_id: c for c in ws.proposal.classifications}
    diff_by = {d.representative_node_id: d for d in (ws.diff.node_diffs if ws.diff else [])}
    parent = {e.node_id: e.parent_node_id for e in ws.rep.elements}
    loops = _loop_tables(ws.proposal)
    rows = []
    for e in ws.rep.elements:
        if e.parent_node_id and _in_loop(e.node_id, parent, loops):
            continue
        c = cls.get(e.node_id)
        if only == "review" and not (c and needs_review(c)):
            continue
        if only == "fields" and not (c and needs_field(c.classification)):
            continue
        rows.append(_node_row(e, c, diff_by.get(e.node_id), text_chars))
    page = rows[offset: offset + limit] if limit > 0 else rows[offset:]
    end = offset + len(page)
    return {
        "total": len(rows),
        "offset": offset,
        "returned": len(page),
        "next_offset": end if end < len(rows) else None,
        "samples_compared": ws.diff.n_documents if ws.diff else 1,
        "nodes": page,
    }


def _tool(ws: Workspace, name: str):
    return {t.name: t for t in classify_tools(ws.rep, ws.diff)}[name]


def _known(ws: Workspace, node_id: str) -> None:
    if ws.rep.by_id(node_id) is None:
        raise ToolkitError(f"unknown node_id {node_id!r}", hint="Node ids come from `outline`.")


def node(ws: Workspace, node_id: str) -> dict:
    """Full text and formatting of one element, its proposal and its diff evidence."""
    _known(ws, node_id)
    out = _tool(ws, "get_node_text").run({"node_id": node_id})
    c = ws.proposal.by_node(node_id)
    if c is not None:
        out["proposal"] = c.model_dump(mode="json", exclude_defaults=True)
    out["evidence"] = _tool(ws, "get_diff_evidence").run({"node_id": node_id})
    return out


def neighbors(ws: Workspace, node_id: str, radius: int = 2) -> dict:
    _known(ws, node_id)
    return _tool(ws, "get_neighbors").run({"node_id": node_id, "radius": radius})


def evidence(ws: Workspace, node_id: str) -> dict:
    _known(ws, node_id)
    return _tool(ws, "get_diff_evidence").run({"node_id": node_id})


# --------------------------------------------------------------------------
# Decisions -> classification, fields, rules
# --------------------------------------------------------------------------


@dataclass
class Draft:
    result: ClassificationResult
    fields: list[FieldDefinition]
    rules: list[ValidationRule]
    warnings: list[str] = field(default_factory=list)


def _check_name(name: str, where: str, errors: list[str], warnings: list[str]) -> None:
    if not _FIELD_NAME.match(name):
        errors.append(
            f"{where}: field_name {name!r} must be snake_case: lowercase letters, digits "
            "and underscores, starting with a letter"
        )
    elif name in _RESERVED:
        errors.append(f"{where}: field_name {name!r} is reserved; pick another name")
    elif name.startswith(("show_", "_show_")):
        errors.append(f"{where}: field_name {name!r} looks like a section toggle; rename it")
    elif len(name) > 32:
        warnings.append(f"{where}: field_name {name!r} is long; tags read best at 1–3 words")


def _apply_classification(c: ElementClassification, d: dict, where: str, errors: list[str]) -> bool:
    if "classification" in d:
        try:
            c.classification = ClassificationType(str(d["classification"]).strip().upper())
        except ValueError:
            errors.append(
                f"{where}: unknown classification {d['classification']!r}; use one of "
                + ", ".join(t.value for t in ClassificationType)
            )
            return False
    if "field_type" in d:
        ft = d["field_type"]
        if ft in (None, ""):
            c.field_type = None
        else:
            try:
                c.field_type = FieldType(str(ft).strip().lower())
            except ValueError:
                errors.append(
                    f"{where}: unknown field_type {ft!r}; use one of "
                    + ", ".join(t.value for t in FieldType)
                )
                return False
    # Optional strings accept null; plain strings never hold None, or the
    # package would fail validation when it is read back.
    for key in ("field_name", "static_prefix", "static_suffix"):
        if key in d:
            value = d[key]
            setattr(c, key, None if value is None else str(value))
    if c.field_name is not None:
        c.field_name = c.field_name.strip() or None
    for key in ("description", "rationale"):
        if key in d:
            setattr(c, key, str(d[key] or ""))
    for key in ("required", "optional"):
        if key in d:
            setattr(c, key, bool(d[key]))
    if "enum_values" in d:
        c.enum_values = [str(v) for v in (d["enum_values"] or [])]
    c.confidence = float(d.get("confidence", 0.95))
    c.source = "llm"
    if needs_field(c.classification):
        if not c.field_name:
            errors.append(f"{where}: {c.classification.value} needs a field_name")
            return False
        if c.field_type is None:
            c.field_type = default_field_type(c.classification)
    else:
        # Fixed text, Word fields and unknowns never carry a field.
        c.field_name = None
        c.field_type = None
    return True


def _apply_field_overrides(fields: list[FieldDefinition], overrides: dict, errors: list[str], warnings: list[str]) -> None:
    by_name = {f.field_name: f for f in fields}
    for name, ov in (overrides or {}).items():
        where = f"fields.{name}"
        f = by_name.get(name)
        if f is None:
            errors.append(f"{where}: no such field (fields are: {', '.join(by_name) or 'none'})")
            continue
        if not isinstance(ov, dict):
            errors.append(f"{where}: expected an object of changes")
            continue
        for key, value in ov.items():
            if key not in _FIELD_OVERRIDE_KEYS:
                if key in ("field_name", "field_type", "classification"):
                    errors.append(
                        f"{where}.{key}: change this in the node's entry under "
                        "\"classifications\" instead"
                    )
                else:
                    errors.append(f"{where}.{key}: unknown key")
                continue
            if key == "columns":
                _apply_column_overrides(f, value, f"{where}.columns", errors, warnings)
            elif key == "enum_values":
                f.enum_values = [str(v) for v in (value or [])]
            elif key == "required":
                f.required = bool(value)
            else:
                setattr(f, key, value)


def _apply_column_overrides(f: FieldDefinition, value: dict, where: str, errors: list[str], warnings: list[str]) -> None:
    if f.field_type != FieldType.TABLE or not f.columns:
        errors.append(f"{where}: {f.field_name!r} has no columns")
        return
    cols = {c.field_name: c for c in f.columns}
    for col_name, changes in (value or {}).items():
        col = cols.get(col_name)
        if col is None:
            errors.append(f"{where}.{col_name}: no such column (columns are: {', '.join(cols)})")
            continue
        for key, v in (changes or {}).items():
            if key not in _COLUMN_OVERRIDE_KEYS:
                errors.append(f"{where}.{col_name}.{key}: unknown key")
            elif key == "field_name":
                _check_name(str(v), f"{where}.{col_name}", errors, warnings)
                col.field_name = str(v)
            elif key == "field_type":
                try:
                    col.field_type = FieldType(str(v).lower())
                except ValueError:
                    errors.append(f"{where}.{col_name}: unknown field_type {v!r}")
            elif key == "required":
                col.required = bool(v)
            else:
                col.label = str(v)
    names = [c.field_name for c in f.columns]
    if len(names) != len(set(names)):
        errors.append(f"{where}: column names must be unique ({names})")


def apply_decisions(ws: Workspace, decisions: dict) -> Draft:
    """Apply Claude's decisions to the proposal, the way publish applies review edits."""
    if not isinstance(decisions, dict):
        raise ToolkitError("decisions must be a JSON object")
    result = ws.proposal.model_copy(deep=True)
    rep = ws.rep
    known_ids = {e.node_id for e in rep.elements}
    by_node = {c.node_id: c for c in result.classifications}
    errors: list[str] = []
    warnings: list[str] = []

    for i, d in enumerate(decisions.get("classifications") or []):
        where = f"classifications[{i}]"
        if not isinstance(d, dict) or not d.get("node_id"):
            errors.append(f"{where}: needs a node_id")
            continue
        nid = str(d["node_id"])
        where = f"{where} ({nid})"
        if nid not in known_ids:
            errors.append(f"{where}: unknown node_id; ids come from `outline`")
            continue
        extra = sorted(set(d) - _DECISION_KEYS)
        if extra:
            warnings.append(f"{where}: ignored unknown keys {extra}")
        c = by_node.get(nid)
        if c is None:
            c = ElementClassification(node_id=nid)
            result.classifications.append(c)
            by_node[nid] = c
        _apply_classification(c, d, where, errors)

    if decisions.get("document_type"):
        result.document_type_guess = str(decisions["document_type"])
    if decisions.get("sections"):
        try:
            result.sections = [SectionUnderstanding.model_validate(s) for s in decisions["sections"]]
        except Exception as exc:  # noqa: BLE001 - pydantic's message is specific enough
            errors.append(f"sections: {exc}")

    if ws.mode == "full":
        # "Full template": the finished template holds only placeholders.
        enforce_tags_only(rep, result)
        keep_page_fields(rep, result)
    drop_loop_owned(rep, result)
    if ws.mode == "full":
        tag_every_cell(rep, result)

    names_by_kind: dict[str, set[str]] = {}
    for c in result.classifications:
        if c.field_name and needs_field(c.classification):
            _check_name(c.field_name, c.node_id, errors, warnings)
            names_by_kind.setdefault(c.field_name, set()).add(c.classification.value)
    for name, kinds in names_by_kind.items():
        if len(kinds) > 1:
            errors.append(f"field_name {name!r} is used for different kinds of content ({sorted(kinds)})")

    if errors:
        raise ToolkitError(
            "decisions.json has problems:\n- " + "\n- ".join(errors),
            hint="Fix these entries and run the command again.",
        )

    fields = derive_field_definitions(rep, result)
    image_names = {f.field_name for f in fields if f.field_type == FieldType.IMAGE}
    keep_images = {str(n) for n in (decisions.get("image_fields") or [])}
    unknown_images = sorted(keep_images - image_names)
    if unknown_images:
        errors.append(
            f"image_fields: unknown picture(s) {unknown_images}; pictures are "
            f"{sorted(image_names) or 'none'}"
        )
    # Pictures stay exactly as in the example unless chosen (logos, signatures).
    fields = [f for f in fields if f.field_type != FieldType.IMAGE or f.field_name in keep_images]
    _apply_field_overrides(fields, decisions.get("fields") or {}, errors, warnings)
    if errors:
        raise ToolkitError(
            "decisions.json has problems:\n- " + "\n- ".join(errors),
            hint="Fix these entries and run the command again.",
        )
    if not [f for f in fields if f.field_type != FieldType.BOOLEAN]:
        warnings.append("no fillable fields: the template would reproduce the example as-is")
    final_names = {f.field_name for f in fields}
    for s in result.sections:
        unknown = [n for n in s.field_names if n not in final_names]
        if unknown:
            warnings.append(
                f"section {s.section_key!r} lists field(s) that don't exist: {unknown} "
                "(renamed or removed?)"
            )
    rules = derive_validation_rules(fields)
    return Draft(result=result, fields=fields, rules=rules, warnings=warnings)


def load_decisions(ws: Workspace, path: str | Path | None) -> dict:
    p = Path(path) if path else ws.root / DECISIONS
    return read_json(p) if p.is_file() else {}


# --------------------------------------------------------------------------
# Preview and build
# --------------------------------------------------------------------------


def preview(ws: Workspace, draft: Draft, *, tags: bool = False) -> bytes:
    """The template these decisions produce, with tags or with «Label» values."""
    from ..template_builder import build_template_docx

    template_bytes = build_template_docx(str(ws.rep_source), draft.result, draft.fields)
    if tags:
        return template_bytes
    from ..assembler import assemble

    return assemble(template_bytes, sample_context(draft.fields), draft.fields)


def build(ws: Workspace, draft: Draft, *, out: str | Path, name: str, notes: str = "") -> tuple[TemplateVersion, list[str]]:
    """Build the template and write the next version of the package at ``out``."""
    from ..template_builder import build_template_docx

    warnings = list(draft.warnings)
    writer = PackageWriter(out, name)
    template_bytes = build_template_docx(str(ws.rep_source), draft.result, draft.fields)
    try:
        from ..assembler import assemble

        labels = assemble(template_bytes, sample_context(draft.fields), draft.fields)
    except ImportError:
        labels = None
        warnings.append("docxtpl is not installed, so the with-labels copy was skipped")

    result = draft.result
    intelligence = TemplateIntelligence(
        template_id=writer.template_id,
        version=writer.version,
        document_type_guess=result.document_type_guess,
        sections=result.sections,
        classifications=result.classifications,
        diff_summary=ws.diff.summary if ws.diff else None,
        model_used_for_analysis="claude",
        notes=notes,
    )
    review = ReviewSnapshot(
        document_type_guess=result.document_type_guess,
        classifications=result.classifications,
        field_definitions=draft.fields,
        validation_rules=draft.rules,
        sections=result.sections,
        edited_by_user=True,
    )
    sources: dict[str, bytes] = {}
    extracted: dict[str, dict] = {}
    for d, ex in zip(ws.meta["documents"], ws.extractions, strict=True):
        sources[d["file"]] = (ws.root / d["source"]).read_bytes()
        extracted[d["file"]] = ex.model_dump(mode="json")
    tv = writer.write(
        template_docx=template_bytes,
        labels_docx=labels,
        intelligence=intelligence,
        fields=draft.fields,
        rules=draft.rules,
        review=review,
        source_examples=sources,
        extracted_sources=extracted,
        representative_extraction=ws.rep.model_dump(mode="json"),
        representative_docx=ws.rep_source.read_bytes(),
        notes=notes,
    )
    return tv, warnings
