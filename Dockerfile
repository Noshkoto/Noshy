# syntax=docker/dockerfile:1.6
#
# Noshy — persistent memory for AI agents
#
# Build: docker build -t noshy .
# Run:   docker run -d --name noshy -p 8720:8720 -v noshy-data:/data noshy
#
# Optional embedding providers:
#   -e OPENAI_API_KEY=sk-...        # OpenAI embeddings (recommended)
#   -e NOSHY_EMBED_PROVIDER=none    # keyword-only (no embeddings)
#   --build-arg WITH_FASTEMBED=1    # bake local fastembed into the image
#
# Optional HTTP auth:
#   -e NOSHY_HTTP_TOKEN=$(openssl rand -hex 32)

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NOSHY_DB=/data/memories.db

# Layer 1: deps (cached when ARGs don't change)
ARG WITH_FASTEMBED=0
ARG WITH_SQLITE_VEC=1
RUN set -eux; \
    if [ "$WITH_SQLITE_VEC" = "1" ]; then pip install --no-cache-dir 'sqlite-vec>=0.1'; fi; \
    if [ "$WITH_FASTEMBED" = "1" ]; then pip install --no-cache-dir 'fastembed>=0.4'; fi

# Layer 2: source (changes most often, so it sits on top)
WORKDIR /app
COPY store.py embed.py extractor.py context.py hooks.py \
     hermes_skill.py decorator.py noshy.py server.py ./

# Non-root user — Noshy stores its DB in a mounted volume so this is safe
RUN useradd --create-home --uid 1000 noshy && \
    mkdir -p /data && chown -R noshy:noshy /data /app
USER noshy

VOLUME ["/data"]
EXPOSE 8720

# Lightweight health check that hits the public /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; \
import os; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"NOSHY_PORT\",\"8720\")}/health',timeout=3).status==200 else 1)"

ENV NOSHY_PORT=8720
# Bind 0.0.0.0 inside the container; host port-mapping handles exposure
CMD ["sh", "-c", "exec python server.py serve --host 0.0.0.0 --port ${NOSHY_PORT}"]
