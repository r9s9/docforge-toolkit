"""Shared enumerations used across the platform.

These are the canonical vocabularies for element types, AI classifications,
field types, diff outcomes, job state, generation modes and validation.
"""

from __future__ import annotations

from enum import Enum


class ElementType(str, Enum):
    """Structural element kinds in the normalized tree."""

    PARAGRAPH = "paragraph"
    HEADING = "heading"
    TABLE = "table"
    TABLE_ROW = "table_row"
    TABLE_CELL = "table_cell"
    IMAGE = "image"
    LIST_ITEM = "list_item"
    SECTION_BREAK = "section_break"
    HEADER = "header"
    FOOTER = "footer"
    FIELD = "field"  # Word field (TOC, page number, etc.) — usually AUTO
    UNKNOWN = "unknown"


class ClassificationType(str, Enum):
    """How a node behaves across documents of the same type."""

    FIXED = "FIXED"
    DYNAMIC_TEXT = "DYNAMIC_TEXT"
    DYNAMIC_DATE = "DYNAMIC_DATE"
    DYNAMIC_PERSON = "DYNAMIC_PERSON"
    DYNAMIC_ENUM = "DYNAMIC_ENUM"
    DYNAMIC_NUMBER = "DYNAMIC_NUMBER"
    DYNAMIC_IMAGE = "DYNAMIC_IMAGE"
    REPEATABLE_TABLE = "REPEATABLE_TABLE"
    REPEATABLE_SECTION = "REPEATABLE_SECTION"
    # A heading plus its body, repeated once per item (one block per project,
    # per finding, per milestone). Unlike REPEATABLE_SECTION, which repeats a
    # single paragraph, this repeats a titled group.
    REPEATABLE_BLOCK = "REPEATABLE_BLOCK"
    AUTO_FIELD = "AUTO_FIELD"
    UNKNOWN = "UNKNOWN"


class FieldType(str, Enum):
    """Logical data type of a template field."""

    TEXT = "text"
    MULTILINE_TEXT = "multiline_text"
    DATE = "date"
    PERSON = "person"
    NUMBER = "number"
    ENUM = "enum"
    TABLE = "table"
    BOOLEAN = "boolean"
    IMAGE = "image"


class DiffStatus(str, Enum):
    """Result of comparing one logical node across multiple documents."""

    IDENTICAL = "identical"
    CHANGED = "changed"
    PARTIAL_CHANGE = "partial_change"  # static prefix/suffix + variable token(s)
    ADDED = "added"
    REMOVED = "removed"
    ROW_COUNT_CHANGED = "row_count_changed"
    IMAGE_CHANGED = "image_changed"


class JobStatus(str, Enum):
    """Lifecycle of a background job (analysis / generation)."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GenerationMode(str, Enum):
    """How the caller supplies content for a generation request."""

    STRUCTURED_JSON = "structured_json"
    STRUCTURED_FORM = "structured_form"
    UNSTRUCTURED_TEXT = "unstructured_text"


class ValidationStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class RuleType(str, Enum):
    """Kinds of validation rules supported by the validator engine."""

    REQUIRED = "required"
    DATA_TYPE = "data_type"
    ENUM = "enum"
    MIN_LENGTH = "min_length"
    MAX_LENGTH = "max_length"
    DATE_FORMAT = "date_format"
    NUMERIC_FORMAT = "numeric_format"
    TABLE_SCHEMA = "table_schema"
    REGEX = "regex"
    CROSS_FIELD = "cross_field"  # placeholder for future use


# --- Helper predicates / mappings -------------------------------------------

DYNAMIC_TYPES = {
    ClassificationType.DYNAMIC_TEXT,
    ClassificationType.DYNAMIC_DATE,
    ClassificationType.DYNAMIC_PERSON,
    ClassificationType.DYNAMIC_ENUM,
    ClassificationType.DYNAMIC_NUMBER,
}

REPEATABLE_TYPES = {
    ClassificationType.REPEATABLE_TABLE,
    ClassificationType.REPEATABLE_SECTION,
    ClassificationType.REPEATABLE_BLOCK,
}


def is_dynamic(c: ClassificationType) -> bool:
    return c in DYNAMIC_TYPES


def is_repeatable(c: ClassificationType) -> bool:
    return c in REPEATABLE_TYPES


def needs_field(c: ClassificationType) -> bool:
    """Whether this classification produces a user-fillable field."""
    return is_dynamic(c) or is_repeatable(c)


_FIELD_TYPE_MAP: dict[ClassificationType, FieldType] = {
    ClassificationType.DYNAMIC_TEXT: FieldType.TEXT,
    ClassificationType.DYNAMIC_DATE: FieldType.DATE,
    ClassificationType.DYNAMIC_PERSON: FieldType.PERSON,
    ClassificationType.DYNAMIC_ENUM: FieldType.ENUM,
    ClassificationType.DYNAMIC_NUMBER: FieldType.NUMBER,
    ClassificationType.DYNAMIC_IMAGE: FieldType.IMAGE,
    ClassificationType.REPEATABLE_TABLE: FieldType.TABLE,
    ClassificationType.REPEATABLE_SECTION: FieldType.MULTILINE_TEXT,
    # Rows of {title, body} — the same shape as a table, so no new field type.
    ClassificationType.REPEATABLE_BLOCK: FieldType.TABLE,
}


def default_field_type(c: ClassificationType) -> FieldType:
    """Map a classification to its natural field type."""
    return _FIELD_TYPE_MAP.get(c, FieldType.TEXT)
