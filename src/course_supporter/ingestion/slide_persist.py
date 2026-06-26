"""Slide-WebP persistence for the student portal media layer (Phase 6 T3).

This module is the orchestrator-seam half of presentation media delivery
(KD17). ``PresentationProcessor`` renders one image per slide transiently
for Pass 1 vision description and discards them; here the
``arq_ingest_material`` seam re-encodes those already-rendered images to
WebP, uploads them to S3, and returns the ordered object keys for the
``AuthoredDocument.slide_keys`` carrier.

Design boundary (Design 2): the processor is untouched bar a read-only
accessor (``PresentationProcessor.rendered_slides``). All encode / upload /
key-composition lives here — never in the processor — and the LLM contour
(KD-2.3-F) is byte-identical, since this consumes the images Pass 1 already
produced and adds nothing to that path.

Keys are deterministic per ``(document_id, slide_number)`` so a re-ingest
overwrites the same objects in place (a residual orphan can remain only if
the slide count shrinks across re-ingests — a known, reactive edge).
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import TYPE_CHECKING

import structlog
from PIL import Image

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from course_supporter.ingestion.presentation import SlideRaw
    from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger()

# WebP content-type so the browser renders the object inline via the
# presigned-GET URL (no download prompt).
_WEBP_CONTENT_TYPE = "image/webp"


def _slide_key(
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
    document_id: uuid.UUID,
    slide_number: int,
) -> str:
    """Compose the deterministic S3 key for one persisted slide.

    Mirrors the richer upload prefix convention
    (``tenants/{tid}/nodes/{node_id}/...``). Deterministic in
    ``(document_id, slide_number)`` — no UUID segment — so a re-ingest
    overwrites the same object in place. ``slide_number`` is 1-indexed and
    zero-padded for stable lexical ordering.
    """
    return (
        f"tenants/{tenant_id}/nodes/{node_id}/slides/"
        f"{document_id}/{slide_number:04d}.webp"
    )


def _encode_webp(png_bytes: bytes) -> bytes:
    """Re-encode a rendered PNG slide to lossless WebP (blocking).

    Lossless keeps slide text and diagrams fully legible — storage volume is
    deliberately not optimised in T3 (the 150-dpi render is the fidelity
    ceiling). Runs under ``asyncio.to_thread`` from the async caller because
    Pillow is CPU-blocking.
    """
    with Image.open(BytesIO(png_bytes)) as img:
        buffer = BytesIO()
        img.save(buffer, format="WEBP", lossless=True)
        return buffer.getvalue()


async def persist_slide_webps(
    s3: S3Client,
    *,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
    document_id: uuid.UUID,
    slides: Sequence[SlideRaw],
) -> list[str]:
    """Encode + upload every slide as WebP; return the ordered S3 keys.

    The returned list is positional (index = slide_number - 1) and is written
    to ``AuthoredDocument.slide_keys``. Any encode/upload failure propagates
    so the caller's ingest transaction rolls back (material-complete iff
    ingest-success — Q3 strict). Sequential by slide: a deck is tens of
    slides, capped at 100, so fan-out is unnecessary.
    """
    keys: list[str] = []
    for slide in slides:
        webp = await asyncio.to_thread(_encode_webp, slide.image_bytes)
        key = _slide_key(tenant_id, node_id, document_id, slide.slide_number)
        await s3.upload_file(key, webp, _WEBP_CONTENT_TYPE)
        keys.append(key)

    logger.info(
        "slides_persisted_webp",
        document_id=str(document_id),
        slide_count=len(keys),
    )
    return keys
