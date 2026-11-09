"""template_builder — turn a representative DOCX + classifications into a
docxtpl template (spec §6, §11).

Layout is preserved by *modifying the representative document's XML in place*
rather than rebuilding it: FIXED/AUTO content is left untouched, dynamic values
are replaced with ``{{ field }}`` (keeping any static label prefix/suffix), and
repeatable tables get a ``{%tr for ... %}`` / ``{%tr endfor %}`` loop using the
verified 3-row pattern (for-row / template-row / endfor-row).

When in-place run rewriting cannot express a case cleanly, the OOXML fallback in
``ooxml_ops.py`` provides direct lxml manipulation.
"""

from __future__ import annotations

import logging
import re
from io import BytesIO

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from ..ai_classifier import classify, derive_field_definitions
from ..ai_classifier.contents import is_entry_number
from ..ai_classifier.fields import include_field_name
from ..common.textutil import slugify_field
from ..multi_doc_differ import diff_documents, pick_representative
from ..schemas.classification import ClassificationResult
from ..schemas.enums import ClassificationType, FieldType, is_dynamic, needs_field
from ..schemas.extraction import DocumentExtraction
from ..schemas.template import FieldDefinition
from ..structure_normalizer import build_extraction, walk_document

logger = logging.getLogger("docforge.template_builder")

# Jinja loop variable used inside repeatable tables/sections.
_LOOP_VAR = "item"
# The two parts of a repeated heading+body group (see ai_classifier.fields).
_BLOCK_TITLE = "title"
_BLOCK_BODY = "body"

# A field name must be a valid Jinja/Python identifier to become a placeholder
# ({{ name }}); anything else (spaces, dots, a leading digit) makes docxtpl fail
# to compile the whole template. Names are sanitized upstream, but this is the
# last-line guard so one stray name can never crash a build/preview.
_VALID_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str | None) -> str | None:
    return name if name and _VALID_IDENT.match(name) else None


# An uploaded example may itself contain literal Jinja-like delimiters in its
# text (e.g. a Word template that already uses "{{ Client }}" markers). docxtpl
# parses the WHOLE document, so those stray tokens would crash rendering. We make
# them inert by slipping a zero-width space between the braces — invisible to the
# reader, but no longer a tag to Jinja. Our own placeholders are written AFTER
# this pass, so they stay intact.
_ZWSP = "​"
_STRAY = (("{{", "{" + _ZWSP + "{"), ("}}", "}" + _ZWSP + "}"),
          ("{%", "{" + _ZWSP + "%"), ("%}", "%" + _ZWSP + "}"))


def _neutralize_run(text: str) -> str:
    for a, b in _STRAY:
        if a in text:
            text = text.replace(a, b)
    return text


def _neutralize_stray_tags(doc) -> None:
    """Neutralize literal Jinja delimiters anywhere in the document's own text.

    Walks every paragraph the builder knows about (body, tables, headers/footers)
    and rewrites EVERY ``<w:t>`` it contains — including runs nested inside
    hyperlinks or text boxes, which ``paragraph.runs`` silently skips — so no
    stray "{{ … }}" / "{% … %}" from the example survives to confuse docxtpl.
    """
    for wn in walk_document(doc):
        if wn.kind != "paragraph":
            continue
        texts = [t for t in wn.obj._p.iter(qn("w:t")) if t.text]
        for t in texts:
            if "{" in t.text or "%}" in t.text:
                t.text = _neutralize_run(t.text)
        # A delimiter can also be split across two runs — "{" then "{", which
        # docxtpl re-joins into "{{" before Jinja sees it (Word does this when an
        # author types braces with a spell-check or formatting change between the
        # two characters). Break every delimiter that straddles a run boundary.
        for prev, cur in zip(texts, texts[1:], strict=False):
            if prev.text and cur.text and (prev.text[-1] + cur.text[0]) in ("{{", "{%", "}}", "%}"):
                cur.text = _ZWSP + cur.text


# DrawingML picture namespace — used to find a run's <pic:cNvPr> so we can tag a
# picture with a stable key. docxtpl.replace_pic() matches a picture by its
# cNvPr name/title/descr, so writing the field key into @title lets the assembler
# swap that exact picture at generation while leaving the original as the default.
_PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"


def _paragraph_has_picture(paragraph: Paragraph) -> bool:
    """True if the paragraph contains a real DrawingML picture (not just any shape)."""
    return bool(paragraph._p.findall(".//{%s}cNvPr" % _PIC_NS))


