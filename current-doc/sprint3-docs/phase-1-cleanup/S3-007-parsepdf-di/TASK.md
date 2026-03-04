# S3-007: ParsePDFFunc DI Extraction

**Phase:** 1 (Cleanup)
**Складність:** S
**Статус:** PENDING

## Контекст

`ParsePDFFunc` protocol визначений в `heavy_steps.py:140` (Sprint 2, S2-031), але реалізація НЕ витягнута з `PresentationProcessor._extract_text_from_pdf()` (line 99). Це єдиний heavy step без DI extraction — всі інші (transcribe, describe_slides, scrape_web) вже мають окремі `local_*()` функції та factory wiring.

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/ingestion/parse_pdf.py` | НОВИЙ — `local_parse_pdf()` function |
| `src/course_supporter/ingestion/heavy_steps.py` | Перевірити ParsePDFFunc protocol |
| `src/course_supporter/ingestion/presentation.py` | Замінити inline fitz usage на DI callable |
| `src/course_supporter/ingestion/factory.py` | Додати ParsePDFFunc до HeavySteps, wire через factory |
| `tests/unit/test_ingestion/test_parse_pdf.py` | НОВИЙ — тести для local_parse_pdf() |

## Деталі реалізації

### 1. Extract local_parse_pdf() (parse_pdf.py)

Витягти логіку з `PresentationProcessor._extract_text_from_pdf()`:
```python
async def local_parse_pdf(file_path: str) -> str:
    """Extract text from PDF using PyMuPDF (fitz)."""
    import fitz  # lazy import (heavy dep)

    doc = fitz.open(file_path)
    pages = []
    for page in doc:
        pages.append(page.get_text())
    doc.close()
    return "\n\n".join(pages)
```

**Важливо:** fitz не thread-safe → render має бути sequential (pattern з describe_slides).

### 2. Update PresentationProcessor

```python
class PresentationProcessor:
    def __init__(self, *, parse_pdf: ParsePDFFunc | None = None, ...):
        self._parse_pdf = parse_pdf or self._default_parse_pdf_func()

    @staticmethod
    def _default_parse_pdf_func() -> ParsePDFFunc:
        from course_supporter.ingestion.parse_pdf import local_parse_pdf
        return local_parse_pdf
```

### 3. Factory wiring (factory.py)

Додати `parse_pdf` до `HeavySteps` dataclass та `create_processors()`.

### 4. Tests

Тести аналогічні іншим heavy steps:
- Happy path (mock fitz)
- Empty PDF
- Error handling (corrupt file)
- Lazy import patching: `patch.dict("sys.modules", {"fitz": mock})`

## Acceptance Criteria

- [ ] `local_parse_pdf()` існує як окрема функція
- [ ] `PresentationProcessor` використовує DI через `__init__`
- [ ] Factory включає `ParsePDFFunc`
- [ ] Тести покривають happy path + edge cases
- [ ] `_default_parse_pdf_func()` lazy-import pattern
