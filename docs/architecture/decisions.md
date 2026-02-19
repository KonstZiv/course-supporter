# Architecture Decision Records

Key architectural decisions made throughout the project, organized by sprint.

## Sprint 0 — Materials-to-Structure MVP

### ADR-001: psycopg v3 only

**Decision:** Use `postgresql+psycopg://` (psycopg v3) as the only DB driver.

**Context:** psycopg v3 supports both sync and async modes natively. Alembic uses sync template, application uses async — same driver handles both.

**Consequences:** No psycopg2 dependency. Alembic sync migrations work with the same connection string.

### ADR-002: UUIDv7 for all primary keys

**Decision:** Use UUIDv7 (via `uuid-utils`) for all table PKs.

**Context:** Time-ordered UUIDs are sortable, avoid sequence bottlenecks, and work well in distributed systems.

**Consequences:** No auto-increment sequences. IDs are sortable by creation time. Slightly larger than integer PKs.

### ADR-003: PEP 735 dependency groups

**Decision:** Use `[dependency-groups]` (PEP 735) for dev and docs tooling. Use `[project.optional-dependencies]` only for `media` (Whisper + PyTorch ~2GB).

**Context:** PEP 735 cleanly separates development tools from runtime dependencies. `uv sync` includes dev by default; `uv sync --group docs` adds docs tools.

**Consequences:** `media` stays as optional dependency because it affects the Docker image size and is needed at runtime.

### ADR-004: Strategy-based ModelRouter with two-level fallback

**Decision:** `ModelRouter` selects LLM provider based on action + strategy from `config/models.yaml`. Two-level fallback: within chain (next model) + cross-strategy (fallback strategy).

**Context:** 4 LLM providers with different strengths. Need graceful degradation when a provider is down.

**Consequences:** Any component calls LLM through `ModelRouter` without knowing provider details. Error classification (permanent vs transient) determines retry behavior.

### ADR-005: Composition pattern for VideoProcessor

**Decision:** `VideoProcessor` is a shell that composes `GeminiVideoProcessor` (primary) and `WhisperVideoProcessor` (fallback) as separate classes.

**Context:** Gemini handles short videos directly; Whisper is needed for long videos. Different APIs, different resource requirements.

**Consequences:** Each processor is independently testable. Fallback logic is in the shell, not mixed with processing.

### ADR-006: Repository flush() not commit()

**Decision:** Repositories call `flush()` instead of `commit()`. The caller controls transaction boundaries.

**Context:** Multiple repository operations often need to be atomic. If repositories committed, partial failures would leave inconsistent data.

**Consequences:** Service layer or endpoint handler calls `session.commit()` after all operations succeed.

### ADR-007: selectinload chains (not joinedload)

**Decision:** Use `selectinload` for eager loading of relationships, not `joinedload`.

**Context:** `joinedload` with multiple one-to-many relationships creates cartesian products, returning N×M rows.

**Consequences:** N+1 queries become N+1 SELECTs (batched), but no cartesian explosion. Acceptable for typical depths.

## Sprint 1 — Production Deploy

### ADR-008: API key auth (not OAuth/JWT)

**Decision:** Simple API key authentication via `X-API-Key` header. Key stored as SHA-256 hash.

**Context:** B2B API with a small number of tenants. OAuth/JWT adds complexity without proportional benefit.

**Consequences:** Stateless auth. Easy key rotation via Admin CLI. Raw key never stored in DB.

### ADR-009: In-memory rate limiter

**Decision:** Sliding window rate limiter in application memory, per (tenant_id, scope).

**Context:** Single-process deployment on one VPS. Redis-based limiter would add infrastructure for no gain.

**Consequences:** Rate limits reset on app restart. Clear upgrade path to Redis when horizontal scaling is needed.

### ADR-010: Shared Docker network for nginx

**Decision:** App container connects to a shared Docker network where nginx (from a separate compose project) runs. No ports exposed to host.

**Context:** VPS runs multiple services behind one nginx. Each service is a separate Docker Compose project.

**Consequences:** All traffic goes through nginx. Security headers, TLS termination, and rate limiting at nginx level. `resolver 127.0.0.11` pattern for dynamic DNS.

### ADR-011: S3 multipart upload (10MB parts)

**Decision:** Files > 50MB use multipart upload with 10MB parts via aiobotocore. Streaming from client through nginx to S3.

**Context:** Course materials can be up to 1GB (video files). Must not buffer entire file in memory.

**Consequences:** Constant ~10-20 MB RAM regardless of file size. `proxy_request_buffering off` in nginx config.

## Sprint 2 — Material Tree (in progress)

See [Sprint 2 plan](../sprints/sprint-2/index.md) for AR-1 through AR-7.
