# PD-016: GitHub Actions Deploy Workflow — Detail

## Workflow

Окремий workflow файл з manual dispatch (без автоматичного тригера на push).
Запуск: GitHub Actions UI → "Deploy to Production" → "Run workflow".

```yaml
# .github/workflows/deploy.yml
name: Deploy to Production

on:
  workflow_dispatch:

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy via SSH
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          script_stop: true
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
            for i in $(seq 1 10); do
              if curl -sf http://localhost:8000/health; then
                echo ""
                echo "Application is healthy."
                break
              fi
              echo "Waiting for application to start... ($i/10)"
              sleep 5
              if [ "$i" -eq 10 ]; then
                echo "Health check failed after 10 retries." >&2
                exit 1
              fi
            done

            echo "=== Deploy complete: $(date) ==="
```

Ключові деталі:
- `workflow_dispatch` — тільки ручний запуск, без auto-deploy на push
- `script_stop: true` — appleboy зупиняє скрипт при першій помилці
- `-T` flag на `exec` — без TTY (CI середовище)
- `curl -sf` — silent + fail on HTTP error

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

- [x] Workflow файл створено (`.github/workflows/deploy.yml`)
- [ ] GitHub Secrets налаштовані
- [ ] Manual dispatch → deploy працює
- [x] Health check після deploy (в скрипті)
- [x] Документ оновлений відповідно до фінальної реалізації
