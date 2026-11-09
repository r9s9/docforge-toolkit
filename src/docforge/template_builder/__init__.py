"""template_builder — generate a docxtpl template from a representative DOCX."""

from __future__ import annotations

from .builder import build_template_docx, build_template_from_examples
from .ooxml_ops import replace_token_across_runs, set_paragraph_text

__all__ = [
    "build_template_docx",
    "build_template_from_examples",
    "replace_token_across_runs",
    "set_paragraph_text",
]
