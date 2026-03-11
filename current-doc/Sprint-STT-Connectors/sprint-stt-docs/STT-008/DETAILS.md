# STT-008: Порівняльний тест якості + документація результатів — Деталі для виконавця

**Sprint:** STT Connectors
**Оцінка:** 3h

---

## Мета

Емпірично визначити оптимальну provider chain для українськомовних лекцій

## Контекст

Ця задача є фінальною в Sprint STT (6-8 днів).

Всі три конектори (Deepgram, Soniox, ElevenLabs), Orchestrator, і VideoProcessor integration вже працюють. Тепер потрібно **протестувати на реальному аудіо** і вибрати оптимальну chain.

## Залежності

**Попередня задача:** [STT-007: Factory + VideoProcessor інтеграція](../STT-007/README.md)

---

## Детальний план реалізації

### 1. `scripts/stt_compare.py`

```python
"""Compare STT providers on the same audio file.

Usage:
    uv run python scripts/stt_compare.py --audio lecture.wav --providers deepgram,soniox,elevenlabs
    uv run python scripts/stt_compare.py --audio lecture.wav --providers deepgram --dry-run
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

from course_supporter.config import Settings
from course_supporter.stt.factory import create_stt_providers
from course_supporter.stt.models import AudioInput, TranscriptResult


async def compare(audio_path: Path, provider_names: list[str], dry_run: bool = False) -> dict:
    settings = Settings()
    # Override chain to test specific providers
    settings.stt.stt_provider_chain = ",".join(provider_names)
    providers = create_stt_providers(settings.stt)

    audio = AudioInput(file_path=audio_path)
    results: dict[str, dict] = {}

    for provider in providers:
        print(f"\n{'='*60}")
        print(f"Provider: {provider.name}")
        print(f"{'='*60}")

        if dry_run:
            results[provider.name] = {"status": "dry_run"}
            continue

        start = time.monotonic()
        try:
            result: TranscriptResult = await provider.transcribe(
                audio,
                language=settings.stt.stt_default_language,
                additional_languages=settings.stt.additional_languages_list,
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)

            # Save transcript to file
            output_file = audio_path.parent / f"{provider.name}_{audio_path.stem}.txt"
            output_file.write_text(result.text, encoding="utf-8")

            results[provider.name] = {
                "status": "success",
                "duration_ms": elapsed_ms,
                "cost_usd": provider._estimate_cost(result.duration_seconds),
                "segments_count": len(result.segments),
                "text_length": len(result.text),
                "language_detected": result.language_detected,
                "languages_detected": result.languages_detected,
                "audio_duration_sec": result.duration_seconds,
                "transcript_file": str(output_file),
                "first_500_chars": result.text[:500],
            }

            print(f"  Duration: {elapsed_ms}ms")
            print(f"  Segments: {len(result.segments)}")
            print(f"  Languages: {result.languages_detected}")
            print(f"  Saved to: {output_file}")

        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            results[provider.name] = {
                "status": "error",
                "duration_ms": elapsed_ms,
                "error": str(exc),
            }
            print(f"  ERROR: {exc}")

    # Print comparison table
    print(f"\n{'='*60}")
    print("COMPARISON TABLE")
    print(f"{'='*60}")
    print(f"{'Provider':<15} {'Status':<10} {'Time(ms)':<10} {'Cost($)':<10} {'Segments':<10} {'Langs':<15}")
    print("-" * 70)
    for name, data in results.items():
        if data["status"] == "success":
            print(f"{name:<15} {'OK':<10} {data['duration_ms']:<10} {data.get('cost_usd', 'N/A'):<10.4f} {data['segments_count']:<10} {','.join(data['languages_detected']):<15}")
        else:
            print(f"{name:<15} {'FAIL':<10} {data.get('duration_ms', 'N/A'):<10}")

    # Save JSON report
    report_file = audio_path.parent / f"stt_comparison_{audio_path.stem}.json"
    report_file.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nJSON report: {report_file}")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare STT providers")
    parser.add_argument("--audio", required=True, type=Path, help="Path to audio file")
    parser.add_argument("--providers", required=True, help="Comma-separated provider list")
    parser.add_argument("--dry-run", action="store_true", help="Skip actual transcription")
    args = parser.parse_args()

    provider_list = [p.strip() for p in args.providers.split(",")]
    asyncio.run(compare(args.audio, provider_list, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
```

