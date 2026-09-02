# ── Build stage ──
FROM python:3.13-slim AS builder

WORKDIR /build

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install dependencies (cached layer)
COPY pyproject.toml uv.lock .python-version ./
ENV UV_LINK_MODE=copy
RUN uv sync --no-dev --frozen --no-install-project

# Copy application code
COPY src/ src/
COPY config/ config/
COPY prompts/ prompts/
COPY migrations/ migrations/
COPY alembic.ini .
COPY scripts/ scripts/

# ── Runtime stage ──
FROM python:3.13-slim

# System dependencies: libpq for psycopg, ffmpeg for ingestion,
# libmagic for security file-type detection (KD14).
#
# libmagic is PINNED to 1:5.47-4 from sid. Trixie ships 1:5.46-5, whose
# ``magic_buffer`` entry point does not recognise a plain zip -- it answers
# ``application/octet-stream`` / ``data`` where ``magic_file`` on the same bytes
# answers ``application/zip`` (Debian #1102577; the cherry-pick in -5 does not
# cover this path). Stage 1 reads every upload through ``magic.from_buffer``, so
# on 5.46 no student archive could pass the door: the whole homework archive
# branch was dead in production while every local check stayed green on the
# host's own libmagic build.
#
# Verified 2026-09-02 against deb.debian.org (vision-rules#17): sid and forky
# both carry 1:5.47-4; trixie-backports carries no ``file`` package at all;
# trixie-updates / trixie-security carry only 1:5.46-5. Installing these two
# from sid pulls exactly two packages and leaves libc6 at trixie's version.
# The exact ``=1:5.47-4`` is deliberate rather than a floating pin: sid moves,
# and a detector that changes under us without a rebuild is precisely the
# failure mode being fixed here.
#
# Remove the pin once trixie itself carries >= 5.47 -- recheck by 2026-12-01.
RUN printf 'Types: deb\nURIs: http://deb.debian.org/debian\nSuites: sid\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.pgp\n' \
        > /etc/apt/sources.list.d/sid.sources \
    && printf 'Package: *\nPin: release a=unstable\nPin-Priority: 100\n\nPackage: libmagic1t64 libmagic-mgc\nPin: release a=unstable\nPin-Priority: 990\n' \
        > /etc/apt/preferences.d/libmagic-sid \
    && apt-get update && apt-get install -y --no-install-recommends \
        libpq5 curl ffmpeg \
        libmagic1t64=1:5.47-4 libmagic-mgc=1:5.47-4 \
    && rm -f /etc/apt/sources.list.d/sid.sources /etc/apt/preferences.d/libmagic-sid \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r app && useradd -r -g app -d /app app
WORKDIR /app
RUN mkdir -p /app/.cache && chown app:app /app/.cache

# Copy virtual environment and application
COPY --from=builder /build/.venv .venv/
COPY --from=builder /build/src src/
COPY --from=builder /build/config config/
COPY --from=builder /build/prompts prompts/
COPY --from=builder /build/migrations migrations/
COPY --from=builder /build/alembic.ini .
COPY --from=builder /build/scripts scripts/

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV XDG_CACHE_HOME="/app/.cache"

# Build-time format gate (same discipline as ``libreoffice --version`` in
# Dockerfile.worker): a detector that cannot see an accepted format must fail
# the BUILD, not surface months later as a student's rejected submission. It
# runs HERE, in the runtime stage, because only this stage carries the libmagic
# that ships -- a gate in the builder stage would certify the wrong library.
# The script generates its fixtures into a temp dir and removes them within
# this same layer, so nothing it touches reaches the shipped image.
#
# ``--image app`` selects this image's exception list from the single
# ``UNPROVABLE`` table in the script: doc / xls / ppt, each printed with its
# reason. There is no LibreOffice here, so no legacy CDFV2 container can be
# generated; the worker image proves ppt, and doc / xls are unreachable formats
# no policy accepts at all.
RUN python scripts/magic_format_gate.py --image app

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "course_supporter.api:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--log-level", "info"]
