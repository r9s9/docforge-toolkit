"""Template package schemas (spec §11–13): field definitions, validation rules,
template intelligence, manifest and the review snapshot.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .classification import ElementClassification, SectionUnderstanding
from .enums import ClassificationType, FieldType, IssueSeverity, RuleType


class TableColumn(BaseModel):
    """One column of a REPEATABLE_TABLE field."""

    field_name: str
    label: str = ""
    field_type: FieldType = FieldType.TEXT
    required: bool = False


class FieldDefinition(BaseModel):
    """A user-fillable field exposed by a template."""

    field_name: str  # the Jinja variable name used in template.docx
    label: str = ""
    field_type: FieldType = FieldType.TEXT
    classification: ClassificationType = ClassificationType.DYNAMIC_TEXT
    description: str = ""
    required: bool = True
    enum_values: list[str] = Field(default_factory=list)
    default: Any | None = None
    node_ids: list[str] = Field(default_factory=list)  # source elements
    section_key: str | None = None
    columns: list[TableColumn] = Field(default_factory=list)  # for TABLE fields
    confidence: float = 1.0


class ValidationRule(BaseModel):
    """A single declarative validation rule (spec §13)."""

    rule_id: str
    rule_type: RuleType
    field_name: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None
    severity: IssueSeverity = IssueSeverity.ERROR


class TemplateIntelligence(BaseModel):
    """Metadata describing how the template was understood (template_intelligence.json)."""

    template_id: str
    version: int
    document_type_guess: str = ""
    sections: list[SectionUnderstanding] = Field(default_factory=list)
    classifications: list[ElementClassification] = Field(default_factory=list)
    diff_summary: dict[str, Any] | None = None
    model_used_for_analysis: str | None = None
    notes: str = ""


class TemplateManifest(BaseModel):
    """manifest.json for a template version (spec §12)."""

    template_id: str
    version: int
    name: str
    source_file_names: list[str] = Field(default_factory=list)
    created_at: str  # ISO-8601 (stamped by caller; no clock in pure logic)
    created_by: str = "local"
    renderer: str = "docxtpl"
    model_used_for_analysis: str | None = None
    notes: str = ""
    supported_generation_modes: list[str] = Field(
        default_factory=lambda: ["structured_json", "structured_form", "unstructured_text"]
    )


class ReviewSnapshot(BaseModel):
    """The exact reviewed state captured when a user approves a template
    (review_snapshot.json). Enables reproducible re-builds and auditing.
    """

    document_type_guess: str = ""
    classifications: list[ElementClassification] = Field(default_factory=list)
    field_definitions: list[FieldDefinition] = Field(default_factory=list)
    validation_rules: list[ValidationRule] = Field(default_factory=list)
    sections: list[SectionUnderstanding] = Field(default_factory=list)
    edited_by_user: bool = False
