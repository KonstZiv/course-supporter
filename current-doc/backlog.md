# Backlog — Future Tasks

Items not tied to a specific sprint. Pick up when relevant epic starts or as standalone improvements.

---

## Web Source Validation — Platform Allowlist

**Context:** `source_type=web` accepts any URL, but not all platforms can be processed. VideoProcessor uses yt-dlp for video URLs and WebProcessor uses trafilatura for HTML. Some platforms block scraping, require auth, or serve JS-only content.

**Task:** Define an allowlist of supported platforms per source_type:

- **video URLs** — YouTube (confirmed working via yt-dlp), Vimeo, possibly others. Need to test and confirm which platforms yt-dlp can handle reliably.
- **web URLs** — trafilatura works well for static HTML. Platforms with heavy JS rendering (SPAs), paywalls, or anti-scraping (e.g. Medium with limits, LinkedIn) may fail silently or return garbage.

**Deliverables:**

1. Test major platforms (YouTube, Vimeo, Dailymotion for video; Wikipedia, GitHub, dev blogs for web) and document results.
2. Create a `SUPPORTED_PLATFORMS` config (domain -> source_type mapping) with known-good entries.
3. Optionally warn (not block) when URL domain is not in the allowlist: "This platform has not been verified, processing may fail."
4. Add platform-specific notes to API docs (e.g. "YouTube: public videos only, age-restricted may fail").

**Priority:** Medium — current behavior works for known-good URLs, but users will hit confusing errors with unsupported platforms.

---

## Rename LLMCall → ExternalServiceCall + Unified Service Registry

**Context:** `LLMCall` table tracks only LLM API calls, but the system is expanding to call non-LLM external services (transcription APIs, etc.) with different pricing models. The current `config/models.yaml` only covers LLM models. Field values (`action`, `strategy`, `provider`, `model_id`) are not validated against any registry.

**Task:**

1. **Rename ORM model**: `LLMCall` → `ExternalServiceCall`, table `llm_calls` → `external_service_calls`. Alembic migration with `op.rename_table()`.
2. **Unified config**: Replace `config/models.yaml` with `config/external_services.yaml` — single registry for all external services (LLM providers, transcription services, future integrations). Define providers, actions, strategies, pricing models.
3. **Pydantic validation**: Load `external_services.yaml` at startup, validate with Pydantic model. Validate `ExternalServiceCall` field values against the registry.
4. **Update all references**: repositories, routes, agents, tasks, tests.
5. **Update TO-BE ERD diagram** (`current-doc/erd-course-vs-rootnode.md`).

### ExternalServiceCall — final field specification (decided)

- `id` UUID PK
- `tenant_id` UUID FK → Tenant, nullable (NULL = system calls without tenant context)
- `job_id` UUID FK → Job, nullable (NULL = outside of job queue, e.g. direct API call)
- `action` String(100) — what was done: transcribe, describe_slides, generate_structure
- `strategy` String(50) — model selection strategy from external_services.yaml
- `provider` String(50) — provider: gemini, anthropic, openai, elevenlabs, assemblyai, ...
- `model_id` String(100) — specific model/service
- `prompt_ref` String(50), nullable — reference to prompt file/version from external_services.yaml (renamed from prompt_version)
- `unit_type` String(20), nullable — type of billing units: tokens, minutes, characters, ...
- `unit_in` int, nullable — input units consumed
- `unit_out` int, nullable — output units produced
- `latency_ms` int, nullable — response time in milliseconds
- `cost_usd` float, nullable — cost of the call
- `success` bool, default True — whether the call succeeded
- `error_message` Text, nullable — error description on failure
- `created_at` DateTime(tz) — timestamp

16 fields. Changes from current LLMCall:
- Added: `job_id` FK (link to Job), `unit_type` (billing unit kind)
- Renamed: `tokens_in/tokens_out` → `unit_in/unit_out`, `prompt_version` → `prompt_ref`
- Removed: nothing (all existing fields preserved or renamed)

**Priority:** Medium — needed before adding non-LLM external service integrations.

---

## Auth Scopes Registry — config/auth.yaml

**Context:** API scopes (`"prep"`, `"check"`) are hardcoded as string literals across route files (`courses.py`, `nodes.py`, `materials.py`, `generation.py`, `jobs.py`, `reports.py`). There is no central registry, no documentation describing what each scope permits.

