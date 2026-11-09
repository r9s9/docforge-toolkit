"""ai_router (plugin build): deterministic content routing.

Upstream's ``route`` prefers a model for free text. In the plugin, Claude
writes field values itself (fill-template skill), so ``route`` here is the
no-model path: structured data maps key by key, free text goes to the
heuristic, exactly as upstream does with no AI configured.
"""

from __future__ import annotations

from ..schemas.routing import RoutingResult
from ..schemas.template import FieldDefinition
from .document import (
    document_content,
    extraction_blocks,
    route_document_content,
    route_document_structural,
)
from .router import route_structured, route_unstructured_heuristic


def route(
    fields: list[FieldDefinition],
    *,
    template_id: str,
    version: int,
    raw_text: str | None = None,
    data: dict | None = None,
    **_upstream_only: object,
) -> RoutingResult:
    """Route content into fields without a model (upstream's no-AI path)."""
    if data and not raw_text:
        return route_structured(fields, data, template_id, version)
    return route_unstructured_heuristic(fields, raw_text or "", template_id, version)


__all__ = [
    "route",
    "route_structured",
    "route_unstructured_heuristic",
    "route_document_content",
    "route_document_structural",
    "document_content",
    "extraction_blocks",
]
