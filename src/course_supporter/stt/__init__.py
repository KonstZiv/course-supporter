"""STT infrastructure: providers, schemas, router.

Quick start::

    from course_supporter.config import get_settings
    from course_supporter.stt import create_stt_router

    router = create_stt_router(get_settings())
    result = await router.transcribe("transcribe", "/path/to/audio.mp3", language="uk")
"""

from course_supporter.stt.router import AllSTTProvidersFailedError, STTRouter
from course_supporter.stt.schemas import STTRequest, STTResult, STTSegment
from course_supporter.stt.setup import create_stt_router

__all__ = [
    "AllSTTProvidersFailedError",
    "STTRequest",
    "STTResult",
    "STTRouter",
    "STTSegment",
    "create_stt_router",
]