**Task:**

1. Create `config/auth.yaml` with `scopes` section defining all valid scopes, descriptions, and associated rate limit semantics.
2. Load and validate with Pydantic model at startup (same pattern as `external_services.yaml`).
3. Replace hardcoded `"prep"` / `"check"` strings in all routes with references from the registry.
4. Validate `APIKey.scopes` JSONB values against the registry on key creation.

**Priority:** Low — current system works, but lacks discoverability and validation for scope values.

---

## PostgreSQL COMMENT ON — Table & Column Descriptions

**Context:** PostgreSQL supports `COMMENT ON TABLE` and `COMMENT ON COLUMN` for documenting database objects. These comments are visible in `psql` (`\dt+`, `\d+ table_name`), pgAdmin, DBeaver, and any SQL client — making the schema self-documenting without external docs.

**Task:**

1. Create Alembic migration adding `COMMENT ON TABLE` for all tables (purpose, key invariants).
2. Add `COMMENT ON COLUMN` for non-obvious fields (FK semantics, nullable meaning, JSONB structure, derived values, default rationale).
3. Use SQLAlchemy `comment=` parameter on `mapped_column()` and `__table_args__` for new tables going forward.

**Example:**
```sql
COMMENT ON TABLE external_service_calls IS 'Audit log of every call to external APIs (LLM, transcription, etc.)';
COMMENT ON COLUMN external_service_calls.strategy IS 'Model selection strategy from external_services.yaml (e.g. default, fallback)';
COMMENT ON COLUMN api_keys.key_hash IS 'SHA-256 hash of the API key. The raw key is never stored.';
```

**Priority:** Low — zero risk, no schema changes, pure documentation. Can be done incrementally.

---

## Replace Module/Lesson/Concept/Exercise with recursive StructureNode

**Context:** Current output structure is a rigid 4-level hierarchy: `Module → Lesson → Concept → Exercise`. This cannot mirror arbitrary-depth input trees (MaterialNode). If MaterialNode has 6 levels, the generated structure is artificially flattened to 4.

**Decision:** Replace 4 tables with a single recursive `StructureNode` table (self-ref adjacency list, same pattern as MaterialNode). `node_type` field determines the role (module, lesson, concept, exercise, or new types).

**Key design points:**

1. **Recursive self-ref**: `parent_id FK → structure_nodes.id`, NULL = root of generated structure.
2. **`node_type`**: StrEnum — `"module"`, `"lesson"`, `"concept"`, `"exercise"`, extensible.
3. **`snapshot_id` FK**: ties to StructureSnapshot (which generation produced this node).
4. **Fields filled by different agents at different stages** (all nullable except system fields).

### StructureNode fields — final specification

**System fields:**
- `id` UUID PK
- `snapshot_id` UUID FK → StructureSnapshot
- `parent_id` UUID FK → self (NULL = root)
- `node_type` StrEnum
- `order` int
- `created_at` datetime
- `updated_at` datetime (tracks manual edits)

**Section 1 — Formal & organizational (filled by: Methodologist agent):**
- `title` String — node name
- `description` Text — what is studied, expanded content description
- `learning_goal` Text — objective
- `expected_knowledge` JSONB list[{summary, details}] — what student should know after completion. `summary`: short label for UI/presentation. `details`: expanded for indexing/embedding
- `expected_skills` JSONB list[{summary, details}] — what student should be able to do. Same two-level structure
- `prerequisites` JSONB list[str] — what student must know/be able before starting
- `difficulty` String — easy | medium | hard
- `estimated_duration` int — minutes. Must be >= total duration of all nested video/audio + sum of children estimates

**Section 2 — Results & assessment (filled by: Methodologist agent):**
- `success_criteria` Text — criterion for successful completion
- `assessment_method` String — recommended verification method (test, project, peer review, ...). Free text with examples in help_text
- `competencies` JSONB list[str] — which competencies the acquired knowledge/skills relate to

**Section 3 — Methodological accents (filled by: Methodologist agent):**
- `key_concepts` JSONB list[{summary, details}] — key terms introduced for the first time. Same two-level structure as knowledge/skills
- `common_mistakes` JSONB list[str] — typical mistakes expected from students
- `teaching_strategy` String — methodological approach. Free text with examples in help_text: "Problem-based learning", "Direct instruction", "Flipped classroom", "Case study", ...
- `activities` JSONB list[str] — what the student does: listens to lecture, writes project, develops strategy, prepares presentation, analyzes case, ...