def _tag_picture(paragraph: Paragraph, key: str) -> bool:
    """Tag the first real picture in ``paragraph`` with ``key`` (its cNvPr title)."""
    cnvprs = paragraph._p.findall(".//{%s}cNvPr" % _PIC_NS)
    if not cnvprs:
        return False
    cnvprs[0].set("title", key)
    return True


def _marker_paragraph_xml(text: str):
    """A bare <w:p> carrying a docxtpl control tag (e.g. {%p if x %})."""
    p = OxmlElement("w:p")
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    run.append(t)
    p.append(run)
    return p


def _insert_marker_before(paragraph: Paragraph, text: str) -> None:
    paragraph._p.addprevious(_marker_paragraph_xml(text))


def _insert_marker_after(paragraph: Paragraph, text: str) -> None:
    paragraph._p.addnext(_marker_paragraph_xml(text))


# --- run-formatting helpers -------------------------------------------------
def _run_format_at(paragraph: Paragraph, offset: int) -> dict:
    """Capture character formatting from the run covering ``offset``."""
    runs = paragraph.runs
    if not runs:
        return {}
    acc = 0
    chosen = runs[0]
    for r in runs:
        length = len(r.text or "")
        if acc <= offset < acc + length:
            chosen = r
            break
        acc += length
    else:
        chosen = runs[-1]
    f = chosen.font
    fmt: dict = {"bold": f.bold, "italic": f.italic, "underline": f.underline, "name": f.name}
    try:
        fmt["size"] = f.size
    except (AttributeError, ValueError):
        fmt["size"] = None
    try:
        fmt["color"] = f.color.rgb if (f.color is not None and f.color.rgb is not None) else None
    except (AttributeError, ValueError):
        fmt["color"] = None
    return fmt


def _run_has_image(run) -> bool:
    """True if a run carries a picture/drawing/embedded object (not just text)."""
    el = run._element
    return (
        el.find(qn("w:drawing")) is not None
        or el.find(qn("w:pict")) is not None
        or el.find(qn("w:object")) is not None
    )


def _run_has_field(run) -> bool:
    """True if a run carries a Word field (a caption's auto number, a page ref).

    Word computes these; replacing one with a placeholder would break the
    document's own numbering, so the field is kept and only the words around it
    become editable.
    """
    el = run._element
    return (
        el.find(qn("w:fldChar")) is not None
        or el.find(qn("w:instrText")) is not None
        or el.find(qn("w:fldSimple")) is not None
    )


def _clear_runs(
    paragraph: Paragraph, *, keep_images: bool = True, keep_fields: bool = True
) -> None:
    """Remove a paragraph's runs.

    By default, runs that carry an image/drawing/object are preserved: when we
    swap a paragraph's dynamic text for a ``{{ placeholder }}`` we must not delete
    its logo or picture along with the text, or images vanish from the template
    (and every document generated from it). The placeholder text is appended
    alongside the surviving image run.
    """
    for r in list(paragraph.runs):
        if keep_images and _run_has_image(r):
            continue
        if keep_fields and _run_has_field(r):
            continue
        r._element.getparent().remove(r._element)


def _append_run(paragraph: Paragraph, text: str, fmt: dict):
    run = paragraph.add_run(text)
    if fmt:
        if fmt.get("bold") is not None:
            run.font.bold = fmt["bold"]
        if fmt.get("italic") is not None:
            run.font.italic = fmt["italic"]
        if fmt.get("underline") is not None:
            run.font.underline = fmt["underline"]
        if fmt.get("name"):
            run.font.name = fmt["name"]
        if fmt.get("size"):
            run.font.size = fmt["size"]
        if fmt.get("color") is not None:
            try:
                run.font.color.rgb = fmt["color"]
            except (AttributeError, ValueError):
                pass
    return run


def _skeleton(s: str) -> str:
    """Letters and digits only: compares text however it is spaced or punctuated."""
    return re.sub(r"\W+", "", (s or "").lower())


def _starts_with(text: str, part: str) -> bool:
    sk = _skeleton(part)
    if sk:
        return _skeleton(text).startswith(sk)
    return (text or "").lstrip().startswith((part or "").strip())


