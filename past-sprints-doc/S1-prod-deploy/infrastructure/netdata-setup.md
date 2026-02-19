# Netdata Monitoring Setup

Netdata provides real-time system monitoring with a web dashboard and Telegram alerts.

## Architecture

```
Browser → nginx (/netdata/) → basic auth → netdata:19999
                                              │
                                    ┌─────────┼─────────┐
                                    │         │         │
                                  /proc    /sys    docker.sock
                                  (CPU,    (disk,   (container
                                   RAM)    net)     metrics)
```

- Dashboard: `https://api.pythoncourse.me/netdata/`
- Protected by HTTP basic auth
- Memory limit: 200 MB
- No Netdata Cloud telemetry (`DO_NOT_TRACK=1`)

## Step 1: Verify Netdata is Running

After `docker compose -f docker-compose.prod.yaml up -d`:

```bash
docker compose -f docker-compose.prod.yaml ps netdata
# Status: running

docker compose -f docker-compose.prod.yaml logs netdata --tail=20
# Should show "NETDATA AGENT STARTED" or similar
```

## Step 2: Create Basic Auth for Nginx

```bash
# Install htpasswd utility (on VPS, not in container)
sudo apt-get install -y apache2-utils

# Create password file (inside nginx container's mounted volume)
# Adjust path to your nginx config directory
htpasswd -c /path/to/nginx/.htpasswd_netdata admin
# Enter password when prompted

# Or create directly in nginx container:
docker exec -it <nginx-container> sh -c \
    'apk add apache2-utils && htpasswd -c /etc/nginx/.htpasswd_netdata admin'
```

Reload nginx after creating the password file:

```bash
docker exec <nginx-container> nginx -s reload
```

Verify access: open `https://api.pythoncourse.me/netdata/` — should prompt for credentials.

## Step 3: Configure Telegram Alerts

### 3.1 Create Telegram Bot

1. Open Telegram, search for `@BotFather`
2. Send `/newbot`
3. Name: `course-supporter-alerts`
4. Save the **bot token** (format: `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)

### 3.2 Get Chat ID

1. Send any message to your new bot
2. Open in browser:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
3. Find `"chat":{"id": 123456789}` — that's your **chat_id**

### 3.3 Copy Alert Config

Edit the template file with your credentials:

```bash
cd /opt/course-supporter

# Edit template
nano deploy/netdata/health_alarm_notify.conf
# Replace <your-bot-token> and <your-chat-id>

# Copy into netdata container
docker cp deploy/netdata/health_alarm_notify.conf \
    netdata:/etc/netdata/health_alarm_notify.conf
```

### 3.4 Copy Custom Alert Thresholds

```bash
docker cp deploy/netdata/custom-alerts.conf \
    netdata:/etc/netdata/health.d/custom.conf
```

### 3.5 Restart Netdata

```bash
docker restart netdata
```

### 3.6 Test Alerts

```bash
docker exec netdata \
    /usr/libexec/netdata/plugins.d/alarm-notify.sh test
```

You should receive a test message in Telegram within a few seconds.

## Monitored Metrics

| Metric | Default Alert | Custom Threshold |
|--------|---------------|------------------|
| Disk space | < 20% free | > 80% warn, > 90% crit |
| RAM usage | Built-in | > 85% warn, > 95% crit |
| CPU | Sustained high | Default |
| Docker containers | Restart detection | Default |
| Network I/O | Built-in | Default |
| PostgreSQL | Connections (if plugin available) | Default |

## Maintenance

### Check Netdata Memory Usage

```bash
docker stats netdata --no-stream
# MEM USAGE should be < 200 MB
```

### Update Netdata

```bash
docker compose -f docker-compose.prod.yaml pull netdata
docker compose -f docker-compose.prod.yaml up -d netdata
```

Note: after update, re-copy config files if using named volumes (configs persist in `netdata-config` volume).

### Disable Netdata (if needed)

```bash
docker compose -f docker-compose.prod.yaml stop netdata
```

This does not affect the API — netdata is fully independent.
