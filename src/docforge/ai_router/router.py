"""Heuristic content routing (deterministic, offline fallback).

Structured input is mapped key-by-key. Unstructured input is parsed line-by-line:
"Label: value" lines are matched to fields by name/label similarity; a single
remaining free-text field absorbs the leftover prose. Table fields are left to
the LLM router (heuristics can't reliably infer rows) and flagged if required.
"""

from __future__ import annotations

from typing import Any

from ..common.textutil import slugify_field
from ..schemas.enums import FieldType
from ..schemas.routing import PlacementInstruction, RoutingResult
from ..schemas.template import FieldDefinition


def _tokens(s: str) -> set[str]:
    return {t for t in slugify_field(s, fallback="").split("_") if t}


def _match_field(fields: list[FieldDefinition], key: str, label: str) -> FieldDefinition | None:
    best: FieldDefinition | None = None
    best_score = 0.0
    key_tokens = _tokens(key) or _tokens(label)
    for f in fields:
        if f.field_name == key:
            return f
        score = 0.0
        if slugify_field(f.label) == key:
            score = 0.9
        else:
            ft = _tokens(f.field_name) | _tokens(f.label)
            if key_tokens and ft:
                overlap = len(key_tokens & ft) / len(key_tokens | ft)
                score = overlap
        if score > best_score:
            best, best_score = f, score
    return best if best_score >= 0.5 else None


def route_structured(
    fields: list[FieldDefinition], data: dict[str, Any], template_id: str, version: int
) -> RoutingResult:
    placements: list[PlacementInstruction] = []
    field_names = {f.field_name for f in fields}
    for f in fields:
        if f.field_name in data and data[f.field_name] is not None:
            placements.append(
                PlacementInstruction(
                    field_name=f.field_name,
                    value=data[f.field_name],
                    confidence=1.0,
                    source_excerpt="(structured input)",
                )
            )
    placed = {p.field_name for p in placements}
    missing = [f.field_name for f in fields if f.required and f.field_name not in placed]
    unmapped = [f"unknown key: {k}" for k in data if k not in field_names]
    return RoutingResult(
        template_id=template_id,
        version=version,
        placements=placements,
        missing_required=missing,
        unmapped_content=unmapped,
        source="structured",
    )


# Minimum fraction of a field's descriptive words (label + description) that
# must appear in a leftover line/paragraph for it to be matched to that field —
# tuned low because the field side is short and specific while user prose is
# long and free-form, so even a partial keyword hit is a real signal.
_PROSE_MATCH_THRESHOLD = 0.34


def _field_topic_tokens(f: FieldDefinition) -> set[str]:
    return _tokens(f.label) | _tokens(f.description)


def _distribute_prose(
    prose_lines: list[str], remaining_fields: list[FieldDefinition]
) -> tuple[list[PlacementInstruction], list[str]]:
    """Match each leftover line to whichever remaining text field it best fits.

    A template with MANY text fields (e.g. every section of a fully-tagged
    template) needs its content spread across them, not dumped into just the
    first one — that alone would leave every other field blank. Lines that
    don't clear the match threshold fall back to the single best-covered
    field as a last resort, so at least something is placed rather than lost.
    """
    if not remaining_fields:
        return [], prose_lines

    topic_tokens = {f.field_name: _field_topic_tokens(f) for f in remaining_fields}
    buckets: dict[str, list[str]] = {f.field_name: [] for f in remaining_fields}
    unmatched: list[str] = []

    for line in prose_lines:
        line_tokens = _tokens(line)
        best_name, best_score = None, 0.0
        for f in remaining_fields:
            field_tokens = topic_tokens[f.field_name]
            if not field_tokens or not line_tokens:
                continue
            score = len(line_tokens & field_tokens) / len(field_tokens)
            if score > best_score:
                best_name, best_score = f.field_name, score
        if best_name is not None and best_score >= _PROSE_MATCH_THRESHOLD:
            buckets[best_name].append(line)
        else:
            unmatched.append(line)

    placements: list[PlacementInstruction] = []
    for f in remaining_fields:
        chunk = buckets[f.field_name]
        if not chunk:
            continue
        placements.append(
            PlacementInstruction(
                field_name=f.field_name,
                value=" ".join(chunk),
                confidence=0.45,
                source_excerpt=chunk[0][:80],
                note="Low-confidence: matched to this field by keyword overlap.",
            )
        )

    # Nothing cleared the threshold anywhere -> last resort, put it all in the
    # single most generic remaining field rather than dropping it entirely.
    if not placements and unmatched:
        target = remaining_fields[0]
        placements.append(
            PlacementInstruction(
                field_name=target.field_name,
                value=" ".join(unmatched),
                confidence=0.3,
                source_excerpt=unmatched[0][:80],
                ambiguous=len(remaining_fields) > 1,
                alternatives=[f.field_name for f in remaining_fields[1:]],
                note="Low-confidence: leftover text assigned to a free-text field.",
            )
        )
        unmatched = []

    return placements, unmatched


def route_unstructured_heuristic(
    fields: list[FieldDefinition], raw_text: str, template_id: str, version: int
) -> RoutingResult:
    lines = [ln.strip() for ln in (raw_text or "").splitlines() if ln.strip()]
    placements: list[PlacementInstruction] = []
    assigned: set[str] = set()
    leftovers: list[str] = []

    for line in lines:
        matched = False
        if ":" in line:
            label, _, value = line.partition(":")
            value = value.strip()
            field = _match_field(fields, slugify_field(label), label)
            if (
                field is not None
                and field.field_name not in assigned
                and field.field_type != FieldType.TABLE
                and value
            ):
                placements.append(
                    PlacementInstruction(
                        field_name=field.field_name,
                        value=value,
                        confidence=0.7,
                        source_excerpt=line,
                    )
                )
                assigned.add(field.field_name)
                matched = True
        if not matched:
            leftovers.append(line)

    # Distribute the leftover prose across whichever remaining free-text
    # fields it best matches (falls back to a single field if none match).
    # Multiline fields are checked first so the "dump it all in one field"
    # last resort prefers a body-style field over a short title/label field
    # when both are unmatched candidates.
    remaining_text = sorted(
        (
            f
            for f in fields
            if f.field_name not in assigned and f.field_type in (FieldType.TEXT, FieldType.MULTILINE_TEXT)
        ),
        key=lambda f: f.field_type != FieldType.MULTILINE_TEXT,
    )
    # Drop all-caps title-ish lines from the leftover prose (reduces boilerplate
    # noise when mapping a whole document's content into one free-text field).
    prose = [ln for ln in leftovers if not (ln.isupper() and len(ln) < 60)]
    prose_placements, unmapped = _distribute_prose(prose, remaining_text)
    placements.extend(prose_placements)
    assigned.update(p.field_name for p in prose_placements)
    leftovers = unmapped

    missing = [f.field_name for f in fields if f.required and f.field_name not in assigned]
    ambiguous = [p.field_name for p in placements if p.ambiguous]
    return RoutingResult(
        template_id=template_id,
        version=version,
        placements=placements,
        missing_required=missing,
        ambiguous_fields=ambiguous,
        unmapped_content=leftovers,
        source="heuristic",
    )
