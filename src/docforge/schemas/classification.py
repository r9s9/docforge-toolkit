"""AI classification schemas (spec §5 ai_classifier, §9 AI tasks A & B)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .enums import ClassificationType, FieldType


class ElementClassification(BaseModel):
    """Classification of a single node, with field metadata + evidence."""

    node_id: str
    classification: ClassificationType = ClassificationType.UNKNOWN
    field_name: str | None = None  # snake_case identifier when dynamic/repeatable
    field_type: FieldType | None = None
    description: str = ""
    required: bool = True
    # Optional content: present in some example documents but absent in others.
    # The builder wraps such nodes in a conditional and exposes an include toggle.
    optional: bool = False
    confidence: float = 0.5
    validation_hints: list[str] = Field(default_factory=list)

    # Partial-dynamic support: a node like "Date: 2026-06-01" splits into a
    # static prefix ("Date: ") + a dynamic token. The assembler/template builder
    # uses these to keep the literal text and only templatize the variable part.
    static_prefix: str | None = None
    static_suffix: str | None = None

    enum_values: list[str] = Field(default_factory=list)
    source: str = "heuristic"  # "heuristic" | "llm" | "user"
    rationale: str = ""

    # A table of contents entry: the node of the heading it lists. Not a field
    # of its own; the builder gives it that heading's tag.
    links_to: str | None = None


class SectionUnderstanding(BaseModel):
    """Semantic understanding of a document section (AI task B)."""

    section_key: str  # stable key, usually derived from the heading node id
    title: str = ""
    purpose: str = ""
    expected_content: str = ""
    field_names: list[str] = Field(default_factory=list)
    related_sections: list[str] = Field(default_factory=list)


class ClassificationResult(BaseModel):
    """Full output of the classifier for one (representative) extraction."""

    extraction_document_id: str
    classifications: list[ElementClassification] = Field(default_factory=list)
    sections: list[SectionUnderstanding] = Field(default_factory=list)
    document_type_guess: str = ""
    model_used: str | None = None
    source: str = "heuristic"  # overall provenance
    # Non-fatal note when AI was configured but couldn't run (e.g. context-window
    # overflow) and the platform fell back to heuristics. Surfaced in the UI so the
    # user knows AI was skipped and why, without the job failing.
    ai_warning: str | None = None

    def by_node(self, node_id: str) -> ElementClassification | None:
        for c in self.classifications:
            if c.node_id == node_id:
                return c
        return None
