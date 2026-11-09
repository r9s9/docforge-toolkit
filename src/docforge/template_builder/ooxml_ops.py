"""OOXML fallback layer (spec §11): direct lxml manipulation for cases the
python-docx run API cannot express cleanly — most importantly replacing a token
that Word has split across multiple ``<w:r>``/``<w:t>`` runs.

Kept intentionally small and dependency-light so it composes with the builder.
"""

from __future__ import annotations

from docx.oxml.ns import qn
from lxml import etree


def iter_w_t(element: etree._Element) -> list[etree._Element]:
    """All ``<w:t>`` text nodes under an element, in document order."""
    return element.findall(".//" + qn("w:t"))


def paragraph_full_text(p_el: etree._Element) -> str:
    return "".join(t.text or "" for t in iter_w_t(p_el))


def set_paragraph_text(p_el: etree._Element, new_text: str) -> bool:
    """Collapse a paragraph's text into its first run, preserving whitespace.

    Returns False if the paragraph has no text run to write into.
    """
    ts = iter_w_t(p_el)
    if not ts:
        return False
    ts[0].text = new_text
    ts[0].set(qn("xml:space"), "preserve")
    for t in ts[1:]:
        t.text = ""
    return True


def replace_token_across_runs(p_el: etree._Element, token: str, replacement: str) -> bool:
    """Replace ``token`` with ``replacement`` even when split across runs.

    Word frequently fragments typed text (e.g. a ``{{field}}`` placeholder) into
    several runs; a naive per-run replace then misses it. This flattens the
    paragraph text, substitutes, and writes the result back into the first run.
    """
    full = paragraph_full_text(p_el)
    if token not in full:
        return False
    return set_paragraph_text(p_el, full.replace(token, replacement))
