# Testing

!!! info "Coming in Sprint 2, Epic 7"
    Detailed testing strategy and patterns will be published here.

**Current setup:**

- **Framework:** pytest with `pytest-asyncio` (`asyncio_mode = "auto"`)
- **Test count:** 407 (Sprint 0: 326, Sprint 1: +81)
- **Coverage:** `pytest --cov --cov-report=term-missing`

**Key patterns:**

- `httpx.AsyncClient` + `ASGITransport(app=app)` for API tests
- Patch `PROCESSOR_MAP` dict directly for ingestion tests
- `MagicMock(return_value=ctx)` for `async_sessionmaker` mock
- Fixtures over classes (`pytest` style)

**Commands:**

```bash
uv run pytest                           # all tests
uv run pytest tests/unit/test_config.py # single file
uv run pytest -k "test_name"            # by name
make check                              # full: ruff + mypy + pytest
```
