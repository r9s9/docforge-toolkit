"""assembler — deterministically fill a template DOCX (spec §6).

No intelligence here: it takes a render context (already-resolved field values)
and produces the final DOCX via docxtpl. Field definitions are used only to
*coerce* values into render-safe shapes (strings; table fields -> list[dict]).

One exception to "a value is just text": a value written in the markdown-lite
rich format (see :mod:`.richtext`) stands for several paragraphs or a list,
which a single Jinja placeholder cannot express. Those values are rendered as a
sentinel token and expanded into real paragraphs afterwards by
:mod:`.postprocess` — still deterministic, and still driven entirely by the
template's own formatting.
"""

from __future__ import annotations

import base64
import binascii
from io import BytesIO
from typing import Any

from docxtpl import DocxTemplate
from jinja2 import ChainableUndefined
from jinja2.sandbox import SandboxedEnvironment

from ..schemas.enums import ClassificationType, FieldType
from ..schemas.template import FieldDefinition
from .placeholders import prepare_template
from .postprocess import apply_rich_values, request_field_update, sentinel
from .richtext import RichBlock, is_rich, parse_rich_blocks, strip_markers

# Field types whose value is prose the user may structure into paragraphs and
# lists. Dates, numbers, enums and people stay single-valued on purpose.
_RICH_FIELD_TYPES = (FieldType.TEXT, FieldType.MULTILINE_TEXT)

# The body column of a repeated heading+body group (see ai_classifier.fields).
_BLOCK_BODY = "body"


class _SilentUndefined(ChainableUndefined):
    """Render missing variables as empty strings instead of raising.

    A template can outlive the field that named it — a field removed in review,
    or a node the classifier named but ``derive_field_definitions`` dropped. One
    such tag must cost that one spot, not the whole document.

    ``ChainableUndefined`` is the base that survives ``autoescape=True``: escaping
    probes a value for ``__html__``, so an undefined whose ``__getattr__`` answers
    every name (dunders included) gets called and raises the very error it was
    written to prevent. This one answers ``__html__`` with the empty string and
    lets real dunder lookups fail, which is what keeps missing values silent.
    """


def _coerce_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_bool(value: Any) -> bool:
    """Coerce a value to a real boolean (kept un-stringified for {%p if %})."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "no", "0", "off")
    return bool(value)


def _coerce_str_list(value: Any) -> list[str]:
    """Coerce a repeatable-section value into a list of non-empty strings.

    Each entry becomes one rendered paragraph via the template's own
    ``{%p for %}`` loop, so markdown-lite markers a user (or the AI) wrote here
    are redundant — strip them rather than let a literal "- " reach Word.
    """
    if value is None:
        return []
    if isinstance(value, str):
        lines = [strip_markers(ln) for ln in value.splitlines()]
        return [ln for ln in lines if ln.strip()]
    if isinstance(value, list):
        out = [strip_markers(_coerce_scalar(x)) for x in value]
        return [s for s in out if s.strip()]
    return [_coerce_scalar(value)]


def _coerce_rows(value: Any, field: FieldDefinition) -> list[dict]:
    if not isinstance(value, list):
        return []
    cols = [c.field_name for c in field.columns]
    rows: list[dict] = []
    for row in value:
        if isinstance(row, dict):
            if cols:
                rows.append({c: _coerce_scalar(row.get(c, "")) for c in cols})
            else:
                rows.append({k: _coerce_scalar(v) for k, v in row.items()})
        elif isinstance(row, (list, tuple)):
            rows.append({cols[i]: _coerce_scalar(row[i]) for i in range(min(len(cols), len(row)))})
    return rows


def _image_bytes(value: Any) -> bytes | None:
    """Decode an image field value (raw bytes or a base64 / data-URL string)."""
    if not value:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.startswith("data:"):  # strip a "data:image/png;base64," prefix
            comma = s.find(",")
            if comma != -1:
                s = s[comma + 1 :]
        try:
            return base64.b64decode(s, validate=False)
        except (binascii.Error, ValueError):
            return None
    return None


def build_render_context(fields: list[FieldDefinition], raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw field->value map into a render-safe context.

    Every defined field is present (missing -> '' or []); table fields become a
    list of column-keyed dicts. Extra keys are passed through untouched.
    """
    ctx: dict[str, Any] = {}
    defined = set()
    for f in fields:
        defined.add(f.field_name)
        value = raw.get(f.field_name)
        if f.field_type == FieldType.IMAGE:
            ctx[f.field_name] = value  # raw (bytes / base64); applied via replace_pic
        elif f.field_type == FieldType.TABLE:
            ctx[f.field_name] = _coerce_rows(value, f)
        elif f.field_type == FieldType.BOOLEAN:
            ctx[f.field_name] = _coerce_bool(value if value is not None else f.default)
        elif f.classification == ClassificationType.REPEATABLE_SECTION:
            ctx[f.field_name] = _coerce_str_list(value)
        else:
            # A field carrying a default stands for text the template already
            # had (a section label). Supplying nothing means "leave it as it
            # was", not "delete it".
            if value in (None, "") and f.default not in (None, ""):
                value = f.default
            ctx[f.field_name] = _coerce_scalar(value)
    for k, v in raw.items():
        if k not in defined:
            ctx[k] = v
    return ctx