def _ends_with(text: str, part: str) -> bool:
    sk = _skeleton(part)
    if sk:
        return _skeleton(text).endswith(sk)
    return (text or "").rstrip().endswith((part or "").strip())


def _real_affixes(text: str, prefix: str, suffix: str) -> tuple[str, str]:
    """The label text to keep around a tag: only what the paragraph really says.

    A prefix stays in front of the tag as written, so it must be how the
    paragraph's own text starts (and a suffix how it ends). A classifier can
    hand a table cell its *row's* label as a prefix ("Document ID" for the cell
    "VLT-PMP-2026-014"); written as is, the label landed in the value cell and
    the template read "Document ID«Document ID»".
    """
    keep_prefix = prefix if prefix and _starts_with(text, prefix) else ""
    keep_suffix = suffix if suffix and _ends_with(text, suffix) else ""
    if keep_prefix and keep_suffix and (
        len(_skeleton(keep_prefix)) + len(_skeleton(keep_suffix)) > len(_skeleton(text))
    ):
        keep_suffix = ""  # they overlap: the paragraph is shorter than both
    return keep_prefix, keep_suffix


def _templatize_paragraph(
    paragraph: Paragraph, prefix: str, expr: str, suffix: str, *, keep_fields: bool = True
) -> None:
    """Rewrite a paragraph to ``prefix{{ field }}suffix`` keeping run formatting.

    ``keep_fields=False`` also removes any Word field in the paragraph — used
    where the placeholder *replaces* generated content (a contents entry), so
    the field cannot regenerate over it.
    """
    full = paragraph.text or ""
    fmt_prefix = _run_format_at(paragraph, 0)
    fmt_value = _run_format_at(paragraph, len(prefix))
    fmt_suffix = _run_format_at(paragraph, max(0, len(full) - len(suffix)))
    _clear_runs(paragraph, keep_fields=keep_fields)
    # ``prefix``/``suffix`` are the field's label text, carried over from the
    # reviewed classification — content the user controls. ``expr`` is our own
    # ``{{ field }}`` tag. Neutralising the label text (but not ``expr``) keeps
    # a label like "{{ x }}" literal instead of letting it become live Jinja the
    # renderer would execute. The stray-tag pre-pass can't cover these: they are
    # written here, after it runs.
    if prefix:
        _append_run(paragraph, _neutralize_run(prefix), fmt_prefix)
    _append_run(paragraph, expr, fmt_value)
    if suffix:
        _append_run(paragraph, _neutralize_run(suffix), fmt_suffix)


def _set_cell_expr(cell, expr: str) -> None:
    """Set a table cell to a single ``expr``, preserving the first run's format."""
    paras = cell.paragraphs
    if not paras:
        cell.text = expr
        return
    first = paras[0]
    fmt = _run_format_at(first, 0)
    _clear_runs(first)
    _append_run(first, expr, fmt)
    for extra in paras[1:]:
        extra._element.getparent().remove(extra._element)


def _tag_row(row, text: str) -> None:
    seen: set[int] = set()
    for i, cell in enumerate(row.cells):
        key = id(cell._tc)
        if key in seen:
            continue
        seen.add(key)
        cell.text = text if i == 0 else ""


def _templatize_table(table: Table, field_name: str, columns: list) -> None:
    """Convert a data row into a repeated row driven by ``field_name``.

    Tables with >= 2 rows keep row 0 as a frozen header and loop row 1. A table
    with only ONE row has no separate header — that single row IS the loop
    template, or the whole table would otherwise be left completely untouched
    (never templatized, text never removed) since there'd be no "row 1" to pick.
    """
    rows = table.rows
    if len(rows) < 1:
        return  # no rows at all — nothing to repeat
    single_row = len(rows) == 1
    template_row = rows[0] if single_row else rows[1]

    # Rewrite each physical cell of the template row to {{ item.col }}.
    seen: set[int] = set()
    for ci, cell in enumerate(template_row.cells):
        key = id(cell._tc)
        if key in seen:
            continue
        seen.add(key)
        # A column name becomes part of our own tag ({{ item.<col> }}), so an
        # unsafe one could break out of the expression. _safe_ident rejects
        # anything that is not a plain identifier; fall back to a positional
        # name, which can never inject.
        col = _safe_ident(columns[ci].field_name if ci < len(columns) else None) or f"col{ci + 1}"
        _set_cell_expr(cell, f"{{{{ {_LOOP_VAR}.{col} }}}}")

    # Drop any remaining example data rows after the template row.
    extra_start = 1 if single_row else 2
    for extra in list(table.rows)[extra_start:]:
        extra._tr.getparent().remove(extra._tr)

    # Wrap the template row with for/endfor marker rows (verified pattern).
    for_row = table.add_row()
    _tag_row(for_row, f"{{%tr for {_LOOP_VAR} in {field_name} %}}")
    endfor_row = table.add_row()
    _tag_row(endfor_row, "{%tr endfor %}")
    template_row._tr.addprevious(for_row._tr)
    template_row._tr.addnext(endfor_row._tr)


