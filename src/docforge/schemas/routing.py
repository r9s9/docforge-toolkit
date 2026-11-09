"""Content routing schemas (spec §5 ai_router, §9 AI task C)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PlacementInstruction(BaseModel):
    """Where a piece of incoming content should go in the template.

    ``value`` is a scalar for simple fields, or a list[dict] for table fields
    (one dict per row, keyed by column field_name).
    """

    field_name: str
    value: Any = None
    confidence: float = 1.0
    source_excerpt: str = ""  # the chunk of input this came from
    ambiguous: bool = False
    alternatives: list[str] = Field(default_factory=list)
    note: str = ""
    # True when the compose step *drafted* this value from context rather than
    # finding it verbatim (e.g. a required field with no explicit source). The UI
    # flags these for review.
    ai_drafted: bool = False


# Sources that mean a model was actually called. "structural" aligns an upload
# to the template deterministically and "heuristic"/"structured"/"user" never
# touch a model, so none of them cost an AI credit.
AI_SOURCES = frozenset({"llm", "writer"})


class RoutingResult(BaseModel):
    """Output of routing structured or unstructured content into a template."""

    template_id: str
    version: int
    placements: list[PlacementInstruction] = Field(default_factory=list)
    missing_required: list[str] = Field(default_factory=list)
    ambiguous_fields: list[str] = Field(default_factory=list)
    unmapped_content: list[str] = Field(default_factory=list)
    # Sections the writer deliberately left empty: [{section_key, reason}].
    skipped_sections: list[dict] = Field(default_factory=list)
    model_used: str | None = None
    source: str = "heuristic"
    # AI token usage for this routing+compose pass (set by the preview endpoint).
    token_usage: dict | None = None

    @property
    def used_ai(self) -> bool:
        """Whether a model actually produced this result.

        Gates free-tier accounting and audit records, so it must name every
        model-backed source — "structural" and "heuristic" are deterministic and
        deliberately excluded.
        """
        return self.source in AI_SOURCES

    def to_context(self) -> dict[str, Any]:
        """Collapse placements into a flat ``{field_name: value}`` render context."""
        return {p.field_name: p.value for p in self.placements if p.value is not None}
