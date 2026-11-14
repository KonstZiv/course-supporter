#!/usr/bin/env bash
# scripts/smoke_test.sh — Post-deploy verification
#
# Usage:
#   ./scripts/smoke_test.sh <base_url> <api_key>
#
# Example:
#   ./scripts/smoke_test.sh https://api.pythoncourse.me cs_live_abc123...

set -euo pipefail

# --- Prerequisite check: fail fast if a required tool is missing ---
for tool in curl python3; do
    command -v "$tool" >/dev/null || { echo "❌ required tool missing: $tool"; exit 2; }
done

BASE_URL="${1:?Usage: smoke_test.sh <base_url> <api_key>}"
API_KEY="${2:?Usage: smoke_test.sh <base_url> <api_key>}"

PASSED=0
FAILED=0

check() {
    local name="$1"
    local expected_code="$2"
    local actual_code="$3"

    if [ "$actual_code" -eq "$expected_code" ]; then
        echo "  ✅ $name (HTTP $actual_code)"
        PASSED=$((PASSED + 1))
    else
        echo "  ❌ $name (expected $expected_code, got $actual_code)"
        FAILED=$((FAILED + 1))
    fi
}

echo "🔍 Smoke Test: $BASE_URL"
echo "================================"

# --- 1. Health Check ---
echo ""
echo "1. Health Check"
HEALTH_FILE=$(mktemp)
trap 'rm -f "$HEALTH_FILE"' EXIT
CODE=$(curl -s -o "$HEALTH_FILE" -w "%{http_code}" "$BASE_URL/health")
check "GET /health returns 200" 200 "$CODE"

HEALTH=$(cat "$HEALTH_FILE")

# Validate that /health returned parseable JSON
if ! printf '%s' "$HEALTH" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>&1; then
    echo "  ❌ /health response is not valid JSON"
    FAILED=$((FAILED + 1))
    HEALTH=""
fi

if [ -n "$HEALTH" ]; then
    DB_STATUS=$(printf '%s' "$HEALTH" | python3 -c 'import json,sys; print(json.load(sys.stdin)["checks"]["db"])')
    S3_STATUS=$(printf '%s' "$HEALTH" | python3 -c 'import json,sys; print(json.load(sys.stdin)["checks"]["s3"])')
else
    DB_STATUS="unknown (invalid JSON)"
    S3_STATUS="unknown (invalid JSON)"
fi

if [ "$DB_STATUS" = "ok" ]; then
    echo "  ✅ DB connectivity: ok"
    PASSED=$((PASSED + 1))
else
    echo "  ❌ DB connectivity: $DB_STATUS"
    FAILED=$((FAILED + 1))
fi

if [ "$S3_STATUS" = "ok" ]; then
    echo "  ✅ S3 connectivity: ok"
    PASSED=$((PASSED + 1))
else
    echo "  ❌ S3 connectivity: $S3_STATUS"
    FAILED=$((FAILED + 1))
fi

# --- 2. Auth ---
echo ""
echo "2. Authentication"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/nodes")
check "GET /nodes without key → 401" 401 "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "X-API-Key: cs_live_invalid_key_000000000000" \
    "$BASE_URL/api/v1/nodes")
check "GET /nodes with invalid key → 401" 401 "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "X-API-Key: $API_KEY" \
    "$BASE_URL/api/v1/nodes")
check "GET /nodes with valid key → 200" 200 "$CODE"

# --- 3. API Documentation ---
echo ""
echo "3. API Documentation"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/docs")
check "GET /docs → 404" 404 "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/openapi.json")
check "GET /openapi.json → 200" 200 "$CODE"

# --- 4. Security Headers ---
echo ""
echo "4. Security Headers"
HEADERS=$(curl -sI -H "X-API-Key: $API_KEY" "$BASE_URL/api/v1/nodes")

if echo "$HEADERS" | grep -qi "x-content-type-options: nosniff"; then
    echo "  ✅ X-Content-Type-Options: nosniff"
    PASSED=$((PASSED + 1))
else
    echo "  ❌ X-Content-Type-Options header missing"
    FAILED=$((FAILED + 1))
fi

if echo "$HEADERS" | grep -qi "x-frame-options:.*\(deny\|sameorigin\)"; then
    echo "  ✅ X-Frame-Options present"
    PASSED=$((PASSED + 1))
else
    echo "  ❌ X-Frame-Options header missing"
    FAILED=$((FAILED + 1))
fi

# --- 5. Response Format ---
echo ""
echo "5. Response Format"
NODES=$(curl -s -H "X-API-Key: $API_KEY" "$BASE_URL/api/v1/nodes")
if printf '%s' "$NODES" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>&1; then
    echo "  ✅ Response is valid JSON"
    PASSED=$((PASSED + 1))
else
    echo "  ❌ Response is not valid JSON"
    FAILED=$((FAILED + 1))
fi

# --- Summary ---
echo ""
echo "================================"
TOTAL=$((PASSED + FAILED))
echo "Results: $PASSED/$TOTAL passed"

if [ "$FAILED" -gt 0 ]; then
    echo "❌ SMOKE TEST FAILED ($FAILED failures)"
    exit 1
else
    echo "✅ ALL SMOKE TESTS PASSED"
    exit 0
fi
