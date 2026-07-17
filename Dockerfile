# syntax=docker/dockerfile:1.18
FROM python:3.14-alpine3.23@sha256:b165067c5afc37fa5608a3c05609cc3d51aafd808a30fbfd822ee594fef55ad4 AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

RUN python -m pip install --no-cache-dir "uv==0.11.29"
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY app.py run.py spotify_auth.py sync.py ytmusic_auth.py ./
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-alpine3.23@sha256:b165067c5afc37fa5608a3c05609cc3d51aafd808a30fbfd822ee594fef55ad4

LABEL org.opencontainers.image.source="https://github.com/breixopd/music-sync" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.description="Unattended Spotify and YouTube Music library synchronization"

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apk add --no-cache ca-certificates curl ffmpeg tzdata

RUN addgroup -S -g 1000 music-sync \
    && adduser -S -D -H -u 1000 -G music-sync music-sync \
    && mkdir -p /app /config/spotify /config/ytmusic /config/state /music \
    && chown -R music-sync:music-sync /config /music /app

WORKDIR /app
COPY --from=builder --chown=music-sync:music-sync /app/.venv /app/.venv
COPY --chown=music-sync:music-sync app.py run.py spotify_auth.py sync.py ytmusic_auth.py ./

EXPOSE 8845
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8845/health >/dev/null || exit 1
USER music-sync:music-sync
CMD ["python", "/app/run.py"]
