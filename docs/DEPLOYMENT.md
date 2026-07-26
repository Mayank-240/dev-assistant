# Deployment

The container is the supported production deployment path for AI Dev Assistant.
This guide covers why, and how to run it well.

## Threat model: why the container is the isolation boundary

The app executes LLM-directed work: it materializes repositories, runs agent
file edits in a workspace, and (by default, `ADA_ALLOW_RUN_COMMAND=true`) runs
shell commands such as test suites on behalf of agents.

The in-process "sandbox" around those subprocesses (`ADA_SANDBOX=subprocess`,
the default) provides **containment of accidents, not isolation**:

- environment scrubbing (secrets like `ANTHROPIC_API_KEY` are not inherited),
- resource limits (`ADA_SANDBOX_CPU_SECONDS`, `ADA_SANDBOX_MEM_MB` via rlimits),
- process-group kill on timeout/cancel.

It does **not** provide filesystem isolation (a spawned command can read any
world-readable file the server's user can) or network isolation (it can reach
anything the host can). A malicious or confused agent-generated command is only
truly confined by an OS-level boundary around the whole server.

That boundary is the container: the process tree, filesystem view, and (with
the shipped compose hardening) an immutable root filesystem, dropped
capabilities, and `no-new-privileges` all live inside it. Anything an agent
subprocess does is confined to the container's filesystem (only `/data` and
`/tmp` are writable) and the container's network namespace.

## Quickstart

**Prebuilt image (no clone needed)** — published to GHCR by CI on every commit
to main (`.github/workflows/docker.yml`):

```sh
docker run -d --name ada -p 8000:8000 -v ada-data:/data \
  -e ANTHROPIC_API_KEY=sk-ant-... ghcr.io/mayank-240/dev-assistant:latest
```

**From a clone** (hardened compose: read-only rootfs, dropped capabilities,
named volumes):

```sh
docker compose up -d --build
```

The image binds `0.0.0.0` inside the container, and any non-loopback bind
forces bearer-token auth on. If you did not set `ADA_API_TOKEN`, a token is
auto-generated at startup and printed once to the container logs:

```sh
docker compose logs ada | grep "API token"
# [ai-dev-assistant] API token: <token>
```

Then:

```sh
curl -H "Authorization: Bearer <token>" http://localhost:8000/api/tasks
```

The landing page is at `http://localhost:8000/` and the console at
`http://localhost:8000/app` (both shells and `/static` are open;
every `/api/*` call and WebSocket needs the token). The UI shows a sign-in
screen: paste the token once and an HttpOnly `ada_token` session cookie takes
over for all fetches, WebSockets and downloads. Non-browser clients keep using
`Authorization: Bearer <token>` (or `?token=<token>` on WebSockets/downloads).
`/healthz` and `/readyz` are unauthenticated.

Set `ANTHROPIC_API_KEY` in your shell or a `.env` file next to
`docker-compose.yml`; compose passes it through. The image defaults to
`ADA_LLM_BACKEND=anthropic` because the `claude_sdk` backend requires an
interactive Claude Code login that a container does not have.

## Pinning the API token

The auto-generated token changes on every container start (it is generated in
`create_app`, not persisted). For anything beyond a quick trial, pin it:

```yaml
    environment:
      - ADA_API_TOKEN=<long random string>   # e.g. openssl rand -hex 32
```

or put `ADA_API_TOKEN=...` in the compose `.env` file and reference it as
`- ADA_API_TOKEN=${ADA_API_TOKEN}`. A pinned token survives restarts, works
with orchestration that can't scrape logs, and keeps the token out of `docker
logs` entirely.

## Remote access & TLS

To reach the assistant from another machine:

- **`ADA_BIND_HOST`** — the server binds `127.0.0.1` by default. Set
  `ADA_BIND_HOST=0.0.0.0` (or a specific interface) to accept remote
  connections; any non-loopback bind forces bearer-token auth on.
- **`ADA_API_TOKEN`** — the token remote clients must present. If unset, one is
  auto-generated and printed once at startup (see "Pinning the API token"
  above). In the browser, paste it into the sign-in screen once — an HttpOnly
  session cookie handles everything after that.
- **`ADA_COOKIE_SECURE=1`** — set this when the server is reached over HTTPS.
  It marks the session cookie `Secure`, so browsers never send it over plain
  HTTP. Always set it behind a TLS-terminating proxy.

The server speaks plain HTTP; put a reverse proxy in front for TLS. Caddy
provisions certificates automatically with three lines:

```
ada.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

With Docker, publish the port to loopback only so the proxy is the sole way in:
`-p 127.0.0.1:8000:8000` (a bare `-p 8000:8000` exposes the container on every
host interface, bypassing the proxy).

Note on credentials: the default LLM backend is the Claude Agent SDK, which
uses the host's Claude Code login — no `ANTHROPIC_API_KEY` is needed, and
`ADA_API_TOKEN` is unrelated to Anthropic credentials. It only guards access
to this server's API. (The container image is the exception: it switches to
`ADA_LLM_BACKEND=anthropic` with an `ANTHROPIC_API_KEY`, as covered in the
Quickstart, because a container has no interactive Claude Code login.)

## Volumes: what state lives where

