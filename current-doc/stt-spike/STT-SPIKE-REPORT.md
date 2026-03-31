# STT Quality Spike Report

**Date:** 2026-03-30
**Goal:** Evaluate STT providers for Ukrainian-language course video transcription
**Context:** Current pipeline uses Gemini Vision for video analysis, which produces poor Ukrainian transcription (surzhyk). STT chain should replace it.

## Test Conditions

| Parameter | Value |
|---|---|
| **Source video** | [2.01 Python Quick Start: Datatype — what it is?](https://www.youtube.com/watch?v=bRYsA9Yyvy4) |
| **Audio duration** | 10 min 35 sec (635 seconds) |
| **Audio format** | MP3, 16 kHz, mono, 64 kbps (~5 MB) |
| **Audio file** | [sample.mp3](sample.mp3) |
| **Language** | Ukrainian (spoken by native speaker, informal lecture style) |
| **Content type** | Python programming course, includes technical terms (STR, INT, FLOAT, type, traceback, etc.) |
| **Environment** | macOS, tested via Python scripts using provider HTTP APIs |

## Providers Tested

### 1. Deepgram Nova-3

- **API:** `POST https://api.deepgram.com/v1/listen`
- **Model:** `nova-3`
- **Parameters:** `language=uk, punctuate=true, paragraphs=true, smart_format=true`

### 2. ElevenLabs Scribe

- **API:** `POST https://api.elevenlabs.io/v1/speech-to-text`
- **Model:** `scribe_v1`
- **Parameters:** `language_code=ukr`

### 3. OpenAI Whisper

- **API:** `POST https://api.openai.com/v1/audio/transcriptions`
- **Model:** `whisper-1`
- **Parameters:** `language=uk, response_format=verbose_json`

### 4. OpenAI GPT-4o Mini Transcribe

- **API:** `POST https://api.openai.com/v1/audio/transcriptions`
- **Model:** `gpt-4o-mini-transcribe`
- **Parameters:** `language=uk, response_format=json`

### 5. Sonix

- **API:** `POST https://api.sonix.ai/v1/media` (async — submit, poll, retrieve)
- **Model:** Default (not selectable)
- **Parameters:** `language=uk`
- **Note:** Asynchronous API — submit file, poll `GET /v1/media/<id>` until `status=completed`, then `GET /v1/media/<id>/transcript`

## Results Summary

| Metric | Deepgram Nova-3 | ElevenLabs Scribe | OpenAI Whisper | GPT-4o Mini Transcribe | Sonix |
|---|---|---|---|---|---|
| **Latency** | **5.4s** | 26.0s | 41.7s | 18.9s | 65.0s |
| **Speed ratio** | **117x realtime** | 24x realtime | 15x realtime | 34x realtime | 9.8x realtime |
| **Confidence** | 0.969 | N/A | N/A | N/A | 94.16 (quality score) |
| **Segments returned** | N/A | N/A | 167 | N/A | 6 (speaker turns) |
| **Extra features** | — | Pause markers | Segment timestamps | — | Speaker diarization, word-level timestamps |

## Pricing (as of March 2026)

| Provider | Price per minute | Price per hour | 100 hours cost |
|---|---|---|---|
| **OpenAI GPT-4o Mini Transcribe** | $0.003 | $0.18 | **$18.00** |
| **ElevenLabs Scribe** | $0.0037 | $0.22 | **$22.00** |
| **Deepgram Nova-3** (multilingual pre-recorded) | $0.0052 | $0.312 | **$31.20** |
| **OpenAI Whisper** | $0.006 | $0.36 | **$36.00** |
| **Sonix** (Premium plan) | $0.083 | $5.00 | **$500.00** + $22/mo seat |
| **Sonix** (Standard pay-as-you-go) | $0.167 | $10.00 | **$1,000.00** |

First four providers are very affordable for our volumes. Sonix is **25–55x more expensive** than the cheapest option — a different price category entirely.

## Quality Comparison

### Example 1: Opening phrase

| Provider | Transcription | Verdict |
|---|---|---|
| **Original** | *"Привіт! Я вас вітаю."* | — |
| Deepgram | "Привіт! **Його** вітаю!" | **Wrong** — "Його" instead of "Я вас" |
| ElevenLabs | "Привіт! Я вас вітаю." | Correct |
| Whisper | "Привіт! Я вас вітаю." | Correct |
| GPT-4o Mini | "Привіт! Я вас вітаю!" | Correct |
| Sonix | "Привіт! Я вас вітаю!" | Correct |

### Example 2: Technical term "Python"

| Provider | Transcription | Verdict |
|---|---|---|
| **Original** | *"...вбудованими типами даних в Пайтоні"* | — |
| Deepgram | "...типами даних в **питанню**" | **Wrong** — completely garbled |
| ElevenLabs | "...типами даних в Пайтоні" | Correct |
| Whisper | "...типами даних в Пайтоні" | Correct |
| GPT-4o Mini | "...типами даних в **Python**" | Uses English spelling (acceptable) |
| Sonix | "...типами даних в **Пайтон**" | Correct (nominative case, minor) |

### Example 3: Technical terms STR/INT/FLOAT

| Provider | Transcription | Verdict |
|---|---|---|
| **Original** | *"десь писало STR... десь INT... десь FLOAT"* | — |
| Deepgram | "десь писали STR... десь INT... десь **флот**" | Mixed — STR/INT correct, FLOAT wrong |
| ElevenLabs | "десь писало **\"стр\"**... десь **\"інт\"**... десь **\"флот\"**" | Transliterated to Cyrillic (loss of original form) |
| Whisper | "Десь писало STR... десь INT... десь FLOAT" | **Best** — preserves Latin originals |
| GPT-4o Mini | "Десь писало STR... десь INT... десь FLOAT" | **Best** — preserves Latin originals (same as Whisper) |
| Sonix | "десь писали **стр**... десь **інде**... **Флот**" | **Worst** — transliterated + "INT" garbled to "інде" (Ukrainian word "elsewhere") |

### Example 4: Complex phrase "з тлумачного словника"

| Provider | Transcription | Verdict |
|---|---|---|
| **Original** | *"що таке з тлумачного словника, тип"* | — |
| Deepgram | "що таке **тлумаче словоникада**, тип" | **Wrong** — garbled |
| ElevenLabs | "що таке з тлумачного словника, да, тип" | Correct |
| Whisper | "що таке **сломачена слоникада**, тип" | **Wrong** — garbled worse |
| GPT-4o Mini | "що таке **словничний словника**, тип" | **Wrong** — garbled but less severely |
| Sonix | "що таке з тлумачного словника. Тип." | **Correct** (only Sonix + ElevenLabs got this right) |

### Example 5: Technical terms — type hierarchy

| Provider | Transcription | Verdict |
|---|---|---|
| **Original** | *"є int, float, є bool... decimal... fraction"* | — |
| Deepgram | "є Intflow, є Bull... D-cimal... Fractal" | **Wrong** — merged/garbled |
| ElevenLabs | "є int, float, є bool... decimal... fractal" | Correct (minor: fractal instead of fraction) |
| Whisper | "є інт, фло, є булт... децимал... фрактал" | All transliterated to Cyrillic |
| GPT-4o Mini | "є int, float, є bool... decimal... fraction" | **Best** — all terms correct in Latin |
| Sonix | "для її **флот** був... де символ... **фрактал**" | **Wrong** — garbled ("її" for int, "був" for bool), transliterated rest |

### Example 6: Hesitations and pauses

| Provider | Transcription | Verdict |
|---|---|---|
| Deepgram | Omits all pauses and hesitations | Clean but loses speech rhythm |
| ElevenLabs | "(чотирисекундна пауза)", "а-а", "е-е", "ааа" | **Best** — preserves natural speech flow |
| Whisper | Omits pauses, uses "…" occasionally | Middle ground |
| GPT-4o Mini | Omits pauses, uses "..." occasionally | Middle ground (similar to Whisper) |
| Sonix | Omits pauses and hesitations | Clean, similar to Deepgram |

### Example 7: Duck typing concept

| Provider | Transcription | Verdict |
|---|---|---|
| **Original** | *"качину типізацію"* | — |
| Deepgram | "качину типізацію" | Correct |
| ElevenLabs | "качину типізацію" | Correct |
| Whisper | "**качівну** типізацію" | **Wrong** — misspelled |
| GPT-4o Mini | "**як чінити** типізацію" | **Wrong** — misheard completely |
| Sonix | "**качина типізація**" | Correct concept (nominative case instead of accusative — minor grammar) |

## Detailed Assessment

### Deepgram Nova-3

**Strengths:**
- Extremely fast (5.4s for 10.5 min audio — 117x realtime)
- Good confidence score (0.969)
- Handles some technical terms well (STR, INT)

**Weaknesses:**
- Worst Ukrainian quality of the four
- Garbles complex Ukrainian words ("тлумаче словоникада", "питанню")
- Misses pronouns ("Його" instead of "Я вас")
- Loses context on mixed Ukrainian/English content

**Verdict:** Fast and cheap, but not suitable as primary STT for Ukrainian educational content. Usable as a fast fallback for simple content.

### ElevenLabs Scribe

**Strengths:**
- Best overall Ukrainian language quality
- Preserves speech structure: pauses, hesitations, self-corrections
- Most accurate for complex Ukrainian phrases
- Correctly handles technical terms in context
- Natural punctuation with "--" for interrupted thoughts

**Weaknesses:**
- Moderate speed (26s, but still 24x realtime)
- Transliterates code identifiers to Cyrillic ("стр", "інт", "флот" instead of STR, INT, FLOAT)
- Uses `scribe_v1` (v2 is realtime-focused, different API)

**Verdict:** Best quality for our use case. The transliteration issue can be handled in post-processing (regex replacement of known Cyrillic forms to Latin originals).

### OpenAI Whisper

**Strengths:**
- Good at preserving Latin technical terms (STR, INT, FLOAT, traceback, frame)
- Good general Ukrainian quality
- Returns detailed segment-level timestamps (167 segments)
- Well-known, stable API

**Weaknesses:**
- Slowest (41.7s)
- Garbles some Ukrainian phrases ("сломачена слоникада")
- Misspells some terms ("качівну" instead of "качину")
- Most expensive per minute

**Verdict:** Superseded by GPT-4o Mini Transcribe which is faster, cheaper, and better quality.

### OpenAI GPT-4o Mini Transcribe

**Strengths:**
- Best at preserving Latin technical terms — all correct (STR, INT, FLOAT, int, float, bool, decimal, fraction, number, type, id, traceback, frame, bytes, ByteArea)
- Good Ukrainian quality overall
- Fast (18.9s — 34x realtime)
- Cheapest option ($0.003/min)
- Uses same OpenAI API key as Whisper — no additional registration

**Weaknesses:**
- Garbles some Ukrainian phrases ("словничний словника" instead of "тлумачного словника")
- Misheard "качину типізацію" as "як чінити типізацію"
- Does not preserve pauses or hesitations
- Does not support `verbose_json` response format (no segment timestamps)
- Writes "Python" instead of "Пайтон" (may be desirable or not depending on context)

**Verdict:** Excellent complement to ElevenLabs. Best technical term preservation + cheapest price. Strong candidate for primary or secondary provider.

### Sonix

**Strengths:**
- Excellent Ukrainian language quality — correctly transcribes complex phrases ("з тлумачного словника", "качина типізація")
- On par with ElevenLabs for general Ukrainian (best two providers for Ukrainian prose)
- Speaker diarization out of the box (detected "Speaker 1" correctly)
- Word-level timestamps with precise timing in JSON format
- Self-reported quality score (94.16)

**Weaknesses:**
- **Extremely expensive** — $5–10/hour vs $0.18–0.36/hour for others (25–55x more)
- Slowest provider (65s, 9.8x realtime) — async API adds overhead
- Worst technical term handling — "INT" misheard as "інде" (Ukrainian word), garbles type hierarchy ("її" for int, "був" for bool)
- Transliterates all code identifiers to Cyrillic (same as ElevenLabs but worse accuracy)
- Async API (submit → poll → retrieve) — more complex integration
- Watermark on trial account transcripts
- "bytes" → "баштовий", "ByteArray" → "бейт Ілія", "traceback" → "1с бек" — badly garbled technical terms

**Verdict:** Good Ukrainian prose quality (comparable to ElevenLabs), but the worst technical term recognition of all five providers, combined with pricing 25–55x higher than alternatives. **Not suitable for our use case** — educational programming content requires strong technical term handling, and the cost is prohibitive.

## Recommendation

### Primary: ElevenLabs Scribe
Best overall quality for Ukrainian educational content. Transliteration of code terms to Cyrillic is a solvable post-processing issue.

### Secondary: GPT-4o Mini Transcribe
Best technical term preservation (Latin), cheapest ($0.003/min), good speed (34x realtime). Excellent complement to ElevenLabs — strong where ElevenLabs is weak.

### Fallback: Deepgram Nova-3
Fastest option (117x realtime). Lower quality for Ukrainian, but adequate for simple content or when speed matters.

### Deprecated: OpenAI Whisper
Superseded by GPT-4o Mini Transcribe in every metric (faster, cheaper, better quality). No reason to use Whisper-1.

### Not recommended: Sonix
Good Ukrainian prose quality (on par with ElevenLabs), but worst technical term recognition and 25–55x higher pricing. Async API adds integration complexity. Not viable for programming course content.

### Optional (not yet tested): Soniox
Planned for future evaluation. API key pending.

## Next Steps

1. ~~**Implement STT foundation** (abstract interface, models, settings) — STT-001 + STT-002~~ DONE
2. ~~**Implement ElevenLabs + GPT-4o Mini + Deepgram providers** — STT-003 + STT-004 + STT-005~~ DONE
3. **Add post-processing** for Cyrillic→Latin term normalization
4. **Integrate into VideoProcessor** — replace Gemini Vision transcription path
5. ~~**Test Sonix** when API key is available~~ DONE — not recommended (see above)
6. **Test Soniox** when API key is available
