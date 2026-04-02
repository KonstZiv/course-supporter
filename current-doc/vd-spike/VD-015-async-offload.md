# VD-015: Async offload — виведення blocking I/O з event loop

**Фаза:** 6 — Polish
**Пріоритет:** Medium (стає High при паралельній обробці кількох відео)
**Залежності:** VD-005 (pipeline зібраний і працює)
**Блокує:** Production scale-out

## Контекст

`FrameSampler.sample()` — async метод, який викликається з `VideoProcessor`
всередині ARQ worker. Зараз він містить blocking операції, які виконуються
безпосередньо в event loop:

- **CPU-bound:** `imagehash.dhash()`, `cv2.absdiff/cvtColor`, `np.array/mean`
- **I/O-bound:** `Image.open()`, `cv2.imread()`, `Path.glob()`, `Path.exists()`

Для 10-хвилинного spike відео (17 кадрів) це непомітно. Для production
(2-годинне відео, fps=0.5) масштаб інший:

| Операція | 10 хв (~17 frames) | 2 год (~3600 raw → ~200 deduped) |
|----------|--------------------|---------------------------------|
| FFmpeg extraction | ~5s | ~2 min |
| `_compute_hashes` (Image.open + dhash) | ~0.5s | ~30s CPU-bound |
| `_detect_pip` (cv2.imread × 30 pairs) | ~0.3s | ~0.5s |
| `_fill_gaps` (ffmpeg singles) | ~1s | ~20s (sequential!) |
| `_segment_scenes` | instant | ~0.1s |

**При паралельній обробці кількох відео** (кілька ARQ workers або
`asyncio.gather` у `VideoProcessor`) — 30-секундний `_compute_hashes`
блокує event loop і не дає іншим coroutines (STT, API) працювати.

## Що робимо

### Крок 1: Offload CPU-bound блоків через `run_in_executor`

Перенести **цілі методи** (не окремі рядки) в thread pool:

```python
async def sample(self, video_path, output_dir):
    ...
    # CPU-bound: hashing 200+ images
    loop = asyncio.get_running_loop()
    raw_entries = await loop.run_in_executor(
        None, self._compute_hashes, raw_paths, interval, p.hash_size, mask_rect
    )
    deduped = self._dedup_with_cooldown(raw_entries, p)  # pure CPU, fast

    # CPU-bound: PiP detection with cv2
    pip_mask = await loop.run_in_executor(
        None, _detect_pip, raw_paths, width, height
    )
    ...
```

**НЕ** робити `to_thread` на кожен `Path.exists()` або `Path.mkdir()` — це
overhead без користі (наносекунди).

### Крок 2: Паралельний gap fill

`_fill_gaps` зараз витягує кадри послідовно. Для 2-годинного відео з
великими gaps це може бути 20+ кадрів × ~1s кожен = 20s.

```python
# Замість послідовного:
for ts in fill_timestamps:
    await _ffmpeg_extract_single(video, ts, path)

# Паралельно (з semaphore для обмеження):
sem = asyncio.Semaphore(4)
async def _extract_one(ts, path):
    async with sem:
        return await _ffmpeg_extract_single(video, ts, path)

results = await asyncio.gather(*[
    _extract_one(ts, path) for ts, path in fill_tasks
])
```

### Крок 3: ProcessPoolExecutor для CPU-heavy hashing (опціонально)

Якщо profiling покаже що `_compute_hashes` — bottleneck навіть у thread pool
(GIL не звільняється в imagehash C-extensions), замінити на ProcessPool:

```python
from concurrent.futures import ProcessPoolExecutor

_hash_pool = ProcessPoolExecutor(max_workers=4)

raw_entries = await loop.run_in_executor(
    _hash_pool, self._compute_hashes, ...
)
```

## Що НЕ робимо

- `asyncio.to_thread(Path.mkdir, ...)` — overhead без виграшу
- `asyncio.to_thread(output.exists)` — атомарна FS операція, наносекунди
- Async-native image libraries (aiofiles, etc.) — зайва складність

## Acceptance criteria

- [ ] `_compute_hashes` і `_detect_pip` виконуються в executor
- [ ] `_fill_gaps` використовує `asyncio.gather` з semaphore для паралельного FFmpeg
- [ ] 2-годинне відео: event loop не блокується >100ms за раз
- [ ] Benchmark: обробка 2-год відео до і після (wall time, event loop latency)
- [ ] Існуючі тести проходять без змін
