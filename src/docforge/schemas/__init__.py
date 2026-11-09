"""Pydantic schemas — the serializable data contract for the whole platform.

Importing from ``docforge.schemas`` re-exports every public model so call sites
can do ``from docforge.schemas import DocumentExtraction, FieldDefinition``.
"""

from __future__ import annotations

from .classification import (
    ClassificationResult,
    ElementClassification,
    SectionUnderstanding,
)
from .diff import DiffRunResult, NodeDiff
from .enums import (
    DYNAMIC_TYPES,
    REPEATABLE_TYPES,
    ClassificationType,
    DiffStatus,
    ElementType,
    FieldType,
    GenerationMode,
    IssueSeverity,
    JobStatus,
    RuleType,
    ValidationStatus,
    default_field_type,
    is_dynamic,
    is_repeatable,
    needs_field,
)
from .extraction import (
    DocumentExtraction,
    DocumentSection,
    ImageRef,
    NormalizedElement,
    NumberingInfo,
    ParagraphFormatting,
    Run,
    RunFormatting,
    TableStructure,
)
from .generation import GenerationInput, GenerationResult
from .routing import PlacementInstruction, RoutingResult
from .template import (
    FieldDefinition,
    ReviewSnapshot,
    TableColumn,
    TemplateIntelligence,
    TemplateManifest,
    ValidationRule,
)
from .validation import ValidationIssue, ValidationReport

__all__ = [
    # enums
    "ElementType",
    "ClassificationType",
    "FieldType",
    "DiffStatus",
    "JobStatus",
    "GenerationMode",
    "ValidationStatus",
    "IssueSeverity",
    "RuleType",
    "DYNAMIC_TYPES",
    "REPEATABLE_TYPES",
    "is_dynamic",
    "is_repeatable",
    "needs_field",
    "default_field_type",
    # extraction
    "DocumentExtraction",
    "DocumentSection",
    "NormalizedElement",
    "Run",
    "RunFormatting",
    "ParagraphFormatting",
    "NumberingInfo",
    "ImageRef",
    "TableStructure",
    # classification
    "ElementClassification",
    "SectionUnderstanding",
    "ClassificationResult",
    # diff
    "NodeDiff",
    "DiffRunResult",
    # template
    "FieldDefinition",
    "TableColumn",
    "ValidationRule",
    "TemplateIntelligence",
    "TemplateManifest",
    "ReviewSnapshot",
    # routing
    "PlacementInstruction",
    "RoutingResult",
    # validation
    "ValidationIssue",
    "ValidationReport",
    # generation
    "GenerationInput",
    "GenerationResult",
]