def _extract_rich(render_ctx: dict[str, Any], fields: list[FieldDefinition]) -> dict[str, list[RichBlock]]:
    """Swap structured prose values for sentinels; return the blocks they stand for.

    Only prose fields qualify, and only when the value actually carries
    structure — a one-line value renders through the ordinary path untouched,
    so the common case is byte-for-byte identical to before this existed.
    Repeatable sections are skipped: the template already loops them.
    """
    rich: dict[str, list[RichBlock]] = {}
    for f in fields:
        # A repeated heading+body group renders each item's body through the same
        # placeholder, so every item needs its own token or they would all expand
        # to whichever content was seen last.
        if f.classification == ClassificationType.REPEATABLE_BLOCK:
            for row in render_ctx.get(f.field_name) or []:
                body = row.get(_BLOCK_BODY) if isinstance(row, dict) else None
                if not is_rich(body):
                    continue
                blocks = parse_rich_blocks(body)
                if not blocks:
                    continue
                token = sentinel(len(rich))
                rich[token] = blocks
                row[_BLOCK_BODY] = token
            continue
        if f.field_type not in _RICH_FIELD_TYPES:
            continue
        if f.classification == ClassificationType.REPEATABLE_SECTION:
            continue
        value = render_ctx.get(f.field_name)
        if not is_rich(value):
            continue
        blocks = parse_rich_blocks(value)
        if not blocks:
            render_ctx[f.field_name] = ""
            continue
        only = blocks[0] if len(blocks) == 1 else None
        if only is not None and only.kind == "paragraph" and not any(
            s.bold or s.italic for s in only.spans
        ):
            # One unadorned paragraph is just text — skip the post-pass entirely.
            render_ctx[f.field_name] = only.text
            continue
        token = sentinel(len(rich))
        rich[token] = blocks
        render_ctx[f.field_name] = token
    return rich


def assemble(
    template: str | bytes,
    context: dict[str, Any],
    fields: list[FieldDefinition] | None = None,
) -> bytes:
    """Render ``template`` (path or bytes) with ``context`` -> output DOCX bytes."""
    if isinstance(template, (bytes, bytearray)):
        data = bytes(template)
    else:
        with open(str(template), "rb") as fh:
            data = fh.read()
    render_ctx = build_render_context(fields, context) if fields else dict(context)
    # Drop the paragraphs of empty lone placeholders and default the section
    # toggles to shown — what templates used to spell out as {%p if %} tags.
    tpl = DocxTemplate(BytesIO(prepare_template(data, render_ctx)))
    # Image fields: swap the tagged picture in place (keeping its size/position),
    # or leave the original when no image was supplied. Must be queued before
    # render() — docxtpl applies replacements during its pre-processing pass.
    if fields:
        tpl.allow_missing_pics = True  # tolerate a template without that picture
        for f in fields:
            if f.field_type == FieldType.IMAGE:
                data = _image_bytes(render_ctx.pop(f.field_name, None))
                if data:
                    tpl.replace_pic(f.field_name, BytesIO(data))
    # Structured prose is rendered as a sentinel and expanded below: docxtpl can
    # only place a value inside one run, which cannot hold several paragraphs.
    rich_map = _extract_rich(render_ctx, fields) if fields else {}
    # autoescape is required: field values can contain &, <, > (e.g. "R&D",
    # "Smith & Jones"). Without it those land raw in the document XML and either
    # corrupt it (xmlParseEntityRef) or get silently dropped.
    #
    # Sandboxed, so a template can never execute Python or reach process state:
    # the tag text comes from uploaded documents and reviewed field labels, which
    # are user-controlled, and the builder makes those literal — but if any
    # ``{{ … }}`` survives, the sandbox blocks attribute access to the object
    # internals an exploit needs (``__class__``, ``__globals__``, …), raising
    # SecurityError rather than disclosing anything. Values are passed as the
    # render context (data), so they are never parsed as template code.
    jinja_env = SandboxedEnvironment(undefined=_SilentUndefined, autoescape=True)
    tpl.render(render_ctx, jinja_env=jinja_env)
    out = BytesIO()
    tpl.save(out)
    return request_field_update(apply_rich_values(out.getvalue(), rich_map))
