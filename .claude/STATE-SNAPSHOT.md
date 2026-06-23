# Phase 1 STATE-SNAPSHOT — bridge for fresh Claude Code session

**Generated:** 2026-05-05 (post sub-area `ui` completion + state.md ratify; before session compaction)
**Purpose:** bootstrap fresh implementer instance without archeology through compacted transcript.

---

## §1 — Repos final state

| Repo | Branch | HEAD | Ahead origin | Pushed | Working tree |
|---|---|---|---|---|---|
| `course-supporter` (backend) | `410-phase-1-data-model-rename-kd3-adoption-ui-alignment` | `8e69a81` | 17 | NO | clean |
| `course-supporter-ui` | `phase-1-ui-kd-eta-adoption` | `fa772e9` | 4 | NO | clean |
| `refactoring-vision/` (sibling untracked) | — | `state.md` 412 lines, all amendments ratified | — | — |

`docker compose ps`: postgres + redis + minio UP (alembic head `phase1_rename_and_kd3`; dev DB `course_supporter`). Investigation §8 residual rows in dev DB: 1 row `structure_snapshots`, 1 row `structure_nodes_editable`, 1 row `reconciliation_previews` under tenant `probe-deep` (id `019df6dd-bfaa-7ef0-9b82-cf9a215ae29b`). Operator may `make reset` if clean DB needed for CHECKPOINT 2.

---

## §2 — Phase 1 commit chain (full breakdown)

### Backend — 17 commits ahead of origin

```
8e69a81 phase(1.kd3-fix-3): bridge Pydantic schemas to legacy Phase 2.x ORM columns via validation_alias  ← HEAD
06951c4 phase(1.kd3-fix-2): wire on_cancel_jobs into 3 KD3 handlers + on_invalidate_hashes into delete_document per vision §KD13
5749cb2 phase(1.kd3-fix): scrub callables emit KD3 formatted marker per vision §3 KD3
4c9f58c phase(1.kd3): rewrite delete_file with cascade soft-delete + force-orphan s3_cleanup + remove AuthoredDocumentRepository.delete()
dd0c3da phase(1.kd3): rename routes/materials.py → documents.py + KD3 rewrite delete_document + retry_document HTTP 410 + 8-handler payload renames
5e85305 phase(1.kd3): rewrite delete_node with cascade soft-delete + s3_cleanup enqueue + remove CourseNodeRepository.delete() + nodes.py payload renames
1d20f26 phase(1.models-fix-3): per-class __scrub_callable__ dispatch on cascade descendants
5cef505 phase(1.kd3): CourseNodeRepository.get_subtree tenant_id kwarg per Gap 1
83ff814 phase(1.kd3): on_invalidate_hashes exclude_ids + scrub_authored_document + s3_cleanup_orchestration helper
f322c15 phase(1.models-fix-2): disambiguate cascade-engine FK resolution for multi-FK child entities
ec510cb phase(1.models): KD-delta defensive default for AuthoredDocument.course_root_id
20e35b5 phase(1.models): extend content_hash formula on DocumentSegment + DocumentSummary
4cda9d9 phase(1.models): KD-alpha delete-orphan strip + survival/decoupling tests
4eaf496 phase(1.models): __cascades_soft_delete_to__ declarations + KD-β scrub_callable
c6bca17 phase(1.models-fix): disambiguate AuthoredDocument FKs after course_root_id introduction
45e6baf phase(1.models): atomic ORM rename + repository renames + new column declarations
f53b6a0 phase(1.schema): consolidated rename + KD3 prep + course_root_id denormalization migration
```

