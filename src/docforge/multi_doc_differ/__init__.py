"""multi_doc_differ — structural comparison of same-type documents."""

from __future__ import annotations

from .differ import (
    align_signature,
    align_to_representative,
    diff_documents,
    pick_representative,
)

__all__ = [
    "diff_documents",
    "pick_representative",
    "align_signature",
    "align_to_representative",
]
