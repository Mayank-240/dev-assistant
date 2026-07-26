// ADA Assistant — minimal VS Code client for a running ai-dev-assistant server.
// Plain JS, no build step, no dependencies: uses the global fetch of VS Code's
// Node runtime (Node 18+). Defensive by design — when the server is absent the
// status bar quietly reads "ADA: offline" and every command degrades to a toast.
"use strict";
const vscode = require("vscode");

const POLL_MS = 10000;
let statusItem = null;
let pollTimer = null;
let lastHome = null; // last successful /api/home payload (null = offline)

function cfg() {
  const c = vscode.workspace.getConfiguration("ada");
  return {
    baseUrl: (c.get("baseUrl") || "http://127.0.0.1:8000").replace(/\/+$/, ""),
    token: c.get("token") || "",
  };
}

async function api(path, options) {
  const { baseUrl, token } = cfg();
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = "Bearer " + token;
  const resp = await fetch(baseUrl + path, Object.assign({ headers }, options || {}));
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  return resp.json();
}

async function refresh() {
  try {
    lastHome = await api("/api/home");
    const running = (lastHome.running || []).length;
    const attention = (lastHome.attention || []).length;
    statusItem.text = `◆ ADA: ${running} running / ${attention} need you`;
    statusItem.tooltip = "ai-dev-assistant — click to review attention items";
    statusItem.backgroundColor = attention > 0
      ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
  } catch (e) {
    lastHome = null;
    statusItem.text = "ADA: offline";
    statusItem.tooltip = "No ai-dev-assistant server at " + cfg().baseUrl;
    statusItem.backgroundColor = undefined;
  }
}

// Post a steering note in the exact format the web console uses (see web/static/app.js):
//   ask        -> "[answer <id>] <text>"
//   permission -> "[permission <id>] ALLOW ONCE|ALLOW FOR THIS RUN: <request>"  or  "DENIED: <request>"
async function steer(taskId, note) {
  await api(`/api/run/${encodeURIComponent(taskId)}/steer`, {
    method: "POST",
    body: JSON.stringify({ note }),
  });
}

async function answerItem(item) {
  const text = item.question || item.request || "";
  if (item.kind === "permission") {
    const pick = await vscode.window.showQuickPick(
      ["Allow once", "Allow for this run", "Deny"],
      { title: "ADA permission request", placeHolder: text });
    if (!pick) return;
    const denied = pick === "Deny";
    const note = `[permission ${item.id || ""}] ${denied ? "DENIED" : pick.toUpperCase()}: ${text}`;
    await steer(item.task_id, note);
    vscode.window.showInformationMessage(denied ? "ADA: denied — the agent will plan around it" : "ADA: granted");
    return;
  }
  // Clarifying question: offer the agent's options plus free text.
  const options = (item.options || []).map((o) => ({ label: o }));
  options.push({ label: "$(edit) Answer in plain English…", free: true });
  const pick = await vscode.window.showQuickPick(options, { title: "ADA question", placeHolder: text });
  if (!pick) return;
  let answer = pick.label;
  if (pick.free) {
    answer = await vscode.window.showInputBox({ title: "ADA question", prompt: text });
    if (!answer) return;
  }
  await steer(item.task_id, `[answer ${item.id || ""}] ${answer}`);
  vscode.window.showInformationMessage("ADA: answered — the run resumes");
}

async function showAttention() {
  await refresh();
  if (!lastHome) {
    vscode.window.showWarningMessage("ADA: server unreachable at " + cfg().baseUrl);
    return;
  }
  const items = lastHome.attention || [];
  if (!items.length) {
    vscode.window.showInformationMessage("ADA: nothing needs you right now");
    return;
  }
  const picks = items.map((it) => ({
    label: (it.kind === "permission" ? "$(shield) " : "$(question) ") + (it.question || it.request || ""),
    description: it.agent ? "agent: " + it.agent : "",
    detail: `${it.project || "default"} · task ${it.task_id}`,
    item: it,
  }));
  const pick = await vscode.window.showQuickPick(picks, {
    title: "ADA — items needing your input", matchOnDetail: true });
  if (pick) await answerItem(pick.item).catch(err);
}

async function answerAttention() {
  await refresh();
  const items = (lastHome && lastHome.attention) || [];
  if (items.length === 1) { await answerItem(items[0]).catch(err); return; }
  await showAttention(); // zero or many -> same list flow
}

function err(e) {
  vscode.window.showWarningMessage("ADA: could not deliver — " + (e && e.message ? e.message : e));
}

function activate(context) {
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  statusItem.text = "ADA: …";
  statusItem.command = "ada.showAttention";
  statusItem.show();

  context.subscriptions.push(
    statusItem,
    vscode.commands.registerCommand("ada.openConsole", () => {
      vscode.env.openExternal(vscode.Uri.parse(cfg().baseUrl + "/app"));
    }),
    vscode.commands.registerCommand("ada.showAttention", showAttention),
    vscode.commands.registerCommand("ada.answerAttention", answerAttention),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("ada")) refresh();
    }),
  );

  refresh();
  pollTimer = setInterval(refresh, POLL_MS);
}

function deactivate() {
  if (pollTimer) clearInterval(pollTimer);
}

module.exports = { activate, deactivate };
