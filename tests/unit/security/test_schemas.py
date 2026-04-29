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
        # strings, not Python enum identifiers.
        assert ViolationCategory.PROMPT_INJECTION == "prompt_injection"
        assert ViolationCategory.OFF_TOPIC == "off_topic"
        assert ViolationCategory.POLICY_VIOLATION == "policy_violation"
        assert ViolationCategory.SUSPICIOUS_BEHAVIOR == "suspicious_behavior"
