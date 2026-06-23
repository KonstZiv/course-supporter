"""Tests for webhook delivery module."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from course_supporter.homework.webhook import (
    build_reviewed_payload,
    deliver_webhook,
    resolve_webhook_url,
)
from course_supporter.models.webhook import WebhookReviewedPayload

# --- Helpers ---


def _make_submission(
    *,
    webhook_url: str | None = "https://example.com/hook",
    review_result: dict | None = None,
    review_markdown: str | None = None,
    response_language: str | None = "uk",
) -> MagicMock:
    sub = MagicMock()
    sub.id = uuid.uuid4()
    sub.tenant_id = uuid.uuid4()
    sub.student_id = uuid.uuid4()
    sub.webhook_url = webhook_url
    sub.review_result = review_result
    sub.review_markdown = review_markdown
    sub.response_language = response_language
    return sub


def _make_student(external_id: str = "student-42") -> MagicMock:
    s = MagicMock()
    s.external_id = external_id
    return s


def _make_tenant(webhook_url: str | None = None) -> MagicMock:
    t = MagicMock()
    t.webhook_url = webhook_url
    return t


def _sample_review_result() -> dict:
    return {
        "analysis": {
            "passed": True,
            "score": 85,
            "correctness": "correct",
        },
    }


# --- resolve_webhook_url ---


class TestResolveWebhookUrl:
    def test_submission_url_takes_priority(self) -> None:
        sub = _make_submission(webhook_url="https://sub.example.com")
        tenant = _make_tenant(webhook_url="https://tenant.example.com")
        assert resolve_webhook_url(sub, tenant) == "https://sub.example.com"

    def test_falls_back_to_tenant(self) -> None:
        sub = _make_submission(webhook_url=None)
        tenant = _make_tenant(webhook_url="https://tenant.example.com")
        assert resolve_webhook_url(sub, tenant) == "https://tenant.example.com"

    def test_none_when_no_url(self) -> None:
        sub = _make_submission(webhook_url=None)
        tenant = _make_tenant(webhook_url=None)
        assert resolve_webhook_url(sub, tenant) is None

    def test_none_when_no_tenant(self) -> None:
        sub = _make_submission(webhook_url=None)
        assert resolve_webhook_url(sub, None) is None


# --- Payload builders ---


class TestBuildReviewedPayload:
    def test_builds_from_review_result(self) -> None:
        sub = _make_submission(
            review_result=_sample_review_result(),
            review_markdown="Рішення правильне.",
            response_language="uk",
        )
        student = _make_student()

        payload = build_reviewed_payload(sub, student)

        assert payload.event == "reviewed"
        assert payload.review.passed is True
        assert payload.review.score == 85
        assert payload.review.correctness == "correct"
        assert payload.review.review_text == "Рішення правильне."
        assert payload.review.response_language == "uk"

    def test_defaults_for_missing_review(self) -> None:
        sub = _make_submission(
            review_result=None,
            review_markdown=None,
            response_language=None,
        )
        student = _make_student()

        payload = build_reviewed_payload(sub, student)

        assert payload.review.passed is False
        assert payload.review.score == 0
        assert payload.review.review_text == ""
        assert payload.review.response_language == "en"


# --- deliver_webhook ---


class TestDeliverWebhook:
    """Webhook delivery with mocked httpx and SSRF validation."""

    @pytest.fixture()
    def mock_session(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture()
    def payload(self) -> WebhookReviewedPayload:
        return WebhookReviewedPayload(
            submission_id="sub-1",
            student_external_id="stu-1",
            review={
                "passed": True,
                "score": 80,
                "correctness": "correct",
                "review_text": "OK.",
                "response_language": "en",
            },
            timestamp=datetime.now(UTC),
        )

    @patch(
        "course_supporter.homework.webhook.validate_webhook_url", new_callable=AsyncMock
    )
    @patch("course_supporter.homework.webhook.httpx.AsyncClient")
    async def test_successful_delivery(
        self,
        mock_client_cls: MagicMock,
        mock_validate: AsyncMock,
        mock_session: MagicMock,
        payload: WebhookReviewedPayload,
    ) -> None:
        mock_validate.return_value = "https://example.com/hook"
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await deliver_webhook(
            url="https://example.com/hook",
            payload=payload,
            session=mock_session,
        )

        assert result is True
        mock_client.post.assert_awaited_once()
        mock_session.add.assert_called_once()
        esc = mock_session.add.call_args[0][0]
        assert esc.success is True
        assert esc.action == "webhook_reviewed"

    @patch(
        "course_supporter.homework.webhook.validate_webhook_url", new_callable=AsyncMock
    )
    async def test_ssrf_blocked(
        self,
        mock_validate: AsyncMock,
        mock_session: MagicMock,
        payload: WebhookReviewedPayload,
    ) -> None:
        from fastapi import HTTPException

        mock_validate.side_effect = HTTPException(status_code=422, detail="blocked")

        result = await deliver_webhook(
            url="http://169.254.169.254/metadata",
            payload=payload,
            session=mock_session,
        )

        assert result is False
        mock_session.add.assert_called_once()
        esc = mock_session.add.call_args[0][0]
        assert esc.success is False

    @patch(
        "course_supporter.homework.webhook.validate_webhook_url", new_callable=AsyncMock
    )
    @patch("course_supporter.homework.webhook.httpx.AsyncClient")
    @patch("course_supporter.homework.webhook.asyncio.sleep", new_callable=AsyncMock)
    async def test_retries_on_server_error(
        self,
        mock_sleep: AsyncMock,
        mock_client_cls: MagicMock,
        mock_validate: AsyncMock,
        mock_session: MagicMock,
        payload: WebhookReviewedPayload,
    ) -> None:
        mock_validate.return_value = "https://example.com/hook"

        error_response = MagicMock()
        error_response.is_success = False
        error_response.status_code = 500

        success_response = MagicMock()
        success_response.is_success = True
        success_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=[error_response, success_response],
        )
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await deliver_webhook(
            url="https://example.com/hook",
            payload=payload,
            session=mock_session,
        )

        assert result is True
        assert mock_client.post.await_count == 2
        mock_sleep.assert_awaited_once_with(1)  # 2^0 = 1s backoff

    @patch(
        "course_supporter.homework.webhook.validate_webhook_url", new_callable=AsyncMock
    )
    @patch("course_supporter.homework.webhook.httpx.AsyncClient")
    async def test_no_retry_on_4xx(
        self,
        mock_client_cls: MagicMock,
        mock_validate: AsyncMock,
        mock_session: MagicMock,
        payload: WebhookReviewedPayload,
    ) -> None:
        mock_validate.return_value = "https://example.com/hook"

        error_response = MagicMock()
        error_response.is_success = False
        error_response.status_code = 404

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=error_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await deliver_webhook(
            url="https://example.com/hook",
            payload=payload,
            session=mock_session,
        )

        assert result is False
        assert mock_client.post.await_count == 1  # no retry

    @patch(
        "course_supporter.homework.webhook.validate_webhook_url", new_callable=AsyncMock
    )
    @patch("course_supporter.homework.webhook.httpx.AsyncClient")
    @patch("course_supporter.homework.webhook.asyncio.sleep", new_callable=AsyncMock)
    async def test_network_error_retries(
        self,
        mock_sleep: AsyncMock,
        mock_client_cls: MagicMock,
        mock_validate: AsyncMock,
        mock_session: MagicMock,
        payload: WebhookReviewedPayload,
    ) -> None:
        mock_validate.return_value = "https://example.com/hook"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused"),
        )
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await deliver_webhook(
            url="https://example.com/hook",
            payload=payload,
            session=mock_session,
        )

        assert result is False
        assert mock_client.post.await_count == 3  # all retries exhausted

    @patch(
        "course_supporter.homework.webhook.validate_webhook_url", new_callable=AsyncMock
    )
    @patch("course_supporter.homework.webhook.httpx.AsyncClient")
    async def test_reviewed_payload_delivery(
        self,
        mock_client_cls: MagicMock,
        mock_validate: AsyncMock,
        mock_session: MagicMock,
    ) -> None:
        mock_validate.return_value = "https://example.com/hook"
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        reviewed_payload = WebhookReviewedPayload(
            submission_id="sub-1",
            student_external_id="stu-1",
            review={
                "passed": True,
                "score": 90,
                "correctness": "correct",
                "review_text": "Good job.",
                "response_language": "en",
            },
            timestamp=datetime.now(UTC),
        )

        result = await deliver_webhook(
            url="https://example.com/hook",
            payload=reviewed_payload,
            session=mock_session,
        )

        assert result is True
        esc = mock_session.add.call_args[0][0]
        assert esc.action == "webhook_reviewed"
