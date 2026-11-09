"""validator — check a render context against field definitions + rules (spec §13).

Produces a ValidationReport with pass/warning/fail status and human-readable,
field-scoped issues with suggested fixes.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from dateutil import parser as date_parser

from ..schemas.enums import IssueSeverity, RuleType
from ..schemas.template import FieldDefinition, ValidationRule
from ..schemas.validation import ValidationIssue, ValidationReport

_NUMBER_CLEAN_RE = re.compile(r"[^\d.\-]")


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _parse_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = _NUMBER_CLEAN_RE.sub("", value.strip())
    if cleaned in ("", "-", ".", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_date(value: Any, formats: list[str]) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    for fmt in formats:
        try:
            datetime.strptime(value.strip(), fmt)
            return True
        except ValueError:
            continue
    try:
        date_parser.parse(value.strip(), fuzzy=False)
        return True
    except (ValueError, OverflowError):
        return False


def validate(
    context: dict[str, Any],
    fields: list[FieldDefinition],
    rules: list[ValidationRule],
) -> ValidationReport:
    """Evaluate ``rules`` against ``context``; return a finalized report."""
    report = ValidationReport(checked_fields=[f.field_name for f in fields])
    field_by_name = {f.field_name: f for f in fields}

    for rule in rules:
        field = field_by_name.get(rule.field_name) if rule.field_name else None
        label = field.label if field else (rule.field_name or "")
        value = context.get(rule.field_name) if rule.field_name else None
        _apply_rule(report, rule, field, label, value)

    return report.finalize()


def _issue(rule: ValidationRule, label: str, message: str, fix: str = "") -> ValidationIssue:
    return ValidationIssue(
        rule_id=rule.rule_id,
        field_name=rule.field_name,
        severity=rule.severity,
        message=message,
        suggested_fix=fix,
    )


def _apply_rule(report, rule, field, label, value) -> None:
    rt = rule.rule_type

    if rt == RuleType.REQUIRED:
        if _is_empty(value):
            report.add(_issue(rule, label, rule.message or f"'{label}' is required", f"Provide a value for '{label}'."))
        return

    # All remaining rules only apply when a value is present.
    if _is_empty(value):
        return

    if rt == RuleType.DATE_FORMAT:
        formats = rule.params.get("formats", ["%Y-%m-%d"])
        if not _parse_date(value, formats):
            report.add(_issue(rule, label, rule.message or f"'{label}' is not a recognizable date", f"Use a date like {datetime(2026, 6, 1).strftime(formats[0])}."))

    elif rt == RuleType.NUMERIC_FORMAT:
        if _parse_number(value) is None:
            report.add(_issue(rule, label, rule.message or f"'{label}' is not numeric", f"Enter a number for '{label}'."))

    elif rt == RuleType.ENUM:
        allowed = rule.params.get("allowed", [])
        if allowed and str(value) not in allowed:
            report.add(_issue(rule, label, rule.message or f"'{label}' must be one of: {', '.join(allowed)}", f"Choose one of: {', '.join(allowed)}."))

    elif rt == RuleType.MIN_LENGTH:
        n = int(rule.params.get("min", 0))
        if len(str(value)) < n:
            report.add(_issue(rule, label, rule.message or f"'{label}' must be at least {n} characters"))

    elif rt == RuleType.MAX_LENGTH:
        n = int(rule.params.get("max", 0))
        if n and len(str(value)) > n:
            report.add(_issue(rule, label, rule.message or f"'{label}' must be at most {n} characters", f"Shorten '{label}' to {n} characters or fewer."))

    elif rt == RuleType.REGEX:
        pattern = rule.params.get("pattern", "")
        if pattern and not re.fullmatch(pattern, str(value)):
            report.add(_issue(rule, label, rule.message or f"'{label}' does not match the required format"))

    elif rt == RuleType.TABLE_SCHEMA:
        _validate_table(report, rule, label, value)

    # DATA_TYPE / CROSS_FIELD: reserved for future use (no-op in v1).


def _validate_table(report, rule, label, value) -> None:
    if not isinstance(value, list):
        report.add(_issue(rule, label, f"'{label}' must be a list of rows", "Provide rows as a list of objects."))
        return
    required_cols = rule.params.get("required_columns", [])
    allowed_cols = set(rule.params.get("columns", []))
    for i, row in enumerate(value):
        if not isinstance(row, dict):
            report.add(_issue(rule, label, f"Row {i + 1} of '{label}' is not an object"))
            continue
        for col in required_cols:
            if _is_empty(row.get(col)):
                report.add(_issue(rule, label, f"Row {i + 1} of '{label}' is missing required column '{col}'"))
        if allowed_cols:
            for key in row:
                if key not in allowed_cols:
                    report.add(
                        ValidationIssue(
                            rule_id=rule.rule_id,
                            field_name=rule.field_name,
                            severity=IssueSeverity.INFO,
                            message=f"Row {i + 1} of '{label}' has unexpected column '{key}'",
                            suggested_fix=f"Allowed columns: {', '.join(sorted(allowed_cols))}.",
                        )
                    )