**Section 4 — Context & adaptivity (filled by: Methodologist agent):**
- `teaching_style` String — academic, friendly, gamified. Free text with examples in help_text
- `deep_dive_references` JSONB list[str|object] — materials for deeper study, links for those who want more
- `content_version` datetime — when materials were last updated

**Section 5 — Material references (filled by: Indexer/Analyst agent, AFTER methodologist):**
- `timecodes` JSONB — maps concepts/topics to video fragments with timestamps
- `slide_references` JSONB — maps concepts/topics to presentation slides
- `web_references` JSONB — maps concepts/topics to external URLs

**Section 6 — Semantic search (filled by: Embedding pipeline):**
- `embedding` Vector(1536) — for semantic search. Created for nodes, key_concepts items, knowledge items, skills items. Enables mentor agent to find where in the course a student's weak point is covered.

### Two-level JSONB structure (variant B — decided)

`key_concepts`, `expected_knowledge`, `expected_skills` use identical format:
```json
[
  {
    "summary": "Транзакції в PostgreSQL",
    "details": "BEGIN, COMMIT, ROLLBACK, SAVEPOINT, вкладені транзакції, автоматичний rollback при помилці"
  }
]
```
- `summary` — short label for UI/presentations
- `details` — expanded description for indexing and embedding

### Fields NOT included (deferred to Mentor agent scope)

- `definition` (was Concept) — content for student, not macro-structure
- `examples` (was Concept) — content for student
- `reference_solution` (was Exercise) — content for student
- `grading_criteria` (was Exercise) — covered by `success_criteria` + `assessment_method`

### MaterialNode — no changes to fields

MaterialNode remains as-is (TO-BE version with tenant_id, learning_goal, expected_knowledge, expected_skills). Input materials come as-is from course author; if methodological metadata exists in text documents, LLM will parse it into StructureNode fields.

**Migration:** Data migration from 4 tables → 1 with `node_type` mapping. Alembic.

**Impact:** Removes 3 tables (Lesson, Concept, Exercise), replaces Module. TO-BE diagram: 12 → 9 tables (−Module, −Lesson, −Concept, −Exercise, +StructureNode).

**Priority:** High — architectural decision, blocks future multi-level generation.

**Status:** SPECIFICATION COMPLETE. Ready for TO-BE diagram update and implementation planning.

### StructureSnapshot — simplified (decided)

Removed duplicated fields (tenant_id, prompt_version, model_id, tokens_in, tokens_out, cost_usd) — single source of truth in ExternalServiceCall. Removed `mode` — determined by strategy/prompt in ExternalServiceCall + external_services.yaml.

Final fields:
- `id` UUID PK
- `node_id` UUID FK → MaterialNode (which subtree was generated for)
- `service_call_id` UUID FK → ExternalServiceCall (call details: model, strategy, prompt, cost)
- `node_fingerprint` String(64) — Merkle hash for idempotency
- `structure` JSONB — raw LLM response
- `created_at` datetime

6 fields total. Strategies and prompts registered in `config/external_services.yaml`.

---

## Remove content_fingerprint from MaterialEntry, use processed_hash in Merkle tree

**Context:** `MaterialEntry.content_fingerprint` is a cached `sha256(processed_content)` used as intermediate step for computing `MaterialNode.node_fingerprint`. But `processed_hash` is also `sha256` of the content — the two fields are redundant.

**Task:**

1. **Remove `content_fingerprint`** from MaterialEntry ORM, schemas, API responses.
2. **Update `FingerprintService._compute_material_fp()`** — return `entry.processed_hash` directly instead of computing and caching a separate fingerprint.
3. **Alembic migration** — drop column `content_fingerprint` from `material_entries`.
4. **Update tests** — remove all references to `content_fingerprint`.

### `node_fingerprint` on MaterialNode — computation and validation

**What it is:** Merkle hash that represents the complete state of a node's content (own materials + all children recursively). Used for idempotency — if fingerprint hasn't changed since last generation, StructureSnapshot is reused.

**How it is computed (bottom-up):**
```
node_fingerprint = sha256(sorted(
    ["m:" + entry.processed_hash for entry in node.materials if entry.processed_content is not None]
    + ["n:" + child.node_fingerprint for child in node.children]
).join("\n"))
```