| Volume | Mount | Contents |
|---|---|---|
| `ada_data` | `/data/.ada_data` | Run store (`runs.db` SQLite + WAL), memory, knowledge base/graph state. Lose this and you lose run history and learned context. |
| `ada_workspace` | `/data/workspace` | Agent working tree: materialized repo checkouts, git worktrees, file edits in progress. Safe to wipe between projects; losing it mid-run loses uncommitted work. |
| `ada_docs` | `/data/docs` | Generated documentation output. |

These are the only writable paths the app uses (plus `/tmp`). The image
pre-creates all three under `/data` owned by the runtime user (uid/gid 1000),
so named volumes inherit correct ownership on first mount. If you bind-mount
host directories instead of named volumes, `chown -R 1000:1000` them first.

## Hardening in the shipped compose file

`docker-compose.yml` runs the service with:

- `read_only: true` — immutable root filesystem. **Verified working**: the app
  routes all writes to `/data` (volumes) and `/tmp` (tmpfs); startup, auth,
  the run-store SQLite (WAL files included) and API all function with the
  rootfs read-only. If a future change hits `Read-only file system`, route the
  new write onto `/data` or `/tmp` rather than dropping this flag.
- `tmpfs: /tmp` (`rw,noexec,nosuid,size=256m`) — scratch space; also what the
  inner subprocess hardening and git use for temp files.
- `security_opt: no-new-privileges:true` — no setuid escalation inside.
- `cap_drop: ALL` — the server needs no Linux capabilities.

The container itself runs as the unprivileged `app` user (uid 1000), never
root, and the final image contains no build toolchain — just Python, git, and
the locked dependency set.

## Resource limits

Agent runs can spawn test suites and git operations; cap the container rather
than relying only on the inner rlimits:

```yaml
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: 4g
```

(With plain `docker run`: `--cpus 2 --memory 4g`.) The inner per-subprocess
limits remain configurable via `ADA_SANDBOX_CPU_SECONDS` (default 60) and
`ADA_SANDBOX_MEM_MB` (default 1024), and `ADA_BUDGET_USD` caps LLM spend.

## Command sandboxing

The shell commands agents run (test suites, `run_command`, verification) go
through `execution.py`, which supports three isolation tiers selected by the
**Sandbox backend** setting (settings console → Execution & Safety, or
`ADA_SANDBOX`):

- **subprocess** (default) — scrubbed environment (no secrets inherited),
  CPU/memory rlimits, process-group kill on timeout. No filesystem or network
  isolation: a command can still read world-readable files and reach the
  network.
- **bwrap** — additionally wraps each command in bubblewrap (Linux only):
  read-only system dirs, only the workspace writable, private `/tmp`, and no
  network unless **Sandbox network** (`ADA_SANDBOX_NET=1`) allows it. Does not
  defend against kernel exploits or protect files inside the workspace itself.
- **container** — runs each command in a throwaway Docker container with only
  the workspace mounted and `--network=none` unless allowed; the image comes
  from **Sandbox image** (`ADA_SANDBOX_IMAGE`), defaulting per command to
  `python:3.12-slim` / `debian:stable-slim`. Strongest tier, but the Docker
  daemon itself is a privileged host service — protect its socket.

If the chosen tier's binary (`bwrap`/`docker`) is missing, execution falls
back to the subprocess tier with a one-time warning — the guarantee degrades,
it never blocks the run. Settings-console changes are bound at run start, so
they apply to **new runs**, not commands already in flight.

Note the scope: this sandboxes only the shell commands agents spawn. The
Claude Agent SDK model loop itself stays on the host process and keeps using
your Claude Code login — no API key is involved, and nothing about it moves
into bwrap or Docker.

## Inner sandbox backends inside the container

`ADA_SANDBOX` selects the per-subprocess isolation tier: `subprocess`
(default), `bwrap`, or `container`. **Inside this container, leave it on
`subprocess`.** The container is already the filesystem/network boundary, so
the stronger inner tiers buy little here:

- `ADA_SANDBOX=bwrap` — bubblewrap is not installed in the image, and user
  namespaces are typically unavailable to an unprivileged, `cap_drop: ALL`
  container anyway. The app would log a one-time warning and fall back to
  subprocess hardening.
- `ADA_SANDBOX=container` — this backend shells out to `docker run`, i.e.
  Docker-in-Docker. The image ships no docker CLI, so it also falls back with
  a warning. Do **not** "fix" this by mounting `/var/run/docker.sock`: socket
  access is root-equivalent on the host and would hand agent subprocesses a
  bigger privilege than the container was deployed to take away.

Use `bwrap`/`container` only when running the server directly on a host,
un-containerized — there they add real isolation the bare process lacks.

## Operational notes

- **Health**: the image has a `HEALTHCHECK` against `/healthz` (30s interval);
  `docker ps` shows `healthy`/`unhealthy`, and orchestrators can gate on it.
- **Backend/embeddings defaults**: the image sets
  `ADA_EMBEDDINGS_BACKEND=hash` so nothing is downloaded at runtime. Switching
  to `fastembed` will try to fetch a model on first use — it needs egress and
  a writable cache (point the cache at `/data` or `/tmp` under `read_only`).
- **Upgrades**: `docker compose up -d --build` rebuilds and restarts; state
  survives in the named volumes. On restart, runs orphaned by the shutdown are
  marked interrupted by the run store's startup cleanup.