def _templatize_repeatable_block(
    paragraphs: list[Paragraph], field_name: str
) -> None:
    """Repeat a heading and its body once per item.

    The group's first paragraph carries the item's title and the second its
    body; anything after that was example prose for one item and is removed, the
    same way a grouped repeatable section keeps only its first paragraph as the
    loop template.
    """
    head, *rest = paragraphs
    _templatize_paragraph(head, "", f"{{{{ {_LOOP_VAR}.{_BLOCK_TITLE} }}}}", "")
    body = rest[0] if rest else None
    if body is not None:
        _templatize_paragraph(body, "", f"{{{{ {_LOOP_VAR}.{_BLOCK_BODY} }}}}", "")
        for extra in rest[1:]:
            _remove_paragraph(extra)
    _insert_marker_before(head, f"{{%p for {_LOOP_VAR} in {field_name} %}}")
    _insert_marker_after(body if body is not None else head, "{%p endfor %}")


def _templatize_repeatable_paragraph(
    paragraph: Paragraph, field_name: str, *, keep_fields: bool = True
) -> None:
    """Turn a paragraph into a repeated paragraph: one rendered per list item."""
    _templatize_paragraph(paragraph, "", f"{{{{ {_LOOP_VAR} }}}}", "", keep_fields=keep_fields)
    _insert_marker_before(paragraph, f"{{%p for {_LOOP_VAR} in {field_name} %}}")
    _insert_marker_after(paragraph, "{%p endfor %}")


def _remove_paragraph(paragraph: Paragraph) -> None:
    """Delete a paragraph from the document body (used for grouped sections:
    only the first paragraph of a repeatable-section field becomes the loop
    template; the rest of the original example text is removed)."""
    p = paragraph._p
    parent = p.getparent()
    if parent is not None:
        parent.remove(p)


def _wrap_optional(paragraph: Paragraph, include_name: str) -> None:
    """Wrap a paragraph so it only renders when ``include_name`` is truthy."""
    _insert_marker_before(paragraph, f"{{%p if {include_name} %}}")
    _insert_marker_after(paragraph, "{%p endif %}")


def _replaces_generated(cls) -> bool:
    """Whether this field stands in for content Word generated for itself.

    Marked by the tags-only pass (see ``ai_classifier/tags_only.py``). The field
    behind such a paragraph is removed so the placeholder is the real content —
    otherwise Word regenerates the list and the tag is never seen.
    """
    from ..ai_classifier.tags_only import GENERATED_MARK

    return GENERATED_MARK in (cls.rationale or "")


# --- table of contents -------------------------------------------------------
def _contents_title_texts(p_el) -> list:
    """The ``w:t`` elements holding a contents entry's heading text.

    An entry reads "1.1<tab>Agenda<tab>3": a number, the heading, then a
    PAGEREF field showing the page. Field codes and that page number are
    skipped, tabs split the rest, and a leading number is left alone.
    """
    fields: list[dict] = []  # fields opened in this paragraph, innermost last
    segments: list[list] = [[]]
    for el in p_el.iter(qn("w:fldChar"), qn("w:instrText"), qn("w:t"), qn("w:tab")):
        if el.tag == qn("w:fldChar"):
            kind = el.get(qn("w:fldCharType"))
            if kind == "begin":
                fields.append({"code": "", "result": False})
            elif kind == "separate" and fields:
                fields[-1]["result"] = True
            elif kind == "end" and fields:
                fields.pop()
        elif el.tag == qn("w:instrText"):
            if fields:
                fields[-1]["code"] += el.text or ""
        elif any("PAGEREF" in f["code"].upper() for f in fields) or (
            fields and not fields[-1]["result"]
        ):
            continue  # the page number, or a field's own code
        elif el.tag == qn("w:tab"):
            if el.getparent() is not None and el.getparent().tag == qn("w:r"):
                segments.append([])  # a tab character (not a tab-stop definition)
        else:
            segments[-1].append(el)
    texts = [s for s in segments if "".join(t.text or "" for t in s).strip()]
    if len(texts) > 1 and is_entry_number("".join(t.text or "" for t in texts[0])):
        texts = texts[1:]
    return texts[0] if texts else []