- Only READY materials contribute (unprocessed are skipped)
- Child fingerprints are computed recursively (leaves first)
- Parts are sorted for deterministic ordering
- Prefixes `m:` and `n:` distinguish material hashes from child node hashes

**Invalidation (top-down):**
- When any MaterialEntry changes (new content, re-processing, deletion) → `node_fingerprint = NULL` on the entry's node and all ancestors up to root
- `NULL` means "stale / needs recompute"
- Next generation request triggers recomputation via `FingerprintService.ensure_node_fp()`

**Validation:**
- `node_fingerprint IS NOT NULL` → node is up-to-date, safe to use for idempotency check
- `node_fingerprint IS NULL` → at least one material or child changed since last computation
- Recomputation is lazy (on demand), not eager (on every change)

**Priority:** Medium — cleanup, removes redundancy. Can be done alongside StructureNode migration.

---

## Job cleanup: remove result fields + cascading failure propagation

### Remove result_material_id and result_snapshot_id from Job

**Context:** `result_material_id` and `result_snapshot_id` are reverse pointers to Job results. They duplicate relationships that already exist on the result side (MaterialEntry.pending_job_id, StructureSnapshot via node_id + timestamp). They don't scale — each new Job type would need a new result field + CHECK constraint update.

**Task:**
1. Remove `result_material_id`, `result_snapshot_id` from Job ORM.
2. Remove CHECK constraint `chk_job_result_exclusive`.
3. Update JobRepository, tasks, tests.
4. Alembic migration — drop columns.
5. Find results through the "requester" side (MaterialEntry, StructureSnapshot, etc.).

### Cascading failure propagation for depends_on

**Context:** `depends_on` is a flat JSONB list of Job UUIDs. Each Job waits for its direct dependencies. Dependency graph is distributed across individual Job records — this is sufficient. However, there is no failure propagation: if a dependency fails, dependent jobs wait forever.

**Example:**
```
Job A depends_on: [B, C, D]
Job C depends_on: [E, F]
If E fails → C should fail → A should fail (or be cancelled)
Currently: A waits indefinitely.
```

**Task:**
1. When Job transitions to `failed` → find all Jobs where `depends_on` contains this Job's ID.
2. Mark them as `failed` (or `cancelled`) with `error_message: "Dependency {job_id} failed"`.
3. Propagate recursively (failed Job may itself be a dependency of other Jobs).
4. Consider: should failure cancel or fail dependents? (cancel = "never ran", fail = "couldn't run")

**Priority:** Low — current system has few dependency chains (max 2 levels). Becomes important with recursive multi-pass generation.

---

## Recursive LLM Generation Strategy + Reconciliation Passes

**Context:** Per-node LLM generation (each node separately) produces better results than whole-course generation: more focused prompt, less noise in response, smaller context window. However, nodes processed independently may produce contradictions, overlapping content, or inconsistent terminology across siblings/parent-child relationships.

**Task:** Design a multi-pass generation strategy:

### Pass 1 — Leaf-to-root generation (bottom-up)
- Process each leaf node independently (most focused, least noise)
- Then process parent nodes, providing summaries of already-generated children as context
- Continue up to root

### Pass 2 — Reconciliation (top-down)
- After all nodes have initial structure, run reconciliation queries:
  - **Contradiction detection**: identify conflicting definitions, overlapping topics between sibling nodes
  - **Gap detection**: find missing prerequisites, broken concept dependencies across nodes
  - **Parent enrichment**: generate/update parent node descriptions (`learning_goal`, `expected_knowledge`, `expected_skills`) based on aggregated children data
  - **Terminology normalization**: ensure consistent naming across the tree

### Pass 3 — Optional refinement
- User reviews and edits StructureNode (manual corrections)
- Re-run reconciliation only for affected subtree

**Open questions:**
1. Prompt design for reconciliation — single prompt per parent with all children, or pairwise sibling comparison?
2. How to handle conflicting reconciliation suggestions (LLM says "merge these topics" but user wants them separate)?
3. Cost optimization — reconciliation multiplies LLM calls. Cache aggressively via fingerprints?
4. StructureSnapshot granularity — one snapshot per node per pass, or one snapshot per full tree per pass?

**Priority:** Medium — needed after recursive StructureNode is implemented. Blocks production-quality multi-level course generation.
