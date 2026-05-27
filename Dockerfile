# syntax=docker/dockerfile:1.7
# =============================================================================
# Stage 1 — builder: install Python deps into a virtualenv
# =============================================================================
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# "local" = sentence-transformers (needs PyTorch), "bedrock" = API-only (no PyTorch)
ARG EMBEDDING_PROVIDER=local

WORKDIR /build

# Build-time system deps (compilers needed for some wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/*

# Create a virtualenv we'll copy to the runtime stage
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install deps - local provider needs PyTorch + sentence-transformers (~4 GB)
COPY requirements-base.txt requirements.txt ./
RUN if [ "$EMBEDDING_PROVIDER" = "local" ]; then \
        pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
     && pip install --no-cache-dir -r requirements.txt; \
    else \
        pip install --no-cache-dir -r requirements-base.txt; \
    fi

# =============================================================================
# Stage 2 — model cache: pre-download embeddings model (local provider only)
# =============================================================================
FROM builder AS model-cache

ARG EMBEDDING_PROVIDER=local
ARG EMBEDDING_MODEL=google/embeddinggemma-300m
ARG HF_TOKEN=""

ENV HF_HOME=/opt/hf-cache \
    SENTENCE_TRANSFORMERS_HOME=/opt/hf-cache/sentence-transformers

RUN if [ "$EMBEDDING_PROVIDER" = "local" ]; then \
        python -c "\
import os; \
from huggingface_hub import login; \
tok = os.environ.get('HF_TOKEN', '').strip(); \
login(token=tok) if tok else None; \
from sentence_transformers import SentenceTransformer; \
SentenceTransformer(os.environ.get('EMBEDDING_MODEL', '$EMBEDDING_MODEL'), trust_remote_code=True)" \
        && find /opt/hf-cache -name '*.bin' -o -name '*.safetensors' | head; \
    else \
        mkdir -p /opt/hf-cache; \
    fi

# =============================================================================
# Stage 3 — runtime: minimal image with only what's needed to run
# =============================================================================
FROM python:3.12-slim AS runtime

ARG EMBEDDING_PROVIDER=local

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/opt/hf-cache \
    SENTENCE_TRANSFORMERS_HOME=/opt/hf-cache/sentence-transformers \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_OFFLINE=1

# Runtime system deps only (git + ssh for cloning private repos at runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        openssh-client \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Copy virtualenv and model cache from build stages
COPY --from=builder /opt/venv /opt/venv
COPY --from=model-cache /opt/hf-cache /opt/hf-cache

# Non-root user — runs as UID 1000
RUN groupadd --gid 1000 app \
 && useradd  --uid 1000 --gid app --shell /bin/bash --create-home app \
 && mkdir -p /tmp/repo_cache \
 && chown -R app:app /opt/hf-cache /tmp/repo_cache

WORKDIR /app

# Copy application code (last layer — changes most often)
COPY --chown=app:app app/        ./app/
COPY --chown=app:app migrations/ ./migrations/
COPY --chown=app:app scripts/    ./scripts/

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# tini = proper PID 1 signal handling for graceful shutdown
ENTRYPOINT ["/usr/bin/tini", "--"]

# Gunicorn with uvicorn workers — production WSGI server
#   --workers 2 × CPU + 1 is the classic formula
#   --worker-class uvicorn.workers.UvicornWorker for ASGI
#   --timeout 120 — long enough for slow LLM streaming
#   --graceful-timeout 30 — let in-flight requests finish on shutdown
CMD ["gunicorn", "app.main:app", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--keep-alive", "5", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
