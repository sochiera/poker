FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_WORKERS=2 \
    LOG_LEVEL=INFO

WORKDIR /app

COPY pyproject.toml README.md ./
COPY planning_poker ./planning_poker
RUN pip install --no-cache-dir . \
    && useradd --system --create-home --uid 10001 poker \
    && chown -R poker:poker /app

USER poker
EXPOSE 8000

# Same endpoint the reverse proxy uses; keeps a wedged container out of rotation.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"

# Several worker processes share their state through Redis, so any worker can
# serve any request. Proxy headers are trusted because only Caddy can reach
# this port (the app is not published outside the compose network).
CMD ["sh", "-c", "exec uvicorn planning_poker.app:app \
    --host 0.0.0.0 --port 8000 \
    --workers ${APP_WORKERS:-2} \
    --proxy-headers --forwarded-allow-ips='*' \
    --log-level $(echo ${LOG_LEVEL:-info} | tr 'A-Z' 'a-z')"]
