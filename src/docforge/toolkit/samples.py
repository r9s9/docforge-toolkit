"""The "with labels" copy of a template: every variable shown as «Label».

Adapted from upstream ``services/preview_docx.py`` (``_sample_value`` and
``_sample_context``), which the web app uses for the same download.
"""

from __future__ import annotations

from ..common.textutil import sample_label
from ..schemas.enums import ClassificationType, FieldType
from ..schemas.template import FieldDefinition


def sample_value(field: FieldDefinition) -> object:
    """A readable placeholder for one field."""
    if field.field_type == FieldType.IMAGE:
        # No replacement picture: the original stays, as in the example.
        return None
    if field.field_type == FieldType.TABLE:
        # One illustrative row keyed by each column's field_name.
        return [{c.field_name: sample_label(c.label, c.field_name) for c in field.columns}]
    if field.field_type == FieldType.BOOLEAN:
        return True
    if field.classification == ClassificationType.REPEATABLE_SECTION:
        return [sample_label(field.label, field.field_name)]
    return sample_label(field.label, field.field_name)


def sample_context(fields: list[FieldDefinition]) -> dict[str, object]:
    return {f.field_name: sample_value(f) for f in fields}


def labels_docx(template_bytes: bytes, fields: list[FieldDefinition]) -> bytes:
    """Render the template with «Label» values so it reads like a document."""
    from ..assembler import assemble

    return assemble(template_bytes, sample_context(fields), fields)