def _link_contents_entries(
    nodes: list, cls_by_node: dict, fd_by_node: dict, affixes: dict[str, tuple[str, str]]
) -> int:
    """Give each linked contents entry its heading's tag; return how many.

    The entry keeps everything Word put there (its number, the dotted leader,
    the page number, the TOC field around the list); only the heading text
    becomes the heading's own placeholder, so the list follows the headings.
    An entry whose heading is not a field keeps the heading's fixed text.
    """
    linked = 0
    for wn in nodes:
        cls = cls_by_node.get(wn.node_id)
        if wn.kind != "paragraph" or cls is None or not cls.links_to:
            continue
        target = cls_by_node.get(cls.links_to)
        fd = fd_by_node.get(cls.links_to)
        if target is None or fd is None or not is_dynamic(target.classification):
            continue
        if target.classification in (
            ClassificationType.REPEATABLE_SECTION, ClassificationType.REPEATABLE_BLOCK,
        ):
            continue  # a looped heading has no single tag to show
        name = _safe_ident(fd.field_name)
        texts = _contents_title_texts(wn.obj._p)
        if not name or not texts:
            continue
        prefix, suffix = affixes.get(cls.links_to, ("", ""))
        expr = _neutralize_run(prefix) + f"{{{{ {name} }}}}" + _neutralize_run(suffix)
        texts[0].text = expr
        texts[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        for t in texts[1:]:
            t.text = ""
        linked += 1
    return linked


# --- main builder -----------------------------------------------------------
def section_toggle_name(section_key: str) -> str | None:
    """The Jinja variable that decides whether a section renders at all.

    ``None`` for a key that slugifies to nothing: a bare ``_show_`` is a valid
    identifier, so every unnamed section would silently share one toggle.
    """
    slug = slugify_field(section_key, fallback="")
    return _safe_ident(f"_show_{slug}") if slug else None


def _loop_depth(el) -> int:
    """Net loops opened by one block element (a marker paragraph opens or closes)."""
    if el.tag != qn("w:p"):
        return 0
    text = "".join(el.itertext())
    return (
        text.count("{%p for") + text.count("{%tr for")
        - text.count("{%p endfor") - text.count("{%tr endfor")
    )


def _span_close_anchor(first: Paragraph, last: Paragraph):
    """The element to close a section after, or ``None`` if it cannot be closed.

    A section's last paragraph is usually the body of a repeatable section, and
    that loop's ``{%p endfor %}`` marker sits *after* it — so closing the section
    at the paragraph would interleave the tags and break the template. Walking
    forward until every loop opened inside the span has closed keeps the nesting
    valid; running out of siblings first means this section cannot be wrapped.
    """
    parent = first._p.getparent()
    if parent is None or last._p.getparent() is not parent:
        return None
    children = list(parent)
    try:
        lo, hi = children.index(first._p), children.index(last._p)
    except ValueError:
        return None
    depth = 0
    for el in children[lo : hi + 1]:
        depth += _loop_depth(el)
        if depth < 0:
            return None  # closes a loop opened before the section — never valid
    if depth == 0:
        return children[hi]
    for el in children[hi + 1 :]:
        depth += _loop_depth(el)
        if depth < 0:
            return None
        if depth == 0:
            return el
    return None


def _is_heading_node(wn) -> bool:
    if wn.kind != "paragraph":
        return False
    style = (getattr(wn.obj, "style", None) and wn.obj.style.name) or ""
    return style.lower().startswith(("heading", "title"))


def _wrap_section_spans(
    nodes: list,
    fields: list[FieldDefinition],
    sections: list,
) -> list[str]:
    """Wrap each section's paragraphs in ``{%p if _show_x %}`` … ``{%p endif %}``.

    A section is a set of fields, not a range of the document, so its span is
    inferred: from the first paragraph of its earliest field (or the heading
    immediately above it) to the paragraph before the next section starts.

    That inference can be wrong on an unusual document, and a wrong span means a
    malformed template — so every span is checked before use and simply skipped
    when it does not hold. A section that cannot be hidden is a missing feature;
    a template that will not render is a broken product.
    """
    if not sections:
        return []
    position = {wn.node_id: i for i, wn in enumerate(nodes)}
    body = [i for i, wn in enumerate(nodes) if wn.scope is None and wn.parent_node_id is None]
    if not body:
        return []

    starts: list[tuple[int, str]] = []
    for section in sections:
        key = getattr(section, "section_key", "") or ""
        member_nodes = [
            position[nid]
            for f in fields
            if f.section_key == key
            for nid in f.node_ids
            if nid in position and nid not in {n.node_id for n in nodes if n.scope is not None}
        ]
        member_nodes = [i for i in member_nodes if i in body]
        if not member_nodes:
            continue
        first = min(member_nodes)
        # Take the heading directly above the first field with the section: a
        # section hidden without its own title leaves a stranded heading.
        prev = body[body.index(first) - 1] if body.index(first) > 0 else None
        if prev is not None and _is_heading_node(nodes[prev]) and not _is_heading_node(nodes[first]):
            first = prev
        starts.append((first, key))

    starts.sort()
    wrapped: list[str] = []
    for n, (start, key) in enumerate(starts):
        name = section_toggle_name(key)
        if not name:
            continue
        end = starts[n + 1][0] - 1 if n + 1 < len(starts) else body[-1]
        span = [i for i in body if start <= i <= end]
        # A heading just before the next section opens what follows (a chapter
        # title above that section's own heading), so it is not this section's
        # to hide: leaving "Executive Summary" out took "02 Project Overview"
        # with it.
        while len(span) > 1 and _is_heading_node(nodes[span[-1]]):
            span.pop()
        # Both ends must be plain body paragraphs: a marker cannot open before a
        # table row or close inside another section's loop.
        if not span or nodes[span[0]].kind != "paragraph" or nodes[span[-1]].kind != "paragraph":
            logger.warning("skipping section toggle for %r — span is not paragraph-bounded", key)
            continue
        # The span must not cut through a loop this build already inserted. A
        # section boundary is inferred from where its fields sit, so it can land
        # in the middle of a repeatable section — and "{%p for %} … {%p if %} …
        # {%p endfor %} … {%p endif %}" is not valid Jinja, which fails the whole
        # template rather than just this toggle.
        close_after = _span_close_anchor(nodes[span[0]].obj, nodes[span[-1]].obj)
        if close_after is None:
            logger.warning("skipping section toggle for %r — span crosses a loop", key)
            continue
        # Default to showing: an undefined variable renders falsy in Jinja, so
        # the assembler supplies True for every _show_* the template names
        # (assembler.default_section_toggles). Only an explicit False hides.
        _insert_marker_before(nodes[span[0]].obj, f"{{%p if {name} %}}")
        close_after.addnext(_marker_paragraph_xml("{%p endif %}"))
        wrapped.append(key)
    return wrapped


def _nodes_inside_looped_tables(nodes: list, cls_by_node: dict) -> set[str]:
    """Node ids living inside a table that will be rewritten as a row loop."""
    looped = {
        wn.node_id
        for wn in nodes
        if wn.kind == "table"
        and (c := cls_by_node.get(wn.node_id)) is not None
        and c.classification == ClassificationType.REPEATABLE_TABLE
    }
    if not looped:
        return set()
    owned: set[str] = set()
    parent = {wn.node_id: wn.parent_node_id for wn in nodes}
    row_of = {wn.node_id: wn.row for wn in nodes}
    for node_id in parent:
        ancestor = parent.get(node_id)
        while ancestor is not None:
            if ancestor in looped:
                # Row 0 stays put as the table's heading row — it is above the
                # loop, so its cells keep their own placeholders.
                if row_of.get(node_id) != 0:
                    owned.add(node_id)
                break
            ancestor = parent.get(ancestor)
    return owned


def _templatize_blocks(nodes: list, cls_by_node: dict, fd_by_node: dict) -> set[str]:
    """Convert each repeated heading+body group into a loop; returns nodes used.

    The group is the set of consecutive paragraphs the classifier gave one
    field name — the same grouping mechanism repeatable sections use, read here
    as "a title and the body under it" rather than "one paragraph per item".
    """
    by_field: dict[str, list] = {}
    for wn in nodes:
        cls = cls_by_node.get(wn.node_id)
        if cls is None or cls.classification != ClassificationType.REPEATABLE_BLOCK:
            continue
        if wn.kind != "paragraph" or wn.scope is not None:
            continue
        fd = fd_by_node.get(wn.node_id)
        name = _safe_ident(fd.field_name if fd else cls.field_name)
        if not name:
            logger.warning("skipping repeatable block with unsafe name")
            continue
        by_field.setdefault(name, []).append(wn)

    used: set[str] = set()
    for name, group in by_field.items():
        if len(group) < 2:
            # A single paragraph is not a titled group; leave it to the ordinary
            # repeatable-section path rather than inventing an empty body.
            logger.info("repeatable block %r has no body paragraph — treating as a section", name)
            continue
        _templatize_repeatable_block([wn.obj for wn in group], name)
        used.update(wn.node_id for wn in group)
    return used


def build_template_docx(
    representative_docx_path: str,
    result: ClassificationResult,
    fields: list[FieldDefinition],
) -> bytes:
    """Produce the template.docx bytes for a representative document."""
    doc = Document(str(representative_docx_path))
    # Make any literal {{…}}/{%…%} already present in the example inert BEFORE we
    # insert our own placeholders, so docxtpl only ever parses our tags.
    _neutralize_stray_tags(doc)
    nodes = walk_document(doc)

    cls_by_node = {c.node_id: c for c in result.classifications}
    # The label text each tag keeps around it, checked against the paragraph.
    affixes = {
        wn.node_id: _real_affixes(
            wn.obj.text or "",
            cls_by_node[wn.node_id].static_prefix or "",
            cls_by_node[wn.node_id].static_suffix or "",
        )
        for wn in nodes
        if wn.kind == "paragraph" and wn.node_id in cls_by_node
    }
    # Map node -> its VALUE field. Boolean "include_*" toggles and image fields
    # also carry the node id but are applied elsewhere (a conditional wrapper,
    # and replace_pic), so neither may become the text placeholder — a paragraph
    # holding a logo and a caption would otherwise render {{ image_1 }} as text.
    fd_by_node: dict[str, FieldDefinition] = {}
    toggle_by_node: dict[str, str] = {}
    for f in fields:
        if f.field_type == FieldType.BOOLEAN and f.field_name.startswith("include_"):
            for nid in f.node_ids:
                toggle_by_node.setdefault(nid, f.field_name)
        if f.field_type in (FieldType.BOOLEAN, FieldType.IMAGE):
            continue
        for nid in f.node_ids:
            fd_by_node[nid] = f

    # Repeated heading+body groups are handled as a unit before the per-node
    # pass, since one field owns several consecutive paragraphs.
    handled_by_block = _templatize_blocks(nodes, cls_by_node, fd_by_node)

    # A looped table owns every cell inside it: its rows are generated from the
    # loop's items, so a cell that also carried its own {{ field }} would only
    # render when the loop happened to have items — and never for the value the
    # field was named after. One owner per node.
    owned_by_loop = _nodes_inside_looped_tables(nodes, cls_by_node)

    seen_section_fields: set[str] = set()
    # Classified nodes no field definition covers, reported once at the end.
    unfielded: list[str] = []
    for wn in nodes:
        if wn.node_id in handled_by_block or wn.node_id in owned_by_loop:
            continue
        cls = cls_by_node.get(wn.node_id)
        if cls is None:
            continue

        # Repeatable table.
        if cls.classification == ClassificationType.REPEATABLE_TABLE and wn.kind == "table":
            fd = fd_by_node.get(wn.node_id)
            name = _safe_ident(fd.field_name) if fd else None
            if name:
                _templatize_table(wn.obj, name, fd.columns)
            elif fd:
                logger.warning("skipping table field with unsafe name %r", fd.field_name)
            continue

        if wn.kind != "paragraph":
            continue

        para = wn.obj
        # A paragraph holding only a picture has no text to templatize — leave
        # it alone (it may be tagged below as a dynamic image field). One that
        # holds a picture *and* text still gets its text replaced: _clear_runs
        # keeps image runs, so the logo survives beside the placeholder. Without
        # this, a caption sharing a paragraph with a logo stayed literal.
        if _paragraph_has_picture(para) and not (para.text or "").strip():
            continue
        fd = fd_by_node.get(wn.node_id)
        if fd is None and cls.field_name and needs_field(cls.classification):
            # The classifier named this node but no field owns it: a blank
            # spacing paragraph, a picture-only node, a field the user deleted
            # in review. Writing {{ its_name }} anyway invents a variable the
            # render context will never hold — it renders as nothing at best,
            # and under autoescape it used to fail the whole build. Leave the
            # original text exactly as it is instead.
            unfielded.append(cls.field_name)
        # Compute the optional toggle name from the ORIGINAL text (before edits).
        include_name = (
            _safe_ident(toggle_by_node.get(wn.node_id) or include_field_name(cls, para))
            if cls.optional
            else None
        )
        # A placeholder standing in for Word-generated content (a contents entry)
        # must also remove the field that produced it, or Word rebuilds the list
        # on open and writes straight over the tag.
        keep_fields = not _replaces_generated(cls)

        if cls.classification == ClassificationType.REPEATABLE_SECTION:
            name = _safe_ident(fd.field_name) if fd else None
            if name:
                if name in seen_section_fields:
                    # Later paragraph of a grouped section — the first one already
                    # carries the loop; drop this original example text entirely.
                    _remove_paragraph(para)
                    continue
                seen_section_fields.add(name)
                _templatize_repeatable_paragraph(para, name, keep_fields=keep_fields)
        elif is_dynamic(cls.classification):
            name = _safe_ident(fd.field_name) if fd else None
            if name:
                prefix, suffix = affixes.get(wn.node_id, ("", ""))
                _templatize_paragraph(
                    para, prefix, f"{{{{ {name} }}}}", suffix, keep_fields=keep_fields,
                )
                # No "{%p if name %}" wrapper: a paragraph holding only this
                # placeholder is removed at render when the value is empty
                # (assembler.drop_empty_placeholders), which keeps the template
                # one line per field instead of three.
            elif fd and fd.field_name:
                logger.warning(
                    "skipping field with unsafe name %r (left as fixed text)", fd.field_name
                )

        if include_name:
            _wrap_optional(para, include_name)

    # The table of contents shows each heading's tag, as the heading does.
    toc_linked = _link_contents_entries(nodes, cls_by_node, fd_by_node, affixes)
    if toc_linked:
        logger.info("linked %d table of contents entries to their headings", toc_linked)

    # Image fields: tag each underlying picture with the field key so the
    # assembler can swap it via docxtpl.replace_pic at generation. The original
    # picture stays in place — it's the default, so previews and untouched
    # ("keep original") images both render exactly as in the example.
    img_fields = [f for f in fields if f.field_type == FieldType.IMAGE]
    if img_fields:
        node_by_id = {wn.node_id: wn for wn in nodes}
        for f in img_fields:
            key = _safe_ident(f.field_name)
            if not key:
                continue
            for nid in f.node_ids:
                wn = node_by_id.get(nid)
                if wn and wn.kind == "paragraph" and _tag_picture(wn.obj, key):
                    break

    if unfielded:
        names = sorted(set(unfielded))
        logger.info(
            "left %d classified node(s) as fixed text (no field was derived for them): %s",
            len(names), ", ".join(names[:10]) + ("…" if len(names) > 10 else ""),
        )

    # Section toggles go in last, so the spans are measured against the nodes as
    # walked (marker paragraphs inserted here are new siblings, never renumbering
    # anything the extraction already named).
    wrapped = _wrap_section_spans(nodes, fields, result.sections)
    if wrapped:
        logger.info("section toggles added for: %s", ", ".join(wrapped))

    bio = BytesIO()
    doc.save(bio)
    return bio.getvalue()


def build_template_from_examples(
    example_paths: list[str],
) -> tuple[bytes, DocumentExtraction, ClassificationResult, list[FieldDefinition]]:
    """Convenience: full analyze + build directly from example file paths.

    Returns (template_docx_bytes, representative_extraction, classification, fields).
    Useful for tests, the CLI, and seed data.
    """
    extractions = [build_extraction(p, f"ex-{i}") for i, p in enumerate(example_paths)]
    diff = diff_documents(extractions) if len(extractions) >= 2 else None
    rep_i = pick_representative(extractions) if len(extractions) >= 2 else 0
    rep = extractions[rep_i]
    result = classify(rep, diff)
    fields = derive_field_definitions(rep, result)
    template_bytes = build_template_docx(str(example_paths[rep_i]), result, fields)
    return template_bytes, rep, result, fields
