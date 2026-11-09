"""Resolve list-numbering metadata from word/numbering.xml.

python-docx exposes a paragraph's numId/ilvl but not whether the list is a
bullet or an ordered (numbered) list. We resolve that here so the normalizer can
mark LIST_ITEM elements with ``is_ordered`` and the template builder can keep
list formatting intact.
"""

from __future__ import annotations

from .package import NS, DocxPackage

_W = NS["w"]


def _wq(tag: str) -> str:
    return f"{{{_W}}}{tag}"


class NumberingResolver:
    """Maps (num_id, level) -> numFmt (e.g. 'bullet', 'decimal', 'lowerRoman')."""

    def __init__(self, fmt_by_key: dict[tuple[str, int], str]):
        self._fmt = fmt_by_key

    @classmethod
    def from_package(cls, pkg: DocxPackage, doc_part_name: str) -> NumberingResolver:
        """Build a resolver by parsing the numbering part linked to the document."""
        # numbering.xml lives next to the document part; resolve via rels, else guess.
        numbering_name = "word/numbering.xml"
        rels = pkg.rels_for(doc_part_name)
        for rel in rels.values():
            if rel.type.endswith("/numbering"):
                numbering_name = rel.target
                break
        root = pkg.xml(numbering_name)
        if root is None:
            return cls({})

        # 1) abstractNumId -> {level -> numFmt}
        abstract: dict[str, dict[int, str]] = {}
        for anum in root.findall(_wq("abstractNum")):
            aid = anum.get(_wq("abstractNumId"))
            if aid is None:
                continue
            levels: dict[int, str] = {}
            for lvl in anum.findall(_wq("lvl")):
                ilvl_raw = lvl.get(_wq("ilvl"))
                try:
                    ilvl = int(ilvl_raw) if ilvl_raw is not None else 0
                except ValueError:
                    ilvl = 0
                numfmt_el = lvl.find(_wq("numFmt"))
                fmt = numfmt_el.get(_wq("val")) if numfmt_el is not None else None
                if fmt:
                    levels[ilvl] = fmt
            abstract[aid] = levels

        # 2) numId -> abstractNumId
        num_to_abstract: dict[str, str] = {}
        for num in root.findall(_wq("num")):
            nid = num.get(_wq("numId"))
            ref = num.find(_wq("abstractNumId"))
            if nid is not None and ref is not None:
                num_to_abstract[nid] = ref.get(_wq("val"))

        # 3) flatten -> (numId, level) -> numFmt
        fmt_by_key: dict[tuple[str, int], str] = {}
        for nid, aid in num_to_abstract.items():
            for lvl, fmt in abstract.get(aid, {}).items():
                fmt_by_key[(nid, lvl)] = fmt
        return cls(fmt_by_key)

    def num_fmt(self, num_id: str | None, level: int | None) -> str | None:
        if num_id is None:
            return None
        return self._fmt.get((str(num_id), level or 0))

    def is_ordered(self, num_id: str | None, level: int | None) -> bool | None:
        fmt = self.num_fmt(num_id, level)
        if fmt is None:
            return None
        return fmt != "bullet"
