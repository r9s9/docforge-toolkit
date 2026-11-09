"""ai_classifier (plugin build): the deterministic classifier and field derivation.

Upstream's ``classify`` prefers a model and falls back to heuristics. In the
plugin the model is Claude, working in the make-template skill, so ``classify``
here is the no-model path: exactly what upstream runs with no AI configured.
"""

from __future__ import annotations

from ..schemas.classification import ClassificationResult
from ..schemas.diff import DiffRunResult
from ..schemas.extraction import DocumentExtraction
from .contents import link_contents_entries
from .fields import derive_field_definitions, derive_validation_rules
from .heuristic import classify_heuristic
from .tags_only import enforce_tags_only


def classify(
    extraction: DocumentExtraction,
    diff: DiffRunResult | None = None,
    *,
    mode: str = "smart",
    **_upstream_only: object,
) -> ClassificationResult:
    """Classify a document without a model (upstream's no-AI path).

    ``mode`` is "smart" (detect what varies) or "tags_only" (every text block
    becomes a field). Upstream-only keyword arguments (client, settings,
    progress callbacks) are accepted and ignored, for call-site parity.
    """
    result = classify_heuristic(extraction, diff)
    # A table of contents lists the headings, so its entries follow them.
    link_contents_entries(extraction, result)
    if mode == "tags_only":
        enforce_tags_only(extraction, result)
    return result


__all__ = [
    "classify",
    "classify_heuristic",
    "derive_field_definitions",
    "derive_validation_rules",
]
