"""Tests for ``ingestion.slide_persist`` — Phase 6 T3 WebP slide persistence."""

from __future__ import annotations

import uuid
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from course_supporter.ingestion.presentation import SlideRaw
from course_supporter.ingestion.slide_persist import (
    _encode_webp,
    _slide_key,
    persist_slide_webps,
)


def _png(color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    """Produce a tiny real PNG (what PyMuPDF hands the seam)."""
    buffer = BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _slides(*numbers: int) -> list[SlideRaw]:
    return [
        SlideRaw(slide_number=n, raw_text=f"slide {n}", image_bytes=_png())
        for n in numbers
    ]


class TestEncodeWebp:
    def test_produces_decodable_webp(self) -> None:
        """PNG → lossless WebP that PIL can reopen as a WEBP image."""
        webp = _encode_webp(_png())
        with Image.open(BytesIO(webp)) as img:
            assert img.format == "WEBP"
            assert img.size == (8, 8)


class TestSlideKey:
    def test_deterministic_zero_padded(self) -> None:
        """Key is deterministic in (document_id, slide_number), 1-indexed."""
        tid = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        nid = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
        did = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
        key = _slide_key(tid, nid, did, 7)
        assert key == (f"tenants/{tid}/nodes/{nid}/slides/{did}/0007.webp")
        # Same inputs → same key (re-ingest overwrites in place).
        assert key == _slide_key(tid, nid, did, 7)


class TestPersistSlideWebps:
    async def test_uploads_each_slide_as_webp_and_returns_ordered_keys(
        self,
    ) -> None:
        """Every slide uploaded image/webp; keys ordered, deterministic."""
        s3 = AsyncMock()
        s3.upload_file = AsyncMock(return_value="url")
        tid, nid, did = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

        keys = await persist_slide_webps(
            s3, tenant_id=tid, node_id=nid, document_id=did, slides=_slides(1, 2, 3)
        )

        assert keys == [
            _slide_key(tid, nid, did, 1),
            _slide_key(tid, nid, did, 2),
            _slide_key(tid, nid, did, 3),
        ]
        assert s3.upload_file.await_count == 3
        for call in s3.upload_file.await_args_list:
            _key, data, content_type = call.args
            assert content_type == "image/webp"
            with Image.open(BytesIO(data)) as img:
                assert img.format == "WEBP"

    async def test_empty_slides_returns_empty(self) -> None:
        """No slides → no uploads, empty key list."""
        s3 = AsyncMock()
        s3.upload_file = AsyncMock()
        keys = await persist_slide_webps(
            s3,
            tenant_id=uuid.uuid4(),
            node_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            slides=[],
        )
        assert keys == []
        s3.upload_file.assert_not_awaited()

    async def test_upload_failure_propagates(self) -> None:
        """An upload error propagates so the caller's ingest tx rolls back."""
        s3 = AsyncMock()
        s3.upload_file = AsyncMock(side_effect=RuntimeError("s3 down"))
        with pytest.raises(RuntimeError, match="s3 down"):
            await persist_slide_webps(
                s3,
                tenant_id=uuid.uuid4(),
                node_id=uuid.uuid4(),
                document_id=uuid.uuid4(),
                slides=_slides(1),
            )
