# Sprint 2 — Material Tree, Task Queue, Structure Generation

**Status:** In Progress
**Estimate:** 4–5 weeks
**Previous sprint:** [Sprint 1 — Production Deploy](../sprint-1/index.md) (407 tests, live on `api.pythoncourse.me`)

---

## Goal

Intuitive course material flow + production-ready processing with a task queue + per-node structure generation.

## Motivation

Sprint 1 delivered a working MVP, but:

1. **No intuitive flow** — flat list of materials, no hierarchy, no explicit generation trigger
2. **Fire-and-forget processing** — `BackgroundTasks` without queue, no concurrency control, tasks lost on restart
3. **Heavy ops not isolated** — whisper/vision baked into processors, impossible to extract to serverless
4. **No version control** — unclear if course structure matches the current set of materials
5. **External team waiting** — needs documentation of flow + endpoints to start integration

## Architecture Decisions

### AR-1: MaterialTree (recursive adjacency list)

Arbitrary hierarchy of nodes. Materials can belong to any node (not just leaves):

```
Course "Python for Beginners"
  ├── syllabus.pdf                    ← material at course level
  ├── "Intro to Python"
  │     ├── intro-video.mp4           ← material at section level
  │     ├── "Data Types"
  │     │     ├── types-slides.pdf
  │     │     └── types-article.html
  │     └── "Loops"
  │           └── loops-video.mp4
  └── "Web Development"
        └── django-overview.pdf
```

ORM: `MaterialNode(id, course_id, parent_id → self, title, description, order)`.

Fixed levels (Course → Module → Topic) don't cover arbitrary learning constructs. Adjacency list is the simplest implementation for async SQLAlchemy. PostgreSQL `WITH RECURSIVE` available for complex queries, but eager loading sufficient for typical depths of 3–5 levels.

### AR-2: MaterialEntry (replaces SourceMaterial)

Instead of a single `SourceMaterial` mixing raw, processed, and status — separation into clear layers with a "receipt" for processing submission:

- **Raw layer** — `source_url`, `filename`, `raw_hash` (lazy cached sha256), `raw_size_bytes`
- **Processed layer** — `processed_content` (SourceDocument JSON), `processed_hash`, `processed_at`
- **Pending receipt** — `pending_job_id` FK → jobs, `pending_since`
- **State** — derived property: `RAW` → `PENDING` → `READY` / `INTEGRITY_BROKEN` / `ERROR`

The pending receipt enables diagnostics: `pending_since = 40 min ago` → suspicious → `GET /jobs/{pending_job_id}` → see the problem.

### AR-3: Task Queue (ARQ + Redis)

Replace `BackgroundTasks` with ARQ:

- Redis for persistence (jobs survive restarts)
- `max_jobs` for concurrency control (whisper is CPU/RAM heavy)
- Retry with backoff for transient errors
- Job status tracking (queued → active → complete/failed)
- Job dependencies (`depends_on` — structure generation waits for ingestion)
- **Work window** — heavy jobs (whisper, vision) only during configured time window; light jobs (fingerprint, LLM calls) always
- **Queue estimates** — position, avg duration, window-aware estimated start/complete

Infrastructure: +1 Redis container (~50 MB RAM), +1 worker process (~100–200 MB).

### AR-4: Merkle Fingerprints

Two-level fingerprint system with bottom-up cascade invalidation:

- **Material fingerprint** (`content_fingerprint`): `sha256(processed_content)`. Lazy cached in `MaterialEntry`.
- **Node fingerprint** (`node_fingerprint`): hash of child material fingerprints + child node fingerprints. Lazy cached in `MaterialNode`.
- **Course fingerprint**: hash of root node fingerprints.

Any modification → invalidates fingerprints from the change point up to the root. `fingerprint: null` at any level → something changed below. Drill-down to the specific material.

See [ERD](../../architecture/erd.md) for the full schema.

### AR-5: Heavy Steps Extraction (serverless-ready)

Separation into heavy (serverless-ready) and light (on-premise) operations:

| Heavy (serverless-ready) | Light (on-premise) |
|---|---|
| Whisper transcription | Merge documents |
| Slide/image → description (vision) | Architect agent (LLM call) |
| PDF OCR | Fingerprint calculation |
| Video frame extraction | CRUD, status management |

Each heavy step is an injectable callable with a clean contract. `SourceProcessor` becomes an orchestrator: prepare input → call heavy step → package result. When Lambda arrives — only change the heavy step implementation.

### AR-6: Structure Generation — per-node, cascading

Generation can be triggered for any level of the tree. Cascades through the entire subtree from the target node down.

**Two modes:**

- **"free"** — methodologist builds optimal structure freely. Input tree is context only.
- **"guided"** — methodologist preserves input tree as constraint, enriches it.

**Cascade logic:** find stale materials → enqueue ingestion jobs → enqueue structure generation with `depends_on` → return 202 Accepted with plan + estimates.

**Conflict detection:** 409 Conflict only when a new generation overlaps with an active job's subtree scope.

**Idempotency:** same `(node_id, fingerprint, mode)` → 200 OK with existing snapshot.

**Apply snapshot → normalized tables:** When a snapshot is "applied", its `structure` JSONB is unpacked into `modules` → `lessons` → `concepts` → `exercises`. `Module.snapshot_id` FK explicitly links the active structure to the source snapshot.

### AR-7: SlideVideoMapping — explicit references + deferred validation

Mapping links a **specific presentation** to a **specific video** via FK to `MaterialEntry`. Three-level validation:

1. **Structural (always)** — both materials exist and belong to the node, correct `source_type`, valid timecode format
2. **Content (when READY)** — slide number within presentation range, timecode within video duration
3. **Deferred (when not READY)** — mapping created with `pending_validation` + `blocking_factors` JSONB; auto-revalidated when blocking material completes ingestion

