"""Validation report schemas (spec §13)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .enums import IssueSeverity, ValidationStatus


class ValidationIssue(BaseModel):
    rule_id: str | None = None
    field_name: str | None = None
    node_id: str | None = None
    severity: IssueSeverity = IssueSeverity.ERROR
    message: str = ""
    suggested_fix: str = ""


class ValidationReport(BaseModel):
    status: ValidationStatus = ValidationStatus.PASS
    issues: list[ValidationIssue] = Field(default_factory=list)
    checked_fields: list[str] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)  # severity -> count

    def add(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)

    def finalize(self) -> ValidationReport:
        """Compute summary counts and overall status from the collected issues."""
        counts = {sev.value: 0 for sev in IssueSeverity}
        for i in self.issues:
            counts[i.severity.value] = counts.get(i.severity.value, 0) + 1
        self.summary = counts
        if counts.get(IssueSeverity.ERROR.value, 0) > 0:
            self.status = ValidationStatus.FAIL
        elif counts.get(IssueSeverity.WARNING.value, 0) > 0:
            self.status = ValidationStatus.WARNING
        else:
            self.status = ValidationStatus.PASS
        return self
