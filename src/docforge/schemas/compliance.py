"""Compliance-check schemas: score a document against a template."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ComplianceDifference(BaseModel):
    kind: str  # missing_fixed | changed_fixed | missing_field | format | missing_table | table_shape
    node_id: str | None = None
    field_name: str | None = None
    severity: str = "warning"  # error | warning | info
    expected: str = ""
    found: str = ""
    message: str = ""


class DimensionScore(BaseModel):
    name: str  # structure | fields | tables
    satisfied: float = 0.0
    total: float = 0.0
    score: float = 100.0  # 0..100


class ComplianceAlignedPair(BaseModel):
    """One row of the side-by-side comparison: a template element lined up with
    the matching element in the checked document (or its absence)."""

    node_id: str
    classification: str = ""
    # match | changed | missing | field | field_missing | table | table_changed
    # | missing_table | extra
    status: str = "match"
    severity: str = "info"  # error | warning | info
    template_text: str = ""
    # How a field's line reads in the template's "with labels" view («Label»),
    # which is what the comparison shows as the expected side.
    template_sample_text: str = ""
    document_text: str = ""
    field_name: str | None = None
    is_table: bool = False
    template_headers: list[str] = Field(default_factory=list)
    document_headers: list[str] = Field(default_factory=list)


class ComplianceReport(BaseModel):
    template_id: str
    version: int
    document_name: str = ""
    score: float = 100.0  # 0..100 overall
    grade: str = "pass"  # pass | warning | fail
    dimensions: list[DimensionScore] = Field(default_factory=list)
    differences: list[ComplianceDifference] = Field(default_factory=list)
    matched_fields: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    # Element-by-element alignment for the side-by-side diff view.
    alignment: list[ComplianceAlignedPair] = Field(default_factory=list)
    # Ordered preview blocks of the checked document (so users see what was checked).
    document_preview: list[dict] = Field(default_factory=list)
    # True when there are changed/missing FIXED differences the in-place fixer can repair.
    fixable: bool = False
    # AI token usage + estimated cost for the semantic judge (None when AI was off).
    token_usage: dict | None = None
