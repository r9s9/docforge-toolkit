"""Multi-document diff schemas (spec §4 multi_doc_differ, §8)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .enums import DiffStatus, ElementType


class NodeDiff(BaseModel):
    """The cross-document comparison result for one aligned logical node."""

    node_key: str  # alignment key (shared across docs)
    representative_node_id: str  # node id in the representative (first) doc
    status: DiffStatus
    type: ElementType
    sample_texts: list[str] = Field(default_factory=list)  # distinct text variants
    confidence: float = 0.5
    is_constant: bool = False  # identical in every sample
    is_optional: bool = False  # missing from at least one sample document

    # Partial-change detail (e.g. "Date: 2026-06-01" -> prefix "Date: " + token)
    static_prefix: str | None = None
    static_suffix: str | None = None
    variable_parts: list[str] = Field(default_factory=list)
    detected_kind: str | None = None  # date|number|person|enum|text

    # Table-specific evidence
    header_identical: bool | None = None
    row_count_variable: bool | None = None
    row_counts: list[int] = Field(default_factory=list)

    # Image-specific evidence
    image_hashes: list[str] = Field(default_factory=list)

    notes: str = ""


class DiffRunResult(BaseModel):
    """Aggregate result of diffing a set of same-type documents."""

    document_ids: list[str] = Field(default_factory=list)
    n_documents: int = 0
    representative_document_id: str = ""
    node_diffs: list[NodeDiff] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)  # status -> count

    def by_node(self, representative_node_id: str) -> NodeDiff | None:
        for d in self.node_diffs:
            if d.representative_node_id == representative_node_id:
                return d
        return None
