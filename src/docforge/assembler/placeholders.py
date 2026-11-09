"""Template preparation that keeps the tags in a template short.

Two jobs a template used to spell out in its own tags, done here at render time
instead, so the template stays one readable line per field:

* An empty field's paragraph disappears. A paragraph that holds nothing but one
  ``{{ name }}`` placeholder is removed when that value is empty, rather than
  leaving a blank line. Templates used to wrap every such paragraph in
  ``{%p if name %}`` … ``{%p endif %}``, tripling it; those still render the same.
* A table of contents entry whose heading's value is empty goes with it: the
  entry shows that heading's tag (beside its number and page number), so an
  empty heading would leave a numbered line with no title.
* Section toggles default to shown. ``{%p if _show_x %}`` with no value for
  ``_show_x`` would hide the section, so every toggle the template names that
  the caller did not supply is set to True. Older templates spelled this out as
  ``{%p if _show_x is not defined or _show_x %}``.
"""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from typing import Any

from lxml import etree

from ..ooxml_extractor.safety import parse_xml

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_P, _T, _TC = f"{{{_W}}}p", f"{{{_W}}}t", f"{{{_W}}}tc"
_PPR, _SECTPR = f"{{{_W}}}pPr", f"{{{_W}}}sectPr"
_PSTYLE, _VAL = f"{{{_W}}}pStyle", f"{{{_W}}}val"
_FLDCHAR, _FLDTYPE = f"{{{_W}}}fldChar", f"{{{_W}}}fldCharType"
# Content a paragraph must keep even when its text placeholder is empty.
_KEEP = tuple(f"{{{_W}}}{tag}" for tag in ("drawing", "pict", "object"))

_PARTS_RE = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
_LONE_RE = re.compile(r"^\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s*$")
_TAG_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_TOGGLE_RE = re.compile(rb"\{%p\s+if\s+(_show_[A-Za-z0-9_]+)")

# The builder's loop variable: "{{ item }}" is a row of a loop, not a field.
_LOOP_VAR = "item"


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return not value
    return False  # a number or a boolean is a value


def _is_contents_entry(p) -> bool:
    """A table of contents line (styles TOC1 … TOC9)."""
    ppr = p.find(_PPR)
    style = ppr.find(_PSTYLE) if ppr is not None else None
    return style is not None and (style.get(_VAL) or "").lower().startswith("toc")


def _removable(p) -> bool:
    """Whether deleting this paragraph keeps the document valid and intact."""
    if any(True for _ in p.iter(*_KEEP)):
        return False
    ppr = p.find(_PPR)
    if ppr is not None and ppr.find(_SECTPR) is not None:
        return False  # carries a section break: removing it merges sections
    kinds = [f.get(_FLDTYPE) for f in p.iter(_FLDCHAR)]
    if kinds.count("begin") != kinds.count("end"):
        return False  # opens or closes a field that spans other paragraphs
    parent = p.getparent()
    if parent is None:
        return False
    # A table cell (or a text box, a content control) must keep one paragraph.
    if parent.tag != f"{{{_W}}}body" and sum(1 for c in parent if c.tag == _P) <= 1:
        return False
    return True


def _drop_in_part(xml: bytes, context: dict[str, Any]) -> bytes | None:
    """The part with empty lone-placeholder paragraphs removed, or None if unchanged."""
    root = parse_xml(xml)
    doomed = []
    for p in root.iter(_P):
        text = "".join(t.text or "" for t in p.iter(_T))
        if "{{" not in text:
            continue
        m = _LONE_RE.match(text)
        if m:
            name = m.group(1)
        elif _is_contents_entry(p) and len(tags := _TAG_RE.findall(text)) == 1:
            name = tags[0]
        else:
            continue
        # Only a value the caller supplied as empty: a name it never mentioned
        # (a loop variable, a field removed in review) renders as it always did.
        if name == _LOOP_VAR or name not in context or not _is_empty(context[name]):
            continue
        if _removable(p):
            doomed.append(p)
    if not doomed:
        return None
    for p in doomed:
        p.getparent().remove(p)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def prepare_template(data: bytes, context: dict[str, Any]) -> bytes:
    """Apply both render-time rules; ``context`` gains the default toggles.

    Returns the template bytes to render (the input itself when nothing in it
    needed changing, which is the common case for a fully filled document).
    """
    changed: dict[str, bytes] = {}
    with zipfile.ZipFile(BytesIO(data)) as zin:
        for info in zin.infolist():
            if not _PARTS_RE.match(info.filename):
                continue
            xml = zin.read(info)
            for name in _TOGGLE_RE.findall(xml):
                context.setdefault(name.decode(), True)
            if b"{{" in xml:
                new = _drop_in_part(xml, context)
                if new is not None:
                    changed[info.filename] = new
        if not changed:
            return data
        out = BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                zout.writestr(info, changed.get(info.filename, zin.read(info)))
    return out.getvalue()
