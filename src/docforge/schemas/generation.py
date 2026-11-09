"""Generation request/response schemas (spec §5 flows 2 & 3)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .enums import GenerationMode, JobStatus
from .routing import PlacementInstruction
from .validation import ValidationReport


class GenerationInput(BaseModel):
    """Caller-supplied content for a generation request.

    Exactly one of ``data`` / ``raw_text`` / ``placements`` is the primary
    source depending on ``mode``:
      - STRUCTURED_JSON / STRUCTURED_FORM -> ``data`` (flat field->value dict)
      - UNSTRUCTURED_TEXT                 -> ``raw_text`` (routed by ai_router)
    Approved ``placements`` (from a review screen) take precedence when present.
    """

    mode: GenerationMode = GenerationMode.STRUCTURED_JSON
    data: dict[str, Any] | None = None
    raw_text: str | None = None
    placements: list[PlacementInstruction] | None = None
    version: int | None = None  # specific template version; default = latest
    skip_validation: bool = False
    # What the AI proposed before the user edited it. Sent by the review screen
    # so the difference can be learned from; never used to render.
    prior_placements: list[PlacementInstruction] | None = None


class GenerationResult(BaseModel):
    """Outcome metadata for a generation request."""

    generation_id: str
    template_id: str
    version: int
    status: JobStatus = JobStatus.PENDING
    mode: GenerationMode = GenerationMode.STRUCTURED_JSON
    output_filename: str | None = None
    output_path: str | None = None
    validation: ValidationReport | None = None
    error: str | None = None
    context_used: dict[str, Any] = Field(default_factory=dict)
