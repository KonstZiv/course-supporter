# S1-015: TextProcessor (MD / DOCX / HTML)

## Мета

Обробка текстових документів (Markdown, DOCX, HTML, plain text) без LLM: парсинг заголовків та параграфів у `ContentChunk`.

## Що робимо

1. **TextProcessor** — визначення формату за розширенням (.md / .docx / .html / .htm / .txt)
2. **Markdown/TXT** — split by headings → HEADING + PARAGRAPH chunks
3. **DOCX** — `docx.Document()` → iterate paragraphs, heading styles → chunks
4. **HTML** — `BeautifulSoup` → headings + text → chunks
5. **Chunking strategy** — headings з level у metadata, text між → PARAGRAPH
6. **Unit-тести** — ~9 тестів

## Контрольні точки

- [ ] Markdown headings → `HEADING` chunks з `level`
- [ ] Text між headings → `PARAGRAPH` chunks
- [ ] DOCX heading styles → правильний level
- [ ] HTML `<h1>`..`<h6>` → headings
- [ ] Plain text → один PARAGRAPH chunk
- [ ] `.rtf` → `UnsupportedFormatError`
- [ ] Empty file → empty chunks
- [ ] `make check` проходить

## Залежності

- **Блокується:** S1-011
- **Блокує:** немає
