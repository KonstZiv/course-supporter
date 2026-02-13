# S1-016: WebProcessor (URL → trafilatura)

## Мета

Обробка веб-сторінок: fetch HTML через `trafilatura`, extract контент, зберегти snapshot для re-processing.

## Що робимо

1. **WebProcessor** — `trafilatura.fetch_url()` + `trafilatura.extract()` (без LLM)
2. **Content snapshot** — raw HTML зберігається для пізнішої переобробки
3. **Domain extraction** — URL domain у metadata
4. **Chunking** — extracted text → `WEB_CONTENT` chunks
5. **Unit-тести** — ~7 тестів

## Контрольні точки

- [ ] Успішний fetch → `SourceDocument` з `WEB_CONTENT` chunks
- [ ] Fetch failure → `ProcessingError`
- [ ] Extract returns None → empty chunks
- [ ] Domain в metadata
- [ ] Non-web source_type → `UnsupportedFormatError`
- [ ] Raw HTML збережено в content_snapshot
- [ ] `make check` проходить

## Залежності

- **Блокується:** S1-011
- **Блокує:** немає
