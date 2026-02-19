# Course Supporter

AI-powered system for transforming course materials into structured learning plans with automated mentoring.

## What it does

- **Ingests** video, presentations, text, and web links
- **Processes** content via LLM-powered pipeline (Gemini, Anthropic, OpenAI, DeepSeek)
- **Generates** structured course outlines with modules, lessons, concepts, and exercises
- **Serves** results via multi-tenant REST API with API key authentication

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.13+ |
| API | FastAPI |
| Database | PostgreSQL 17 + pgvector |
| ORM | SQLAlchemy (async) |
| Object Storage | S3-compatible (Backblaze B2) |
| LLM Providers | Gemini, Anthropic, OpenAI, DeepSeek |
| Containerization | Docker Compose |
| CI/CD | GitHub Actions |

## Quick Links

- [Architecture & ERD](architecture/erd.md)
- [API Flow Guide](api/flow-guide.md)
- [Development Setup](development/setup.md)
- [Sprint Roadmap](sprints/index.md)
