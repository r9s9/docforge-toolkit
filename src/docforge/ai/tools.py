"""Tools the agentic loop can call (OpenAI-compatible function calling).

A :class:`ToolSpec` pairs a JSON-schema function declaration with a deterministic
Python callable. The callable receives the parsed argument dict and returns a
JSON-serialisable result that is fed back to the model. Tools let the agent
*fetch evidence* (full node text, neighbours, diff samples) and *use precise
deterministic helpers* (date/number normalisation, the validator) instead of
guessing from truncated context.

Phase-specific tool sets are built by the ``*_tools`` factory functions, which
close over the current job/template data — so a tool can only ever read the
material for the action in progress (no cross-user access).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..common.textutil import value_kind


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema for the function arguments
    func: Callable[[dict], Any]

    def openai_schema(self) -> dict:
        """The OpenAI-compatible ``tools`` entry for this function."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def anthropic_schema(self) -> dict:
        """The Claude ``tools`` entry for this function.

        Requests stream, so tool inputs stream as they are written
        (``eager_input_streaming``); the agent loop checks each input is an
        object before running the tool, because eager streaming leaves that to
        the client.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
            "eager_input_streaming": True,
        }

    def run(self, args: dict) -> Any:
        return self.func(args or {})


def _str_param(name: str, desc: str) -> dict:
    return {
        "type": "object",
        "properties": {name: {"type": "string", "description": desc}},
        "required": [name],
    }


# --- generic value normalisers (reused by analysis + generation) ------------

def _normalize_date(args: dict) -> dict:
    text = str(args.get("text") or "").strip()
    if not text:
        return {"ok": False, "reason": "empty"}
    try:
        from dateutil import parser as dateparser

        dt = dateparser.parse(text, fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return {"ok": False, "input": text, "reason": "unparseable"}
    if dt is None:
        return {"ok": False, "input": text, "reason": "unparseable"}
    return {"ok": True, "input": text, "iso": dt.date().isoformat()}


def _normalize_number(args: dict) -> dict:
    import re

    text = str(args.get("text") or "").strip()
    if not text:
        return {"ok": False, "reason": "empty"}
    cleaned = text.replace(",", "")
    m = re.search(r"[-+]?\d*\.?\d+", cleaned)
    if not m:
        return {"ok": False, "input": text, "reason": "no number found"}
    raw = m.group(0)
    try:
        num = float(raw)
    except ValueError:
        return {"ok": False, "input": text, "reason": "no number found"}
    normalized = str(int(num)) if num.is_integer() else str(num)
    return {"ok": True, "input": text, "number": normalized}


def _detect_kind(args: dict) -> dict:
    text = str(args.get("text") or "")
    return {"text": text[:200], "kind": value_kind(text)}


def normalizer_tools() -> list[ToolSpec]:
    """Deterministic value helpers usable by any agentic step."""
    return [
        ToolSpec(
            name="normalize_date",
            description="Parse a human date string and return the canonical ISO date (YYYY-MM-DD).",
            parameters=_str_param("text", "A date in any format, e.g. 'next Friday', 'June 1 2026'."),
            func=_normalize_date,
        ),
        ToolSpec(
            name="normalize_number",
            description="Extract a numeric value from text (strips currency symbols and thousands separators).",
            parameters=_str_param("text", "A value like '$5,000.00', '5k', '12 units'."),
            func=_normalize_number,
        ),
        ToolSpec(
            name="detect_kind",
            description="Classify a value as one of: date | number | person | text.",
            parameters=_str_param("text", "The value to classify."),
            func=_detect_kind,
        ),
    ]


# --- analysis / classification tools ---------------------------------------

_NODE_PARAM = {
    "type": "object",
    "properties": {"node_id": {"type": "string", "description": "An element node_id from the document."}},
    "required": ["node_id"],
}
_NEIGHBOR_PARAM = {
    "type": "object",
    "properties": {
        "node_id": {"type": "string", "description": "The element node_id."},
        "radius": {"type": "integer", "description": "How many elements before/after to include (default 2)."},
    },
    "required": ["node_id"],
}


def classify_tools(extraction, diff=None) -> list[ToolSpec]:
    """Evidence tools for template analysis, scoped to one extraction + diff.

    Let the classifier read the *full* text of a node (past the truncated prompt),
    inspect neighbouring elements for context, and see exactly how a node varies
    across the uploaded examples — instead of guessing from a 200-char snippet.
    """
    by_id = {e.node_id: e for e in extraction.elements}
    order = [e.node_id for e in extraction.top_level_elements()]
    diff_by_node = {d.representative_node_id: d for d in (diff.node_diffs if diff else [])}

    def _view(e) -> dict:
        v: dict = {
            "node_id": e.node_id,
            "type": e.type.value,
            "style": e.style_name,
            "text": e.text or "",
            "hints": e.semantic_hints,
        }
        if e.table_structure:
            v["table_headers"] = e.table_structure.headers
            v["n_rows"] = e.table_structure.n_rows
        return v

    def get_node_text(args: dict) -> dict:
        e = by_id.get(args.get("node_id"))
        return _view(e) if e else {"error": "unknown node_id"}

    def get_neighbors(args: dict) -> dict:
        nid = args.get("node_id")
        if nid not in order:
            return {"error": "unknown node_id"}
        i = order.index(nid)
        radius = max(1, min(6, int(args.get("radius") or 2)))
        lo, hi = max(0, i - radius), min(len(order), i + radius + 1)
        return {"neighbors": [_view(by_id[order[j]]) for j in range(lo, hi)]}

    def get_diff_evidence(args: dict) -> dict:
        nd = diff_by_node.get(args.get("node_id"))
        if not nd:
            return {"has_evidence": False}
        return {
            "has_evidence": True,
            "status": nd.status.value,
            "samples": nd.sample_texts[:8],
            "detected_kind": nd.detected_kind,
            "static_prefix": nd.static_prefix,
            "row_count_variable": nd.row_count_variable,
            "confidence": round(nd.confidence, 2),
        }

    return [
        ToolSpec("get_node_text", "Full untruncated text + formatting of one element by node_id.", _NODE_PARAM, get_node_text),
        ToolSpec("get_neighbors", "The elements immediately before/after a node (by node_id), for surrounding context.", _NEIGHBOR_PARAM, get_neighbors),
        ToolSpec("get_diff_evidence", "All cross-document sample values for a node — how it varies across the uploaded examples.", _NODE_PARAM, get_diff_evidence),
    ]


# --- generation / compose tools --------------------------------------------

_FIELD_PARAM = {
    "type": "object",
    "properties": {"field_name": {"type": "string", "description": "A template field_name."}},
    "required": ["field_name"],
}
_VALIDATE_PARAM = {
    "type": "object",
    "properties": {
        "field_name": {"type": "string", "description": "A template field_name."},
        "value": {"description": "The candidate value to check (string/number/list)."},
    },
    "required": ["field_name", "value"],
}


def validate_field_value(f, value) -> dict:
    """Lightweight, deterministic check of a value against a field's type/enum."""
    ft = f.field_type.value
    if value in (None, ""):
        return {"ok": not f.required, "reason": "empty" if f.required else "empty (optional)"}
    s = str(value)
    if ft == "date":
        ok = _normalize_date({"text": s}).get("ok", False)
        return {"ok": ok, "reason": "" if ok else "not a recognizable date"}
    if ft == "number":
        ok = _normalize_number({"text": s}).get("ok", False)
        return {"ok": ok, "reason": "" if ok else "not numeric"}
    if ft == "enum" and f.enum_values:
        ok = s in f.enum_values
        return {"ok": ok, "reason": "" if ok else f"must be one of {f.enum_values}"}
    return {"ok": True, "reason": ""}


