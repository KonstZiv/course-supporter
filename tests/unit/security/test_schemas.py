"""Tests for SafetyResult / ViolationCategory (KD14 Stage 2 schema)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from course_supporter.security.schemas import SafetyResult, ViolationCategory

# ── Happy-path parsing ─────────────────────────────────────────────


class TestSafetyResultHappyPath:
    def test_parses_clean_text_verdict(self) -> None:
        payload = {
            "is_safe": True,
            "violations": [],
            "confidence": 0.95,
            "reasoning": "No issues detected.",
        }
        result = SafetyResult.model_validate(payload)

        assert result.is_safe is True
        assert result.violations == []
        assert result.confidence == 0.95
        assert result.reasoning == "No issues detected."

    def test_parses_single_violation(self) -> None:
        payload = {
            "is_safe": False,
            "violations": ["prompt_injection"],
            "confidence": 0.88,
            "reasoning": "Detected attempt to override system prompt.",
        }
        result = SafetyResult.model_validate(payload)

        assert result.is_safe is False
        assert result.violations == [ViolationCategory.PROMPT_INJECTION]
        assert result.confidence == 0.88

    def test_parses_multiple_violations(self) -> None:
        payload = {
            "is_safe": False,
            "violations": ["off_topic", "policy_violation"],
            "confidence": 0.7,
            "reasoning": "Off-topic content with policy issue.",
        }
        result = SafetyResult.model_validate(payload)

        assert result.violations == [
            ViolationCategory.OFF_TOPIC,
            ViolationCategory.POLICY_VIOLATION,
        ]

    def test_validate_json_string_round_trip(self) -> None:
        payload = {
            "is_safe": True,
            "violations": [],
            "confidence": 1.0,
            "reasoning": "Clean.",
        }
        result = SafetyResult.model_validate_json(json.dumps(payload))

        assert result.is_safe is True
        assert result.confidence == 1.0


# ── Confidence boundary validation ─────────────────────────────────


class TestConfidenceBounds:
    def test_zero_lower_bound_accepted(self) -> None:
        payload = {
            "is_safe": False,
            "violations": ["suspicious_behavior"],
            "confidence": 0.0,
            "reasoning": "Cannot assess.",
        }
        result = SafetyResult.model_validate(payload)
        assert result.confidence == 0.0

    def test_one_upper_bound_accepted(self) -> None:
        payload = {
            "is_safe": True,
            "violations": [],
            "confidence": 1.0,
            "reasoning": "Definitively safe.",
        }
        result = SafetyResult.model_validate(payload)
        assert result.confidence == 1.0

    def test_negative_confidence_rejected(self) -> None:
        payload = {
            "is_safe": True,
            "violations": [],
            "confidence": -0.1,
            "reasoning": "Negative confidence is invalid.",
        }
        with pytest.raises(ValidationError):
            SafetyResult.model_validate(payload)

    def test_confidence_above_one_rejected(self) -> None:
        payload = {
            "is_safe": True,
            "violations": [],
            "confidence": 1.5,
            "reasoning": "Confidence above 1.0 is invalid.",
        }
        with pytest.raises(ValidationError):
            SafetyResult.model_validate(payload)


# ── extra="forbid" strictness ──────────────────────────────────────


class TestExtraForbid:
    def test_unknown_top_level_key_rejected(self) -> None:
        payload = {
            "is_safe": True,
            "violations": [],
            "confidence": 0.9,
            "reasoning": "OK",
            "hallucinated_field": "extra",
        }
        with pytest.raises(ValidationError):
            SafetyResult.model_validate(payload)

    def test_typo_in_known_field_rejected(self) -> None:
        # "is_safee" rather than "is_safe" -- typo causes both
        # missing-required (is_safe) and extra-forbidden (is_safee)
        # to fire under extra="forbid".
        payload = {
            "is_safee": True,
            "violations": [],
            "confidence": 0.9,
            "reasoning": "OK",
        }
        with pytest.raises(ValidationError):
            SafetyResult.model_validate(payload)


# ── Required-field enforcement ─────────────────────────────────────


class TestRequiredFields:
    def test_missing_is_safe_rejected(self) -> None:
        payload = {
            "violations": [],
            "confidence": 0.9,
            "reasoning": "OK",
        }
        with pytest.raises(ValidationError):
            SafetyResult.model_validate(payload)

    def test_missing_violations_rejected(self) -> None:
        payload = {
            "is_safe": True,
            "confidence": 0.9,
            "reasoning": "OK",
        }
        with pytest.raises(ValidationError):
            SafetyResult.model_validate(payload)

    def test_missing_confidence_rejected(self) -> None:
        payload = {
            "is_safe": True,
            "violations": [],
            "reasoning": "OK",
        }
        with pytest.raises(ValidationError):
            SafetyResult.model_validate(payload)

    def test_missing_reasoning_rejected(self) -> None:
        payload = {
            "is_safe": True,
            "violations": [],
            "confidence": 0.9,
        }
        with pytest.raises(ValidationError):
            SafetyResult.model_validate(payload)


# ── Violation enum validation ──────────────────────────────────────


class TestViolationCategory:
    def test_unknown_violation_string_rejected(self) -> None:
        payload = {
            "is_safe": False,
            "violations": ["unknown_category"],
            "confidence": 0.9,
            "reasoning": "Hallucinated category.",
        }
        with pytest.raises(ValidationError):
            SafetyResult.model_validate(payload)

    def test_all_categories_parseable(self) -> None:
        payload = {
            "is_safe": False,
            "violations": [
                "prompt_injection",
                "off_topic",
                "policy_violation",
                "suspicious_behavior",
            ],
            "confidence": 0.8,
            "reasoning": "All categories present.",
        }
        result = SafetyResult.model_validate(payload)
        assert set(result.violations) == set(ViolationCategory)

    def test_enum_values_match_strings(self) -> None:
        # Forward-compat for HomeworkSubmission.safety_result JSONB
        # per vision §KD15: enum values must serialise as the literal
        # strings, not Python enum identifiers. ``.value`` access is
        # used so mypy resolves the comparison as ``str == str`` --
        # bare ``StrEnum`` instances compare equal to strings at
        # runtime but mypy treats them as non-overlapping literal
        # types and reports a false positive.
        assert ViolationCategory.PROMPT_INJECTION.value == "prompt_injection"
        assert ViolationCategory.OFF_TOPIC.value == "off_topic"
        assert ViolationCategory.POLICY_VIOLATION.value == "policy_violation"
        assert ViolationCategory.SUSPICIOUS_BEHAVIOR.value == "suspicious_behavior"


# ── Phase 2.1 C2 migrations (KD-2.1-I + KD-2.1-J) ──────────────────
#
# Tests for classes migrated from models/safety.py + safety/exceptions.py
# to canonical security/schemas.py per Phase 2.1 C2.


import uuid  # noqa: E402  -- grouped з Phase 2.1 imports for locality

from course_supporter.security.schemas import (  # noqa: E402
    CourseContext,
    SecurityContext,
)


class TestCourseContext:
    def test_required_fields_only(self) -> None:
        cc = CourseContext(course_title="Python 101", node_title="Decorators")
        assert cc.course_title == "Python 101"
        assert cc.node_title == "Decorators"
        # optional fields default to empty string
        assert cc.course_description == ""
        assert cc.node_description == ""
        assert cc.outline_summary == ""

    def test_all_fields_set(self) -> None:
        cc = CourseContext(
            course_title="Python 101",
            course_description="Intro course",
            node_title="Decorators",
            node_description="Functional patterns",
            outline_summary="Module on advanced Python",
        )
        assert cc.course_description == "Intro course"
        assert cc.node_description == "Functional patterns"
        assert cc.outline_summary == "Module on advanced Python"


class TestSecurityContext:
    def test_create_with_no_fields(self) -> None:
        ctx = SecurityContext()
        assert ctx.tenant_id is None
        assert ctx.student_id is None
        assert ctx.submission_id is None
        assert ctx.file_url is None
        assert ctx.filename is None

    def test_create_with_all_fields(self) -> None:
        tenant_id = uuid.UUID("00000000-0000-7000-8000-000000000001")
        student_id = uuid.UUID("00000000-0000-7000-8000-000000000002")
        submission_id = uuid.UUID("00000000-0000-7000-8000-000000000003")
        ctx = SecurityContext(
            tenant_id=tenant_id,
            student_id=student_id,
            submission_id=submission_id,
            file_url="s3://bucket/key",
            filename="submission.zip",
        )
        assert ctx.tenant_id == tenant_id
        assert ctx.student_id == student_id
        assert ctx.submission_id == submission_id
        assert ctx.file_url == "s3://bucket/key"
        assert ctx.filename == "submission.zip"

    def test_frozen_immutable(self) -> None:
        ctx = SecurityContext(filename="test.zip")
        with pytest.raises((AttributeError, Exception)):
            # Frozen dataclass forbids attribute mutation.
            ctx.filename = "other.zip"  # type: ignore[misc]

    def test_as_log_dict_returns_non_none_stringified(self) -> None:
        tenant_id = uuid.UUID("00000000-0000-7000-8000-000000000011")
        ctx = SecurityContext(tenant_id=tenant_id, filename="x.zip")
        payload = ctx.as_log_dict()
        assert payload == {
            "tenant_id": str(tenant_id),
            "filename": "x.zip",
        }

    def test_as_log_dict_empty_for_all_none(self) -> None:
        ctx = SecurityContext()
        assert ctx.as_log_dict() == {}
