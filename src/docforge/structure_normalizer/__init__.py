"""structure_normalizer — produce a normalized element tree from a DOCX."""

from __future__ import annotations

from .normalizer import build_extraction
from .walk import WalkNode, iter_block_items, walk_document

__all__ = ["build_extraction", "walk_document", "iter_block_items", "WalkNode"]
