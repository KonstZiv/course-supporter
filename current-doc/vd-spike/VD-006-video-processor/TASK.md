# VD-006: Redesign VideoProcessor

**Фаза:** 5 — Integration
**Пріоритет:** Високий
**Залежності:** VD-005, CP-4 passed (Gate Review)

## Що робимо

Переписати `src/course_supporter/ingestion/video.py` для паралельної обробки STT і VD як незалежних потоків.

## Яким чином

### Ключова зміна

Старий VideoProcessor відправляв відео до Gemini Vision напряму. Новий запускає STT і VD **повністю паралельно** через `asyncio.gather`, потім виконує cross-modal alignment.

### Архітектура

```python
class VideoProcessor(SourceProcessor):
    def __init__(self, stt_router, vd_pipeline, aligner): ...

    async def process(self, source, *, router=None) -> SourceDocument:
        video_path = await self._download(source.source_url)
        audio_path = await self._extract_audio(video_path)

        # Паралельні незалежні потоки
        stt_task = asyncio.create_task(self._stt.transcribe(audio_path))
        vd_task = asyncio.create_task(self._vd.process(video_path))
        stt_result, vd_result = await asyncio.gather(stt_task, vd_task)

        # Cross-modal alignment ПІСЛЯ обох
        aligned, report = self._aligner.align(stt_result, vd_result)

        chunks = self._build_chunks(aligned, report)
        return SourceDocument(source_type=SourceType.VIDEO, chunks=chunks, ...)
```

## Acceptance criteria

- [ ] STT і VD запускаються паралельно
- [ ] VideoProcessor не залежить від порядку завершення STT/VD
- [ ] Backwards compatible — існуючі тести не ламаються
- [ ] Error handling: якщо VD fails, STT result все одно використовується (graceful degradation)