**Sub-area breakdown:**
- **`schema`** (1 commit, `f53b6a0`): consolidated rename migration `material_nodes → course_nodes`, `material_entries → authored_documents`; FK column rename `materialnode_id → course_node_id` (on entries); KD-δ `course_root_id` denormalization; alembic head `phase1_rename_and_kd3`.
- **`models`** (5 commits, `45e6baf` … `1d20f26`): atomic ORM class rename + repository renames; `__cascades_soft_delete_to__` declarations + KD-β `scrub_callable`; KD-α delete-orphan strip + survival/decoupling tests; content_hash formula extended on DocumentSegment + DocumentSummary; KD-δ defensive default; cascade FK disambiguation; per-class `__scrub_callable__` dispatch.
- **`kd3` core** (5 commits, `83ff814` … `4c9f58c`): handler-level KD3 adoption (delete_node, delete_document, delete_file Mode A/B); module rename routes/materials.py → documents.py; `enqueue_s3_cleanup` helper; Gap 1 + Gap 3 fixes.
- **Hotfix-4** (`5749cb2`): scrub callables emit KD3 formatted marker (Ukrainian-language "інформація видалена автором..."). Amendments 23 + 24 + 25 + 26 ratified.
- **Hotfix-5** (`06951c4`): wire `on_cancel_jobs` into 3 KD3 handlers + tangential `on_invalidate_hashes` gap closed in `delete_document`. Amendment 28 ratified.
- **Hotfix-6** (`8e69a81`): Pydantic schema layer ↔ Phase 2.x legacy ORM columns bridge via `validation_alias` + `populate_by_name=True` + alias-aware `_orm_to_response` getattr. Amendments 32 + 33 ratified. Closed 5+ HTTP 500 endpoints (snapshot/editable). Surface investigation = `~/Desktop/sub-area-ui-deep-investigation.txt` (file may have been auto-pruned — verify with operator; substance preserved in state.md §3 + §7).

### UI — 4 commits ahead of origin

```
fa772e9 task(1.UI.4): adopt renamed fields in pages + UA label sweep + vite proxy switch
e470edb task(1.UI.3): adopt renamed types in flow components
c598069 task(1.UI.2): rename api/materials.ts → api/documents.ts + adopt renamed types in api/* + stores
6d0c542 task(1.UI.1): rename types in src/types/api.ts per KD-η canonical
```

**Sub-area `ui` breakdown:**
- **`task(1.UI.1)`** — types/api.ts (10 Edits incl. 4 KD-ui.6 REVISED Option B Phase 2.x type adoptions). Mid-chain TS errors expected per rule #13.
- **`task(1.UI.2)`** — git mv api/materials.ts → api/documents.ts (70% similarity); identifier `materialsApi → documentsApi`; URL `/materials/* → /documents/*`; type imports updated in api/nodes.ts + stores/course.ts + import-line in NodeDetailPanel.tsx; FlowContextMenu.tsx 2 dynamic imports + bodies.
- **`task(1.UI.3)`** — flow components + treeToFlow + DashboardPage field access (Option B atomic-concept tsc-green restoration; FlowNodeData KEY rename `materials → authored_documents`; KD-ui.7 source-only fingerprint rename). Build green restored at end of this commit.
- **`task(1.UI.4)`** — UA label sweep (7 sites per KD-ui.5 bundle); vite proxy switch to `localhost:8000` for CHECKPOINT 2 enablement; cosmetic comment header in types/api.ts. Final commit; sub-area `ui` ✅ COMPLETE.

---

## §3 — Critical artifacts on operator's Desktop

**Verified present on Desktop (2026-05-05 spot-check):**

| File | Lines | Source |
|---|---|---|
| `~/Desktop/POST-STAGING-VERIFICATION-h6.txt` | 316 | Implementer (this session) — hotfix-6 audit |
| `~/Desktop/commit-1-UI.1-staged.patch` + `.stat.txt` | 123 + 4 | Implementer — task(1.UI.1) staging |
| `~/Desktop/commit-2-UI.2-staged.patch` + `.stat.txt` | 173 + 9 | Implementer — task(1.UI.2) staging |
| `~/Desktop/commit-3-UI.3-staged.patch` + `.stat.txt` | 251 + 10 | Implementer — task(1.UI.3) staging |
| `~/Desktop/commit-4-UI.4-staged.patch` + `.stat.txt` | 105 + 11 | Implementer — task(1.UI.4) staging |

**Operator-supplied (per user-listed expected, but NOT on Desktop at session-end spot-check — may have been auto-pruned by macOS / workflow cleanup; ask operator to drag-drop if needed):**

