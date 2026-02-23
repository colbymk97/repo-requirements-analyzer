from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ValidationResult:
    min_stories: int
    min_evidence: int
    story_count: int
    evidence_count: int
    errors: list[str]

    @property
    def passed(self) -> bool:
        return not self.errors


def validate_report(report: str, min_stories: int, min_evidence: int) -> ValidationResult:
    errors: list[str] = []

    required_sections = [
        "## 1.",
        "## 2.",
        "## 3.",
        "## 4.",
        "## 5.",
        "## 6.",
        "## 7.",
        "## 8.",
    ]
    for section in required_sections:
        if section not in report:
            errors.append(f"Missing required section heading: {section}")

    story_count = len(re.findall(r"As a ", report, flags=re.IGNORECASE))
    if story_count < min_stories:
        errors.append(f"User story count too low: {story_count} < {min_stories}")

    evidence_paths = re.findall(r"`[^`]+(?:/|\\)[^`]+`", report)
    evidence_rows = len(re.findall(r"^\|", report, flags=re.MULTILINE))
    evidence_count = max(len(set(evidence_paths)), evidence_rows)
    if evidence_count < min_evidence:
        errors.append(f"Evidence count too low: {evidence_count} < {min_evidence}")

    return ValidationResult(
        min_stories=min_stories,
        min_evidence=min_evidence,
        story_count=story_count,
        evidence_count=evidence_count,
        errors=errors,
    )


def append_quality_warning(report: str, validation: ValidationResult) -> str:
    if validation.passed:
        return report

    lines = [
        "",
        "---",
        "",
        "## Quality Warning",
        "",
        "This report failed one or more automatic quality checks.",
        f"- Story count: {validation.story_count} (minimum {validation.min_stories})",
        f"- Evidence count: {validation.evidence_count} (minimum {validation.min_evidence})",
        "",
        "Validation issues:",
    ]
    lines.extend([f"- {err}" for err in validation.errors])
    return report.rstrip() + "\n" + "\n".join(lines) + "\n"
