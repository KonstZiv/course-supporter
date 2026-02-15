# PD-016: GitHub Actions Deploy Workflow — Detail

## Workflow

```yaml
# .github/workflows/deploy.yml
name: Deploy to Production

on:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync
      - run: uv run ruff check src/ tests/
      - run: uv run mypy src/
      - run: uv run pytest

  deploy:
    needs: test
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    steps:
      - name: Deploy via SSH
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          script: |
            set -e
            cd /opt/course-supporter

            echo "=== Pull latest ==="
            git pull origin main

            echo "=== Build ==="
            docker compose -f docker-compose.prod.yaml build app

            echo "=== Deploy ==="
            docker compose -f docker-compose.prod.yaml up -d app

            echo "=== Migrations ==="
            docker compose -f docker-compose.prod.yaml exec -T app \
                alembic upgrade head

            echo "=== Health check ==="
            sleep 5
            curl -sf http://localhost:8000/health || exit 1

            echo "=== Deploy complete: $(date) ==="
```

## GitHub Secrets

Налаштувати в Settings → Secrets → Actions:

| Secret | Опис |
|---|---|
| `VPS_HOST` | IP або hostname VPS |
| `VPS_USER` | SSH user (не root) |
| `VPS_SSH_KEY` | Private SSH key для deploy |

### SSH Key Setup

```bash
# На VPS:
ssh-keygen -t ed25519 -f ~/.ssh/deploy_key -N ""
cat ~/.ssh/deploy_key.pub >> ~/.ssh/authorized_keys

# Скопіювати ~/.ssh/deploy_key → GitHub Secret VPS_SSH_KEY
```

## VPS Setup

```bash
# Початкова підготовка (один раз):
sudo mkdir -p /opt/course-supporter
sudo chown $USER:$USER /opt/course-supporter
cd /opt/course-supporter
git clone git@github.com:<org>/course-supporter.git .
cp .env.prod.example .env.prod
# Заповнити .env.prod
```

## Rollback

```bash
# На VPS при проблемах:
cd /opt/course-supporter
git log --oneline -5           # знайти попередній commit
git checkout <prev-commit>
docker compose -f docker-compose.prod.yaml build app
docker compose -f docker-compose.prod.yaml up -d app
docker compose -f docker-compose.prod.yaml exec app alembic downgrade -1
```

## Definition of Done

- [ ] Workflow файл створено
- [ ] GitHub Secrets налаштовані
- [ ] Push to main → автодеплой
- [ ] Health check після deploy
- [ ] Документ оновлений відповідно до фінальної реалізації
