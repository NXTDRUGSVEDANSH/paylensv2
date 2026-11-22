# syntax=docker/dockerfile:1.7
# ──────────────────────────────────────────────────────────────────────────────
# PayLens — immutable, stateless container.
#
# The whole app can be baked: DuckDB is opened READ-ONLY and the Chroma cache is
# deterministic from semantic/seeds.yaml, so data and cache are build artifacts,
# not runtime state. That means no EFS, no RDS, no volume — scale by adding tasks.
#
#   docker build -t paylens .                        # full (with semantic cache)
#   docker build -t paylens --build-arg WITH_KB=0 .  # slim, ~270 MB smaller
# ──────────────────────────────────────────────────────────────────────────────
ARG PYTHON_VERSION=3.12-slim-bookworm

# ── stage 1: build the data and the cache ────────────────────────────────────
FROM python:${PYTHON_VERSION} AS builder
ARG WITH_KB=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build

# Generator deps (pandas/pyarrow/numpy) live ONLY in this stage.
COPY requirements.txt requirements-kb.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$WITH_KB" = "1" ]; then pip install --no-cache-dir -r requirements-kb.txt; fi

COPY data/ ./data/
COPY semantic/ ./semantic/
COPY backend/ ./backend/
COPY paylens2/ ./paylens2/

# Deterministic: seed=42. Verified in the same layer, so a build FAILS rather
# than shipping a dataset whose calibration has drifted.
RUN python data/generate.py && python data/verify.py

# The views are part of the image, not something the container does on boot.
# Without them every question is refused with "the views are not built", and
# building them at startup would need write access to a database the runtime
# opens read-only. This step is why that is never a problem.
RUN python -m paylens2.schema.build

# Pre-build the definitional corpus AND warm the ONNX embedding model into
# ~/.cache/chroma, so no container downloads a 167 MB model on its first user
# request. The few-shot corpus starts empty by design and fills from use —
# retrieval degrades to a schema-only prompt, which is a perfectly good prompt.
ENV CHROMA_PATH=/build/data/chroma_db
RUN if [ "$WITH_KB" = "1" ]; then \
      python -c "import sys; sys.path.insert(0,'.'); \
from backend.kb import store; store.warm()" \
   && python -m paylens2.knowledge.build; \
    else mkdir -p /build/data/chroma_db && mkdir -p /root/.cache/chroma; fi

# ── stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION} AS runtime
ARG WITH_KB=1
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    DUCKDB_MEMORY_LIMIT=512MB \
    DUCKDB_PATH=/app/data/paylens.duckdb \
    CHROMA_PATH=/app/data/chroma_db

RUN useradd --create-home --uid 10001 paylens
WORKDIR /app

COPY requirements-runtime.txt requirements-kb.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt \
 && if [ "$WITH_KB" = "1" ]; then pip install --no-cache-dir -r requirements-kb.txt; \
    else echo "DISABLE_CHROMA=true recommended for this build"; fi

COPY --chown=paylens:paylens backend/   ./backend/
COPY --chown=paylens:paylens paylens2/  ./paylens2/
COPY --chown=paylens:paylens semantic/  ./semantic/
COPY --chown=paylens:paylens --from=builder /build/data/paylens.duckdb ./data/paylens.duckdb
COPY --chown=paylens:paylens --from=builder /build/data/chroma_db      ./data/chroma_db
COPY --chown=paylens:paylens --from=builder /root/.cache/chroma        /home/paylens/.cache/chroma

USER paylens
EXPOSE 8000

# App Runner and ALB both poll this. It asserts the container is SERVICEABLE,
# not merely alive: `views_built` is what separates a process that is
# listening from one that will refuse every question it receives.
#
# The previous version tested `status == 'ok'`. /health has never returned a
# `status` field, so the expression was always false and every container would
# have been marked unhealthy from the moment it started — and on ECS, where
# the same check indexed the key directly, it raised KeyError and the task was
# killed and replaced in a loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import os,urllib.request,json,sys; \
u=f'http://127.0.0.1:{os.getenv(\"PORT\",\"8000\")}/health'; \
d=json.load(urllib.request.urlopen(u, timeout=4)); \
sys.exit(0 if d.get('views_built') else 1)"

# One worker per container: the L0/L2 caches are in-process, so extra workers
# would each keep a separate copy and cut the hit rate. Scale with tasks.
CMD ["sh", "-c", "exec uvicorn paylens2.api.app:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 65"]
