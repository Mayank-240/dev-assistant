# syntax=docker/dockerfile:1
# ---- Notes API container ---------------------------------------------------
# Runs the FastAPI notes app (notes_app package) via uvicorn on port 8000.
# Build context is the repo root so the notes_app/ package is importable.
FROM python:3.12-slim AS runtime

# Sensible Python defaults for containers:
#  - no .pyc files, unbuffered stdout/stderr for live logs
#  - pip: no cache, no version-check chatter
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # SQLite DB lives on a writable volume (WAL mode also writes -wal/-shm sidecars here)
    NOTES_DB_PATH=/data/notes.db

WORKDIR /app

# 1) Install dependencies first (cached layer — only re-runs when requirements change)
COPY notes_app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 2) Copy the application package
COPY notes_app/ ./notes_app/

# 3) Run as an unprivileged user and give it ownership of the data dir
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

# SQLite data directory (DB + WAL/SHM sidecars). Mount a volume to persist.
VOLUME ["/data"]

EXPOSE 8000

# Liveness/readiness probe against the app's healthcheck endpoint (no curl needed).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"]

# Start the FastAPI app via uvicorn, bound to all interfaces on the exposed port.
CMD ["uvicorn", "notes_app.app:app", "--host", "0.0.0.0", "--port", "8000"]