- `~/Desktop/PHASE-1-READINESS-REPORT.txt` — pre-CHECKPOINT-1 readiness (predates this session; substance reflected in state.md §3 CHECKPOINT 1 verdict table)
- `~/Desktop/POST-STAGING-VERIFICATION.txt` — hotfix-5 verification (referenced by state.md §3 hotfix-5 description; Block 7 race observation source)
- `~/Desktop/sub-area-ui-pre-investigation.txt` — 651 lines, fact base for sub-area `ui` (predates this session)
- `~/Desktop/sub-area-ui-pre-flight.txt` — 804 lines, formal pre-flight per rule #2
- `~/Desktop/sub-area-ui-deep-investigation.txt` — 4-layer audit (DB / ORM / Pydantic / JSON) that surfaced Amendment 32 hidden bug
- `~/Desktop/hotfix-6-pre-flight.txt` — formal pre-flight for `phase(1.kd3-fix-3)`
- `~/Desktop/hotfix-6-staged.patch` + `.stat.txt` — hotfix-6 diff (269 lines)

If operator can not re-locate any of the above, substance is preserved across:
- `state.md` (§3 sub-area history; §7 Amendments 17-33)
- Backend commit messages (full body anchors per Phase 1 hotfix-N + sub-area `kd3` chain)
- UI commit messages (task(1.UI.1)-task(1.UI.4) bodies)

---

## §4 — Outstanding workstreams (in order)

### 4.1 — CHECKPOINT 2 (immediate next, vision-side relays plan)

Live UI walkthrough at `http://localhost:5173` against local backend at `http://localhost:8000`. vite proxy already switched in commit `fa772e9` to enable this without per-developer override.

**Scenario shape (per state.md + RULING 2 process):** course-create + child-create + document-upload + state-badge + delete + cascade flows via browser. Verify all 4 previously-broken endpoints render correctly (snapshot list/detail + editable tree + reconciliation status) post-hotfix-6.

**Implementer action on session start:** wait for vision-side to relay CHECKPOINT 2 plan. DO NOT autonomously execute browser flow.

### 4.2 — Phase-end procedure (after CHECKPOINT 2 PASS, operator-confirmed)

1. POST-MERGE-NOTES drafting per `_TEMPLATES/POST-MR-NOTES.md` (verify template path with operator; if not present, vision-side provides shape).
2. Backend repo: `git push origin 410-phase-1-data-model-rename-kd3-adoption-ui-alignment` (operator-confirmed only).
3. UI repo: `git push origin phase-1-ui-kd-eta-adoption` (operator-confirmed only).
4. PR creation per repo (or coordinated PR per monorepo strategy — verify with operator).

---

## §5 — Critical context for fresh implementer instance

### 5.1 — Three-actor model

**Vision-side** (Anthropic Claude.ai chat, separate transcript, operator-only access) — strategy authority; KD-rulings; pre-flight ratify; staged-diff ratify; verbatim text dictation for state.md amendments. Sees probe-derived facts (psql output, grep transcripts, JSON shapes) only via implementer relay through operator.

**Operator** (human user) — relays between vision-side and implementer; runs `git diff --cached` locally; drag-drops artifacts to chats; gates push + PR creation; maintains memory across sessions for both sides.

**Implementer** (this Claude Code session) — code-write + commits; pre-flight authoring; mini-probes; staging; rule #10 verification. NEVER pushes branches without operator confirmation; NEVER advances scope past pre-flight ratify; NEVER fabricates content where source-of-truth is unavailable.

### 5.2 — Workflow rules (most-used)

| Rule | Substance |
|---|---|
| **#2** | Six-point pre-flight before any code change (TASK + vision sections + files modified + files read + out-of-scope + acceptance). Submit FIRST; APPLY ruling required. |
| **#6** | No push origin/branch until phase-end procedure operator-confirmed. |
| **#8** | Pre-flight blocker / scope drift → STOP + escalate to operator. NEVER silently expand scope or patch around pre-flight gaps. |
| **#10** | Post-commit verification: HEAD SHA + clean working-tree status + ahead-of-origin count. |
| **#13** | Atomic commits over artificial bisect-separation. Mid-chain build break acceptable if pre-flight authorizes. |
| **#15** | Pattern reference in pre-flight: cite predecessor commit shape (subject + scope + file count). |
| **#16** | Cross-check vision-side scope against repo state BEFORE drafting. Probes are authoritative. |
| **#17** | Sub-area boundary: operator OK + fresh pre-flight required at every sub-area start; expanded autonomy does NOT carry over. |

### 5.3 — Process learnings from Phase 1 (process discipline)