Batch upload supports partial success — per-item results with errors, hints, and resubmit guidance.

## Target API

### Material Tree Management

```
POST   /api/v1/courses                                     → create course
GET    /api/v1/courses                                     → list courses (pagination)
GET    /api/v1/courses/{course_id}                         → course + tree + statuses + fingerprints
DELETE /api/v1/courses/{course_id}                         → delete course (cascade)

POST   /api/v1/courses/{id}/nodes                          → create root node
POST   /api/v1/courses/{id}/nodes/{node_id}/children       → create child node
PATCH  /api/v1/courses/{id}/nodes/{node_id}                → update (title, description, order, parent_id)
DELETE /api/v1/courses/{id}/nodes/{node_id}                → delete (cascade children + materials)

POST   /api/v1/courses/{id}/nodes/{node_id}/materials      → add material (file or URL)
DELETE /api/v1/courses/{id}/materials/{material_id}         → delete material
POST   /api/v1/courses/{id}/materials/{material_id}/retry   → retry ingestion
```

### Slide-Video Mapping

```
POST   /api/v1/courses/{id}/nodes/{node_id}/slide-mapping  → batch create (partial success)
GET    /api/v1/courses/{id}/nodes/{node_id}/slide-mapping   → list mappings for node
DELETE /api/v1/courses/{id}/slide-mapping/{mapping_id}      → delete mapping
```

### Structure Generation

```
POST   /api/v1/courses/{id}/structure/generate             → trigger for entire course
POST   /api/v1/courses/{id}/nodes/{node_id}/structure/generate → trigger for subtree

         body: { "mode": "free" | "guided" }

         ← 200 OK:        snapshot with this fingerprint+mode already exists
         ← 202 Accepted:  job created (with ingestion plan + estimate)
         ← 409 Conflict:  active job overlaps with requested scope
         ← 422 Unprocessable: no READY materials in scope

GET    /api/v1/courses/{id}/structure                      → latest snapshot (course-level)
GET    /api/v1/courses/{id}/nodes/{node_id}/structure       → latest snapshot (node-level)
```

### Jobs & Reports

```
GET    /api/v1/jobs/{job_id}                               → status of any job
GET    /api/v1/reports/cost                                → LLM cost report
GET    /health                                             → deep health (DB + S3 + Redis)
```

## Epics

### Epic 0: Project Documentation Infrastructure (1–2 days)

Docs site on GitHub Pages (mkdocs). ERD, sprint descriptions. Executed **first** — all subsequent epics are documented in this system.

### Epic 1: Infrastructure — ARQ + Redis (4–5 days)

Task queue with persistence, concurrency control, work window, job tracking, estimates. Redis container + ARQ worker in docker-compose (dev and prod).

### Epic 2: MaterialTree + MaterialEntry (4–5 days)

Recursive tree of nodes, MaterialEntry with raw/processed separation and pending receipt. Tree API endpoints, course detail with full tree.

### Epic 3: Merkle Fingerprints (2–3 days)

Lazy cached fingerprints with bottom-up cascade invalidation. Material → node → course level. Auto-invalidation on CRUD operations.

### Epic 4: Heavy Steps Extraction (2–3 days)

Injectable heavy operations, serverless-ready boundary. Processors become orchestrators with DI.

### Epic 5: SlideVideoMapping Redesign (3–4 days)

Explicit presentation ↔ video FK references, three-level validation, deferred validation with auto-revalidation on ingestion complete.

### Epic 6: Structure Generation Pipeline (3–4 days)

Per-node trigger, cascading processing, fingerprint check, snapshot persistence, conflict detection, free/guided modes.

### Epic 7: Integration Documentation (1–2 days)

Flow guide, API reference, auth guide, error handling guide. Published on docs site. Final polish + ops/infrastructure documentation.

## Epic Dependencies

```
Epic 0 (Docs) ─── FIRST ──────────────────────────────────┐
                                                            │
Epic 1 (Queue) ────────────────────────┐                   │
                                        │                   │
Epic 2 (MaterialTree) ────────────────┤                   │
                                        │                   │
Epic 3 (Fingerprints) ── Epic 2 ──────┤                   │
                                        ├──→ Epic 6 ───────│
Epic 4 (Heavy Steps) ── Epic 1 ──────┤      │             │
                                        │      └──→ Epic 7 ─┘
Epic 5 (SlideVideoMapping) ── E1+E2 ──┘
```

**Recommended order:**

0. Epic 0 (Docs) — first, 1–2 days
1. Epic 1 (Queue) + Epic 2 (MaterialTree) — in parallel
2. Epic 3 (Fingerprints) — after Epic 2
3. Epic 4 (Heavy Steps) — in parallel with Epic 3
4. Epic 5 (SlideVideoMapping) — after Epic 1 + Epic 2
5. Epic 6 (Structure Generation) — after all previous
6. Epic 7 (Integration Documentation) — in parallel with Epic 6

## New Dependencies

```toml
[project]
dependencies = [
    # ... existing ...
    "arq>=0.26",          # task queue
    "redis[hiredis]>=5",  # ARQ backend + fast connection
]

[dependency-groups]
docs = [
    "mkdocs-material>=9",
    "mkdocs-mermaid2-plugin>=1",
    "mkdocs-panzoom-plugin>=0.4",
]
```

## Database Changes

See [ERD](../../architecture/erd.md) for the full schema diagram.

New tables: `material_nodes`, `material_entries`, `jobs`, redesigned `slide_video_mappings`, `course_structure_snapshots`.

Existing table changes: `modules` gets `snapshot_id` FK.

Migration strategy: drop old test data + create new tables (no production data to preserve).
