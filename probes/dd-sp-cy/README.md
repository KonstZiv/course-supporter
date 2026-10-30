# DD-SP-CY — `left()`/`substring()` на TOAST-тексті UTF-8 (PostgreSQL 17.x)

Разова перевірка; не зливається в `main`.

## Файли

| Файл | Що робить |
|---|---|
| `probe.py` | Створює базу UTF8, 6 таблиць (`EXTENDED`/`EXTERNAL`/`MAIN` × `pglz`/`lz4`), 40 синтетичних значень у кожній (4 типи × 5 довжин × 2 стискуваності) і виконує `left`, `substring … from 1 for n`, `substr`, `right` для n = 1, 10, 100, 400, 1000 та контроль `left(c \|\| '', 400)`. Кожен запит окремо; результат звіряється з очікуваним md5 із Python. |
| `summarize.py` | Зводить CSV у `results/summary.md` і перевіряє механізм: помилка ⇔ значення в TOAST і межа шматка `(n+1)×4` байт розрізає символ. |
| `minimal_repro.sql` | Найменший приклад (один рядок, `EXTERNAL`, `'x' \|\| repeat('а', 2000)`). |
| `run_all.sh` | Піднімає контейнер для кожного образу, проганяє `probe.py` і `minimal_repro.sql`, будує зведення. |
| `results/<версія>.csv` | Сирі результати: одна перевірка — один рядок. |
| `results/minimal_repro_<версія>.out` | Вивід `psql` для мінімального прикладу. |
| `results/summary.md` | Зведені таблиці. |
| `results/upstream_evidence.txt` | Витяги з release notes 17.8/17.9 і diff `mbutils.c`/`varlena.c` (REL_17_7 → REL_17_8 → REL_17_9). |
| `results/pytest_run-db_17.11.log` | Прогін тестів репозиторію з `--run-db` на 17.11. |

## Повторити

```bash
uv sync
bash probes/dd-sp-cy/run_all.sh     # потрібен запущений Docker
```

Типи даних: `uk_prose` — українська проза; `uk_code` — проза з рядками Python;
`ascii` — контроль; `mb34` — ASCII-слова з `—`, `…` (3 байти) і `🚀` (4 байти).
Стискуваність: `high` — повтор одного абзацу; `low` — псевдослова з випадкових
літер (seed фіксований, дані детерміновані).
