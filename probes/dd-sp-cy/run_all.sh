#!/usr/bin/env bash
# DD-SP-CY: run the probe matrix and the minimal repro on every image below,
# then rebuild results/summary.md. Needs a running Docker daemon and `uv sync`.
# Each image gets a throwaway container on its own port; all are removed at the end.
#
# Usage (from the repo root): bash probes/dd-sp-cy/run_all.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RESULTS="$HERE/results"
mkdir -p "$RESULTS"

# label|image|host port
TARGETS=(
  "17.7|postgres:17.7|55007"
  "17.8|postgres:17.8|55008"
  "pgvector-0.8.1-pg17|pgvector/pgvector:0.8.1-pg17|55108"
  "17.9|postgres:17.9|55009"
  "17.10|postgres:17.10|55010"
  "17.11|postgres:17.11|55011"
)

cleanup() {
  for t in "${TARGETS[@]}"; do docker rm -f "ddspcy-${t%%|*}" >/dev/null 2>&1 || true; done
}
trap cleanup EXIT

for t in "${TARGETS[@]}"; do
  IFS='|' read -r label image port <<<"$t"
  name="ddspcy-$label"
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" -e POSTGRES_PASSWORD=probe -p "$port:5432" "$image" >/dev/null
  until docker exec "$name" pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1; do sleep 1; done
  echo "== $label ($image): $(docker exec -u postgres "$name" psql -Atc 'SELECT version()')"
  uv run python "$HERE/probe.py" --port "$port" --out "$RESULTS/$label.csv"
  docker exec -i -u postgres "$name" psql -X -q -v ON_ERROR_STOP=0 \
    <"$HERE/minimal_repro.sql" >"$RESULTS/minimal_repro_$label.out" 2>&1
done

(cd "$HERE" && uv run python summarize.py >/dev/null)
echo "Summary: $RESULTS/summary.md"
