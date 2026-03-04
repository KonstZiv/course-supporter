# S3-012: Rewrite Routes & Repositories (Course → Node)

**Phase:** 4b (Remove Course — Rewrite)
**Складність:** XL
**Статус:** PENDING
**Залежність:** S3-011

## Контекст

Переписати всі routes та repositories для роботи без Course entity. Root MaterialNode = course.

## URL Changes (Breaking)

| Старе | Нове |
|-------|------|
| `POST /courses` | `POST /nodes` (створює root з tenant_id) |
| `GET /courses` | `GET /nodes?root=true` |
| `GET /courses/{id}` | `GET /nodes/{id}` |
| `PUT /courses/{id}` | `PUT /nodes/{id}` |
| `DELETE /courses/{id}` | `DELETE /nodes/{id}` |
| `GET /courses/{cid}/nodes/tree` | `GET /nodes/{id}/tree` |
| `POST /courses/{cid}/nodes` | `POST /nodes/{pid}/children` |
| `POST /courses/{cid}/nodes/{nid}/materials` | `POST /nodes/{nid}/materials` |
| `POST /courses/{cid}/generation` | `POST /nodes/{id}/generation` |
| `GET /courses/{cid}/generation/latest` | `GET /nodes/{id}/generation/latest` |

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `src/course_supporter/api/routes/courses.py` | ВИДАЛИТИ або merge into nodes.py |
| `src/course_supporter/api/routes/nodes.py` | Повний rewrite — root node CRUD + tree |
| `src/course_supporter/api/routes/materials.py` | Remove course_id dependency |
| `src/course_supporter/api/routes/generation.py` | course_id → node_id (root) |
| `src/course_supporter/api/routes/jobs.py` | course_id → node_id |
| `src/course_supporter/api/routes/reports.py` | Update queries |
| `src/course_supporter/storage/course_repository.py` | ВИДАЛИТИ — merge into MaterialNodeRepository |
| `src/course_supporter/storage/material_node_repository.py` | Add root node methods |
| `src/course_supporter/storage/snapshot_repository.py` | course_id → materialnode_id |
| `src/course_supporter/storage/job_repository.py` | course_id → materialnode_id |
| `src/course_supporter/enqueue.py` | course_id → node_id |
| `src/course_supporter/generation_orchestrator.py` | course_id → node_id |
| `src/course_supporter/api/tasks.py` | course_id → node_id |
| `src/course_supporter/api/schemas.py` | Course schemas → Node schemas |
| `src/course_supporter/api/deps.py` | Remove course dependency helpers |
| `src/course_supporter/api/app.py` | Update router mounts |
| `tests/` | MAJORITY of test files need updates |

## Деталі реалізації

### Repository Changes

`CourseRepository` → методи MaterialNodeRepository:
```python
class MaterialNodeRepository:
    async def create_root(self, tenant_id, title, description, **kwargs):
        """Create root node (= course)."""
        node = MaterialNode(tenant_id=tenant_id, parent_id=None, ...)
        ...

    async def list_roots(self, tenant_id):
        """List root nodes (= courses) for tenant."""
        ...

    async def get_root(self, node_id):
        """Get root node with validation."""
        ...
```

### Route Helpers

```python
# Було:
async def _require_course(course_id, tenant_id, session):
    course = await CourseRepository(session).get(course_id)
    if not course or course.tenant_id != tenant_id:
        raise HTTPException(404)
    return course

# Стало:
async def _require_root_node(node_id, tenant_id, session):
    node = await MaterialNodeRepository(session).get(node_id)
    if not node or node.tenant_id != tenant_id or node.parent_materialnode_id is not None:
        raise HTTPException(404)
    return node
```

### Job & Snapshot FKs

```python
# ORM:
Job.course_id → Job.materialnode_id  (FK → material_nodes)
# Snapshot вже перейменований в Phase 3
```

## Acceptance Criteria

- [ ] CourseRepository видалений
- [ ] Всі routes використовують `/nodes/...` pattern
- [ ] Root node CRUD працює (create, list, get, update, delete)
- [ ] Tree endpoint працює з node_id замість course_id
- [ ] Generation, materials, jobs — всі через node_id
- [ ] Tenant isolation через `node.tenant_id` (без JOIN через Course)
- [ ] Всі тести оновлені та проходять
