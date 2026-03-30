"""STT provider implementations.

STT_PROVIDER_REGISTRY maps provider names to their implementation
classes. To add a new provider:

1. Create a new module in this package (e.g., soniox.py)
2. Implement STTProvider subclass
3. Add entry to STT_PROVIDER_REGISTRY below
"""

from course_supporter.stt.providers.base import STTProvider
from course_supporter.stt.providers.deepgram import DeepgramSTTProvider
from course_supporter.stt.providers.elevenlabs import ElevenLabsSTTProvider
from course_supporter.stt.providers.openai_stt import OpenAISTTProvider

STT_PROVIDER_REGISTRY: dict[str, type[STTProvider]] = {
    "elevenlabs": ElevenLabsSTTProvider,
    "openai_stt": OpenAISTTProvider,
    "deepgram": DeepgramSTTProvider,
}

__all__ = [
    "STT_PROVIDER_REGISTRY",
    "DeepgramSTTProvider",
    "ElevenLabsSTTProvider",
    "OpenAISTTProvider",
    "STTProvider",
]
