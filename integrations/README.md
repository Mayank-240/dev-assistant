# Editor integrations

ai-dev-assistant is a headless server first: everything the web console does goes
through the REST + streaming API, so any editor can integrate with a few HTTP calls.
This directory holds ready-made clients (currently [`vscode/`](vscode/README.md),
experimental) and this page documents the small API surface an editor integration
needs.

## Auth

- Local loopback server, no token configured: no auth needed.
- Otherwise send `Authorization: Bearer <token>` on every request (`ADA_API_TOKEN`,
  or the token the server prints when it binds a non-loopback host). WebSockets and
  download links accept `?token=<token>` instead, since headers aren't always
  available there.

## The three calls that matter

**1. Poll `GET /api/home`** — one read-only JSON for "what needs me?". The relevant
sections (each degrades to its empty default, never a 500):

```jsonc
{
  "running":   [{"task_id": "…", "title": "…", "project": "…"}],
  "attention": [{
    "task_id": "…", "project": "…",
    "kind": "ask",              // or "permission"
    "id": "q-1",                // request id — echo it back in the steer note
    "agent": "coder",
    "question": "Postgres or SQLite?",   // "request" instead when kind=permission
    "options": ["Postgres", "SQLite"]    // may be empty — offer free text then
  }]
}
```

**2. Answer via `POST /api/run/{task_id}/steer`** with body `{"note": "<note>"}` —
the same endpoint and note format the web console uses:

- Clarifying question: `[answer <id>] <the user's answer>`
- Permission grant: `[permission <id>] ALLOW ONCE: <request text>` (or
  `ALLOW FOR THIS RUN: …`)
- Permission denial: `[permission <id>] DENIED: <request text>`

A 404 means the run is no longer accepting input — surface that instead of marking
the item resolved. The same endpoint with a plain-English note (no `[…]` prefix)
queues a general steering instruction for the orchestrator.

**3. Deep-link into the console** for anything richer: open
`<baseUrl>/app#task=<task_id>` in a browser to land on that run's live view (DAG,
transcripts, diffs, permissions).

## Live events (optional)

Instead of polling, stream a run's events over the WebSocket at `/ws/<task_id>`
(append `?token=<token>` when auth is on). Late joiners receive the backlog first,
then live events; `ask` / `permission` events carry the same `id`/`options` payload
shown above. The durable log is also available as plain REST:
`GET /api/tasks/{task_id}/events`.

## Zed (and other editors without an extension)

Zed users: run `ada attend` in a terminal — a small CLI attention loop that fronts
exactly the API above (poll → pick → answer). Anything it does, your editor's task
runner or a custom extension can do with the three calls in this document.
