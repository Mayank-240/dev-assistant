# syntax=docker/dockerfile:1
# ---- AI Dev Assistant container --------------------------------------------
# The container is the supported isolation boundary for this app: the in-process
# subprocess "sandbox" (env scrub + rlimits + process-group kill) does NOT give
# filesystem or network isolation — running the whole server in a container does.
# See docs/DEPLOYMENT.md for the full deployment guide (compose, volumes, auth).
#
# Build & run:
#   docker build -t ai-dev-assistant .
#   docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-... -v ada-data:/data ai-dev-assistant
#
# Use the 'anthropic' backend in a container (the Claude Agent SDK backend needs
# an interactive Claude Code login, which isn't available here).
#
# Security (S8): the server binds 0.0.0.0, so bearer-token auth is ON by default.
# Set ADA_API_TOKEN to choose the token; otherwise one is auto-generated at startup
# and printed to the container logs on a line starting with
#   [ai-dev-assistant] API token: ...
# (find it with `docker logs <container> | grep "API token"`).

# ---------------------------------------------------------------------------
# Stage 1: build — resolve the locked dependency set with uv into /app/.venv.
# uv, the resolver cache, and any build machinery stay in this stage only.
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependency layer: cached until pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Project layer: install the package itself (non-editable, so the venv is
# self-contained and the runtime stage only needs /app/.venv).
COPY README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------------------
# Stage 2: runtime — slim Python, git, the prebuilt venv. No uv, no compilers.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="AI Dev Assistant" \
      org.opencontainers.image.description="Multi-agent AI dev assistant: FastAPI web server (uvicorn, app factory web.server:create_app)" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.base.name="docker.io/library/python:3.12-slim-bookworm"

# git is required at runtime: repo materialization (clone/fetch) and the
# worktree-based branch/commit delivery features shell out to it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user (uid/gid 1000). All mutable state lives under /data;
# the three state dirs are pre-created and chowned so both anonymous volumes and
# named-volume first mounts inherit app-writable ownership.
RUN groupadd --gid 1000 app \
    && useradd --create-home --uid 1000 --gid app app \
    && mkdir -p /data/.ada_data /data/workspace /data/docs \
    && chown -R app:app /data

COPY --from=build --chown=app:app /app/.venv /app/.venv

# Runtime defaults (override with -e / a compose `environment:` block):
#   ADA_LLM_BACKEND=anthropic   the API backend; the claude_sdk default needs an
#                               interactive login that containers don't have
#   ADA_EMBEDDINGS_BACKEND=hash no model downloads at runtime (fastembed would
#                               fetch a model on first use)
#   ADA_BIND_HOST=0.0.0.0       non-loopback bind => bearer-token auth is forced
#                               on (auto-generated unless ADA_API_TOKEN is set)
#   ADA_*_DIR                   all writable state routed onto the /data volume
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ADA_LLM_BACKEND=anthropic \
    ADA_EMBEDDINGS_BACKEND=hash \
    ADA_BIND_HOST=0.0.0.0 \
    ADA_DATA_DIR=/data/.ada_data \
    ADA_DOCS_DIR=/data/docs \
    ADA_WORKSPACE_DIR=/data/workspace

WORKDIR /app
USER app

VOLUME ["/data"]
EXPOSE 8000

# Liveness against the real /healthz endpoint (stdlib only — no curl in slim).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"]

# W6: the app is built lazily via the factory — importing the module does nothing.
CMD ["uvicorn", "ai_dev_assistant.web.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
