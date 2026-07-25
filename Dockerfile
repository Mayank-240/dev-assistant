# syntax=docker/dockerfile:1
# ---- AI Dev Assistant container --------------------------------------------
# Runs the FastAPI web UI via uvicorn on :8000 (app factory: web.server:create_app).
# Build context is the repo root. Use the 'anthropic' backend in a container (the Claude
# Agent SDK backend needs an interactive Claude Code login, which isn't available here):
#   docker build -t ai-dev-assistant .
#   docker run -p 8000:8000 -e ADA_LLM_BACKEND=anthropic -e ANTHROPIC_API_KEY=sk-... \
#              -v ada-data:/data ai-dev-assistant
#
# Security (S8): the server binds 0.0.0.0, so bearer-token auth is ON by default.
# Set ADA_API_TOKEN to choose the token; otherwise one is auto-generated at startup
# and printed to the container logs on a line starting with
#   [ai-dev-assistant] API token: ...
# (find it with `docker logs <container> | grep "API token"`).
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ADA_LLM_BACKEND=anthropic \
    ADA_EMBEDDINGS_BACKEND=hash \
    ADA_BIND_HOST=0.0.0.0 \
    ADA_DATA_DIR=/data/.ada_data \
    ADA_DOCS_DIR=/data/docs \
    ADA_WORKSPACE_DIR=/data/workspace

WORKDIR /app

# git is needed for the repo-binding + branch/commit delivery features.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# 1) Install the package (deps resolve from pyproject) — cached unless sources change.
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# 2) Run as an unprivileged user; give it the writable data volume.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

VOLUME ["/data"]
EXPOSE 8000

# Liveness/readiness against the real /healthz endpoint (no curl needed).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"]

# W6: the app is built lazily via the factory — importing the module does nothing.
CMD ["uvicorn", "ai_dev_assistant.web.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
