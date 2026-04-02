# VD-009: ChunkType Extension + Alembic Migration

**Фаза:** 5 — Integration
**Пріоритет:** Середній
**Залежності:** VD-001

## Що робимо

Розширити ChunkType enum новими типами для VD pipeline. Створити Alembic migration.

## Яким чином

### Нові ChunkType значення

```python
VISUAL_SCENE = "visual_scene"          # повний scene analysis
VISUAL_CODE = "visual_code"            # code block з екрану
VISUAL_SLIDE = "visual_slide"          # текст слайду
VISUAL_TERMINAL = "visual_terminal"    # terminal output
ALIGNED_SEGMENT = "aligned_segment"    # merged STT + VD segment
```

### Alembic migration
`make migrate msg="add_vd_chunk_types"`

## Acceptance criteria

- [ ] ChunkType enum розширений
- [ ] Alembic migration створена і проходить upgrade/downgrade
- [ ] Існуючі ChunkType значення не змінились
