# ADA Assistant for VS Code (experimental)

**Status: experimental.** A ~150-line, dependency-free extension that keeps an eye on a
running [ai-dev-assistant](../../README.md) server from the VS Code status bar. It is
deliberately thin: the web console remains the full UI; this covers the "a run is
blocked on me" loop without leaving the editor.

## What it does

- Polls `GET /api/home` every 10 seconds and shows
  `◆ ADA: 2 running / 1 need you` in the status bar — with the warning background
  when anything needs your input. Server absent? It quietly reads `ADA: offline`.
- **ADA: Show Attention** (also: click the status bar item) — QuickPick of open
  clarifying questions and permission requests across all running tasks.
- **ADA: Answer Attention Item** — jumps straight into answering (skips the list
  when exactly one item is open). Questions offer the agent's proposed options as a
  QuickPick plus a free-text InputBox; permissions offer Allow once / Allow for this
  run / Deny. Answers are delivered over the same steer endpoint the web console
  uses, so the run resumes immediately.
- **ADA: Open Console** — opens `<baseUrl>/app` in your browser.

## Install

No build step. From this directory, either:

- **Run it from source:** open this folder in VS Code and press **F5**
  ("Run Extension" launches an Extension Development Host), or
- **Package and install:**

  ```sh
  npx vsce package                # produces ada-assistant-0.0.1.vsix
  code --install-extension ada-assistant-0.0.1.vsix
  ```

## Settings

| Setting       | Default                  | Meaning                                        |
| ------------- | ------------------------ | ---------------------------------------------- |
| `ada.baseUrl` | `http://127.0.0.1:8000`  | Where the ADA server lives.                    |
| `ada.token`   | *(empty)*                | Sent as `Authorization: Bearer <token>`.       |

It works against **remote deployments** too: point `ada.baseUrl` at the deployment and
set `ada.token` to your API token (the server prints one when bound non-loopback; see
`docs/DEPLOYMENT.md`). A local loopback server without auth needs no token.
