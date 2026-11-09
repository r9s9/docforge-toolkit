"""ooxml_extractor — unpack a DOCX and expose its raw OOXML parts safely.

Public surface:
  * DocxPackage          — defensive OPC/zip reader (parts, rels, media, hashes)
  * NumberingResolver    — bullet vs ordered list resolution from numbering.xml
  * read_raw_parts(pkg)  — quick inventory of the key parts (for diagnostics/API)
"""

from __future__ import annotations

from .numbering import NumberingResolver
from .package import (
    NS,
    DocxError,
    DocxPackage,
    RelInfo,
    UnsafeDocxError,
)


def read_raw_parts(pkg: DocxPackage) -> dict[str, object]:
    """Summarize which key OOXML parts are present (used by the extractor API).

    Returns a small, JSON-serializable inventory — never the raw bytes.
    """
    doc_name = pkg.main_document_name()
    all_parts = pkg.list_parts()
    return {
        "main_document": doc_name,
        "has_styles": pkg.has("word/styles.xml"),
        "has_numbering": pkg.has("word/numbering.xml"),
        "headers": [p for p in all_parts if p.startswith("word/header")],
        "footers": [p for p in all_parts if p.startswith("word/footer")],
        "media": list(pkg.media().keys()),
        "n_parts": len(all_parts),
    }


__all__ = [
    "DocxPackage",
    "DocxError",
    "UnsafeDocxError",
    "RelInfo",
    "NumberingResolver",
    "NS",
    "read_raw_parts",
]