- **Amendment 26** — write-before-pre-flight guardrail. Every code change goes through formal pre-flight ratify cycle first.
- **Amendment 30** — fabrication-when-source-unavailable. Use uniform placeholder phrase ("vision-side has transcript" / "(check transcript)" / "(verify with operator)"); never invent plausible content. If vision-side relays "the dictated text supplied above" and the actual text is missing, escalate per rule #8.
- **Amendment 33** — cross-cutting probe discipline. Alias-bridge / type-system pre-flights MUST enumerate ALL materialization paths (ORM-instance, dict, kwarg, mixed) AND probe each with REAL fixtures (real ORM via DB session). MagicMock auto-creates missing attributes and masks AttributeError on legacy ORM columns.
- **4 rule #8 escalations during sub-area ui execution all caught real issues at probe-time before silent-patch** (see state.md §7 Amendment 33 trailer for enumeration). Pattern works; trust calibration good.

### 5.4 — Communication preference

User communicates in **Ukrainian**. Respond in Ukrainian; technical terms in English (per global `~/.claude/CLAUDE.md` device rule).

### 5.5 — Working style preference

**One point at a time** — stop, ask, agree, then proceed. Don't dump multi-section blobs without checkpoints. State a decision/section, ask "is this correct?", wait for confirmation, then move to next.

---

## §6 — KD-rulings landed in Phase 1

### 6.1 — Backend KDs (8 rulings, Etap В planning 2026-05-01)

| Ruling | Substance | Status |
|---|---|---|
| KD-α | delete-orphan strip + survival/decoupling tests | ✅ Landed (`4cda9d9`) |
| KD-β | per-call scrub_callable override semantics for Tenant ROOT | ✅ Landed (`4eaf496`, models-fix-3 dispatch) |
| KD-γ | jsonb concept columns (4 columns; Block 8 verified) | ✅ Landed (verified at CHECKPOINT 1 Block 8) |
| KD-δ | `course_root_id` denormalization on AuthoredDocument | ✅ Landed (`f53b6a0` migration; `ec510cb` defensive default) |
| KD-η | canonical type names (forward-rename target) | ✅ Landed (`45e6baf` ORM rename; UI side aligned in sub-area `ui` chain) |
| KD-θ | migration dispatch pre-flight | ✅ Landed (verified at CHECKPOINT 1 Block 0) |
| KD3 | author-content scrub + cascade soft-delete | ✅ Landed (5 core kd3 commits + hotfix-4 marker contract) |
| KD9 | content_hash collapse + propagation | ✅ Landed (`f53b6a0` collapse; `20e35b5` formula extension; backfill) |
| KD13 | active job cancellation race | ✅ Landed (hotfix-5; `cancel_signals` ordering ratified) |

### 6.2 — UI sub-area KDs (8 rulings, sub-area `ui` execution 2026-05-04 → 05)

| Ruling | Substance | Status |
|---|---|---|
| KD-ui.1 | KEEP `MaterialRole` type-name (backend keeps `material_role` payload field) | ✅ Applied |
| KD-ui.2 | Option (a): file + identifier + URL + function rename | ✅ Applied (commit 2) |
| KD-ui.3 | nested key `authored_documents` (matches backend Pydantic) | ✅ Applied (commit 1) |
| KD-ui.4 | name-by-name type renames per KD-η canonical | ✅ Applied (commit 1) |
| KD-ui.5 | Option (a): bundle 7 UA-label translations into commit 4 | ✅ Applied (commit 4) |
| KD-ui.6 REVISED Option B | UI types align with Pydantic-declared contract; 4 additional Phase 2.x adoption Edits (KEEP `'stale_materials'` enum value display-text-only translation) | ✅ Applied (commit 1 +4; commit 4 stale_materials KEY KEEP + display rename) |
| KD-ui.7 | KEEP `FlowNodeData.fingerprint` UI-internal key; only ORM source-field rename | ✅ Applied (commit 3) |
| KD-ui.8 | DEFER backend `NodeWithMaterialsResponse` class rename to Phase 1.X follow-up | ✅ Documented (Amendment 31) |

Full text per ruling: `state.md` §3 (sub-area ui block) + `state.md` §7 (Amendments 31 + 32 + 33 cross-references).

---