def compose_tools(fields) -> list[ToolSpec]:
    """Field-spec lookup + value validation for the generation compose step."""
    by_name = {f.field_name: f for f in fields}

    def get_field_spec(args: dict) -> dict:
        f = by_name.get(args.get("field_name"))
        if not f:
            return {"error": "unknown field_name"}
        spec: dict = {
            "field_name": f.field_name,
            "label": f.label,
            "type": f.field_type.value,
            "required": f.required,
            "description": f.description,
        }
        if f.enum_values:
            spec["allowed_values"] = f.enum_values
        if f.columns:
            spec["columns"] = [
                {"field_name": c.field_name, "label": c.label, "type": c.field_type.value}
                for c in f.columns
            ]
        return spec

    def validate_value(args: dict) -> dict:
        f = by_name.get(args.get("field_name"))
        if not f:
            return {"ok": False, "reason": "unknown field_name"}
        return validate_field_value(f, args.get("value"))

    return normalizer_tools() + [
        ToolSpec("get_field_spec", "Type, description, required flag, allowed values and columns for a field.", _FIELD_PARAM, get_field_spec),
        ToolSpec("validate_value", "Check whether a value satisfies a field's type and allowed values.", _VALIDATE_PARAM, validate_value),
    ]


_SECTION_PARAM = {
    "type": "object",
    "properties": {"section_key": {"type": "string", "description": "A section_key from the template."}},
    "required": ["section_key"],
}
_BLOCK_PARAM = {
    "type": "object",
    "properties": {"block_id": {"type": "string", "description": "A block id such as 'b7' from the outline."}},
    "required": ["block_id"],
}


def writer_tools(fields, template_context: dict | None = None, source_doc=None) -> list[ToolSpec]:
    """Compose's tools plus the two lookups a whole-document writer needs.

    ``get_section_context`` answers "what is this part of the document for, and
    what else lives in it"; ``get_source_block`` recovers the full text behind an
    outline entry the prompt had to truncate.
    """
    sections = {s["section_key"]: s for s in (template_context or {}).get("sections", [])}
    blocks: dict[str, str] = {}
    if source_doc is not None:
        from ..ai_router.document import source_blocks

        blocks = source_blocks(source_doc)

    def get_section_context(args: dict) -> dict:
        section = sections.get(args.get("section_key"))
        if not section:
            return {"error": "unknown section_key", "known": list(sections)}
        return {
            **section,
            "other_sections": [k for k in sections if k != section["section_key"]],
        }

    def get_source_block(args: dict) -> dict:
        if not blocks:
            return {"error": "no source document for this generation"}
        text = blocks.get(args.get("block_id"))
        if text is None:
            return {"error": "unknown block_id"}
        return {"block_id": args.get("block_id"), "text": text}

    return compose_tools(fields) + [
        ToolSpec(
            "get_section_context",
            "What a section of the document is for, what content belongs there, and which other sections exist.",
            _SECTION_PARAM,
            get_section_context,
        ),
        ToolSpec(
            "get_source_block",
            "Full untruncated text of a block from the uploaded document's outline, by its id.",
            _BLOCK_PARAM,
            get_source_block,
        ),
    ]