### 2. Підготувати тестове аудіо

**Джерело:** одна з лекцій з PythonStart-2022 курсу (вже є в системі):
- Node 01 "Introduction to Python" — має відео матеріал
- Або завантажити 10-15 хв фрагмент з YouTube

**Вимоги до тестового аудіо:**
- 10-15 хвилин (достатньо для оцінки якості, не дорого)
- Українськомовна лекція
- Технічна термінологія (Python, Django, ORM, etc.)
- Бажано з суржиком (uk↔ru mixing) — типово для технічних лекцій
- WAV 16kHz mono (пропустити через ffmpeg якщо потрібно)

### 3. Запустити тест

```bash
# Конвертувати в WAV якщо потрібно:
ffmpeg -i lecture.mp3 -ar 16000 -ac 1 lecture.wav

# Запустити порівняння:
uv run python scripts/stt_compare.py \
    --audio lecture.wav \
    --providers deepgram,soniox,elevenlabs
```

### 4. Оцінити результати

**Evaluation form** (заповнити для кожного provider):

| Критерій | Deepgram | Soniox | ElevenLabs |
|---|---|---|---|
| Загальна точність тексту (1-10) | | | |
| Обробка суржику uk↔ru (1-10) | | | |
| Розпізнавання технічних термінів (1-10) | | | |
| Якість пунктуації (1-10) | | | |
| Таймкоди — точність (spot check 5) (1-10) | | | |
| Speaker diarization (1-10) | | | |
| **Загальна оцінка** | | | |
| Час обробки (ms) | | | |
| Вартість ($) | | | |

### 5. Документувати

Створити `docs/stt-evaluation.md`:
```markdown
# STT Provider Evaluation — Ukrainian Technical Lectures

## Test Setup
- Audio: [description], duration, language
- Date: YYYY-MM-DD
- Providers: Deepgram Nova-3, Soniox v3, ElevenLabs Scribe v2

## Results
[Evaluation table from above]

## Analysis
[Winner rationale, strengths/weaknesses of each]

## Selected Chain
STT_PROVIDER_CHAIN=[winner],[runner-up],[third]

## Reasoning
[Why this order]
```

### 6. Оновити конфігурацію

- `.env.prod`: встановити `STT_PROVIDER_CHAIN` з обраним порядком
- `CLAUDE.md`: додати секцію про STT configuration

---

## Очікуваний результат

- Скрипт `scripts/stt_compare.py` працює, проходить `make check`
- Три транскрипти збережені як .txt файли
- JSON report з метриками
- `docs/stt-evaluation.md` з evaluation table
- `.env.prod` з обраною chain
- `CLAUDE.md` оновлений

---

## Тестування

### Автоматизовані тести

Файл: `tests/unit/test_stt_compare.py`

- `stt_compare.py` з `--dry-run`: запускається без помилок
- Output JSON має expected keys (status, duration_ms, ...)
- `--providers unknown` → ValueError

### Ручний контроль (Human testing)

**ГОЛОВНИЙ ТЕСТ СПРІНТУ:**

1. Прочитати всі три транскрипти **повністю** (10-15 хв тексту кожен)
2. Для кожного оцінити 6 критеріїв (1-10 scale)
3. Spot check 5 таймкодів: порівняти з реальним часом у відео
4. Визначити winner і runner-up
5. Зафіксувати `STT_PROVIDER_CHAIN` в `.env.prod`
6. Заповнити `docs/stt-evaluation.md`

---

## Сумісність з існуючим кодом

- Скрипт використовує production `STTProvider` classes і `Settings` — це validation що все працює разом
- `.env.prod` оновлений → deploy підхопить нові STT змінні
- `docs/stt-evaluation.md` — нова документація, не конфліктує

---

## Checklist перед PR

- [ ] `scripts/stt_compare.py` працює і проходить `make check`
- [ ] Тест проведений на реальному українському аудіо
- [ ] Три транскрипти збережені і оцінені
- [ ] `docs/stt-evaluation.md` заповнений
- [ ] `.env.prod` з обраною `STT_PROVIDER_CHAIN`
- [ ] `CLAUDE.md` оновлений з STT секцією

---

## Нотатки

_Простір для нотаток виконавця:_
- [ ] Яке аудіо використовували?
- [ ] Winner?
- [ ] Key findings?