## §7 — Watch-flags carried forward (Phase 5-bound)

| Flag | Substance | Phase-end disposition |
|---|---|---|
| ESC double-logging | 2 ESC rows per LLM call (legacy `ingestion_callback.py` + `service_logging.py`) | Phase 5 dedup; KD5 immutability still honored |
| Job.status not synced post-ARQ-execution | Amendment 27 — `jobs.status` stuck `queued` post completion | Phase 5 ARQ wrapper sync |
| `ingest_material` no `raise_if_cancelled` | Amendment 29 — worker barrels through 706s past cascade DELETE | Phase 5 KD2 ingestion stage discipline |
| `content_hash` NULL on CourseNode INSERT | Watch-flag — KD9 propagation timing | Phase 5 finalization (KD9 propagation timing concern) |
| Wider unit baseline 129+6/20 failures | Broader than Amendment 17/19 24-test snapshot; not from hotfix-6 | Amendment 34 candidate at POST-MERGE-NOTES drafting |
| TestGetSnapshot mock factory shape gap | Amendment 19 baseline; hotfix-6 alias bridge does NOT resolve (mock has `.course_node_id`, not `.materialnode_id`); pre-flight §4.3 GATE option B kept known-failures.txt unchanged | Future hotfix-7 candidate or fold into mock-factory cleanup before Phase 1 PR |
| Amendment 31 NodeWithMaterialsResponse backend class rename | UI side ships forward-rename; backend keeps legacy class name | Phase 1.X follow-up |

---

## §8 — Bootstrap instructions for fresh session

### On session start, in order:

1. **Read this `STATE-SNAPSHOT.md` first.** It's at `course-supporter/.claude/STATE-SNAPSHOT.md`.
2. **Read `refactoring-vision/sprint/phases/phase-1/state.md`** (412 lines; full Phase 1 history, all amendments).
3. **Confirm with operator:** is CHECKPOINT 2 immediate next, or has scope shifted?
4. **If CHECKPOINT 2:** wait for vision-side to relay scenario plan; do NOT autonomously execute browser flow.
5. **If phase-end (CHECKPOINT 2 already passed):** begin POST-MERGE-NOTES drafting per template (verify template path + shape with operator).
6. **If session shifts to Phase 2.x:** require fresh pre-flight per rule #17 sub-area boundary; backward state of Phase 2.x territory at hotfix-6 land = DB columns `materialnode_id` + `node_fingerprint` retained; ORM unchanged; Pydantic forward-renamed with `validation_alias` bridge.

### DO NOT:

- Re-do work already accomplished (4 backend hotfixes + 4 UI commits already landed).
- Push origin/branch (rule #6; operator's call).
- Begin Phase 2 work (rule #17 boundary; await operator + vision-side gate).
- Claim memory of conversation specifics from compacted session — rely only on this file + state.md + git history.
- Fabricate content when source-of-truth unavailable (Amendment 30; use placeholder phrase + escalate).

### Sanity-check commands on session start

```bash
# Backend HEAD verification
cd /Users/kostyantynzivenko/Desktop/documents/COURSE-SUPPORTER/course-supporter
git rev-parse HEAD                                      # expect 8e69a81...
git log --oneline origin/main..HEAD | wc -l            # expect 17
git status --short                                      # expect clean

# UI HEAD verification
cd /Users/kostyantynzivenko/Desktop/documents/COURSE-SUPPORTER/course-supporter-ui
git rev-parse HEAD                                      # expect fa772e9...
git log --oneline origin/main..HEAD | wc -l            # expect 4
git status --short                                      # expect clean
git branch --show-current                              # expect phase-1-ui-kd-eta-adoption

# Infra
docker compose ps                                       # expect postgres + redis + minio UP
```

If any divergence — STOP + escalate per rule #8 before any further action.

---

## §9 — Last-act self-check (this session)

This snapshot composed 2026-05-05 immediately after `state.md` ratify. Implementer-side trust calibration through Phase 1: 4 rule #8 escalations, all surfaced real issues; 0 silent-patch incidents; 0 fabrications-when-source-unavailable; pre-flight discipline held through 21 backend commits + 4 UI commits + 3 hotfixes + 4-layer investigation + state.md ratify cycles. Pattern good for fresh instance to inherit.

End of bootstrap.
