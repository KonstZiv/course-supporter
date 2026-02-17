#!/usr/bin/env bash
# scripts/smoke_test.sh — Post-deploy verification
#
# Usage:
#   ./scripts/smoke_test.sh <base_url> <api_key>
#
# Example:
#   ./scripts/smoke_test.sh https://api.pythoncourse.me cs_live_abc123...

set -euo pipefail

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
        ((PASSED++))
    else
        echo "  ❌ $name (expected $expected_code, got $actual_code)"
        ((FAILED++))
    fi
}

echo "🔍 Smoke Test: $BASE_URL"
echo "================================"

# --- 1. Health Check ---
echo ""
echo "1. Health Check"
CODE=$(curl -s -o /tmp/smoke_health.json -w "%{http_code}" "$BASE_URL/health")
check "GET /health returns 200" 200 "$CODE"

DB_STATUS=$(jq -r '.checks.db' /tmp/smoke_health.json 2>/dev/null || echo "unknown")
S3_STATUS=$(jq -r '.checks.s3' /tmp/smoke_health.json 2>/dev/null || echo "unknown")

if [ "$DB_STATUS" = "ok" ]; then
    echo "  ✅ DB connectivity: ok"
    ((PASSED++))
else
    echo "  ❌ DB connectivity: $DB_STATUS"
    ((FAILED++))
fi

if [ "$S3_STATUS" = "ok" ]; then
    echo "  ✅ S3 connectivity: ok"
    ((PASSED++))
else
    echo "  ❌ S3 connectivity: $S3_STATUS"
    ((FAILED++))
fi

# --- 2. Auth ---
echo ""
echo "2. Authentication"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/api/v1/courses")
check "GET /courses without key → 401" 401 "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "X-API-Key: cs_live_invalid_key_000000000000" \
    "$BASE_URL/api/v1/courses")
check "GET /courses with invalid key → 401" 401 "$CODE"

CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "X-API-Key: $API_KEY" \
    "$BASE_URL/api/v1/courses")
check "GET /courses with valid key → 200" 200 "$CODE"

# --- 3. Swagger UI ---
echo ""
echo "3. Swagger UI"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/docs")
check "GET /docs → 200" 200 "$CODE"

# --- 4. Security Headers ---
echo ""
echo "4. Security Headers"
HEADERS=$(curl -sI -H "X-API-Key: $API_KEY" "$BASE_URL/api/v1/courses")

if echo "$HEADERS" | grep -qi "x-content-type-options: nosniff"; then
    echo "  ✅ X-Content-Type-Options: nosniff"
    ((PASSED++))
else
    echo "  ❌ X-Content-Type-Options header missing"
    ((FAILED++))
fi

if echo "$HEADERS" | grep -qi "x-frame-options"; then
    echo "  ✅ X-Frame-Options present"
    ((PASSED++))
else
    echo "  ❌ X-Frame-Options header missing"
    ((FAILED++))
fi

# --- 5. Response Format ---
echo ""
echo "5. Response Format"
COURSES=$(curl -s -H "X-API-Key: $API_KEY" "$BASE_URL/api/v1/courses")
if echo "$COURSES" | jq . > /dev/null 2>&1; then
    echo "  ✅ Response is valid JSON"
    ((PASSED++))
else
    echo "  ❌ Response is not valid JSON"
    ((FAILED++))
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
