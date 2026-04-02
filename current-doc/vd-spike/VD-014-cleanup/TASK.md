# VD-014: Temp Files Cleanup

**Фаза:** 6 — Polish
**Пріоритет:** Низький
**Залежності:** VD-005

## Що робимо

Verify guaranteed cleanup тимчасових файлів (extracted frames, audio segments).

## Яким чином

- shutil.rmtree в finally block працює в усіх сценаріях: success, error, KeyboardInterrupt
- ARQ task cleanup on worker restart
- Warning logs якщо cleanup fails
- Перевірити що temp dir не росте при повторних запусках

## Acceptance criteria

- [ ] Temp dir видаляється після success
- [ ] Temp dir видаляється після exception
- [ ] Warning log якщо rmtree fails
- [ ] Немає orphan temp dirs після stress test (5 consecutive runs)
