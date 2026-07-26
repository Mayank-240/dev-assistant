const $ = (id) => document.getElementById(id);

// Pure helpers live in util.js (loaded first; also unit-tested under Node).
const {
  escapeHtml, escapeAttr, fmtTok, fmtSize, fmtCost, fmtDuration, classifyDiffLine,
  makeAgentRecord, initialRunAggregates, reduceRunEvent, runProgress, formatStepLine,
  clipText, transcriptLineModel, transcriptViewModel,
  computeBudgetMeter, timelineRows, compareRowModel, isResumable,
  projectStatusLine, activityStripModel,
  projectTaglineModel, deliveryControlModel, filterTasksByProject, projectHomeHeaderModel,
  renderDiffHtml, reviewCardModel, permissionsModel, policyFormModel, parsePolicyForm,
  sparklinePoints, childrenFromRows, childrenGridModel,
  sidebarProjectRows, projectTabsModel, projectTaskRows, composerModel,
  visibleProjects, combineChipsModel, combinedProjectsParam, itemProjects,
  taggedItemRows, combinedBannerModel, projectColorMap, renderMarkdown,
  settingsGroupsModel, fieldControlModel, coerceFieldInput,
  fmtRelTime, paletteResultsModel, scheduleRowModel, scheduleFormModel,
  spendOverviewModel, spendOutcomesModel, runCostModel, abTableModel,
  notifCenterModel, playbookFormModel, tourStepsModel,
  authGate, loginFormModel, loginErrorMessage,
  memoryRowModel, memoryPageModel, gcSummary, gcResultModel, rollbackStateModel,
  homeModel, sidebarGroups, wsDepsModel, wsRunPayload,
  graph2ViewModel, nodePanelModel,
  agentsListModel, agentFormModel, agentFormValidate,
} = window.AdaUtil;

// Respect the user's reduced-motion preference for programmatic scrolling.
const _reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)");
const scrollBehavior = () => (_reduceMotion && _reduceMotion.matches ? "auto" : "smooth");

// ---- cookie-session auth (S8) ----
// When the server has a token configured, /api/auth/status gates boot behind a
// full-screen login. POST /api/login sets an HttpOnly ada_token cookie, which then
// rides on every fetch/WebSocket/download automatically — the token is never kept
// client-side. With no token configured (the localhost default) this is inert.
let _appBooted = false;
function showLogin() {
  if (!$("view-login").classList.contains("hidden")) return;
  $("view-login").classList.remove("hidden");
  $("login-token").focus();
}
function loginError(text) {
  const err = $("login-error");
  err.textContent = text;
  err.classList.toggle("hidden", !text);
}
async function submitLogin() {
  const m = loginFormModel($("login-token").value);
  if (!m.ok) { loginError(m.error); return; }
  $("login-btn").disabled = true;
  try {
    const res = await _fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: m.token }),
    });
    if (!res.ok) { loginError(loginErrorMessage(res.status)); return; }
    if (_appBooted) { location.reload(); return; }  // re-login after an expired session
    loginError("");
    $("login-token").value = "";
    $("view-login").classList.add("hidden");
    $("signout-btn").classList.remove("hidden");
    bootApp();
  } catch (e) {
    loginError("Could not reach the server.");
  } finally {
    $("login-btn").disabled = false;
  }
}
async function signOut() {
  try { await _fetch("/api/logout", { method: "POST" }); } catch (e) { /* reload regardless */ }
  location.reload();
}
const _fetch = window.fetch.bind(window);
window.fetch = async (url, opts = {}) => {
  const res = await _fetch(url, opts);
  // cookie gone (server restarted with a new token) — re-gate instead of failing quietly
  if (res.status === 401 && typeof url === "string" && url.startsWith("/api/")) showLogin();
  return res;
};

// Modernist mono discipline: every specialist prints in ink; state (running/passed/
// failed) carries the color. --agent-ink flips with the theme.
const AGENT_STYLE = {};
const agentStyle = (n) => AGENT_STYLE[n] || { color: "var(--agent-ink)" };
const PHASES = ["plan", "execute", "verify", "document", "done"];

const state = Object.assign({
  agents: {},        // subtask id -> card element
  openAgentId: null, // id of the agent whose detail popup is open (for live updates)
  timer: null,
  startedAt: 0,
  docsId: null,      // run.id used for the docs folder
  continueFrom: null, // task id being re-engaged (continuation), or null for a fresh task
  budgetUsd: 0,      // budget cap for the run on screen (0 = none -> meter hidden)
  wasQueued: false,  // this task was seen queued -> toast when it actually starts
}, initialRunAggregates());  // + agentData / timeline / total / reviewed / messages / costUsd / phase

// ---- W5: safe server config (feeds the cost-vs-budget meter's default cap) ----
let serverConfig = { budget_usd: 0 };
async function loadConfig() {
  try { serverConfig = await (await fetch("/api/config")).json(); } catch (e) { /* keep defaults */ }
}

// ---- W5: live cost-vs-budget meter (hidden when no budget is set) ----
function updateBudgetMeter(cost) {
  const el = $("budget-meter");
  if (!el) return;
  const m = computeBudgetMeter(cost, state.budgetUsd);
  if (!m.visible) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const fill = $("bm-fill");
  fill.style.width = m.pct + "%";
  fill.className = "bm-fill" + (m.severity ? " " + m.severity : "");
  const track = el.querySelector(".bm-track");
  if (track) track.setAttribute("aria-valuenow", String(m.pct));
  $("bm-text").textContent = m.text;
}

function setConn(text, cls) {
  const el = $("conn");
  el.textContent = text;
  el.className = "badge " + cls;
}

function feed(text, cls = "") {
  const li = document.createElement("li");
  if (cls) li.className = cls;
  li.textContent = text;
  $("feed").prepend(li);
}

function setMetricsFrom(data) {
  if ("kg_nodes" in data) $("m-kg").textContent = data.kg_nodes;
  if ("kg_edges" in data) $("m-edges").textContent = data.kg_edges;
  if ("messages" in data) { $("m-msgs").textContent = data.messages; state.messages = Number(data.messages) || 0; }
  if ("memory" in data) $("m-mem").textContent = data.memory;
  if ("cost_usd" in data) $("m-cost").textContent = "$" + Number(data.cost_usd).toFixed(4);
  if ("input_tokens" in data || "output_tokens" in data) {
    const i = data.input_tokens || 0, o = data.output_tokens || 0;
    $("usage-line").innerHTML =
      `<b>$${Number(data.cost_usd || 0).toFixed(4)}</b> · ${fmtTok(i)} input + ${fmtTok(o)} output tokens` +
      ` · ${fmtTok(i + o)} total`;
  }
}

function resetRunView(prompt) {
  $("empty").classList.add("hidden");
  $("run-view").classList.remove("hidden");
  $("run-prompt").textContent = prompt;
  $("status-pill").className = "pill pill-running";
  $("status-pill").textContent = "running";
  $("agents").innerHTML = "";
  $("feed").innerHTML = "";
  $("plan-card").classList.add("hidden");
  $("agents-title").classList.add("hidden");
  $("children-title").classList.add("hidden");
  $("children-grid").classList.add("hidden");
  $("children-grid").innerHTML = "";
  $("brief-card").classList.add("hidden");
  $("brief-points").innerHTML = "";
  ["m-agents", "m-sessions", "m-reaped", "m-kg", "m-edges", "m-mem", "m-msgs"].forEach(id => $(id).textContent = "0");
  $("m-tests").textContent = "—"; $("m-tests").style.color = "";
  $("m-cost").textContent = "—";
  $("usage-line").textContent = "";
  $("plan-editor").classList.add("hidden");
  const cb = $("cancel-btn"); cb.classList.remove("hidden"); cb.disabled = false;
  $("feedback-box").classList.add("hidden"); _fbRating = null;
  if ($("fb-comment")) $("fb-comment").value = "";
  updateRunControls();
  $("pipeline").classList.remove("hidden");
  $("progress-row").classList.remove("hidden");
  $("plan-dag-card").classList.add("hidden");
  $("plan-dag").innerHTML = "";
  $("timeline-card").classList.add("hidden");
  $("timeline").innerHTML = "";
  $("cost-card").classList.add("hidden");
  $("cost-card").removeAttribute("open");
  $("cost-table").innerHTML = "";
  state.subtaskCosts = null;
  $("attention-card").classList.add("hidden");
  $("attention-list").innerHTML = "";
  _attnOpen = 0;
  Object.assign(state, initialRunAggregates());   // timeline / agentData / total / reviewed / messages / costUsd / phase
  state.openAgentId = null;
  setPhase("plan");
  $("progress-bar").style.width = "0%"; $("progress-label").textContent = "";
  state.agents = {};
  state.docsId = null;
  state.wasQueued = false;
  updateBudgetMeter(0);
  startTimer();
}

function startTimer() {
  state.startedAt = performance.now();
  stopTimer();
  state.timer = setInterval(() => {
    $("timer").textContent = ((performance.now() - state.startedAt) / 1000).toFixed(1) + "s";
  }, 100);
}
function stopTimer() { if (state.timer) { clearInterval(state.timer); state.timer = null; } }

function makeAgentCard(st) {
  const card = document.createElement("div");
  card.className = "agent-card queued";
  card.id = "agent-" + st.id;
  const deps = (st.depends_on && st.depends_on.length) ? ("depends on " + st.depends_on.join(", ")) : "no dependencies";
  card.innerHTML = `
    <div class="ac-top"><span class="chip">${escapeHtml(st.id)}</span><span class="ac-agent"><span class="ac-dot" style="background:${agentStyle(st.agent).color}"></span>${escapeHtml(st.agent)}</span></div>
    <div class="ac-title">${escapeHtml(st.title)}</div>
    <div class="ac-deps">${escapeHtml(deps)}</div>
    <div class="ac-state"><span class="dot"></span><span class="ac-statetext">queued</span><span class="ac-score"></span></div>
    <div class="ac-stream"></div>
  `;
  card.title = "Click to see this agent's full activity";
  makeActivatable(card, () => openAgentModal(st.id), `Open activity for subtask ${st.id} · ${st.agent}`);
  return card;
}

// a11y: let a non-button element behave like a button (focus + Enter/Space).
function makeActivatable(el, onActivate, label) {
  el.setAttribute("role", "button");
  el.tabIndex = 0;
  if (label) el.setAttribute("aria-label", label);
  el.addEventListener("click", onActivate);
  el.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    if (e.target !== el) return;              // don't hijack inner buttons/inputs
    e.preventDefault();
    onActivate(e);
  });
}

function setAgentState(id, cls, text, score) {
  const card = $("agent-" + id);
  if (!card) return;
  card.classList.remove("queued", "running", "passed", "failed");
  card.classList.add(cls);
  const t = card.querySelector(".ac-statetext");
  if (t) t.textContent = text;
  if (score !== undefined) card.querySelector(".ac-score").textContent = "score " + score;
}

// ---- F3: children grid (cross-project fan-out parent view) ----
// One card per child project — replaces the agent cards on a fan-out parent.
function renderChildrenGrid() {
  const models = childrenGridModel(state.children);
  const wrap = $("children-grid"), title = $("children-title");
  if (!models.length) { wrap.classList.add("hidden"); title.classList.add("hidden"); wrap.innerHTML = ""; return; }
  title.classList.remove("hidden");
  wrap.classList.remove("hidden");
  wrap.innerHTML = "";
  models.forEach(m => {
    const card = document.createElement("div");
    card.className = "agent-card child-card " + m.cls;
    card.innerHTML =
      `<div class="ac-top"><span class="chip">⋔ ${escapeHtml(m.slug)}</span>` +
      `<span class="pill ${escapeAttr(m.pill)}">${escapeHtml(m.statusLabel)}</span></div>` +
      `<div class="cc-meta"><span title="Review quality score">${escapeHtml(m.qualityLabel)}</span>` +
      `<span title="Child run cost">${escapeHtml(m.costLabel)}</span></div>` +
      (m.branch ? `<div class="cc-branch" title="Branch left for review">⎇ <code>${escapeHtml(m.branch)}</code></div>` : "");
    card.title = "Click to open this project's child task";
    makeActivatable(card, () => openChildTask(m), `Open child task for project ${m.slug}`);
    wrap.appendChild(card);
  });
}

function openChildTask(m) {
  if (!m.taskId) return;
  // children are ordinary tasks — live ones attach to their stream, finished ones open docs
  if (m.open === "live") attachToRun(m.taskId, m.slug);
  else openTask(m.taskId, null);
}

// ---- phase stepper + progress ----
function setPhase(phase) {
  const idx = PHASES.indexOf(phase);
  if (idx < 0) return;
  document.querySelectorAll(".pstep").forEach((el, i) => {
    el.classList.toggle("active", i === idx);
    el.classList.toggle("complete", i < idx);
    const num = el.querySelector(".pnum");
    if (num) num.textContent = i < idx ? "✓" : String(i + 1);
  });
}
function updateProgress() {
  const p = runProgress(state);
  $("progress-bar").style.width = p.pct + "%";
  const track = document.querySelector(".progress-track");
  if (track) track.setAttribute("aria-valuenow", String(p.pct));
  $("progress-label").textContent = p.label;
}

// ---- plan DAG ----
function renderPlanDag(subtasks) {
  const svg = $("plan-dag");
  svg.innerHTML = "";
  if (!subtasks || !subtasks.length) { $("plan-dag-card").classList.add("hidden"); return; }
  $("plan-dag-card").classList.remove("hidden");

  const defs = document.createElementNS(SVGNS, "defs");
  defs.innerHTML = '<marker id="dag-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="var(--muted)"></path></marker>';
  svg.appendChild(defs);

  const byId = Object.fromEntries(subtasks.map(s => [s.id, s]));
  const level = {};
  const lvl = (id, seen) => {
    if (id in level) return level[id];
    if (seen.has(id)) return 0;
    seen.add(id);
    const s = byId[id]; if (!s) return 0;
    const deps = (s.depends_on || []).filter(d => d in byId);
    const l = deps.length ? Math.max(...deps.map(d => lvl(d, seen))) + 1 : 0;
    level[id] = l; return l;
  };
  subtasks.forEach(s => lvl(s.id, new Set()));

  const cols = {};
  subtasks.forEach(s => { (cols[level[s.id]] = cols[level[s.id]] || []).push(s); });
  const NW = 172, NH = 56, COLGAP = 46, ROWGAP = 16, PAD = 12;
  const pos = {};
  let maxRows = 0;
  Object.keys(cols).forEach(L => {
    const arr = cols[L]; maxRows = Math.max(maxRows, arr.length);
    arr.forEach((s, i) => { pos[s.id] = { x: PAD + L * (NW + COLGAP), y: PAD + i * (NH + ROWGAP) }; });
  });
  const maxLevel = Math.max(0, ...Object.values(level));
  const W = PAD * 2 + (maxLevel + 1) * NW + maxLevel * COLGAP;
  const H = PAD * 2 + maxRows * NH + Math.max(0, maxRows - 1) * ROWGAP;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("width", W); svg.setAttribute("height", H);

  subtasks.forEach(s => (s.depends_on || []).forEach(d => {
    if (!pos[d] || !pos[s.id]) return;
    const a = pos[d], b = pos[s.id];
    const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2, mx = (x1 + x2) / 2;
    const path = document.createElementNS(SVGNS, "path");
    path.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
    path.setAttribute("class", "dag-edge");
    svg.appendChild(path);
  }));

  subtasks.forEach(s => {
    const p = pos[s.id], st = agentStyle(s.agent);
    const g = document.createElementNS(SVGNS, "g");
    g.setAttribute("class", "dag-node queued"); g.id = "dag-node-" + s.id;
    g.appendChild(_rect(p.x, p.y, NW, NH, 10));
    const bar = _rect(p.x, p.y, 4, NH, 2); bar.setAttribute("fill", st.color); bar.setAttribute("stroke", "none");
    g.appendChild(bar);
    g.appendChild(_text(p.x + 12, p.y + 18, "dag-id", `${s.id} · ${s.agent}`));
    const title = (s.title || "");
    g.appendChild(_text(p.x + 12, p.y + 38, "dag-title", title.length > 23 ? title.slice(0, 22) + "…" : title));
    g.style.cursor = "pointer";
    g.addEventListener("click", () => openAgentModal(s.id));
    svg.appendChild(g);
  });
}
function _rect(x, y, w, h, r) {
  const el = document.createElementNS(SVGNS, "rect");
  el.setAttribute("x", x); el.setAttribute("y", y); el.setAttribute("width", w);
  el.setAttribute("height", h); el.setAttribute("rx", r);
  return el;
}
function _text(x, y, cls, txt) {
  const el = document.createElementNS(SVGNS, "text");
  el.setAttribute("x", x); el.setAttribute("y", y); el.setAttribute("class", cls);
  el.textContent = txt;
  return el;
}
function setPlanNodeState(id, cls) {
  const g = document.getElementById("dag-node-" + id);
  if (g) g.setAttribute("class", "dag-node " + cls);
}
function flashAgentCard(id) {
  const card = $("agent-" + id);
  if (!card) return;
  card.scrollIntoView({ behavior: scrollBehavior(), block: "center" });
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 1000);
}

// ---- a11y: modal open/close with focus trap, Escape close and focus restore ----
const _modalReturnFocus = {};   // modal id -> element that had focus before opening
function _modalFocusables(box) {
  return [...box.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
    .filter(el => !el.disabled && el.offsetParent !== null);
}
function openModalEl(id) {
  const m = $(id);
  if (!m) return;
  if (m.classList.contains("hidden")) _modalReturnFocus[id] = document.activeElement;
  m.classList.remove("hidden");
  const box = m.querySelector(".modal-box") || m;
  const f = _modalFocusables(box);
  (f.length ? f[0] : box).focus();
}
function closeModalEl(id) {
  const m = $(id);
  if (!m || m.classList.contains("hidden")) return;
  m.classList.add("hidden");
  const back = _modalReturnFocus[id];
  delete _modalReturnFocus[id];
  if (back && document.contains(back) && typeof back.focus === "function") back.focus();
}
function _trapModalTab(m, e) {
  if (e.key !== "Tab") return;
  const box = m.querySelector(".modal-box") || m;
  const f = _modalFocusables(box);
  if (!f.length) { e.preventDefault(); box.focus(); return; }
  const first = f[0], last = f[f.length - 1], active = document.activeElement;
  if (e.shiftKey && (active === first || !box.contains(active))) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && (active === last || !box.contains(active))) { e.preventDefault(); first.focus(); }
}

// ---- per-agent detail popup ----
function openAgentModal(id) {
  const a = state.agentData[id];
  if (!a) return;
  state.openAgentId = id;
  renderAgentModal(a);
  openModalEl("agent-modal");
}
function closeAgentModal() {
  closeModalEl("agent-modal");
  state.openAgentId = null;
}
function renderAgentModal(a) {
  if (!a) return;
  const st = agentStyle(a.agent);
  const status = a.status || "queued";
  $("am-id").textContent = a.id;
  $("am-agent").innerHTML = `<span class="ac-dot" style="background:${st.color}"></span>${escapeHtml(a.agent)}`;
  $("am-title").textContent = a.title || "";

  const pill = $("am-status");
  pill.className = "pill " + (status === "passed" ? "pill-done" : status === "failed" ? "pill-err" : status === "running" ? "pill-running" : "");
  pill.textContent = status;

  const dur = (a.start != null && a.end != null) ? fmtDuration(a.end - a.start)
            : (status === "running" ? "running…" : "—");
  const deps = (a.depends_on && a.depends_on.length) ? a.depends_on.join(", ") : "none";
  $("am-meta").innerHTML =
    `<div><span class="am-k">Score</span><span>${a.score != null ? a.score : "—"}</span></div>` +
    `<div><span class="am-k">Attempts</span><span>${a.attempts != null ? a.attempts : "—"}</span></div>` +
    `<div><span class="am-k">Duration</span><span>${dur}</span></div>` +
    `<div><span class="am-k">Depends on</span><span>${escapeHtml(deps)}</span></div>`;

  const rev = $("am-review");
  if (a.reasons && a.reasons.length) {
    rev.classList.remove("hidden");
    rev.innerHTML = `<span class="kicker">Review — ${a.passed ? "passed" : "needs work"}</span><ul>` +
      a.reasons.map(r => `<li>${escapeHtml(r)}</li>`).join("") + "</ul>";
  } else { rev.classList.add("hidden"); rev.innerHTML = ""; }

  const res = $("am-result");
  if (a.result) {
    res.classList.remove("hidden");
    res.textContent = a.result;
  } else { res.classList.add("hidden"); res.textContent = ""; }
  $("am-result-label").classList.toggle("hidden", !a.result);

  const stream = $("am-stream");
  if (!a.steps || !a.steps.length) {
    if (a.result) {
      stream.innerHTML = `<div class="muted">No step-by-step log for this run — showing the final result.</div>` +
        `<div class="as-text">${escapeHtml(a.result)}</div>`;
    } else {
      stream.innerHTML = `<div class="muted">No activity recorded yet${status === "queued" ? " — not started." : "."}</div>`;
    }
  } else {
    stream.innerHTML = a.steps.map(s =>
      `<div class="as-${escapeAttr(s.kind || "text")}">${escapeHtml(clipText(formatStepLine(s), 400))}</div>`).join("");
    stream.scrollTop = stream.scrollHeight;
  }
}

// ---- full per-subtask transcript panel ----
// Fetches the durable transcript (every agent step, unclipped) from
// /api/runs/{task}/transcript/{subtask}; live steps for the open subtask are
// appended by handleEvent's agent_step case while the run streams.
async function openTranscript(id) {
  const a = state.agentData[id] || {};
  state.transcriptId = id;
  state.transcriptCount = 0;
  const st = agentStyle(a.agent || "");
  $("tr-id").textContent = id;
  $("tr-agent").innerHTML =
    `<span class="ac-dot" style="background:${st.color}"></span>${escapeHtml(a.agent || "")}`;
  $("tr-count").textContent = "…";
  $("tr-body").innerHTML = `<div class="muted">Loading transcript…</div>`;
  openModalEl("transcript-modal");
  const tid = state.docsId || state.taskId;
  let data = null;
  if (tid) {
    try { data = await (await fetch(`/api/runs/${tid}/transcript/${id}`)).json(); } catch (e) { data = null; }
  }
  if (state.transcriptId !== id) return;  // closed (or switched) while fetching
  // Fall back to the steps already aggregated from the live stream if the fetch failed.
  renderTranscript(data && Array.isArray(data.steps) ? data : { agent: a.agent, steps: a.steps || [] });
}
function renderTranscript(data) {
  const vm = transcriptViewModel(data);
  if (vm.agent) {
    const st = agentStyle(vm.agent);
    $("tr-agent").innerHTML =
      `<span class="ac-dot" style="background:${st.color}"></span>${escapeHtml(vm.agent)}`;
  }
  $("tr-count").textContent = vm.countLabel;
  state.transcriptCount = vm.count;
  const body = $("tr-body");
  body.innerHTML = vm.lines.length
    ? vm.lines.map(l => `<div class="${escapeAttr(l.cls)}">${escapeHtml(l.prefix + l.text)}</div>`).join("")
    : `<div class="muted">No transcript recorded for this subtask.</div>`;
  body.scrollTop = body.scrollHeight;
}
function appendTranscriptStep(d) {
  const body = $("tr-body");
  if (!body) return;
  if (!state.transcriptCount) body.innerHTML = "";  // replace the empty/loading notice
  const l = transcriptLineModel(d);
  const div = document.createElement("div");
  div.className = l.cls;
  div.textContent = l.prefix + l.text;
  body.appendChild(div);
  state.transcriptCount = (state.transcriptCount || 0) + 1;
  $("tr-count").textContent = state.transcriptCount + (state.transcriptCount === 1 ? " step" : " steps");
  body.scrollTop = body.scrollHeight;
}
function closeTranscript() {
  closeModalEl("transcript-modal");
  state.transcriptId = null;
}

// ---- controls / toasts / timeline ----
// Model · Effort segmented controls (split — the old single select bundled both).
const segState = { model: "opus", effort: "high" };
function wireSeg(id, key) {
  const seg = $(id);
  if (!seg) return;
  seg.querySelectorAll(".seg-opt").forEach(b => b.onclick = () => {
    segState[key] = b.dataset.v;
    seg.querySelectorAll(".seg-opt").forEach(x =>
      x.setAttribute("aria-pressed", x === b ? "true" : "false"));
  });
}
function getControls() {
  const b = parseFloat($("budget").value);
  const t = $("task-title").value.trim();
  // F3: "Work across projects…" open -> send projects+stagger (2+ slugs fan out;
  // a single slug behaves exactly like `project` server-side).
  const multi = multiOpen ? selectedMultiProjects() : [];
  // Per-run repo binding is gone — the selected *project* owns the repository.
  return {
    effort: segState.effort, model: segState.model,
    budget: (b && b > 0) ? b : null, title: t || null,
    project: selectedProject(), memory_scope: $("mem-scope").value,
    continue_from: state.continueFrom || null,
    git_finalize: $("git-finalize").checked ? true : null,
    ...(multi.length ? { projects: multi, stagger: $("stagger").checked } : {}),
  };
}

// ---- re-engage: continue a completed task with a follow-up prompt ----
function setContinue(taskId, label) {
  if (!taskId) return;
  state.continueFrom = taskId;
  $("continue-ref").textContent = label || taskId;
  $("continue-banner").classList.remove("hidden");
  closeModalEl("modal");
  // The composer lives on the project Overview tab — navigate there so the
  // continue banner is actually visible (the button lives on the task view).
  showMainView("project");
  selectProjectTab("overview", true);
  const p = $("prompt");
  p.placeholder = "What should the assistant do next on this task? e.g. “add error handling and tests”";
  p.focus();
  p.scrollIntoView({ behavior: scrollBehavior(), block: "center" });
}

function clearContinue() {
  state.continueFrom = null;
  $("continue-banner").classList.add("hidden");
  $("prompt").placeholder = "e.g. Add input validation to a sample function and document it";
}

// ---- project-first navigation state ----
// The selected project drives the whole main area. "multi" is the pseudo-entry
// for cross-project fan-out parents (it only has a task list). null means no
// real project exists yet -> the first-run empty state. The backend's scratch
// "default" project is never rendered or auto-selected.
let currentProject = null;
let currentMainView = "project";   // project | task | activity | agents | gsettings | empty
let currentTab = "overview";       // overview | tasks | run | knowledge | settings

let currentKnow = "memory";        // memory | graph | files

function selectedProject() { return currentProject; }

let projectList = [];        // last-fetched /api/projects list
let projectActivity = {};    // slug -> /activity body (feeds sidebar state dots)
let multiActivity = null;    // /api/projects/multi/activity body
let multiHasRuns = false;    // any historical fan-out parents?

// Cached /api/projects entry for a slug (defaults to the selected project).
function currentProjectEntry(slug) {
  const s = slug || selectedProject();
  return projectList.find(p => p.slug === s) || null;
}

// ---- workspaces: named groups of inter-related projects ----
let workspaceList = [];      // last-fetched /api/workspaces entries
let currentWorkspace = null; // slug of the workspace open in view-workspace

async function loadWorkspaces() {
  try {
    const resp = await fetch("/api/workspaces");
    if (resp.ok) {
      const rows = await resp.json();
      workspaceList = Array.isArray(rows) ? rows : [];
    }
  } catch (e) { /* keep the last list */ }
}
function workspaceEntry(slug) {
  return workspaceList.find(w => w.slug === slug) || null;
}

async function loadProjects() {
  let list = [];
  try { list = await (await fetch("/api/projects")).json(); } catch (e) { list = []; }
  projectList = visibleProjects(list);   // the scratch "default" project is never rendered
  await loadWorkspaces();                // group names for the sidebar + workspace view
  if (currentProject !== "multi") {
    const saved = localStorage.getItem("ada-project") || currentProject;
    currentProject = projectList.some(p => p.slug === saved) ? saved
      : (projectList.length ? projectList[0].slug : null);
  }
  renderSidebar();
  renderMultiProjects();
  if (currentProject) {
    updateComposerProject();
    renderProjectHeader(null);
  }
}

// ---- sidebar: workspace-grouped project rows + the "⋔ Across projects" pseudo-entry ----
function _sidebarProjLi(r, inWs) {
  const li = document.createElement("li");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "proj-item pl-state-" + r.state
    + (r.archived ? " pl-archived" : "") + (r.multi ? " pl-multi" : "")
    + (inWs ? " pl-in-ws" : "");
  btn.setAttribute("aria-current", r.current ? "true" : "false");
  btn.title = r.name + (r.state !== "idle" ? " · " + r.state : "")
    + (r.archived ? " · archived" : "");
  btn.innerHTML = `<span class="pl-dot" aria-hidden="true"></span>` +
    `<span class="pl-name">${escapeHtml(r.name)}</span>`;
  btn.onclick = () => selectProject(r.slug);
  li.appendChild(btn);
  return li;
}

function _sidebarWsLi(wsSlug, count) {
  const entry = workspaceEntry(wsSlug);
  const current = currentMainView === "workspace" && currentWorkspace === wsSlug;
  const li = document.createElement("li");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ws-head";
  btn.setAttribute("aria-current", current ? "true" : "false");
  btn.title = "Open workspace " + ((entry && entry.name) || wsSlug);
  btn.innerHTML = `<span class="ws-mark" aria-hidden="true">▦</span>` +
    `<span class="pl-name">${escapeHtml((entry && entry.name) || wsSlug)}</span>` +
    `<span class="ws-count">${count}</span>`;
  btn.onclick = () => openWorkspace(wsSlug);
  li.appendChild(btn);
  return li;
}

function renderSidebar() {
  const ul = $("project-list");
  if (!ul) return;
  ul.innerHTML = "";
  const { groups, ungrouped } = sidebarGroups(projectList);
  // workspace groups first: a clickable header row, then the member projects
  groups.forEach(g => {
    ul.appendChild(_sidebarWsLi(g.workspace, g.projects.length));
    sidebarProjectRows(g.projects, projectActivity, currentProject, {})
      .forEach(r => ul.appendChild(_sidebarProjLi(r, true)));
  });
  // empty workspaces (no member projects yet) stay reachable
  workspaceList.forEach(w => {
    if (!groups.some(g => g.workspace === w.slug)) ul.appendChild(_sidebarWsLi(w.slug, 0));
  });
  // ungrouped projects keep the existing flat list (+ the multi pseudo-entry)
  sidebarProjectRows(ungrouped, projectActivity, currentProject,
                     { activity: multiActivity, hasRuns: multiHasRuns })
    .forEach(r => ul.appendChild(_sidebarProjLi(r, false)));
  const navHome = $("nav-home");
  if (navHome) {
    if (currentMainView === "home") navHome.setAttribute("aria-current", "page");
    else navHome.removeAttribute("aria-current");
  }
  const nav = $("nav-activity");
  if (nav) {
    if (currentMainView === "activity") nav.setAttribute("aria-current", "page");
    else nav.removeAttribute("aria-current");
  }
  const navAgents = $("nav-agents");
  if (navAgents) {
    if (currentMainView === "agents") navAgents.setAttribute("aria-current", "page");
    else navAgents.removeAttribute("aria-current");

  }
  const navSettings = $("nav-gsettings");
  if (navSettings) {
    if (currentMainView === "gsettings") navSettings.setAttribute("aria-current", "page");
    else navSettings.removeAttribute("aria-current");
  }
}

// Refresh per-project activity (sidebar state dots + overview strip inputs).
async function refreshSidebarActivity() {
  const fetchAct = async (slug) => {
    try {
      const resp = await fetch("/api/projects/" + encodeURIComponent(slug) + "/activity");
      return resp.ok ? await resp.json() : null;
    } catch (e) { return null; }
  };
  const shown = projectList.slice(0, 20);
  const acts = await Promise.all([...shown.map(p => fetchAct(p.slug)), fetchAct("multi")]);
  multiActivity = acts.pop();
  multiHasRuns = !!(multiActivity && (multiActivity.recent || []).length);
  projectActivity = {};
  shown.forEach((p, i) => { projectActivity[p.slug] = acts[i]; });
  renderSidebar();
}

// ---- work composer: dynamic heading + delivery control ----
// Projects WITH a repo are delivered per the project policy's git_mode — the
// per-run git_finalize checkbox only survives on the scratch default (no repo).
function updateComposerProject() {
  const entry = currentProjectEntry();
  const cm = composerModel(entry || { slug: selectedProject() });
  $("composer-heading").textContent = cm.heading;
  const m = cm.delivery;
  const row = $("git-finalize-row"), hint = $("delivery-hint");
  if (m.control === "hint") {
    row.classList.add("hidden");
    $("git-finalize").checked = false;   // policy governs delivery; never send the per-run flag
    hint.classList.remove("hidden");
    hint.innerHTML = `⎇ ${escapeHtml(m.text)} ` +
      `<button type="button" class="link-btn delivery-policy" title="Open this project's policy editor">Edit policy →</button>`;
    hint.querySelector(".delivery-policy").onclick = () => selectProjectTab("settings");
  } else {
    hint.classList.add("hidden");
    hint.innerHTML = "";
    row.classList.remove("hidden");
  }
}

// ---- project header: serif name + origin badge + branch chip + root path ----
function renderProjectHeader(st) {
  const isMulti = currentProject === "multi";
  const entry = currentProjectEntry() || { slug: currentProject, name: currentProject };
  const h = projectHomeHeaderModel(entry);
  $("proj-name").textContent = isMulti ? "⋔ Across projects" : (h.name || currentProject);
  const origin = $("proj-origin");
  origin.classList.toggle("hidden", isMulti);
  origin.textContent = h.origin;
  const m = projectTaglineModel(h.name, st);
  const branch = $("proj-branch");
  branch.classList.toggle("hidden", isMulti || !m.branchText);
  branch.textContent = m.branchText ? "⎇ " + m.branchText : "";
  $("proj-dirty").classList.toggle("hidden", isMulti || !m.dirty);
  $("proj-archived").classList.toggle("hidden", isMulti || !(entry && entry.archived));
  const root = $("proj-root");
  root.classList.toggle("hidden", isMulti || !h.root);
  root.textContent = h.root;
  root.title = h.root;
}

// ---- F3: cross-project fan-out composer (multi-select + stagger) ----
function renderMultiProjects() {
  const box = $("multi-projects");
  if (!box) return;
  const checked = new Set(selectedMultiProjects());
  const items = projectList.filter(p => !p.archived);  // archived projects can't be targets
  if (!items.length) { box.innerHTML = '<p class="muted">No projects yet.</p>'; return; }
  box.innerHTML = items.map(p =>
    `<label class="check mp-item"><input type="checkbox" class="mp-check" value="${escapeAttr(p.slug)}"` +
    `${checked.has(p.slug) ? " checked" : ""} /> ${escapeHtml(p.name || p.slug)}</label>`).join("");
}

function selectedMultiProjects() {
  return [...document.querySelectorAll("#multi-projects .mp-check:checked")].map(cb => cb.value);
}

// "Work across projects…" — a link-button reveals the fan-out multi-select.
let multiOpen = false;
function toggleMultiBox() {
  multiOpen = !multiOpen;
  $("multi-box").classList.toggle("hidden", !multiOpen);
  $("multi-open").setAttribute("aria-expanded", String(multiOpen));
  $("multi-open").textContent = multiOpen ? "Single project" : "Work across projects…";
  if (multiOpen) {
    renderMultiProjects();
    // pre-check the current project so fan-out starts from where the user is
    const cur = selectedProject();
    const cb = cur ? document.querySelector(
      `#multi-projects .mp-check[value="${CSS.escape(cur)}"]`) : null;
    if (cb) cb.checked = true;
  }
}

// ---- F1: New / Import project dialog ----
function openProjectModal() {
  ["pm-name", "pm-source", "pm-import-name", "pm-ref"].forEach(id => { $(id).value = ""; });
  $("pm-error").classList.add("hidden");
  $("pm-error").textContent = "";
  openModalEl("project-modal");
}
function closeProjectModal() { closeModalEl("project-modal"); }

function _pmFail(msg) {
  const el = $("pm-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

async function submitProjectModal() {
  const name = $("pm-name").value.trim();
  const source = $("pm-source").value.trim();
  if (!name && !source) { _pmFail("Enter a project name, or a source path / git URL to import."); return; }
  const btn = $("pm-submit");
  btn.disabled = true;
  $("pm-error").classList.add("hidden");
  let created = null;
  try {
    let resp;
    if (source) {
      resp = await fetch("/api/projects/import", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, name: $("pm-import-name").value.trim() || name || null,
                               ref: $("pm-ref").value.trim() || null }),
      });
    } else {
      resp = await fetch("/api/projects", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
    }
    const data = await resp.json();
    if (!resp.ok || data.error) { _pmFail(data.error || ("HTTP " + resp.status)); btn.disabled = false; return; }
    created = data;
  } catch (e) { _pmFail("Request failed: " + e); btn.disabled = false; return; }
  btn.disabled = false;
  if (created && created.slug) {
    closeProjectModal();
    await loadProjects();
    showToast((source ? "Project imported · " : "Project created · ") + (created.name || created.slug), "success");
    selectProject(created.slug);
  }
}

// ---- select a project: the main area becomes that project ----
function selectProject(slug, tab) {
  if (!slug) { showEmptyState(); return; }
  if (slug !== currentProject) { combineSelected = new Set(); setCombineBanner(0); }
  currentProject = slug;
  if (slug !== "multi") localStorage.setItem("ada-project", slug);
  showMainView("project");
  updateComposerProject();
  renderProjectHeader(null);
  renderSidebar();
  selectProjectTab(tab || (slug === "multi" ? "tasks" : currentTab), true);
  refreshProjectPulse(true);
  loadRecent();
}

// ---- project header status + overview activity strip ----
async function loadProjectStatus() {
  const slug = selectedProject();
  if (!slug) return;
  if (slug === "multi") { renderProjectHeader(null); return; }
  let st = null;
  try {
    const resp = await fetch("/api/projects/" + encodeURIComponent(slug) + "/status");
    if (resp.ok) st = await resp.json();
  } catch (e) { /* leave as-is */ }
  renderProjectHeader(st);
  renderSettingsRepo(st);
}

async function loadProjectActivity() {
  const slug = selectedProject();
  if (!slug) return;
  const el = $("ov-activity");
  let act = null;
  try {
    const resp = await fetch("/api/projects/" + encodeURIComponent(slug) + "/activity");
    if (resp.ok) act = await resp.json();
  } catch (e) { /* leave as-is */ }
  projectActivity[slug] = act;
  const m = activityStripModel(act);
  el.className = "proj-activity pa-" + m.state;
  el.innerHTML = `<span class="pa-dot" aria-hidden="true"></span>${escapeHtml(m.text)}`;
}

// One "pulse" refreshes the sidebar dots + overview strip every tick and the
// (heavier, git-backed) header status every third tick — or all immediately
// when force=true.
let _pulseN = 0;
function refreshProjectPulse(force) {
  refreshSidebarActivity();
  loadProjectActivity();
  if (force || _pulseN % 3 === 0) loadProjectStatus();
  _pulseN++;
}

// ---- task queue ----
let queueState = { concurrency: 1, paused: false, running: [], pending: [] };

async function loadQueue() {
  try { queueState = await (await fetch("/api/queue")).json(); }
  catch (e) { return; }
  renderQueue();
}

function renderQueue() {
  const running = queueState.running || [];
  const pending = queueState.pending || [];
  const total = running.length + pending.length;

  const chip = $("queue-chip");
  if (total > 0) {
    chip.classList.remove("hidden");
    chip.textContent = `queue: ${running.length} running · ${pending.length} queued` + (queueState.paused ? " · paused" : "");
    chip.className = "badge " + (queueState.paused ? "badge-idle" : "badge-live");
  } else { chip.classList.add("hidden"); }

  $("q-concurrency").value = String(queueState.concurrency || 1);
  $("q-pause").textContent = queueState.paused ? "▶" : "⏸";
  $("q-pause").title = queueState.paused ? "Resume queue" : "Pause queue";
  $("q-pause").setAttribute("aria-label", queueState.paused ? "Resume queue" : "Pause queue");

  const sec = $("queue-section"), list = $("queue-list");
  if (!total) { sec.classList.add("hidden"); list.innerHTML = ""; return; }
  sec.classList.remove("hidden");
  list.innerHTML = "";

  running.forEach(r => {
    const li = document.createElement("li");
    li.className = "q-item q-running";
    li.innerHTML = `<span class="q-pos" aria-hidden="true">●</span><span class="q-title">${escapeHtml(r.title || r.id)}</span>` +
      `<span class="q-acts"><button type="button" class="q-act q-cancel" title="Cancel run">Cancel</button></span>`;
    li.querySelector(".q-cancel").onclick = (e) => { e.stopPropagation(); cancelTask(r.id); };
    makeActivatable(li, (e) => { if (!e.target.closest("button")) attachToRun(r.id, r.title); },
      `Watch running task ${r.title || r.id}`);
    list.appendChild(li);
  });

  pending.forEach((p, i) => {
    const li = document.createElement("li");
    li.className = "q-item";
    const upDis = i === 0 ? "disabled" : "";
    const downDis = i === pending.length - 1 ? "disabled" : "";
    li.innerHTML = `<span class="q-pos">${p.position}</span><span class="q-title">${escapeHtml(p.title || p.id)}</span>` +
      `<span class="q-acts">` +
      `<button type="button" class="q-act q-up" ${upDis} title="Move up" aria-label="Move up in queue">↑</button>` +
      `<button type="button" class="q-act q-down" ${downDis} title="Move down" aria-label="Move down in queue">↓</button>` +
      `<button type="button" class="q-act q-now" title="Run next">Run next</button>` +
      `<button type="button" class="q-act q-remove" title="Remove from queue" aria-label="Remove from queue">✕</button></span>`;
    li.querySelector(".q-now").onclick = () => promoteTask(p.id);
    li.querySelector(".q-remove").onclick = () => removeQueued(p.id);
    const up = li.querySelector(".q-up"), down = li.querySelector(".q-down");
    if (up && !up.disabled) up.onclick = () => moveQueued(i, -1);
    if (down && !down.disabled) down.onclick = () => moveQueued(i, 1);
    list.appendChild(li);
  });
}

async function promoteTask(id) {
  try { await fetch(`/api/queue/${id}/promote`, { method: "POST" }); } catch (e) { /* ignore */ }
  loadQueue();
}
async function moveQueued(i, dir) {
  const order = (queueState.pending || []).map(p => p.id);
  const j = i + dir;
  if (j < 0 || j >= order.length) return;
  [order[i], order[j]] = [order[j], order[i]];
  try {
    await fetch("/api/queue/reorder", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ order }),
    });
  } catch (e) { /* ignore */ }
  loadQueue();
}
async function removeQueued(id) {
  // queued tasks haven't produced anything; remove immediately so the pump can't start them
  try { await fetch("/api/tasks/" + id, { method: "DELETE" }); } catch (e) { /* ignore */ }
  showToast("Removed from queue", "warn", 2000);
  loadQueue(); loadRecent();
}
async function setConcurrency() {
  const n = parseInt($("q-concurrency").value, 10) || 1;
  try {
    await fetch("/api/queue/config", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ concurrency: n }),
    });
  } catch (e) { /* ignore */ }
  loadQueue();
}
async function togglePause() {
  try { await fetch("/api/queue/" + (queueState.paused ? "resume" : "pause"), { method: "POST" }); }
  catch (e) { /* ignore */ }
  loadQueue();
}
function showToast(msg, type = "", ms = 4000) {
  const el = document.createElement("div");
  el.className = "toast" + (type ? " " + type : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => { el.style.transition = "opacity .3s"; el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, ms);
}
// Timeline zoom (0.5×–4× horizontal scale) + optional per-subtask cost annotations
// (state.subtaskCosts: id -> usd, filled from /api/analytics/run/{id}).
let tlZoom = 1;
function applyTimelineZoom() {
  const el = $("timeline");
  if (el) el.style.width = Math.round(tlZoom * 100) + "%";
  const val = $("tl-zoom-val");
  if (val) val.textContent = tlZoom + "×";
}
function renderTimeline() {
  const rows = timelineRows(state.timeline);
  if (!rows.length) return;
  const el = $("timeline"); el.innerHTML = "";
  applyTimelineZoom();
  const costs = state.subtaskCosts || {};
  rows.forEach(r => {
    const st = agentStyle(r.agent);
    const cost = (r.id in costs) ? `<span class="tl-cost" title="Subtask LLM cost">${escapeHtml(fmtCost(costs[r.id]))}</span>` : "";
    const row = document.createElement("div"); row.className = "tl-row";
    row.innerHTML = `<span class="tl-label"><span class="tl-dot" style="background:${st.color}" aria-hidden="true"></span>${escapeHtml(r.id)} · ${escapeHtml(r.agent)}</span>` +
      `<div class="tl-track" aria-hidden="true"><div class="tl-bar" style="left:${r.leftPct}%;width:${r.widthPct}%;background:${st.color}"></div></div>` +
      `<span class="tl-dur">${escapeHtml(r.durLabel)}</span>${cost}`;
    el.appendChild(row);
  });
}

// Toast once when a task we saw queued emits its first live event (it left the queue).
function noteQueuedStart() {
  if (!state.wasQueued) return;
  state.wasQueued = false;
  showToast("Queued task started", "success");
  notify("start", "Queued task started");
}

function handleEvent(ev) {
  const d = ev.data || {};
  reduceRunEvent(state, ev);   // pure aggregation (util.js) — the switch below only touches the DOM
  switch (ev.type) {
    case "status":
      noteQueuedStart();
      if (d.backend) $("backend-badge").textContent = "backend: " + d.backend;
      if (ev.message && /Documenting/i.test(ev.message)) setPhase("document");
      feed(ev.message);
      break;
    case "queued":
      // task is waiting its turn in the queue
      state.wasQueued = true;
      $("status-pill").className = "pill";
      $("status-pill").textContent = d.position ? `queued · #${d.position}` : "queued";
      setConn("queued", "badge-idle");
      $("pipeline").classList.add("hidden");
      $("progress-row").classList.add("hidden");
      loadQueue();
      break;
    case "plan":
      noteQueuedStart();
      // task left the queue and is now running — restore the live UI
      if ($("status-pill").textContent.startsWith("queued")) {
        $("status-pill").className = "pill pill-running";
        $("status-pill").textContent = "running";
        setConn("live", "badge-live");
        $("pipeline").classList.remove("hidden");
        $("progress-row").classList.remove("hidden");
        loadQueue();
      }
      updateRunControls();
      $("plan-card").classList.remove("hidden");
      $("plan-summary").textContent = d.summary || "";
      if (d.children && d.children.length) {
        // F3: fan-out parent — a children grid replaces the agent cards
        renderChildrenGrid();
        $("m-agents").textContent = state.total;
        setPhase("execute");
        updateProgress();
        feed(ev.message);
        break;
      }
      $("agents-title").classList.remove("hidden");
      (d.subtasks || []).forEach(st => {
        $("agents").appendChild(makeAgentCard(st));
        state.agents[st.id] = true;
      });
      $("m-agents").textContent = state.total;
      renderPlanDag(d.subtasks || []);
      setPhase("execute");
      updateProgress();
      feed(ev.message);
      break;
    case "plan_update":
      // adaptive replan added a subtask mid-run (e.g. a bounded repair) — give it a card
      if (d.id && !state.agents[d.id]) {
        $("agents-title").classList.remove("hidden");
        $("agents").appendChild(makeAgentCard(d));
        state.agents[d.id] = true;
      }
      $("m-agents").textContent = state.total;
      updateProgress();
      feed("+ " + ev.message);
      break;
    case "child_start":
      renderChildrenGrid();
      feed("▶ " + ev.message);
      break;
    case "child_done":
      renderChildrenGrid();
      updateProgress();
      feed((d.status === "completed" ? "✓ " : "✗ ") + ev.message);
      break;
    case "subtask_start":
      setAgentState(d.id, "running", "running");
      setPlanNodeState(d.id, "running");
      if (state.openAgentId === d.id) renderAgentModal(state.agentData[d.id]);
      feed("▶ " + ev.message);
      break;
    case "agent_step": {
      if (state.openAgentId === d.id) renderAgentModal(state.agentData[d.id]);
      // live-follow: the full-transcript panel appends steps for its open subtask
      if (state.transcriptId === d.id && !$("transcript-modal").classList.contains("hidden"))
        appendTranscriptStep(d);
      const card = $("agent-" + d.id);
      if (!card) break;
      const stream = card.querySelector(".ac-stream");
      if (!stream) break;
      const line = document.createElement("div");
      line.className = "as-" + (d.kind || "text");
      line.textContent = clipText(formatStepLine(d), 300);
      stream.appendChild(line);
      while (stream.childElementCount > 6) stream.removeChild(stream.firstChild);
      stream.scrollTop = stream.scrollHeight;
      break;
    }
    case "subtask_review":
      setAgentState(d.id, d.passed ? "passed" : "failed", d.passed ? "passed" : "failed", d.score);
      setPlanNodeState(d.id, d.passed ? "passed" : "failed");
      if (state.openAgentId === d.id) renderAgentModal(state.agentData[d.id]);
      updateProgress();
      setMetricsFrom(d);
      if ("cost_usd" in d) updateBudgetMeter(d.cost_usd);
      feed((d.passed ? "✓ " : "✗ ") + ev.message + (d.objective_note ? "  ·  " + d.objective_note : ""));
      break;
    case "subtask_retry":
      setAgentState(d.id, "running", "retrying");
      setPlanNodeState(d.id, "running");
      if (state.openAgentId === d.id) renderAgentModal(state.agentData[d.id]);
      updateProgress();
      feed("↻ " + ev.message);
      break;
    case "message":
      $("m-msgs").textContent = state.messages;
      feed("✉ " + d.sender + " → " + (d.recipient || "all") + ": " + d.content, "msg");
      break;
    case "sessions":
      $("m-sessions").textContent = d.created ?? 0;
      $("m-reaped").textContent = d.reaped ?? 0;
      feed(ev.message);
      break;
    case "budget":
      if (d.budget) { state.budgetUsd = d.budget; updateBudgetMeter(d.cost); }
      showToast("⚠ " + (ev.message || "Budget exceeded"), "warn", 7000);
      feed("⚠ " + ev.message);
      break;
    case "diff":
      feed(`✎ ${d.id}: +${(d.added || []).length} file(s), ~${(d.modified || []).length} changed`);
      break;
    case "git":
      showToast("⎇ " + (ev.message || "Committed to a branch"), "success", 6000);
      feed("⎇ " + ev.message);
      break;
    case "reflection":
      state.reflection = d;
      feed("✦ Lessons: " + (d.summary || ""));
      break;
    case "control":
      feed("⏸ " + (ev.message || ""));
      if (d.paused === true) { $("status-pill").className = "pill"; $("status-pill").textContent = "paused"; }
      else if (d.paused === false) { $("status-pill").className = "pill pill-running"; $("status-pill").textContent = "running"; }
      updateRunControls();
      break;
    // Provision: an agent needs the user mid-run — a clarifying question (`ask`) or a
    // permission request (`permission`, e.g. a protected path). Answers/decisions are
    // delivered to the run through the steer endpoint.
    case "ask":
      renderAttention({ kind: "question", id: d.id, agent: d.agent, text: d.question || ev.message, options: d.options || [] });
      notify("ask", (d.agent ? d.agent + " asks: " : "") + (d.question || ev.message || "needs your input"));
      feed("? " + ev.message);
      break;
    case "permission":
      renderAttention({ kind: "permission", id: d.id, agent: d.agent, text: d.request || ev.message, options: [] });
      notify("permission", (d.agent ? d.agent + " requests permission: " : "Permission requested: ") + (d.request || ev.message || ""));
      feed("⚠ " + ev.message);
      break;
    case "execution": {
      setPhase("verify");
      const el = $("m-tests");
      if (d.ran === false) { el.textContent = "—"; el.style.color = ""; }
      else { el.textContent = d.passed ? "✓" : "✗"; el.style.color = d.passed ? "var(--green)" : "var(--red)"; }
      feed((d.ran === false ? "○ " : (d.passed ? "✓ " : "✗ ")) + ev.message);
      break;
    }
    case "brief":
      $("brief-card").classList.remove("hidden");
      $("brief-tldr").textContent = d.tldr || "";
      $("brief-points").innerHTML = "";
      (d.key_points || []).forEach(p => {
        const li = document.createElement("li"); li.textContent = p; $("brief-points").appendChild(li);
      });
      break;
    case "done":
      setPhase("done");
      $("progress-bar").style.width = "100%";
      if ("passed" in d) $("progress-label").textContent = `${d.passed}/${d.total} subtasks`;
      setMetricsFrom(d);
      if (d.cost_usd !== undefined) {
        $("m-cost").textContent = fmtCost(d.cost_usd);
        updateBudgetMeter(d.cost_usd);
      }
      $("cancel-btn").classList.add("hidden");
      $("pause-btn").classList.add("hidden"); $("steer-btn").classList.add("hidden");
      if (d.tests && d.tests !== "n/a") {
        const el = $("m-tests");
        el.textContent = d.tests === "passed" ? "✓" : "✗";
        el.style.color = d.tests === "passed" ? "var(--green)" : "var(--red)";
      }
      if ("passed" in d) {
        const soft = d.run_status === "partial";
        $("status-pill").className = "pill " + (soft ? "pill-warn" : "pill-done");
        $("status-pill").textContent = `${d.passed}/${d.total} passed` + (soft ? " · partial" : "");
        if (d.quality_score != null) $("status-pill").title = "quality " + d.quality_score + "/100";
      }
      if (d.quality_score != null) {
        const ul = $("usage-line");
        if (ul) ul.textContent = (ul.textContent ? ul.textContent + "  ·  " : "") + "quality " + d.quality_score + "/100";
      }
      revealFeedback(d.task_id);
      if (d.over_budget) {
        $("status-pill").className = "pill pill-err";
        $("status-pill").textContent = "over budget";
        showToast("Stopped at budget cap · $" + Number(d.cost_usd || 0).toFixed(2) + " spent", "warn", 7000);
      } else if ("passed" in d) {
        showToast(`Done · ${d.passed}/${d.total} passed · $${Number(d.cost_usd || 0).toFixed(4)}`, "success");
      }
      renderTimeline();
      $("timeline-card").classList.remove("hidden");
      state.docsId = d.task_id || null;
      if (state.docsId) loadRunCost(state.docsId);
      notify(d.over_budget ? "error" : "done",
             d.over_budget ? "Run stopped at its budget cap"
               : "passed" in d ? `Task done · ${d.passed}/${d.total} passed` : "Task done",
             d.task_id);
      stopTimer();
      setConn("done", "badge-done");
      $("run-btn").disabled = false;
      loadRecent();
      loadQueue();
      if (knowVisible("memory")) loadMemory();
      if (knowVisible("graph")) loadGraph();
      if (knowVisible("files")) loadFiles();
      break;
    case "error":
      if (ev.message === "unknown task") {  // live stream gone (e.g. server restarted) — show docs
        setConn("idle", "badge-idle");
        $("run-btn").disabled = false;
        if (state.taskId) openDocs(state.taskId);
        return;
      }
      $("status-pill").className = "pill pill-err";
      $("status-pill").textContent = (d.message === "cancelled") ? "cancelled" : "error";
      notify("error", d.message === "cancelled" ? "Run cancelled" : (ev.message || "Run failed"));
      feed("✗ " + (ev.message || "error"));
      stopTimer();
      $("cancel-btn").classList.add("hidden");
      $("pause-btn").classList.add("hidden"); $("steer-btn").classList.add("hidden");
      setConn(d.message === "cancelled" ? "cancelled" : "error", "badge-err");
      showToast(ev.message || "Run failed", d.message === "cancelled" ? "warn" : "error");
      $("run-btn").disabled = false;
      loadRecent();
      loadQueue();
      break;
    default:
      feed(ev.message || ev.type);
  }
}

// ---- needs-your-input cards (clarifying questions + permission requests) ----
let _attnOpen = 0;
function _attnCount() {
  $("attn-count").textContent = _attnOpen ? _attnOpen + " open" : "";
  if (!_attnOpen) $("attention-card").classList.add("hidden");
}
async function _attnSteer(note) {
  if (!state.taskId) return false;
  try {
    const resp = await fetch(`/api/run/${state.taskId}/steer`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ note }),
    });
    if (!resp.ok) {  // e.g. the run just ended (404) — don't mark the card resolved
      showToast("Could not deliver the answer — the run is no longer accepting input", "warn", 6000);
      return false;
    }
    return true;
  } catch (e) { showToast("Could not deliver the answer", "warn"); return false; }
}
function renderAttention(req) {
  const list = $("attention-list");
  const item = document.createElement("div");
  item.className = "attn-item";
  const kindLabel = req.kind === "permission" ? "permission request" : "clarifying question";
  item.innerHTML =
    `<div class="attn-top">` +
    `<span class="ac-agent"><span class="ac-dot"></span>${escapeHtml(req.agent || "agent")}</span>` +
    (req.id ? `<span class="chip">${escapeHtml(req.id)}</span>` : "") +
    `<span class="attn-kind${req.kind === "permission" ? " attn-kind-permission" : ""}">${kindLabel}</span></div>` +
    `<p class="attn-q">${escapeHtml(req.text || "")}</p>` +
    `<div class="attn-controls"></div>`;
  const controls = item.querySelector(".attn-controls");
  const resolve = (label, denied) => {
    controls.remove();
    const done = document.createElement("div");
    done.className = "attn-done" + (denied ? " attn-denied" : "");
    done.textContent = label;
    item.appendChild(done);
    _attnOpen = Math.max(0, _attnOpen - 1);
    _attnCount();
  };
  if (req.kind === "permission") {
    const row = document.createElement("div");
    row.className = "attn-row";
    [["Allow once", "btn-primary"], ["Allow for this run", "ghost-btn"], ["Deny", "btn-danger"]].forEach(([label, cls]) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = cls; b.textContent = label;
      b.onclick = async () => {
        const denied = label === "Deny";
        const note = `[permission ${req.id || ""}] ${denied ? "DENIED" : label.toUpperCase()}: ${req.text || ""}`;
        if (await _attnSteer(note)) resolve(denied ? "denied — the agent will plan around it" : (req.id ? "granted · " + req.id + " resumed" : "granted"), denied);
      };
      row.appendChild(b);
    });
    controls.appendChild(row);
  } else {
    if ((req.options || []).length) {
      const row = document.createElement("div");
      row.className = "attn-row";
      req.options.forEach(opt => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "ghost-btn"; b.textContent = opt;
        b.onclick = async () => {
          if (await _attnSteer(`[answer ${req.id || ""}] ${opt}`)) resolve("answered · " + opt);
        };
        row.appendChild(b);
      });
      controls.appendChild(row);
    }
    const row = document.createElement("div");
    row.className = "attn-row";
    const input = document.createElement("input");
    input.type = "text"; input.className = "title-input"; input.placeholder = "…or answer in plain English";
    const send = document.createElement("button");
    send.type = "button"; send.className = "btn-primary"; send.textContent = "Send";
    const submit = async () => {
      const v = input.value.trim();
      if (!v) { input.focus(); return; }
      if (await _attnSteer(`[answer ${req.id || ""}] ${v}`)) resolve(req.id ? "answered — " + req.id + " resumed" : "answered");
    };
    send.onclick = submit;
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    row.appendChild(input); row.appendChild(send);
    controls.appendChild(row);
  }
  list.appendChild(item);
  _attnOpen++;
  _attnCount();
  $("attention-card").classList.remove("hidden");
}

function runTask() {
  const prompt = $("prompt").value.trim();
  if (!prompt) { $("prompt").focus(); return; }
  if (multiOpen && !selectedMultiProjects().length) {
    showToast("Pick at least one project to fan out across (2+ run in parallel)", "warn");
    return;
  }
  if ($("review-plan").checked) requestPlan(prompt);
  else launchRun({ prompt, ...getControls() }, prompt);
}

async function requestPlan(prompt) {
  openTaskDetail("Plan review");
  $("run-btn").disabled = true;
  $("empty").classList.add("hidden");
  $("run-view").classList.add("hidden");
  $("plan-editor").classList.remove("hidden");
  $("pe-summary").textContent = "Planning…";
  $("pe-list").innerHTML = "";
  setConn("planning…", "badge-idle");
  let data;
  try {
    data = await (await fetch("/api/plan", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt, ...getControls() }),
    })).json();
  } catch (e) { $("pe-summary").textContent = "Could not generate plan: " + e; $("run-btn").disabled = false; return; }
  if (data.error) { $("pe-summary").textContent = "Plan failed: " + data.error; $("run-btn").disabled = false; return; }
  state.currentPrompt = prompt;
  state.currentPlanId = data.plan_id;
  state.currentPlan = data.plan;
  // surface the orchestrator's suggested title so it's visible/editable (only if blank)
  if (!$("task-title").value.trim() && data.plan.title) $("task-title").value = data.plan.title;
  await renderPlanEditor(data.plan);
  setConn("idle", "badge-idle");
  $("run-btn").disabled = false;
}

async function renderPlanEditor(plan) {
  $("pe-summary").textContent = plan.summary || "";
  const names = await getAgentNames();
  const list = $("pe-list");
  list.innerHTML = "";
  (plan.subtasks || []).forEach(st => {
    const row = document.createElement("div");
    row.className = "pe-row";
    const opts = names.map(n => `<option value="${escapeAttr(n)}"${n === st.agent ? " selected" : ""}>${escapeHtml(n)}</option>`).join("");
    row.innerHTML = `<span class="chip">${escapeHtml(st.id)}</span>` +
      `<input class="pe-title" value="${escapeAttr(st.title)}" aria-label="Title for step ${escapeAttr(st.id)}" />` +
      `<span class="pe-agent-wrap"><span class="pe-dot" style="background:${agentStyle(st.agent).color}" aria-hidden="true"></span><select class="pe-agent" aria-label="Agent for step ${escapeAttr(st.id)}">${opts}</select></span>` +
      `<button type="button" class="pe-remove" title="Remove step" aria-label="Remove step ${escapeAttr(st.id)}">✕</button>`;
    const _sel = row.querySelector(".pe-agent"), _dot = row.querySelector(".pe-dot");
    _sel.onchange = () => { _dot.style.background = agentStyle(_sel.value).color; };
    row.querySelector(".pe-remove").onclick = () => row.remove();
    list.appendChild(row);
  });
}

// Collect the current (possibly hand-edited) plan from the editor rows, so refine + approve
// both build on what the user sees.
function collectEditedPlan() {
  const rows = [...document.querySelectorAll("#pe-list .pe-row")];
  const keep = new Set(rows.map(r => r.querySelector(".chip").textContent));
  const subtasks = rows.map(r => {
    const id = r.querySelector(".chip").textContent;
    const orig = (state.currentPlan.subtasks || []).find(s => s.id === id) || {};
    return {
      ...orig, id,
      title: r.querySelector(".pe-title").value,
      agent: r.querySelector(".pe-agent").value,
      depends_on: (orig.depends_on || []).filter(d => keep.has(d)),
    };
  });
  return { title: state.currentPlan.title, summary: state.currentPlan.summary, subtasks };
}

// Interactive plan mode: revise the plan from a natural-language instruction, in place.
async function refinePlan() {
  const instruction = $("pe-instruction").value.trim();
  if (!instruction) return;
  const btn = $("refine-btn");
  btn.disabled = true; btn.textContent = "↻ Refining…";
  $("pe-warning").classList.add("hidden");
  const prevSummary = $("pe-summary").textContent;
  $("pe-summary").textContent = "Refining the plan…";
  let data;
  try {
    data = await (await fetch("/api/plan/refine", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: state.currentPrompt, plan: collectEditedPlan(), instruction, ...getControls() }),
    })).json();
  } catch (e) { $("pe-summary").textContent = prevSummary; showToast("Refine failed: " + e, "warn"); btn.disabled = false; btn.textContent = "↻ Refine"; return; }
  if (data.error) { $("pe-summary").textContent = prevSummary; showToast("Refine failed: " + data.error, "warn"); }
  else {
    state.currentPlan = data.plan;
    state.currentPlanId = data.plan_id;
    await renderPlanEditor(data.plan);
    if (!$("task-title").value.trim() && data.plan.title) $("task-title").value = data.plan.title;
    $("pe-instruction").value = "";
    if (data.warning) { const w = $("pe-warning"); w.textContent = "⚠ " + data.warning; w.classList.remove("hidden"); }
    showToast("Plan revised", "success");
  }
  btn.disabled = false; btn.textContent = "↻ Refine";
  $("pe-instruction").focus();
}

function approvePlan() {
  const rows = [...document.querySelectorAll("#pe-list .pe-row")];
  if (!rows.length) return;
  const plan = collectEditedPlan();
  $("plan-editor").classList.add("hidden");
  showToast("Plan approved · running", "success");
  launchRun({ prompt: state.currentPrompt, plan, task_id: state.currentPlanId, ...getControls() }, state.currentPrompt);
}

function discardPlan() {
  $("plan-editor").classList.add("hidden");
  $("run-btn").disabled = false;
  clearContinue();
  backToProject();
}

async function launchRun(body, prompt) {
  openTaskDetail((body && body.title) || prompt);
  $("run-btn").disabled = true;
  // per-run budget wins; otherwise fall back to the server's ADA_BUDGET_USD default
  state.budgetUsd = (body && body.budget) || serverConfig.budget_usd || 0;
  resetRunView(prompt);
  setConn("connecting…", "badge-idle");
  let taskId;
  try {
    const resp = await fetch("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) {  // e.g. a validation error (400)
      const msg = data.error || ("HTTP " + resp.status);
      feed("✗ could not start run: " + msg);
      showToast("Could not start run: " + msg, "error", 6000);
      $("run-btn").disabled = false;
      return;
    }
    taskId = data.task_id;
  } catch (e) { feed("✗ could not start run: " + e); $("run-btn").disabled = false; return; }
  clearContinue();  // the continuation context was captured in `body`; reset for the next task
  connectWS(taskId);
}

function connectWS(taskId) {
  state.taskId = taskId;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${taskId}`);
  ws.onopen = () => setConn("live", "badge-live");
  ws.onmessage = (m) => handleEvent(JSON.parse(m.data));
  ws.onclose = () => { if ($("status-pill").textContent === "running") setConn("disconnected", "badge-err"); };
  ws.onerror = () => setConn("ws error", "badge-err");
}

// Re-open a task and replay its full event stream (the server buffers every event,
// so a running task is reconstructed live, and a recent one shows completely).
function attachToRun(id, prompt) {
  $("run-btn").disabled = true;
  openTaskDetail(prompt || id);
  // reattaching we no longer know the run's own cap — use the server default (may be 0)
  state.budgetUsd = serverConfig.budget_usd || 0;
  resetRunView(prompt || id);
  setConn("connecting…", "badge-idle");
  connectWS(id);
}

let _agentNames = null;
async function getAgentNames() {
  if (_agentNames) return _agentNames;
  // /api/agents is {builtin, custom, tools} — customs are routable like built-ins
  try { _agentNames = agentsListModel(await (await fetch("/api/agents")).json()).names; }
  catch (e) { _agentNames = ["researcher", "coder", "documenter"]; }
  if (!_agentNames.length) _agentNames = ["researcher", "coder", "documenter"];
  return _agentNames;
}

async function cancelRun() {
  if (!state.taskId) return;
  $("cancel-btn").disabled = true;
  feed("⛔ cancel requested…");
  showToast("Cancel requested…", "warn");
  try { await fetch(`/api/run/${state.taskId}/cancel`, { method: "POST" }); } catch (e) { /* ignore */ }
}

// ---- the project's task lists (Overview: 3 most recent · Tasks tab: all) ----

// Render merged task rows (util.projectTaskRows) into a .task-list <ul>.
function renderTaskRows(ul, rows, limit) {
  if (!ul) return;
  const items = limit ? rows.slice(0, limit) : rows;
  if (!items.length) {
    ul.innerHTML = '<li class="muted">No tasks in this project yet.</li>';
    return;
  }
  ul.innerHTML = "";
  items.forEach(it => {
    const li = document.createElement("li");
    li.className = "item";
    li.dataset.id = it.id;
    const status = it.status || "";
    const cost = (it.cost != null && it.cost > 0) ? fmtCost(it.cost) : "";
    const quality = it.quality != null ? `<span class="r-quality">quality ${escapeHtml(String(it.quality))}/100</span>` : "";
    const tests = it.tests === "passed" ? '<span class="r-tests-pass">tests ✓</span>'
                : it.tests === "failed" ? '<span class="r-tests-fail">tests ✗</span>' : "";
    const statusLabel = status === "queued" && it.position != null ? `queued · #${it.position}` : status;
    const meta = `<div class="r-meta">${status ? `<span class="r-status ${escapeAttr(status)}"><span class="r-status-dot"></span>${escapeHtml(statusLabel)}</span>` : ""}` +
                 `${quality}${cost ? `<span class="r-cost">${cost}</span>` : ""}${tests}</div>`;
    const titleText = it.title || it.id;
    const cancelBtn = (status === "running" || status === "queued")
      ? `<button type="button" class="r-act r-cancel" title="Cancel this run">Cancel</button>` : "";
    // W5: retry/resume affordance on runs that stopped short of completion
    const resumeBtn = isResumable(status)
      ? `<button type="button" class="r-act r-resume" title="Retry / resume this run">Resume</button>` : "";
    li.innerHTML = `<div class="r-title">${escapeHtml(titleText)}</div>` +
      `${meta}<div class="r-actions"><button type="button" class="r-files">Files →</button>` +
      `${cancelBtn}${resumeBtn}<button type="button" class="r-act r-delete" title="Delete this task">Delete</button></div>`;
    makeActivatable(li, (e) => {
      if (e && e.target && e.target.closest("button")) return;
      if (it.live) attachToRun(it.id, titleText); else openTask(it.id, it);
    }, `Open task ${titleText}`);
    li.querySelector(".r-files").onclick = (e) => {
      e.stopPropagation();
      openFilesView(it.id);
    };
    const cb = li.querySelector(".r-cancel");
    if (cb) cb.onclick = (e) => { e.stopPropagation(); cancelTask(it.id); };
    const rb = li.querySelector(".r-resume");
    if (rb) rb.onclick = (e) => { e.stopPropagation(); resumeTask(it.id, titleText); };
    li.querySelector(".r-delete").onclick = (e) => { e.stopPropagation(); deleteTask(it.id, titleText); };
    ul.appendChild(li);
  });
}

// The project's runs: /api/projects/{slug}/runs for real projects; the "multi"
// pseudo-entry composes /api/tasks rows with project === "multi" client-side.
async function fetchProjectRuns(slug) {
  try {
    if (slug === "multi") {
      const all = await (await fetch("/api/tasks")).json();
      return (Array.isArray(all) ? all : []).filter(t => t.project === "multi");
    }
    const r = await fetch("/api/projects/" + encodeURIComponent(slug) + "/runs?limit=100");
    if (!r.ok) return [];
    const rows = await r.json();
    return Array.isArray(rows) ? rows : [];
  } catch (e) { return []; }
}

async function loadRecent() {
  const slug = selectedProject();
  if (!slug) return;
  let act = null;
  try {
    const r = await fetch("/api/projects/" + encodeURIComponent(slug) + "/activity");
    if (r.ok) act = await r.json();
  } catch (e) { /* tolerate */ }
  const runs = await fetchProjectRuns(slug);
  const rows = projectTaskRows(runs, act);
  renderTaskRows($("ov-recent-list"), rows, 3);
  renderTaskRows($("task-list"), rows);
  renderQualitySparkline(runs);
}

// Quality-trend sparkline in the Tasks tab header (rows arrive newest-first).
function renderQualitySparkline(runs) {
  const svg = $("ph-sparkline");
  if (!svg) return;
  svg.innerHTML = "";
  const scores = (runs || []).slice().reverse()
    .map(r => r.quality_score).filter(v => v != null);
  const sp = sparklinePoints(scores, 140, 32, 3);
  $("ph-trend-label").textContent = sp.last != null ? `latest quality ${sp.last}/100` : "";
  if (sp.drawable) {
    const pl = document.createElementNS(SVGNS, "polyline");
    pl.setAttribute("points", sp.points);
    pl.setAttribute("class", "ph-spark-line");
    svg.appendChild(pl);
  }
}

async function resumeTask(id, title) {
  let resp;
  try { resp = await fetch(`/api/tasks/${encodeURIComponent(id)}/resume`, { method: "POST" }); }
  catch (e) { showToast("Resume failed: " + e, "error"); return; }
  if (resp.status === 501) { showToast("Resume is not available yet on this server", "warn", 6000); return; }
  let data = {};
  try { data = await resp.json(); } catch (e) { /* ignore */ }
  if (!resp.ok) { showToast("Resume failed: " + (data.error || ("HTTP " + resp.status)), "error", 6000); return; }
  showToast(data.status === "queued" ? "Resume queued" : "Resuming task", "success");
  attachToRun(id, title || id);
  loadQueue();
  loadRecent();
}

async function cancelTask(id) {
  try {
    await fetch(`/api/run/${id}/cancel`, { method: "POST" });
    showToast("Cancelling run…", "warn");
  } catch (e) { showToast("Cancel failed", "error"); }
  setTimeout(() => { loadRecent(); loadQueue(); }, 700);
}

function deleteTask(id, label) {
  // Optimistic + deferred: hide the row(s) now, actually delete after a grace window
  // unless the user clicks Undo. No backend restore needed — the DELETE never fires if undone.
  const rows = [...document.querySelectorAll(`.task-list li[data-id="${CSS.escape(id)}"]`)];
  rows.forEach(r => { r.style.display = "none"; });

  const wasOnScreen = (state.taskId === id || state.docsId === id);
  if (wasOnScreen) {
    state.taskId = null; state.docsId = null;
    if (currentMainView === "task") backToProject();
  }

  let undone = false;
  const commit = setTimeout(async () => {
    if (undone) return;
    try { await fetch("/api/tasks/" + id, { method: "DELETE" }); }
    catch (e) { showToast("Delete failed", "error"); }
    loadRecent();
  }, 6000);

  showUndoToast(`Deleted "${label || id}"`, () => {
    undone = true;
    clearTimeout(commit);
    rows.forEach(r => { r.style.display = ""; });
    if (wasOnScreen) loadRecent();   // bring the view back into reach
    showToast("Restored", "success", 2000);
  }, 6000);
}

function showUndoToast(msg, onUndo, ms = 6000) {
  const el = document.createElement("div");
  el.className = "toast warn toast-action";
  const span = document.createElement("span");
  span.textContent = msg;
  const btn = document.createElement("button");
  btn.type = "button"; btn.className = "toast-undo"; btn.textContent = "Undo";
  el.appendChild(span); el.appendChild(btn);
  $("toasts").appendChild(el);
  const dismiss = () => { el.style.transition = "opacity .3s"; el.style.opacity = "0"; setTimeout(() => el.remove(), 300); };
  const timer = setTimeout(dismiss, ms);
  btn.onclick = () => { clearTimeout(timer); dismiss(); if (onUndo) onUndo(); };
}

let currentDocs = null;
let currentDocsId = null;
async function openDocs(id) {
  try {
    currentDocs = await (await fetch("/api/tasks/" + id)).json();
  } catch (e) { return; }
  currentDocsId = id;
  openModalEl("modal");
  selectTab("brief");
}
function selectTab(which) {
  document.querySelectorAll("#modal .tab").forEach(t => {
    const on = t.dataset.doc === which;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  });
  if (which === "diff") { renderDiffTab(); return; }
  $("modal-content").textContent = (currentDocs && currentDocs[which]) || "(empty)";
}

// ---- W5: diff viewer — unified `git diff HEAD` of the task's workspace ----
async function renderDiffTab() {
  const el = $("modal-content");
  el.textContent = "Loading diff…";
  let d;
  try { d = await (await fetch("/api/tasks/" + encodeURIComponent(currentDocsId) + "/diff")).json(); }
  catch (e) { el.textContent = "Could not load the diff."; return; }
  if (!d.is_git) { el.textContent = "Not a git workspace — no diff available."; return; }
  if (!d.diff && !d.status) { el.textContent = "No changes — the workspace is clean."; return; }
  const lines = renderDiffHtml(d.diff || "");
  const status = d.status
    ? `<span class="d-hunk">git status --porcelain</span>\n${escapeHtml(d.status)}\n` : "";
  el.innerHTML = status + lines + (d.truncated ? "\n\n…[diff truncated at 200 KB]" : "");
}

// ---- full historical task view (reconstructed from plan.md + report.md + brief.md) ----
function planSummary(md) {
  const lines = (md || "").split("\n");
  const i = lines.findIndex(l => /^##\s+Approach/i.test(l));
  if (i < 0) return "";
  const out = [];
  for (let j = i + 1; j < lines.length; j++) { if (/^##\s/.test(lines[j])) break; if (lines[j].trim()) out.push(lines[j].trim()); }
  return out.join(" ");
}
function parsePlan(md) {
  const lines = (md || "").split("\n");
  const subs = []; let cur = null, inCrit = false;
  for (const l of lines) {
    let m;
    if ((m = l.match(/^###\s+([^:]+):\s+(.+)$/))) { cur = { id: m[1].trim(), title: m[2].trim(), agent: "", depends_on: [], acceptance_criteria: [] }; subs.push(cur); inCrit = false; continue; }
    if (!cur) continue;
    if ((m = l.match(/^-\s+\*\*Agent:\*\*\s+(.+)$/))) { cur.agent = m[1].trim(); inCrit = false; continue; }
    if ((m = l.match(/^-\s+\*\*Depends on:\*\*\s+(.+)$/))) { const v = m[1].trim(); cur.depends_on = (v === "\u2014" || v === "-") ? [] : v.split(/,\s*/).map(s => s.trim()).filter(Boolean); inCrit = false; continue; }
    if (/^-\s+\*\*Acceptance criteria:\*\*/.test(l)) { inCrit = true; continue; }
    if (inCrit) { const c = l.match(/^\s+-\s+(.+)$/); if (c && c[1].trim() !== "(none)") cur.acceptance_criteria.push(c[1].trim()); }
  }
  return subs;
}
function parseReport(md) {
  const lines = (md || "").split("\n");
  const map = {}; let cur = null, inResult = false, buf = [];
  const flush = () => { if (cur) { let r = buf.join("\n").trim(); if (/^_\(no result\)_$/.test(r)) r = ""; cur.result = r; } };
  for (const l of lines) {
    let m;
    if ((m = l.match(/^###\s+([^:]+):\s+(.*?)\s+\u2014\s+`([^`]+)`\s*$/))) { flush(); cur = { status: m[3].trim(), score: null, result: "" }; map[m[1].trim()] = cur; inResult = false; buf = []; continue; }
    if (!cur) continue;
    if ((m = l.match(/\(score\s+(\d+)\)/))) cur.score = m[1];
    if (/^\*\*Result:\*\*/.test(l)) { inResult = true; buf = []; continue; }
    if (inResult) { if (/^##?#?\s/.test(l)) inResult = false; else buf.push(l); }
  }
  flush();
  return map;
}
function parseBrief(md) {
  const lines = (md || "").split("\n"); let tldr = "", sec = ""; const points = [];
  for (const l of lines) {
    if (/^##\s+TL;DR/i.test(l)) { sec = "tldr"; continue; }
    if (/^##\s+Key points/i.test(l)) { sec = "points"; continue; }
    if (/^##\s/.test(l)) { sec = ""; continue; }
    if (sec === "tldr" && l.trim() && !tldr) tldr = l.trim();
    if (sec === "points") { const m = l.match(/^-\s+(.+)$/); if (m) points.push(m[1].trim()); }
  }
  return { tldr, points };
}

// F3: a task's children (cross-project fan-out) — null when it has none / endpoint 501s.
async function fetchChildren(id) {
  try {
    const resp = await fetch("/api/tasks/" + encodeURIComponent(id) + "/children");
    if (!resp.ok) return null;
    const data = await resp.json();
    return (data.children && data.children.length) ? data.children : null;
  } catch (e) { return null; }
}

// F3: historical view of a fan-out parent — children grid + brief instead of agent cards.
function openParentTask(id, docs, kids, meta) {
  const mm = (docs.meta && Object.keys(docs.meta).length) ? docs.meta : (meta || {});
  const prompt = mm.prompt || (meta && (meta.prompt || meta.title)) || id;
  resetRunView(prompt);
  stopTimer();
  $("timer").textContent = "";
  $("cancel-btn").classList.add("hidden");
  state.children = childrenFromRows(kids);
  state.total = state.children.length;
  renderChildrenGrid();
  $("m-agents").textContent = state.children.length;
  if (mm.cost_usd != null) $("m-cost").textContent = mm.cost_usd > 0 ? fmtCost(mm.cost_usd) : "—";
  const b = parseBrief(docs.brief || "");
  if (b.tldr) {
    $("brief-card").classList.remove("hidden");
    $("brief-tldr").textContent = b.tldr;
    $("brief-points").innerHTML = "";
    b.points.forEach(p => { const li = document.createElement("li"); li.textContent = p; $("brief-points").appendChild(li); });
  }
  const status = mm.status || "";
  const bad = status === "failed" || status === "interrupted" || status === "cancelled" || status === "over_budget";
  const okKids = state.children.filter(c => c.status === "completed").length;
  $("status-pill").className = "pill " + (bad ? "pill-err" : "pill-done");
  $("status-pill").textContent = bad ? status : `${okKids}/${state.children.length} projects`;
  setPhase("done");
  $("progress-bar").style.width = "100%";
  $("progress-label").textContent = `${okKids}/${state.children.length} projects`;
  state.docsId = id;
  updateRunControls();   // historical view — the pill is final, so Pause/Steer hide
}

async function openTask(id, meta) {
  openTaskDetail((meta && (meta.title || meta.prompt)) || id);
  let docs;
  try { docs = await (await fetch("/api/tasks/" + id)).json(); } catch (e) { docs = null; }
  // Breadcrumb: "<Project> / <task title>" — resolved from the task's own record.
  const taskProject = (docs && docs.meta && docs.meta.project) || (meta && meta.project) || currentProject;
  const crumbTitle = (docs && docs.meta && (docs.meta.title || docs.meta.prompt))
    || (meta && (meta.title || meta.prompt)) || id;
  setCrumb(crumbTitle, taskProject);
  // Only a cross-project parent (project "multi") gets the children grid —
  // re-engaged tasks also have child rows but stay ordinary tasks.
  const isMulti = docs && docs.meta && docs.meta.project === "multi";
  const kids = isMulti ? await fetchChildren(id) : null;
  if (kids) { openParentTask(id, docs || {}, kids, meta); return; }  // fan-out parent
  if (!docs || !docs.plan) { backToProject(); openDocs(id); return; }   // older task without parseable docs
  const subtasks = parsePlan(docs.plan);
  const rep = parseReport(docs.report || "");
  const prompt = (meta && (meta.prompt || meta.title)) || id;

  const mm = (docs.meta && Object.keys(docs.meta).length) ? docs.meta : (meta || {});
  resetRunView(prompt);
  stopTimer();
  $("timer").textContent = "";
  $("cancel-btn").classList.add("hidden");
  const setM = (k, v) => { $(k).textContent = (v != null) ? v : "\u2014"; };
  setM("m-sessions", mm.sessions_spawned); setM("m-reaped", mm.sessions_reaped);
  setM("m-kg", mm.kg_nodes); setM("m-edges", mm.kg_edges);
  setM("m-mem", mm.memories); setM("m-msgs", mm.messages);

  const summary = planSummary(docs.plan);
  if (summary) { $("plan-card").classList.remove("hidden"); $("plan-summary").textContent = summary; }

  $("agents-title").classList.remove("hidden");
  subtasks.forEach(st => {
    $("agents").appendChild(makeAgentCard(st));
    state.agentData[st.id] = Object.assign(makeAgentRecord(st), {
      steps: (docs.activity && docs.activity[st.id]) || [],
    });
  });
  $("m-agents").textContent = subtasks.length;
  renderPlanDag(subtasks);

  let passed = 0;
  subtasks.forEach(st => {
    const r = rep[st.id] || {};
    const status = r.status || "passed";
    const cls = status === "passed" ? "passed" : (status === "failed" || status === "blocked") ? "failed" : "queued";
    if (status === "passed") passed++;
    setAgentState(st.id, cls, status, r.score != null ? r.score : undefined);
    setPlanNodeState(st.id, cls);
    const a = state.agentData[st.id];
    if (a) {
      a.status = status;
      a.passed = status === "passed";
      a.score = r.score != null ? r.score : null;
      if (r.result) a.result = r.result;
    }
    if (r.result) {
      const card = $("agent-" + st.id), stream = card && card.querySelector(".ac-stream");
      if (stream) { const line = document.createElement("div"); line.className = "as-text"; line.textContent = r.result; stream.appendChild(line); }
    }
  });

  if (mm.cost_usd != null) $("m-cost").textContent = mm.cost_usd > 0 ? "$" + Number(mm.cost_usd).toFixed(4) : "\u2014";
  if (mm.tests && mm.tests !== "n/a") { const el = $("m-tests"); el.textContent = mm.tests === "passed" ? "\u2713" : "\u2717"; el.style.color = mm.tests === "passed" ? "var(--green)" : "var(--red)"; }
  if (mm.input_tokens != null || mm.output_tokens != null) {
    const i = mm.input_tokens || 0, o = mm.output_tokens || 0;
    $("usage-line").innerHTML = "<b>$" + Number(mm.cost_usd || 0).toFixed(4) + "</b> \u00b7 " + fmtTok(i) + " input + " + fmtTok(o) + " output tokens \u00b7 " + fmtTok(i + o) + " total";
  }
  if (Array.isArray(docs.timeline) && docs.timeline.length) {
    state.timeline = {};
    docs.timeline.forEach(t => { state.timeline[t.id] = { agent: t.agent, start: t.start, end: t.end }; });
    renderTimeline();
    $("timeline-card").classList.remove("hidden");
  }

  const b = parseBrief(docs.brief || "");
  if (b.tldr) {
    $("brief-card").classList.remove("hidden");
    $("brief-tldr").textContent = b.tldr;
    $("brief-points").innerHTML = "";
    b.points.forEach(p => { const li = document.createElement("li"); li.textContent = p; $("brief-points").appendChild(li); });
  }

  const status = mm.status || "";
  const tally = (mm.passed != null && mm.total != null) ? mm.passed + "/" + mm.total : passed + "/" + subtasks.length;
  const bad = status === "failed" || status === "interrupted" || status === "cancelled" || status === "over_budget";
  $("status-pill").className = "pill " + (bad ? "pill-err" : "pill-done");
  $("status-pill").textContent = status === "failed" ? "failed" : status === "cancelled" ? "cancelled" : status === "interrupted" ? "interrupted" : tally + " passed";
  setPhase("done");
  $("progress-bar").style.width = "100%";
  $("progress-label").textContent = tally + " subtasks";
  state.docsId = id;
  loadRunCost(id);   // collapsible per-subtask cost table + timeline annotations
  updateRunControls();   // historical view — the pill is final, so Pause/Steer hide
}

// ---- main views: project (tabs) · task detail · all activity · agents ·
//      global settings console · empty ----
const MAIN_VIEWS = {
  home: "view-home", workspace: "view-workspace",
  project: "view-project", task: "view-task", activity: "view-activity",
  agents: "view-agents", gsettings: "view-gsettings", empty: "view-empty",
};


function showMainView(name) {
  currentMainView = name;
  Object.entries(MAIN_VIEWS).forEach(([n, id]) => $(id).classList.toggle("hidden", n !== name));
  renderSidebar();   // keep the active row / global-nav highlight in sync
}

// First-run empty state: no real projects yet -> invite to create/import one.
function showEmptyState() {
  showMainView("empty");
}

// Is a given knowledge sub-view currently on screen? (refresh-on-done checks)
function knowVisible(sub) {
  return currentMainView === "project" && currentTab === "knowledge" && currentKnow === sub;
}

// ---- project tab bar: Overview · Tasks · Knowledge · Settings ----
function selectProjectTab(tab, force) {
  const tabs = projectTabsModel(currentProject, tab);
  currentTab = tabs.find(t => t.selected).id;
  if (currentMainView !== "project" || force) showMainView("project");
  const visible = new Set(tabs.map(t => t.id));
  document.querySelectorAll("#project-tabs .ptab").forEach(b => {
    const id = b.dataset.tab;
    b.classList.toggle("hidden", !visible.has(id));
    b.setAttribute("aria-selected", id === currentTab ? "true" : "false");
  });
  ["overview", "tasks", "run", "knowledge", "settings"].forEach(t => {
    $("panel-" + t).classList.toggle("hidden", t !== currentTab);
  });
  if (currentTab === "overview") { loadProjectActivity(); loadRecent(); loadPlaybooks(); }
  if (currentTab === "tasks") { loadRecent(); loadQueue(); loadSchedules(); }
  if (currentTab === "run") loadRunPanel();
  if (currentTab === "knowledge") return showKnowledge(currentKnow);
  if (currentTab === "settings") loadSettingsPanel();
}

// ---- knowledge sub-tabs: Memory · Knowledge graph · Files ----
function showKnowledge(sub) {
  currentKnow = sub;
  document.querySelectorAll("#know-tabs .ktab").forEach(b => {
    b.setAttribute("aria-selected", b.dataset.know === sub ? "true" : "false");
  });
  ["memory", "graph", "files"].forEach(s => {
    $("know-" + s).classList.toggle("hidden", s !== sub);
  });
  renderCombineRow();
  if (sub === "memory") return loadMemory();
  if (sub === "graph") return loadGraph();
  if (sub === "files") { setCombineBanner(0); return loadFiles(); }
}

// Jump to a task's files inside the current project's Knowledge tab.
function openFilesView(taskId) {
  if (taskId !== undefined) filesTask = taskId;
  currentKnow = "files";
  showMainView("project");
  return selectProjectTab("knowledge");
}

// ---- task detail: opens INSIDE the project context, with a breadcrumb ----
function setCrumb(title, projectSlug) {
  const slug = projectSlug || currentProject;
  if (!slug) {   // opened from Home with no project context — crumb goes back home
    $("crumb-project").textContent = "Home";
    $("crumb-project").onclick = showHome;
    $("crumb-title").textContent = title || "";
    return;
  }
  const isMulti = slug === "multi";
  const entry = currentProjectEntry(slug);
  $("crumb-project").textContent = isMulti
    ? "⋔ Across projects" : ((entry && entry.name) || slug);
  $("crumb-project").onclick = () => selectProject(slug, "tasks");
  $("crumb-title").textContent = title || "";
}

function openTaskDetail(title, projectSlug) {
  setCrumb(title, projectSlug);
  showMainView("task");
}

function backToProject() {
  if (!currentProject) { showHome(); return; }
  selectProject(currentProject, currentTab);
}

// ============================================================
// HOME — the console's entry screen (GET /api/home).
// ============================================================
function showHome() {
  showMainView("home");
  loadHome();
}

async function loadHome() {
  if (currentMainView !== "home") return;
  let data = null;
  try {
    const resp = await fetch("/api/home");
    if (resp.ok) data = await resp.json();
  } catch (e) { /* leave the view as-is */ }
  if (!data) {
    const err = $("home-errors");
    err.textContent = "Could not reach the server.";
    err.classList.remove("hidden");
    return;
  }
  renderHome(homeModel(data));
}

function _homeTaskLi(r, onOpen) {
  const li = document.createElement("li");
  li.className = "item";
  const proj = r.project
    ? `<span class="proj-tag" title="Project">${escapeHtml(r.project)}</span>` : "";
  const status = r.state || r.status || "";
  const extra = [
    r.progressLabel ? `<span class="r-quality">${escapeHtml(r.progressLabel)}</span>` : "",
    r.qualityLabel ? `<span class="r-quality">${escapeHtml(r.qualityLabel)}</span>` : "",
    r.costLabel ? `<span class="r-cost">${escapeHtml(r.costLabel)}</span>` : "",
  ].join("");
  li.innerHTML = `<div class="r-title">${escapeHtml(r.title)}</div>` +
    `<div class="r-meta">` +
    (status ? `<span class="r-status ${escapeAttr(status)}"><span class="r-status-dot"></span>${escapeHtml(status)}</span>` : "") +
    proj + extra + `</div>`;
  makeActivatable(li, onOpen, `Open task ${r.title}`);
  return li;
}

function renderHome(m) {
  $("home-counts").textContent =
    `${m.counts.projects} project${m.counts.projects === 1 ? "" : "s"}` +
    ` · ${m.counts.workspaces} workspace${m.counts.workspaces === 1 ? "" : "s"}` +
    ` · ${m.counts.custom_agents} custom agent${m.counts.custom_agents === 1 ? "" : "s"}`;
  const err = $("home-errors");
  err.classList.toggle("hidden", !m.errors.length);
  err.textContent = m.errors.length ? "Some sections failed: " + m.errors.join(" · ") : "";

  // ATTENTION — red-accented and first when anything is waiting on the user
  const attn = $("home-attn");
  attn.classList.toggle("home-attn-live", m.prominent);
  $("home-attn-head").textContent = m.prominent
    ? `Needs you — ${m.attentionCount} open` : "Needs you";
  const list = $("home-attn-list");
  if (!m.prominent) {
    list.innerHTML = '<p class="muted home-quiet">Nothing needs you.</p>';
  } else {
    list.innerHTML = "";
    m.attention.forEach(a => {
      const item = document.createElement("div");
      item.className = "attn-item";
      item.innerHTML =
        `<div class="attn-top">` +
        `<span class="ac-agent"><span class="ac-dot"></span>${escapeHtml(a.agent || "agent")}</span>` +
        (a.project ? `<span class="proj-tag">${escapeHtml(a.project)}</span>` : "") +
        `<span class="attn-kind${a.kind === "permission" ? " attn-kind-permission" : ""}">${escapeHtml(a.kindLabel)}</span></div>` +
        `<p class="attn-q">${escapeHtml(a.text)}</p>` +
        (a.options.length
          ? `<p class="attn-q muted">options: ${escapeHtml(a.options.join(" · "))}</p>` : "") +
        `<span class="home-attn-open">Answer in the task →</span>`;
      makeActivatable(item, () => attachToRun(a.taskId, a.text || a.taskId),
        `Open task ${a.taskId} to answer`);
      list.appendChild(item);
    });
  }

  // RUNNING / QUEUED strip
  const live = $("home-live");
  live.innerHTML = "";
  if (!m.live.length) live.innerHTML = '<li class="muted">Nothing running or queued.</li>';
  m.live.forEach(r => live.appendChild(
    _homeTaskLi(r, () => attachToRun(r.taskId, r.title))));

  // RECENT
  const recent = $("home-recent");
  recent.innerHTML = "";
  if (!m.recent.length) recent.innerHTML = '<li class="muted">No finished runs yet.</li>';
  m.recent.forEach(r => recent.appendChild(
    _homeTaskLi(r, () => openTask(r.taskId, null))));

  // SPEND mini-card
  $("home-spend-total").textContent = m.spend.totalLabel;
  $("home-spend-top").textContent = m.spend.topProject
    ? `top: ${m.spend.topProject.project} ${m.spend.topProject.usdLabel} · ${m.spend.runs} runs`
    : (m.spend.runs ? m.spend.runs + " runs" : "no spend yet");

  // BENCHMARKS mini-card
  const stats = $("home-bench-stats");
  const bars = $("home-bench-bars");
  if (!m.benchmarks.available) {
    stats.innerHTML = '<span class="muted">No benchmark history yet.</span>';
    bars.innerHTML = "";
  } else {
    const arrow = (d) => d.dir === "flat" ? ""
      : `<span class="hb-delta hb-${escapeAttr(d.dir)}" title="vs previous run">${escapeHtml(d.label)}</span>`;
    stats.innerHTML =
      `<span class="hb-stat"><b>${escapeHtml(m.benchmarks.passLabel)}</b> pass${arrow(m.benchmarks.passDelta)}</span>` +
      `<span class="hb-stat"><b>${escapeHtml(m.benchmarks.qualityLabel)}</b> quality${arrow(m.benchmarks.qualityDelta)}</span>`;
    bars.innerHTML = m.benchmarks.bars.map(b =>
      `<span class="hb-bar" style="height:${b.hPct}%" title="${escapeAttr(b.label)}"></span>`).join("");
  }

  // WORKSPACES card
  const ws = $("home-ws-list");
  if (!m.workspaces.length) {
    ws.innerHTML = '<p class="muted">No workspaces yet — group related projects from the sidebar.</p>';
  } else {
    ws.innerHTML = "";
    m.workspaces.forEach(w => {
      const row = document.createElement("div");
      row.className = "home-ws-row";
      row.innerHTML = `<span class="ws-mark" aria-hidden="true">▦</span>` +
        `<span class="home-ws-name">${escapeHtml(w.name)}</span>` +
        `<span class="muted">${escapeHtml(w.projectsLabel)}</span>`;
      makeActivatable(row, () => openWorkspace(w.slug), `Open workspace ${w.name}`);
      ws.appendChild(row);
    });
  }
}

// ============================================================
// WORKSPACE VIEW — members, deps editor and the fan-out composer.
// ============================================================
function openWorkspace(slug) {
  currentWorkspace = slug;
  showMainView("workspace");
  renderWorkspace();
  refreshWorkspaceView();
}

async function refreshWorkspaceView() {
  await loadProjects();   // also refreshes workspaceList + sidebar grouping
  renderWorkspace();
}

function _wsInline(id, msg) {
  const el = $(id);
  el.textContent = msg || "";
  el.classList.toggle("hidden", !msg);
}

function renderWorkspace() {
  const entry = workspaceEntry(currentWorkspace);
  if (!entry) {
    $("ws-name").textContent = currentWorkspace || "Workspace";
    $("ws-desc").textContent = "This workspace no longer exists.";
    $("ws-members").innerHTML = "";
    $("ws-deps").innerHTML = "";
    $("ws-subset").innerHTML = "";
    return;
  }
  $("ws-name").textContent = entry.name || entry.slug;
  $("ws-desc").textContent = entry.description || "";
  $("ws-composer-heading").textContent = "Run across " + (entry.name || entry.slug);

  const members = entry.project_slugs || [];
  const nameOf = (slug) => {
    const p = projectList.find(x => x.slug === slug);
    return (p && p.name) || slug;
  };

  // member grid — click opens the project; ✕ unassigns
  const grid = $("ws-members");
  grid.innerHTML = "";
  if (!members.length) {
    grid.innerHTML = '<p class="muted">No member projects yet — assign one below.</p>';
  } else {
    members.forEach(slug => {
      const card = document.createElement("div");
      card.className = "agent-card ws-member";
      const m = activityStripModel(projectActivity[slug]);
      card.innerHTML =
        `<div class="ac-top"><span class="ac-agent ac-name">${escapeHtml(nameOf(slug))}</span>` +
        `<button type="button" class="ghost ws-unassign" title="Remove from this workspace" aria-label="Remove ${escapeAttr(slug)} from workspace">✕</button></div>` +
        `<div class="ac-deps">${escapeHtml(slug)} · ${escapeHtml(m.text)}</div>`;
      makeActivatable(card, (e) => {
        if (e && e.target && e.target.closest("button")) return;
        selectProject(slug, "overview");
      }, `Open project ${nameOf(slug)}`);
      card.querySelector(".ws-unassign").onclick = (e) => {
        e.stopPropagation();
        unassignWorkspaceProject(slug);
      };
      grid.appendChild(card);
    });
  }

  // assign picker — ungrouped projects only
  const sel = $("ws-assign-select");
  const ungrouped = sidebarGroups(projectList).ungrouped.filter(p => !p.archived);
  sel.innerHTML = ungrouped.length
    ? ungrouped.map(p =>
        `<option value="${escapeAttr(p.slug)}">${escapeHtml(p.name || p.slug)}</option>`).join("")
    : '<option value="">(no ungrouped projects)</option>';
  $("ws-assign-btn").disabled = !ungrouped.length;
  _wsInline("ws-assign-error", "");

  // deps editor — per member, multi-select upstream members
  const deps = wsDepsModel(entry);
  const dbox = $("ws-deps");
  if (!deps.editable) {
    dbox.innerHTML = '<p class="muted">Dependencies need at least two member projects.</p>';
  } else {
    dbox.innerHTML = deps.rows.map(r =>
      `<div class="ws-dep-row" data-slug="${escapeAttr(r.slug)}">` +
      `<span class="ws-dep-name">${escapeHtml(nameOf(r.slug))}</span>` +
      `<span class="ws-dep-arrow" aria-hidden="true">← builds on</span>` +
      `<span class="ws-dep-opts">` +
      r.upstreams.map(u =>
        `<label class="check ws-dep-check"><input type="checkbox" value="${escapeAttr(u.slug)}"` +
        `${u.selected ? " checked" : ""} /> ${escapeHtml(nameOf(u.slug))}</label>`).join("") +
      `</span></div>`).join("");
  }
  $("ws-deps-save").disabled = !deps.editable;
  _wsInline("ws-deps-error", "");

  // composer subset — all members checked by default
  const subset = $("ws-subset");
  subset.innerHTML = members.map(slug =>
    `<label class="check ws-sub-check"><input type="checkbox" value="${escapeAttr(slug)}" checked /> ` +
    `${escapeHtml(nameOf(slug))}</label>`).join("") || '<span class="muted">no members</span>';
  _wsInline("ws-run-error", "");
}

async function createWorkspace() {
  const name = $("ws-create-name").value.trim();
  if (!name) { $("ws-create-name").focus(); return; }
  let resp, data = {};
  try {
    resp = await fetch("/api/workspaces", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    data = await resp.json().catch(() => ({}));
  } catch (e) { showToast("Could not create workspace: " + e, "error"); return; }
  if (!resp.ok || data.error) { showToast(data.error || ("HTTP " + resp.status), "error", 6000); return; }
  $("ws-create-name").value = "";
  $("ws-create-row").classList.add("hidden");
  showToast("Workspace created · " + (data.name || name), "success");
  await loadWorkspaces();
  openWorkspace(data.slug);
}

async function saveWorkspaceMeta() {
  const body = { name: $("ws-name-input").value.trim() || null,
                 description: $("ws-desc-input").value };
  let resp, data = {};
  try {
    resp = await fetch("/api/workspaces/" + encodeURIComponent(currentWorkspace), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    data = await resp.json().catch(() => ({}));
  } catch (e) { _wsInline("ws-edit-error", "Save failed: " + e); return; }
  if (!resp.ok || data.error) { _wsInline("ws-edit-error", data.error || ("HTTP " + resp.status)); return; }
  $("ws-edit-form").classList.add("hidden");
  showToast("Workspace updated", "success");
  await refreshWorkspaceView();
}

async function assignWorkspaceProject() {
  const slug = $("ws-assign-select").value;
  if (!slug) return;
  let resp, data = {};
  try {
    resp = await fetch("/api/workspaces/" + encodeURIComponent(currentWorkspace) + "/projects", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: slug }),
    });
    data = await resp.json().catch(() => ({}));
  } catch (e) { _wsInline("ws-assign-error", "Assign failed: " + e); return; }
  if (!resp.ok || data.error) { _wsInline("ws-assign-error", data.error || ("HTTP " + resp.status)); return; }
  showToast("Assigned " + slug, "success", 2500);
  await refreshWorkspaceView();
}

async function unassignWorkspaceProject(slug) {
  let resp, data = {};
  try {
    resp = await fetch("/api/workspaces/" + encodeURIComponent(currentWorkspace)
      + "/projects/" + encodeURIComponent(slug), { method: "DELETE" });
    data = await resp.json().catch(() => ({}));
  } catch (e) { _wsInline("ws-assign-error", "Unassign failed: " + e); return; }
  if (!resp.ok || data.error) { _wsInline("ws-assign-error", data.error || ("HTTP " + resp.status)); return; }
  showToast("Removed " + slug + " from the workspace", "warn", 2500);
  await refreshWorkspaceView();
}

async function saveWorkspaceDeps() {
  const deps = {};
  document.querySelectorAll("#ws-deps .ws-dep-row").forEach(row => {
    const checked = [...row.querySelectorAll("input:checked")].map(cb => cb.value);
    if (checked.length) deps[row.dataset.slug] = checked;
  });
  const btn = $("ws-deps-save");
  btn.disabled = true;
  let resp, data = {};
  try {
    resp = await fetch("/api/workspaces/" + encodeURIComponent(currentWorkspace) + "/deps", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deps }),
    });
    data = await resp.json().catch(() => ({}));
  } catch (e) { _wsInline("ws-deps-error", "Save failed: " + e); btn.disabled = false; return; }
  btn.disabled = false;
  // 400s (unknown slug / self-dep / cycle) surface inline with the server's message
  if (!resp.ok || data.error) { _wsInline("ws-deps-error", data.error || ("HTTP " + resp.status)); return; }
  _wsInline("ws-deps-error", "");
  showToast("Dependencies saved", "success");
  await refreshWorkspaceView();
}

async function runWorkspace() {
  const entry = workspaceEntry(currentWorkspace) || {};
  const subset = [...document.querySelectorAll("#ws-subset input:checked")].map(cb => cb.value);
  const m = wsRunPayload({
    prompt: $("ws-prompt").value, title: $("ws-title").value,
    effort: $("ws-effort").value, budget: $("ws-budget").value,
    subset, members: entry.project_slugs || [],
  });
  if (!m.ok) { _wsInline("ws-run-error", m.errors.join(" · ")); return; }
  _wsInline("ws-run-error", "");
  const btn = $("ws-run");
  btn.disabled = true;
  let resp, data = {};
  try {
    resp = await fetch("/api/workspaces/" + encodeURIComponent(currentWorkspace) + "/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(m.body),
    });
    data = await resp.json().catch(() => ({}));
  } catch (e) { _wsInline("ws-run-error", "Could not start the run: " + e); btn.disabled = false; return; }
  btn.disabled = false;
  if (!resp.ok || data.error || !data.task_id) {
    _wsInline("ws-run-error", data.error || ("HTTP " + resp.status));
    return;
  }
  $("ws-prompt").value = ""; $("ws-title").value = "";
  showToast("Workspace run started", "success");
  state.budgetUsd = m.body.budget || serverConfig.budget_usd || 0;
  // navigate to the created task exactly like the existing multi-project flow
  attachToRun(data.task_id, m.body.title || m.body.prompt);
  loadQueue();
}

// ---- All activity (formerly the Dashboard) ----
function showActivity() {
  showMainView("activity");
  loadDashboard();
}

// ---- Agents: the specialist roster as its own view ----
function showAgents() {

  showMainView("agents");
  loadAgents();
}

// ---- Global settings console: assistant-wide config over the JSON overlay ----
// Field-level auto-save: every control PATCHes its single key on change; a
// per-field flash confirms saved/error and the view re-renders from the server
// (so source badges and reset buttons always reflect persisted truth).
let _gsFields = {};          // key -> field render model (coercion + restart flags)
let _gsFlash = {};           // key -> { cls: "saved"|"error", text }  (transient)
let _gsRestartTouched = false;  // a restart_required field was edited this session

function showGlobalSettings() {
  showMainView("gsettings");
  loadGlobalSettings();
}

async function loadGlobalSettings() {
  const box = $("gs-groups");
  let data = null;
  try {
    const resp = await fetch("/api/settings");
    if (resp.ok) data = await resp.json();
  } catch (e) { /* fall through to the error state */ }
  if (!data) { box.innerHTML = '<p class="muted">Could not load settings.</p>'; return; }
  renderGlobalSettings(settingsGroupsModel(data));
  loadGithubStatus();   // status line above the GitHub group's fields
}

function gsControlHtml(f) {
  const key = escapeAttr(f.key);
  if (f.control === "checkbox") {
    return `<input type="checkbox" id="gs-${key}" class="gs-control" data-key="${key}"${f.checked ? " checked" : ""} />`;
  }
  if (f.control === "select") {
    const opts = f.choices.map(c =>
      `<option value="${escapeAttr(c)}"${c === String(f.value) ? " selected" : ""}>${escapeHtml(c)}</option>`).join("");
    return `<select id="gs-${key}" class="gs-control select" data-key="${key}">${opts}</select>`;
  }
  if (f.control === "number") {
    return `<input type="number" id="gs-${key}" class="gs-control num" data-key="${key}" step="${escapeAttr(f.step)}" value="${escapeAttr(f.value)}" />`;
  }
  return `<input type="text" id="gs-${key}" class="gs-control title-input" data-key="${key}" value="${escapeAttr(f.value)}" />`;
}

function gsFieldHtml(f) {
  const flash = _gsFlash[f.key];
  return `<div class="gs-field">
    <div class="gs-field-head">
      <label class="gs-label" for="gs-${escapeAttr(f.key)}">${escapeHtml(f.label)}</label>
      ${f.restart ? '<span class="gs-restart-tag">restart required</span>' : ""}
      <span class="gs-src ${f.sourceCls}">${escapeHtml(f.sourceLabel)}</span>
      ${f.showReset ? `<button type="button" class="gs-reset" data-key="${escapeAttr(f.key)}" title="Remove override — revert to env/default" aria-label="Reset ${escapeAttr(f.label)}">✕</button>` : ""}
      ${flash ? `<span class="gs-flash gs-flash-${flash.cls}" data-key="${escapeAttr(f.key)}" role="status">${escapeHtml(flash.text)}</span>` : ""}
    </div>
    ${gsControlHtml(f)}
    ${f.help ? `<p class="gs-help">${escapeHtml(f.help)}</p>` : ""}
  </div>`;
}

function renderGlobalSettings(model) {
  $("gs-info").innerHTML = model.info.map(i =>
    `<div class="gs-info-item"><span class="gs-info-k">${escapeHtml(i.label)}</span><code>${escapeHtml(i.value)}</code></div>`).join("");
  $("gs-restart-banner").classList.toggle("hidden", !_gsRestartTouched);
  _gsFields = {};
  model.groups.forEach(g => g.fields.forEach(f => { _gsFields[f.key] = f; }));
  const box = $("gs-groups");
  box.innerHTML = model.groups.map(g => `<section class="card gs-card">
      <h3 class="section-label">${escapeHtml(g.name)}</h3>
      <div class="gs-fields">${g.fields.map(gsFieldHtml).join("")}</div>
    </section>`).join("");
  box.querySelectorAll(".gs-control").forEach(el => { el.onchange = () => gsSubmit(el); });
  box.querySelectorAll(".gs-reset").forEach(b => { b.onclick = () => gsReset(b.dataset.key); });
}

function _gsSetFlash(key, cls, text) {
  _gsFlash[key] = { cls, text: text || (cls === "saved" ? "saved" : "error") };
  setTimeout(() => {   // expire in place — never re-render (the user may be typing)
    if (_gsFlash[key] && _gsFlash[key].cls === cls) {
      delete _gsFlash[key];
      const span = document.querySelector(
        `#gs-groups .gs-flash[data-key="${CSS.escape(key)}"]`);
      if (span) span.remove();
    }
  }, cls === "saved" ? 2000 : 6000);
}

async function gsSubmit(el) {
  const key = el.dataset.key;
  const f = _gsFields[key];
  if (!f) return;
  const raw = f.control === "checkbox" ? el.checked : el.value;
  const c = coerceFieldInput(f, raw);
  if (!c.ok) { _gsSetFlash(key, "error", c.error); loadGlobalSettings(); return; }
  try {
    const resp = await fetch("/api/settings", {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [key]: c.value }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok || body.error) _gsSetFlash(key, "error", body.error || ("HTTP " + resp.status));
    else {
      _gsSetFlash(key, "saved");
      if (f.restart) _gsRestartTouched = true;
    }
  } catch (e) { _gsSetFlash(key, "error", "request failed"); }
  loadGlobalSettings();
}

async function gsReset(key) {
  try {
    const resp = await fetch("/api/settings/" + encodeURIComponent(key), { method: "DELETE" });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok || body.error) _gsSetFlash(key, "error", body.error || ("HTTP " + resp.status));
    else _gsSetFlash(key, "saved", "reverted");
  } catch (e) { _gsSetFlash(key, "error", "request failed"); }
  loadGlobalSettings();
}

// ---- W5: run comparison (Dashboard) ----
async function populateCompareOptions() {
  let tasks = [];
  try { tasks = await (await fetch("/api/tasks")).json(); } catch (e) { /* ignore */ }
  tasks = tasks.filter(t => t.status);  // only rows the run store knows (metrics live there)
  const selA = $("cmp-a"), selB = $("cmp-b");
  if (!selA || !selB) return;
  const prevA = selA.value, prevB = selB.value;
  const opts = tasks.map(t =>
    `<option value="${escapeAttr(t.id)}">${escapeHtml((t.title || t.tldr || t.id).slice(0, 60))}</option>`).join("");
  selA.innerHTML = opts; selB.innerHTML = opts;
  if (tasks.some(t => t.id === prevA)) selA.value = prevA;
  if (tasks.some(t => t.id === prevB)) selB.value = prevB;
  else if (tasks.length > 1 && selB.value === selA.value) selB.value = tasks[1].id;
  $("cmp-btn").disabled = tasks.length < 2;
}

async function compareRuns() {
  const a = $("cmp-a").value, b = $("cmp-b").value;
  const el = $("cmp-result");
  if (!a || !b) return;
  el.classList.remove("muted");
  el.textContent = "Comparing…";
  let data;
  try {
    const resp = await fetch(`/api/runs/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
    data = await resp.json();
    if (!resp.ok || data.error) { el.textContent = "Compare failed: " + (data.error || ("HTTP " + resp.status)); return; }
  } catch (e) { el.textContent = "Compare failed: " + e; return; }
  const rows = compareRowModel(data.a, data.b);
  el.innerHTML =
    `<table class="cmp-table"><thead><tr><th scope="col"></th>` +
    `<th scope="col">${escapeHtml(data.a.title || data.a.id)}</th>` +
    `<th scope="col">${escapeHtml(data.b.title || data.b.id)}</th></tr></thead><tbody>` +
    rows.map(r =>
      `<tr><td>${escapeHtml(r.label)}</td><td>${escapeHtml(r.a)}</td><td>${escapeHtml(r.b)}</td></tr>`).join("") +
    `</tbody></table>`;
}

// ---- F4: all-projects activity table (Dashboard) ----
async function loadProjectsActivity() {
  const wrap = $("ds-activity");
  if (!wrap) return;
  let list = [];
  try { list = await (await fetch("/api/projects")).json(); } catch (e) { list = []; }
  list = visibleProjects(list);   // the scratch "default" project is never rendered
  if (!list.length) { wrap.innerHTML = '<p class="muted">No projects yet.</p>'; return; }
  const fetchAct = async (slug) => {
    try {
      const resp = await fetch("/api/projects/" + encodeURIComponent(slug) + "/activity");
      return resp.ok ? await resp.json() : null;
    } catch (e) { return null; }
  };
  const shown = list.slice(0, 20);
  const acts = await Promise.all([...shown.map(p => fetchAct(p.slug)), fetchAct("multi")]);
  const multiAct = acts.pop();
  const entries = shown.map((p, i) => ({ p, act: acts[i] }));
  // F3: "multi" pseudo-project row — cross-project fan-out parents, shown while active
  const multiModel = activityStripModel(multiAct);
  if (multiAct && (multiModel.running || multiModel.queued)) {
    entries.unshift({ p: { slug: "multi", name: "⋔ multi" }, act: multiAct });
  }
  const rows = entries.map(({ p, act }) => {
    const m = activityStripModel(act);
    const runningTitle = (act && act.running && act.running[0])
      ? (act.running[0].title || act.running[0].id) : "—";
    return `<tr>` +
      `<td class="dsa-name">${escapeHtml(p.name || p.slug)}` +
      (p.archived ? ' <span class="ps-archived">archived</span>' : "") + `</td>` +
      `<td><span class="dsa-state dsa-state-${escapeAttr(m.state)}">${escapeHtml(m.state)}</span></td>` +
      `<td class="dsa-task">${escapeHtml(runningTitle)}${m.running > 1 ? escapeHtml(" (+" + (m.running - 1) + " more)") : ""}</td>` +
      `<td class="dsa-queued">${m.queued ? escapeHtml(m.queued + " queued") : "—"}</td>` +
      `</tr>`;
  }).join("");
  wrap.innerHTML =
    `<table class="dsa-table"><thead><tr>` +
    `<th scope="col">Project</th><th scope="col">State</th>` +
    `<th scope="col">Running task</th><th scope="col">Queued</th>` +
    `</tr></thead><tbody>${rows}</tbody></table>`;
}

// ---- Settings tab: repository state + policy editor + archive/delete ----
async function loadSettingsPanel() {
  const slug = selectedProject();
  if (!slug || slug === "multi") return;
  let list = [];
  try { list = await (await fetch("/api/projects")).json(); } catch (e) { list = []; }
  list = visibleProjects(list);
  if (list.length) projectList = list;
  const entry = currentProjectEntry(slug) || { slug, name: slug };
  renderSettingsPolicy(entry);
  renderSettingsDanger(entry);
  loadGcReport();        // QoL: workspace-cleanup card
  loadProjectStatus();   // fills the repository card + header chips
}

// ---- QoL: workspace GC (Settings tab) — dry-run report + explicit cleanup ----
let _gcReport = null;

async function loadGcReport(keepOverride) {
  const slug = selectedProject();
  if (!slug || slug === "multi") return;
  const card = $("gc-card");
  if (!card) return;
  $("gc-error").classList.add("hidden");
  $("gc-list").innerHTML = '<p class="muted">Checking…</p>';
  const q = keepOverride != null ? "?keep_days=" + encodeURIComponent(keepOverride) : "";
  let resp, data = null;
  try {
    resp = await fetch("/api/projects/" + encodeURIComponent(slug) + "/gc" + q);
    data = await resp.json().catch(() => null);
  } catch (e) {
    $("gc-list").innerHTML = '<p class="muted">Could not reach the server.</p>';
    return;
  }
  if (resp.status === 404 || resp.status === 501) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  if (!resp.ok || !data || data.error) {
    $("gc-list").innerHTML = "";
    const err = $("gc-error");
    err.textContent = (data && data.error) || ("HTTP " + resp.status);
    err.classList.remove("hidden");
    return;
  }
  _gcReport = data;
  if (keepOverride == null) $("gc-keep").value = String(data.keep_days);
  renderGcReport(data);
}

function renderGcReport(report) {
  const m = gcSummary(report);
  const btn = $("gc-clean");
  btn.disabled = !m.count;
  btn.dataset.armed = "";
  btn.textContent = m.count ? m.label : "Clean up";
  const list = $("gc-list");
  if (!m.count) {
    list.innerHTML = `<p class="muted">${escapeHtml(m.emptyText)}</p>`;
    return;
  }
  list.innerHTML = m.items.map(it =>
    `<div class="gc-item"><span class="chip">${escapeHtml(it.taskId)}</span>` +
    `<span class="gc-kind gc-kind-${escapeAttr(it.kind)}">${escapeHtml(it.kind)}</span>` +
    `<code class="gc-detail" title="${escapeAttr(it.detail)}">${escapeHtml(it.detail)}</code>` +
    `<span class="gc-meta">${escapeHtml(it.status)}${it.ageLabel ? " · " + escapeHtml(it.ageLabel) : ""}</span></div>`
  ).join("");
}

async function runGcCleanup() {
  const slug = selectedProject();
  if (!slug || slug === "multi" || !_gcReport) return;
  const btn = $("gc-clean");
  const m = gcSummary(_gcReport);
  if (!m.count) return;
  // Two-step arm/confirm — consistent with "Accept all & deliver", no dialogs.
  if (!btn.dataset.armed) {
    btn.dataset.armed = "1";
    btn.textContent = "Confirm: remove " + m.count + " item" + (m.count === 1 ? "" : "s") + "?";
    setTimeout(() => {
      if (btn.dataset.armed) { btn.dataset.armed = ""; btn.textContent = m.label; }
    }, 6000);
    return;
  }
  btn.disabled = true;
  btn.textContent = "Cleaning…";
  $("gc-error").classList.add("hidden");
  const keep = parseInt($("gc-keep").value, 10);
  let resp, data = {};
  try {
    resp = await fetch("/api/projects/" + encodeURIComponent(slug) + "/gc", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keep_days: isFinite(keep) ? keep : null }),
    });
    data = await resp.json().catch(() => ({}));
  } catch (e) {
    $("gc-error").textContent = "Cleanup failed: " + e;
    $("gc-error").classList.remove("hidden");
    btn.disabled = false; btn.dataset.armed = ""; btn.textContent = m.label;
    return;
  }
  if (!resp.ok || data.error) {
    $("gc-error").textContent = data.error || ("HTTP " + resp.status);
    $("gc-error").classList.remove("hidden");
    btn.disabled = false; btn.dataset.armed = ""; btn.textContent = m.label;
    return;
  }
  const res = gcResultModel(data);
  const out = $("gc-result");
  out.innerHTML = escapeHtml(res.text) + (res.skipped.length
    ? "<br>" + res.skipped.map(s =>
        `<span class="gc-skip">${escapeHtml(s.taskId)} — ${escapeHtml(s.reason)}</span>`).join("<br>")
    : "");
  out.classList.remove("hidden");
  showToast(res.removedCount ? "Workspace cleanup · " + res.text : "Nothing removed", res.removedCount ? "success" : "warn");
  loadGcReport(isFinite(keep) ? keep : undefined);   // re-check — a second pass finds nothing
}

// The Settings tab's repository card (fed by the header's status poll).
function renderSettingsRepo(st) {
  const el = $("ph-status");
  if (!el) return;
  const entry = currentProjectEntry() || { slug: selectedProject() };
  const h = projectHomeHeaderModel(entry);
  const m = projectStatusLine(st);
  el.innerHTML = m.visible
    ? `<span class="ps-branch">⎇ ${escapeHtml(m.text)}</span>` +
      (m.dirty ? '<span class="ps-dirty" title="Uncommitted changes in the checkout">●</span>' : "") +
      (m.archived ? '<span class="ps-archived">archived</span>' : "")
    : `<span class="muted">${escapeHtml(h.scratch
        ? h.emptyText : "No repository info for this project.")}</span>`;
  const idx = $("ph-indexed");
  if (idx) {
    const short = st && st.last_indexed_commit ? String(st.last_indexed_commit).slice(0, 7) : "";
    idx.classList.toggle("hidden", h.scratch);
    idx.textContent = h.scratch ? "" : (short ? "last indexed @ " + short : "not indexed yet");
  }
}

function renderSettingsPolicy(entry) {
  const f = policyFormModel(entry.policy || {});
  $("ph-budget").value = f.budget_usd;
  $("ph-effort").value = f.effort;
  $("ph-gitmode").value = f.git_mode;
  $("ph-protected").value = f.protected_paths;
  $("ph-policy-error").classList.add("hidden");
}

function renderSettingsDanger(entry) {
  const archived = !!(entry && entry.archived);
  $("proj-archive").textContent = archived ? "Unarchive project" : "Archive project";
  $("proj-delete").disabled = entry && entry.slug === "default";
  $("proj-delete").title = (entry && entry.slug === "default")
    ? "The default project cannot be deleted" : "Delete this project's data directory and checkout";
  $("proj-danger-error").classList.add("hidden");
}

function _dangerFail(msg) {
  const el = $("proj-danger-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

async function toggleArchiveProject() {
  const slug = selectedProject();
  const entry = currentProjectEntry(slug) || {};
  const next = !entry.archived;
  const btn = $("proj-archive");
  btn.disabled = true;
  try {
    const resp = await fetch("/api/projects/" + encodeURIComponent(slug), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ archived: next }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 501) showToast("Archiving is not available yet on this server", "warn", 6000);
    else if (!resp.ok || data.error) _dangerFail(data.error || ("HTTP " + resp.status));
    else {
      showToast(next ? "Project archived" : "Project unarchived", "success");
      await loadProjects();
      renderSettingsDanger(currentProjectEntry(slug) || {});
      renderProjectHeader(null);
    }
  } catch (e) { _dangerFail("Request failed: " + e); }
  btn.disabled = false;
}

async function deleteProject() {
  const slug = selectedProject();
  const entry = currentProjectEntry(slug) || { slug };
  const name = entry.name || slug;
  if (!window.confirm(`Delete project "${name}"? This removes its data directory and checkout.`)) return;
  const btn = $("proj-delete");
  btn.disabled = true;
  try {
    const resp = await fetch("/api/projects/" + encodeURIComponent(slug), { method: "DELETE" });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 501) showToast("Project delete is not available yet on this server", "warn", 6000);
    else if (!resp.ok || data.error) _dangerFail(data.error || ("HTTP " + resp.status));
    else {
      showToast(`Deleted project "${name}"`, "warn");
      currentProject = null;
      localStorage.removeItem("ada-project");
      await loadProjects();   // re-selects the first remaining project (or none)
      if (currentProject) selectProject(currentProject, "overview");
      else showEmptyState();
      return;
    }
  } catch (e) { _dangerFail("Request failed: " + e); }
  btn.disabled = false;
}

async function saveProjectPolicy() {
  const slug = selectedProject();
  const parsed = parsePolicyForm({
    budget_usd: $("ph-budget").value, effort: $("ph-effort").value,
    git_mode: $("ph-gitmode").value, protected_paths: $("ph-protected").value,
  });
  const err = $("ph-policy-error");
  if (!parsed.ok) { err.textContent = parsed.errors.join(" · "); err.classList.remove("hidden"); return; }
  err.classList.add("hidden");
  const btn = $("ph-policy-save");
  btn.disabled = true;
  try {
    const resp = await fetch("/api/projects/" + encodeURIComponent(slug), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ policy: parsed.policy }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 501) showToast("Policy editing is not available yet on this server", "warn", 6000);
    else if (!resp.ok || data.error) { err.textContent = data.error || ("HTTP " + resp.status); err.classList.remove("hidden"); }
    else showToast("Policy saved", "success");
  } catch (e) { showToast("Save failed: " + e, "error"); }
  btn.disabled = false;
}

// ---- F4 decision #5: Reviews & Permissions panel ----
let reviewTaskId = null;

function openReviewPanel() {
  const tid = state.docsId || state.taskId;
  if (!tid) { showToast("No task on screen yet", "warn"); return; }
  reviewTaskId = tid;
  selectReviewTab("reviews");
  $("rvp-reviews").innerHTML = '<p class="muted">Loading…</p>';
  $("rvp-permissions").innerHTML = '<p class="muted">Loading…</p>';
  $("rvp-branches").classList.add("hidden");
  openModalEl("review-modal");
  loadReviewTab();
  loadPermissionsTab();
}
function closeReviewPanel() { closeModalEl("review-modal"); }

function selectReviewTab(which) {
  document.querySelectorAll("#review-modal .tab").forEach(t => {
    const on = t.dataset.rtab === which;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  });
  $("rvp-reviews").classList.toggle("hidden", which !== "reviews");
  $("rvp-permissions").classList.toggle("hidden", which !== "permissions");
}

async function loadReviewTab() {
  const el = $("rvp-reviews");
  let data;
  try {
    const resp = await fetch("/api/tasks/" + encodeURIComponent(reviewTaskId) + "/review");
    data = await resp.json().catch(() => ({}));
    if (resp.status === 501) { el.innerHTML = '<p class="muted">Per-subtask review is not available yet on this server.</p>'; return; }
    if (!resp.ok || data.error) {
      el.innerHTML = `<p class="muted">Could not load reviews: ${escapeHtml(data.error || ("HTTP " + resp.status))}</p>`;
      return;
    }
  } catch (e) { el.innerHTML = '<p class="muted">Could not load reviews.</p>'; return; }
  const br = $("rvp-branches");
  if (data.task_branch || data.review_target) {
    br.classList.remove("hidden");
    br.innerHTML = `⎇ task branch <code>${escapeHtml(data.task_branch || "—")}</code>` +
      ` · accepting merges into <code>${escapeHtml(data.review_target || "—")}</code>`;
  }
  // Accept all & deliver — shown when the task has a branch to deliver. Two-step
  // arm/confirm instead of a blocking dialog.
  const drow = $("rvp-deliver-row");
  const dbtn = $("rvp-deliver");
  if (data.task_branch) {
    drow.classList.remove("hidden");
    $("rvp-deliver-note").textContent = "";
    dbtn.disabled = false;
    dbtn.dataset.armed = "";
    dbtn.textContent = "Accept all & deliver →";
    dbtn.onclick = async () => {
      if (!dbtn.dataset.armed) {
        dbtn.dataset.armed = "1";
        dbtn.textContent = "Confirm: merge branch into " + (data.review_target || "target") + "?";
        setTimeout(() => { if (dbtn.dataset.armed) { dbtn.dataset.armed = ""; dbtn.textContent = "Accept all & deliver →"; } }, 6000);
        return;
      }
      dbtn.disabled = true;
      dbtn.textContent = "Delivering…";
      try {
        const resp = await fetch("/api/tasks/" + encodeURIComponent(reviewTaskId) + "/deliver",
                                 { method: "POST" });
        const res = await resp.json().catch(() => ({}));
        if (!resp.ok || !res.merged) {
          const why = res.conflict ? ("conflict: " + (res.files || []).join(", ")) : (res.error || ("HTTP " + resp.status));
          $("rvp-deliver-note").textContent = "✕ " + why;
          showToast("Deliver failed — " + why, "warn");
          dbtn.disabled = false;
          dbtn.dataset.armed = "";
          dbtn.textContent = "Accept all & deliver →";
          return;
        }
        $("rvp-deliver-note").textContent = "✓ delivered @ " + String(res.commit || "").slice(0, 8);
        showToast("Task branch delivered to " + (data.review_target || "target"));
        refreshProjectPulse(true);
        loadReviewTab();  // re-render with the recorded decisions
      } catch (e) {
        $("rvp-deliver-note").textContent = "✕ could not deliver";
        dbtn.disabled = false;
        dbtn.dataset.armed = "";
        dbtn.textContent = "Accept all & deliver →";
      }
    };
  } else {
    drow.classList.add("hidden");
  }
  const subs = data.subtasks || [];
  if (!subs.length) { el.innerHTML = '<p class="muted">No subtask records for this task yet.</p>'; return; }
  el.innerHTML = subs.map(reviewCardHtml).join("");
  el.querySelectorAll(".review-card").forEach(card => {
    const accept = card.querySelector(".rc-accept");
    const reject = card.querySelector(".rc-reject");
    const rollback = card.querySelector(".rc-rollback");
    if (accept) accept.onclick = () => acceptSubtask(card, accept.dataset.sid);
    if (reject) reject.onclick = () => rejectSubtask(card, reject.dataset.sid);
    if (rollback) rollback.onclick = () => rollbackSubtask(card, rollback.dataset.sid);
  });
}

function reviewCardHtml(s) {
  const m = reviewCardModel(s);
  const rb = rollbackStateModel(s);
  const badgeCls = m.badge === "passed" ? "pill-done" : m.badge === "failed" ? "pill-err" : "";
  const score = m.score != null ? `<span class="rc-meta-bit">score ${escapeHtml(m.score)}</span>` : "";
  const attempts = m.attempts != null
    ? `<span class="rc-meta-bit">${escapeHtml(m.attempts)} attempt${m.attempts === 1 ? "" : "s"}</span>` : "";
  const decision = m.decision
    ? `<span class="rc-decision rc-decision-${escapeAttr(m.decision)}">${escapeHtml(m.decisionLabel)}</span>` : "";
  const criteria = m.criteria.length
    ? `<ul class="rc-criteria">` + m.criteria.map(c =>
        `<li class="${c.met === true ? "rc-crit-ok" : c.met === false ? "rc-crit-bad" : ""}">` +
        `${c.met === true ? "✓ " : c.met === false ? "✗ " : "· "}${escapeHtml(c.name)}</li>`).join("") + `</ul>` : "";
  const reasons = m.reasons.length
    ? `<ul class="rc-reasons">` + m.reasons.map(r => `<li>${escapeHtml(r)}</li>`).join("") + `</ul>` : "";
  const suggestions = m.suggestions.length
    ? `<div class="rc-suggestions"><span class="kicker">Suggestions</span><ul>` +
      m.suggestions.map(r => `<li>${escapeHtml(r)}</li>`).join("") + `</ul></div>` : "";
  const changed = m.changed.length
    ? `<div class="rc-files">` + m.changed.map(f => `<span class="tool-chip">${escapeHtml(f)}</span>`).join("") + `</div>`
    : '<p class="muted rc-nofiles">No changed files recorded.</p>';
  const diff = m.hasDiff
    ? `<details class="rc-diff-wrap"><summary>Diff${m.mergeShort ? " · " + escapeHtml(m.mergeShort) : ""}</summary>` +
      `<pre class="rc-diff">${renderDiffHtml(s.diff)}</pre></details>` : "";
  return `<div class="review-card">` +
    `<div class="rc-head"><span class="chip">${escapeHtml(m.id)}</span>` +
    (m.agent ? `<span class="ac-agent"><span class="ac-dot" style="background:${agentStyle(m.agent).color}"></span>${escapeHtml(m.agent)}</span>` : "") +
    `<span class="pill ${badgeCls}">${escapeHtml(m.badge)}</span>${score}${attempts}${decision}</div>` +
    (m.title ? `<div class="rc-title">${escapeHtml(m.title)}</div>` : "") +
    criteria + reasons + suggestions +
    `<span class="kicker">Changed files</span>` + changed + diff +
    `<div class="rc-actions">` +
    `<button type="button" class="rc-accept ghost-btn" data-sid="${escapeAttr(m.id)}"${m.canAccept ? "" : " disabled"} ` +
    `title="${m.canAccept ? "Merge this subtask's commit into the review target" : "No merge commit, or already accepted"}">✓ Accept</button>` +
    `<button type="button" class="rc-reject ghost-btn" data-sid="${escapeAttr(m.id)}"${m.canReject ? "" : " disabled"} ` +
    `title="Record rejection feedback; the commit stays on the task branch">✗ Reject</button>` +
    (rb.canRollback
      ? `<button type="button" class="rc-rollback ghost-btn" data-sid="${escapeAttr(m.id)}" ` +
        `title="Revert this accepted subtask's commit on the review target">↶ ${escapeHtml(rb.label)}</button>` : "") +
    `<input type="text" class="rc-comment title-input" maxlength="280" ` +
    `placeholder="Rejection comment (optional) — feeds future planning" aria-label="Rejection comment for subtask ${escapeAttr(m.id)}" />` +
    `</div><p class="rc-error pm-error hidden" role="alert"></p></div>`;
}

function _rcShowError(card, msg) {
  const el = card.querySelector(".rc-error");
  if (!el) return;
  el.textContent = msg;
  el.classList.remove("hidden");
}

async function acceptSubtask(card, sid) {
  const btn = card.querySelector(".rc-accept");
  btn.disabled = true;
  let resp, data = {};
  try {
    resp = await fetch(`/api/tasks/${encodeURIComponent(reviewTaskId)}/subtasks/${encodeURIComponent(sid)}/accept`,
                       { method: "POST" });
    data = await resp.json().catch(() => ({}));
  } catch (e) { _rcShowError(card, "Accept failed: " + e); btn.disabled = false; return; }
  if (resp.status === 501) {
    showToast("Per-subtask acceptance is not available yet on this server", "warn", 6000);
    btn.disabled = false;
    return;
  }
  if (!resp.ok) {
    const files = (data.files || []).length ? " · conflicts: " + data.files.join(", ") : "";
    _rcShowError(card, (data.error || ("HTTP " + resp.status)) + files);
    btn.disabled = false;
    return;
  }
  showToast(`Accepted ${sid}` + (data.commit ? ` · merged as ${String(data.commit).slice(0, 7)}` : ""), "success");
  loadReviewTab();  // re-render with the recorded decision
}

async function rejectSubtask(card, sid) {
  const comment = (card.querySelector(".rc-comment") || { value: "" }).value.trim();
  const btn = card.querySelector(".rc-reject");
  btn.disabled = true;
  let resp, data = {};
  try {
    resp = await fetch(`/api/tasks/${encodeURIComponent(reviewTaskId)}/subtasks/${encodeURIComponent(sid)}/reject`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ comment }),
    });
    data = await resp.json().catch(() => ({}));
  } catch (e) { _rcShowError(card, "Reject failed: " + e); btn.disabled = false; return; }
  if (resp.status === 501) {
    showToast("Per-subtask review is not available yet on this server", "warn", 6000);
    btn.disabled = false;
    return;
  }
  if (!resp.ok) { _rcShowError(card, data.error || ("HTTP " + resp.status)); btn.disabled = false; return; }
  showToast(`Rejected ${sid} — feedback recorded`, "warn");
  loadReviewTab();
}

// QoL: revert an accepted subtask's commit on the review target. The server
// records decision "rolled_back" (kept in the review listing); 409 carries a
// conflict/nothing-to-revert explanation which is shown inline on the card.
async function rollbackSubtask(card, sid) {
  const btn = card.querySelector(".rc-rollback");
  btn.disabled = true;
  let resp, data = {};
  try {
    resp = await fetch(`/api/runs/${encodeURIComponent(reviewTaskId)}/subtasks/${encodeURIComponent(sid)}/rollback`,
                       { method: "POST" });
    data = await resp.json().catch(() => ({}));
  } catch (e) { _rcShowError(card, "Rollback failed: " + e); btn.disabled = false; return; }
  if (resp.status === 501) {
    showToast("Rollback is not available yet on this server", "warn", 6000);
    btn.disabled = false;
    return;
  }
  if (!resp.ok) {   // 409: revert conflict or nothing recorded to roll back
    const files = (data.files || []).length ? " · conflicts: " + data.files.join(", ") : "";
    _rcShowError(card, (data.error || ("HTTP " + resp.status)) + files);
    btn.disabled = false;
    return;
  }
  showToast(`Rolled back ${sid}`
    + (data.rollback_commit ? ` · revert ${String(data.rollback_commit).slice(0, 7)}` : ""), "warn");
  loadReviewTab();  // re-render — the row now shows the "rolled back" decision
}

async function loadPermissionsTab() {
  const el = $("rvp-permissions");
  let data;
  try {
    const resp = await fetch("/api/tasks/" + encodeURIComponent(reviewTaskId) + "/permissions");
    data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.error) {
      el.innerHTML = `<p class="muted">Could not load permissions: ${escapeHtml(data.error || ("HTTP " + resp.status))}</p>`;
      return;
    }
  } catch (e) { el.innerHTML = '<p class="muted">Could not load permissions.</p>'; return; }
  const m = permissionsModel(data);
  const branches = (m.taskBranch || m.reviewTarget)
    ? `<p class="perm-branches">⎇ task branch <code>${escapeHtml(m.taskBranch || "—")}</code>` +
      ` · review target <code>${escapeHtml(m.reviewTarget || "—")}</code></p>` : "";
  const policy = m.policyRows.length
    ? `<table class="dsa-table perm-table"><thead><tr><th scope="col">Setting</th><th scope="col">Value</th></tr></thead><tbody>` +
      m.policyRows.map(r => `<tr><td>${escapeHtml(r.key)}</td><td class="perm-val">${escapeHtml(r.value)}</td></tr>`).join("") +
      `</tbody></table>`
    : '<p class="muted">No resolved policy recorded for this run.</p>';
  const agents = m.agents.length
    ? m.agents.map(a =>
        `<div class="perm-agent"><span class="ac-agent"><span class="ac-dot" style="background:${agentStyle(a.agent).color}"></span>${escapeHtml(a.agent)}</span>` +
        `<span class="tool-chips">` +
        (a.tools.length ? a.tools.map(t => `<span class="tool-chip">${escapeHtml(t)}</span>`).join("")
                        : '<span class="muted">no tools</span>') +
        `</span></div>`).join("")
    : '<p class="muted">No per-agent tool allowlist recorded.</p>';
  const denied = m.deniedEmpty
    ? '<p class="muted">no denied actions</p>'
    : `<table class="dsa-table perm-table"><thead><tr><th scope="col">When</th><th scope="col">Agent</th>` +
      `<th scope="col">Tool</th><th scope="col">Outcome</th></tr></thead><tbody>` +
      m.denied.map(d =>
        `<tr><td>${d.ts != null ? escapeHtml(new Date(d.ts * 1000).toLocaleString()) : "—"}</td>` +
        `<td>${escapeHtml(d.agent || "—")}</td><td>${escapeHtml(d.tool || "—")}</td>` +
        `<td class="perm-denied">${escapeHtml(d.outcome)}</td></tr>`).join("") +
      `</tbody></table>`;
  el.innerHTML = `<span class="kicker">Resolved policy in force</span>${branches}${policy}` +
    `<span class="kicker">Tool allowlist per agent</span><div class="perm-agents">${agents}</div>` +
    `<span class="kicker">Denied actions — from the audit log</span>${denied}`;
}

// ---- Dashboard (Tier 5) ----
async function loadDashboard() {
  populateCompareOptions();
  loadProjectsActivity();
  loadSpend();
  let s;
  try { s = await (await fetch("/api/stats")).json(); } catch (e) { return; }
  $("ds-runs").textContent = s.runs ?? 0;
  $("ds-cost").textContent = "$" + Number(s.total_cost_usd || 0).toFixed(2);
  $("ds-quality").textContent = s.avg_quality != null ? s.avg_quality : "—";
  const by = s.by_status || {};
  $("ds-ok").textContent = by.completed || 0;
  $("ds-partial").textContent = by.partial || 0;
  $("ds-failed").textContent = (by.failed || 0) + (by.over_budget || 0);
  const wrap = $("ds-agents");
  const agents = s.agents || {};
  const names = Object.keys(agents).sort((a, b) => agents[b].pass_rate - agents[a].pass_rate);
  if (!names.length) { wrap.innerHTML = '<p class="muted">No data yet — run a few tasks.</p>'; return; }
  wrap.innerHTML = "";
  names.forEach(n => {
    const a = agents[n];
    const row = document.createElement("div");
    row.className = "ds-agent-row";
    const pct = Math.round((a.pass_rate || 0) * 100);
    row.innerHTML = `<span class="ds-agent-name">${escapeHtml(n)}</span>`
      + `<span class="ds-bar"><span class="ds-bar-fill" style="width:${pct}%"></span></span>`
      + `<span class="ds-agent-num">${pct}% · ${a.passed}/${a.n}${a.n < 5 ? " (low data)" : ""}</span>`;
    wrap.appendChild(row);
  });
}

// ---- In-run control (Tier 5): pause / resume / steer ----
function updateRunControls() {
  const running = $("status-pill").textContent === "running" || $("status-pill").textContent === "paused";
  const paused = $("status-pill").textContent === "paused";
  const pb = $("pause-btn"), sb = $("steer-btn");
  if (!pb || !sb) return;
  pb.classList.toggle("hidden", !running);
  sb.classList.toggle("hidden", !running);
  pb.textContent = paused ? "▶ Resume" : "⏸ Pause";
}
async function pauseResume() {
  if (!state.taskId) return;
  const paused = $("status-pill").textContent === "paused";
  try { await fetch(`/api/run/${state.taskId}/${paused ? "resume" : "pause"}`, { method: "POST" }); }
  catch (e) { showToast("Control failed", "warn"); }
}
async function steerRun() {
  if (!state.taskId) return;
  const note = prompt("Steering note for the next subtask:");
  if (!note) return;
  try { await fetch(`/api/run/${state.taskId}/steer`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ note }) }); showToast("Steering note queued", "success"); }
  catch (e) { showToast("Steer failed", "warn"); }
}

// ---- Feedback (Tier 4) ----
let _fbRating = null;
function revealFeedback(taskId) {
  const box = $("feedback-box");
  if (!box) return;
  box.dataset.task = taskId || state.taskId || "";
  box.classList.remove("hidden");
  $("fb-thanks").classList.add("hidden");
}
async function sendFeedback(extra) {
  const box = $("feedback-box");
  const tid = box && box.dataset.task;
  if (!tid) return;
  const body = { rating: _fbRating, comment: $("fb-comment").value || "", ...extra };
  try {
    await fetch(`/api/run/${tid}/feedback`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("fb-thanks").classList.remove("hidden");
  } catch (e) { showToast("Feedback failed", "warn"); }
}

// ---- Agents view: built-in roster (read-only) + custom-agent editor ----
let agentsLoaded = false;
let rosterAgents = {};   // name -> agent profile, for the detail popup
let agentsBody = agentsListModel(null);   // last-fetched {builtin, custom, tools, names}

function _agentCard(a, custom) {
  const tools = Array.isArray(a.tools) ? a.tools : [];
  const card = document.createElement("div");
  card.className = "agent-card";
  const effortChip = a.effort
    ? `<span class="chip" title="Effort override">${escapeHtml(a.effort)}</span>` : "";
  const acts = custom
    ? `<div class="af-card-acts">` +
      `<button type="button" class="q-act af-edit" title="Edit this agent">Edit</button>` +
      `<button type="button" class="q-act af-delete" title="Delete this agent (click twice to confirm)">Delete</button></div>`
    : "";
  card.innerHTML = `
    <div class="ac-top"><span class="ac-dot" style="background:${agentStyle(a.name).color}"></span><span class="ac-agent ac-name">${escapeHtml(a.name)}</span>${effortChip}<span class="chip chip-tools">${tools.length} tools</span></div>
    <div class="ac-desc">${escapeHtml(a.description || "")}</div>
    <div class="ac-when">${escapeHtml(a.when_to_use || "")}</div>
    <div class="tool-chips">${tools.map(t => `<span class="tool-chip">${escapeHtml(t)}</span>`).join("")}</div>${acts}`;
  if (custom) {
    card.title = "Custom agent — click to edit";
    makeActivatable(card, (e) => {
      if (e && e.target && e.target.closest("button")) return;
      openAgentForm(a);
    }, `Edit custom agent ${a.name}`);
    card.querySelector(".af-edit").onclick = (e) => { e.stopPropagation(); openAgentForm(a); };
    const del = card.querySelector(".af-delete");
    del.onclick = (e) => {   // two-click confirm — never window.confirm
      e.stopPropagation();
      if (!del.dataset.armed) {
        del.dataset.armed = "1";
        del.textContent = "Confirm delete";
        del.classList.add("memc-arm");
        setTimeout(() => {
          if (del.dataset.armed) { del.dataset.armed = ""; del.textContent = "Delete"; del.classList.remove("memc-arm"); }
        }, 5000);
        return;
      }
      deleteCustomAgent(a.name);
    };
  } else {
    card.title = "Built-in specialist — click for full details";
    makeActivatable(card, () => openRosterModal(a.name), `Open profile for agent ${a.name}`);
  }
  return card;
}

async function loadAgents(force) {
  if (agentsLoaded && !force) return;
  try {
    agentsBody = agentsListModel(await (await fetch("/api/agents")).json());
  } catch (e) { return; }
  rosterAgents = {};
  const roster = $("agents-roster");
  roster.innerHTML = "";
  agentsBody.builtin.forEach(a => {
    rosterAgents[a.name] = a;
    roster.appendChild(_agentCard(a, false));
  });
  const customs = $("agents-custom");
  customs.innerHTML = "";
  if (!agentsBody.custom.length) {
    customs.innerHTML = '<p class="muted af-none">No custom agents yet — create one with “+ New agent”.</p>';
  } else {
    agentsBody.custom.forEach(a => {
      rosterAgents[a.name] = a;
      customs.appendChild(_agentCard(a, true));
    });
  }
  agentsLoaded = true;
}

// ---- custom-agent form (new + edit prefill; upsert by name) ----
function openAgentForm(spec) {
  const m = agentFormModel(spec, agentsBody.tools);
  $("af-heading").textContent = m.heading;
  $("af-name").value = m.name;
  $("af-desc").value = m.description;
  $("af-when").value = m.when_to_use;
  $("af-system").value = m.system_prompt;
  $("af-effort").value = m.effort;
  $("af-model").value = m.model;
  $("af-tools").innerHTML = m.tools.length
    ? m.tools.map(t =>
        `<label class="check af-tool"><input type="checkbox" value="${escapeAttr(t.name)}"` +
        `${t.checked ? " checked" : ""} /> ${escapeHtml(t.name)}</label>`).join("")
    : '<span class="muted">Could not load the toolbox — refresh the roster.</span>';
  $("af-error").classList.add("hidden");
  $("af-error").textContent = "";
  $("agent-form-card").classList.remove("hidden");
  $("af-name").focus();
  $("agent-form-card").scrollIntoView({ behavior: scrollBehavior(), block: "start" });
}

function closeAgentForm() {
  $("agent-form-card").classList.add("hidden");
}

function _afFail(msg) {
  const el = $("af-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

async function saveAgentForm() {
  const v = agentFormValidate({
    name: $("af-name").value, description: $("af-desc").value,
    when_to_use: $("af-when").value, system_prompt: $("af-system").value,
    tools: [...document.querySelectorAll("#af-tools input:checked")].map(cb => cb.value),
    effort: $("af-effort").value, model: $("af-model").value,
  }, agentsBody.tools);
  if (!v.ok) { _afFail(v.errors.join(" · ")); return; }
  const btn = $("af-save");
  btn.disabled = true;
  let resp, data = {};
  try {
    resp = await fetch("/api/agents", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec: v.spec }),
    });
    data = await resp.json().catch(() => ({}));
  } catch (e) { _afFail("Request failed: " + e); btn.disabled = false; return; }
  btn.disabled = false;
  if (!resp.ok || data.error) { _afFail(data.error || ("HTTP " + resp.status)); return; }
  closeAgentForm();
  showToast(`Agent "${v.spec.name}" saved — routable on the next run`, "success");
  _agentNames = null;   // the plan editor's roster must pick the new agent up
  loadAgents(true);
}

async function deleteCustomAgent(name) {
  let resp, data = {};
  try {
    resp = await fetch("/api/agents/" + encodeURIComponent(name), { method: "DELETE" });
    data = await resp.json().catch(() => ({}));
  } catch (e) { showToast("Delete failed: " + e, "error"); return; }
  if (!resp.ok || data.error) { showToast(data.error || ("HTTP " + resp.status), "error", 6000); return; }
  showToast(`Deleted agent "${name}"`, "warn");
  _agentNames = null;
  loadAgents(true);
}

function openRosterModal(name) {
  const a = rosterAgents[name];
  if (!a) return;
  const st = agentStyle(a.name);
  $("rm-name").innerHTML = `<span class="ac-dot" style="background:${st.color}"></span>${escapeHtml(a.name)}`;
  $("rm-tools-count").textContent = a.tools.length + " tools";
  $("rm-desc").textContent = a.description || "";
  $("rm-when").textContent = a.when_to_use || "—";
  $("rm-tools").innerHTML = a.tools.map(t => `<span class="tool-chip">${escapeHtml(t)}</span>`).join("") || '<span class="muted">none</span>';
  openModalEl("roster-modal");
}
function closeRosterModal() { closeModalEl("roster-modal"); }

// ---- Combined knowledge across projects (read-only) ----
// The Knowledge tab's Memory and Graph sub-views can merge OTHER non-archived
// projects in via "Combine with…" chips. Combined fetches send an additional
// `projects=<active,plus,selected>` query param; if that hasn't landed on the
// server yet the code falls back to the single-project fetch and hides the
// banner. Clearing the chips returns to the single-project view.
let combineSelected = new Set();   // slugs of OTHER projects merged in

function renderCombineRow() {
  const row = $("combine-row"), box = $("combine-chips");
  if (!row || !box) return;
  const slug = selectedProject();
  const chips = combineChipsModel(projectList, slug, [...combineSelected]);
  // prune selections for projects that no longer exist / are archived
  combineSelected = new Set(chips.filter(c => c.selected).map(c => c.slug));
  const applicable = !!slug && slug !== "multi" && currentKnow !== "files" && chips.length > 0;
  row.classList.toggle("hidden", !applicable);
  if (!applicable) { box.innerHTML = ""; return; }
  box.innerHTML = chips.map(c =>
    `<label class="combine-chip${c.selected ? " on" : ""}">` +
    `<input type="checkbox" class="cmb-check" value="${escapeAttr(c.slug)}"${c.selected ? " checked" : ""} /> ` +
    `${escapeHtml(c.name)}</label>`).join("");
  box.querySelectorAll(".cmb-check").forEach(cb => {
    cb.onchange = () => {
      if (cb.checked) combineSelected.add(cb.value);
      else combineSelected.delete(cb.value);
      if (currentKnow === "memory") loadMemory();
      else if (currentKnow === "graph") loadGraph();
      else renderCombineRow();
    };
  });
}

function setCombineBanner(projectCount) {
  const el = $("combine-banner");
  if (!el) return;
  const m = combinedBannerModel(projectCount);
  el.classList.toggle("hidden", !m.visible);
  el.textContent = m.text;
}

// A combined response is only trusted when the server clearly honored the
// `projects` param: it declares a "projects" list, tags items with the
// additive "project"/"sources" fields, or returns an empty merge.
function _combinedDeclared(body, items) {
  const declared = (body && !Array.isArray(body) && Array.isArray(body.projects))
    ? body.projects : null;
  if (declared) return declared;
  const arr = Array.isArray(items) ? items : [];
  const tagged = arr.some(it => itemProjects(it).length > 0);
  return (tagged || !arr.length) ? [] : null;   // [] = honored but undeclared
}

// ---- Memory (single project, or combined read-only across projects) ----
async function loadMemory() {
  const slug = selectedProject();
  if (!slug || slug === "multi") return;
  renderCombineRow();
  loadMemoryCuration(true);   // QoL: the curation section refreshes alongside recall
  const param = combinedProjectsParam(slug, [...combineSelected]);
  let items = null, combinedProjects = null;
  if (param) {
    try {
      const resp = await fetch("/api/memory?projects=" + encodeURIComponent(param));
      if (resp.ok) {
        const body = await resp.json();
        const arr = Array.isArray(body) ? body
          : (Array.isArray(body.items) ? body.items : null);
        const declared = arr ? _combinedDeclared(body, arr) : null;
        if (arr && declared !== null) {
          items = arr;
          combinedProjects = declared.length ? declared : param.split(",");
        }
      }
    } catch (e) { /* fall back to the single-project fetch below */ }
  }
  if (!items) {   // single-project view, or the combined param hasn't landed server-side
    try { items = await (await fetch("/api/memory?project=" + encodeURIComponent(slug))).json(); }
    catch (e) { return; }
    combinedProjects = null;
  }
  const combined = !!(combinedProjects && combinedProjects.length > 1);
  setCombineBanner(combined ? combinedProjects.length : 0);
  const wrap = $("memory-list");
  const rows = taggedItemRows(items, combined);
  if (!rows.length) { wrap.innerHTML = '<p class="muted">No memories yet — run a task.</p>'; return; }
  wrap.innerHTML = "";
  rows.forEach(({ item: m, tag }) => {
    const when = m.created_at ? new Date(m.created_at * 1000).toLocaleString() : "";
    const scope = m.mem_scope === "global" ? "global" : "project";
    const el = document.createElement("div");
    el.className = "mem-item";
    el.innerHTML = `
      <div class="mem-meta"><span class="mem-scope-tag mem-scope-${scope}">${scope}</span>
        ${tag ? `<span class="proj-tag" title="Project this memory belongs to">${escapeHtml(tag)}</span>` : ""}
        <span class="mem-author">${escapeHtml(m.author || "agent")}</span>
        <span>· ${escapeHtml(m.subtask || m.scope)}</span><span>· ${when}</span></div>
      <div class="mem-content">${escapeHtml(m.content)}</div>`;
    wrap.appendChild(el);
  });
}

// ---- QoL: memory curation (Knowledge · Memory) — edit / forget stored memories ----
// Backed by GET/PATCH/DELETE /api/projects/{slug}/memories[/{id}]. Single-project
// by design (curation writes to one project's store); "Load more" pages by offset.
const MEMC_PAGE = 50;
let _memCur = { slug: null, rows: [], total: 0 };

async function loadMemoryCuration(reset) {
  const slug = selectedProject();
  const box = $("mem-curation");
  if (!box) return;
  const applicable = !!slug && slug !== "multi";
  box.classList.toggle("hidden", !applicable);
  if (!applicable) return;
  if (reset || _memCur.slug !== slug) _memCur = { slug, rows: [], total: 0 };
  let data = null;
  try {
    const resp = await fetch("/api/projects/" + encodeURIComponent(slug)
      + "/memories?limit=" + MEMC_PAGE + "&offset=" + _memCur.rows.length);
    if (resp.status === 404 || resp.status === 501) { box.classList.add("hidden"); return; }
    if (resp.ok) data = await resp.json();
  } catch (e) { /* leave the section as-is */ }
  if (!data || !Array.isArray(data.memories)) {
    if (!_memCur.rows.length) $("memc-list").innerHTML = '<p class="muted">Could not load stored memories.</p>';
    return;
  }
  _memCur.total = Number(data.total) || 0;
  _memCur.rows = _memCur.rows.concat(data.memories);
  renderMemoryCuration();
}

function renderMemoryCuration() {
  const page = memoryPageModel(_memCur.total, _memCur.rows.length);
  $("memc-count").textContent = page.countLabel;
  const more = $("memc-more");
  more.classList.toggle("hidden", !page.hasMore);
  if (page.hasMore) more.textContent = page.moreLabel;
  const wrap = $("memc-list");
  if (!_memCur.rows.length) {
    wrap.innerHTML = '<p class="muted">No stored memories yet — run a task.</p>';
    return;
  }
  wrap.innerHTML = "";
  _memCur.rows.forEach(raw => wrap.appendChild(memoryCurationRow(raw)));
}

function _memcError(el, msg) {
  const err = el.querySelector(".memc-error");
  err.textContent = msg;
  err.classList.remove("hidden");
}

function memoryCurationRow(raw) {
  const m = memoryRowModel(raw);
  const el = document.createElement("div");
  el.className = "mem-item memc-item";
  const when = m.createdAt ? new Date(m.createdAt * 1000).toLocaleDateString() : "";
  el.innerHTML =
    `<div class="mem-meta"><span class="mem-scope-tag mem-scope-project">${escapeHtml(m.scope || "memory")}</span>` +
    (m.key ? `<span title="Memory key">· ${escapeHtml(m.key)}</span>` : "") +
    (when ? `<span>· ${escapeHtml(when)}</span>` : "") +
    `<span class="memc-acts">` +
    `<button type="button" class="q-act memc-edit" title="Edit this memory's text">Edit</button>` +
    `<button type="button" class="q-act memc-delete" title="Forget this memory (click twice to confirm)">Delete</button>` +
    `</span></div>` +
    `<div class="mem-content memc-content"></div>` +
    (m.clamped ? `<button type="button" class="link-btn memc-expand" aria-expanded="false">Show all</button>` : "") +
    `<div class="memc-editor hidden">` +
    `<textarea class="memc-textarea" rows="4" aria-label="Memory content"></textarea>` +
    `<div class="pe-actions"><button type="button" class="btn-primary memc-save">Save</button>` +
    `<button type="button" class="ghost-btn memc-cancel">Cancel</button></div></div>` +
    `<p class="pm-error memc-error hidden" role="alert"></p>`;
  el.querySelector(".memc-content").textContent = m.clamped ? m.preview : m.content;
  const expand = el.querySelector(".memc-expand");
  if (expand) expand.onclick = () => {
    const open = expand.getAttribute("aria-expanded") === "true";
    el.querySelector(".memc-content").textContent = open ? m.preview : m.content;
    expand.setAttribute("aria-expanded", String(!open));
    expand.textContent = open ? "Show all" : "Show less";
  };
  const editor = el.querySelector(".memc-editor");
  el.querySelector(".memc-edit").onclick = () => {
    const opening = editor.classList.contains("hidden");
    editor.classList.toggle("hidden", !opening);
    el.querySelector(".memc-error").classList.add("hidden");
    if (opening) {
      const ta = editor.querySelector(".memc-textarea");
      ta.value = String(raw.content || "");
      ta.focus();
    }
  };
  el.querySelector(".memc-cancel").onclick = () => editor.classList.add("hidden");
  el.querySelector(".memc-save").onclick = () => saveMemoryEdit(el, raw, editor);
  // Two-click Delete → Confirm (no browser dialogs); disarms after 5s.
  const del = el.querySelector(".memc-delete");
  del.onclick = () => {
    if (!del.dataset.armed) {
      del.dataset.armed = "1";
      del.textContent = "Confirm delete";
      del.classList.add("memc-arm");
      setTimeout(() => {
        if (del.dataset.armed) { del.dataset.armed = ""; del.textContent = "Delete"; del.classList.remove("memc-arm"); }
      }, 5000);
      return;
    }
    deleteMemoryRow(el, raw);
  };
  return el;
}

async function saveMemoryEdit(el, raw, editor) {
  const content = editor.querySelector(".memc-textarea").value.trim();
  if (!content) { _memcError(el, "Memory content must not be empty."); return; }
  const btn = el.querySelector(".memc-save");
  btn.disabled = true;
  let resp, data = {};
  try {
    resp = await fetch("/api/projects/" + encodeURIComponent(_memCur.slug)
      + "/memories/" + encodeURIComponent(raw.id), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    data = await resp.json().catch(() => ({}));
  } catch (e) { _memcError(el, "Save failed: " + e); btn.disabled = false; return; }
  btn.disabled = false;
  if (!resp.ok || data.error) { _memcError(el, data.error || ("HTTP " + resp.status)); return; }
  raw.content = content;   // keep the cached row current, then re-render in place
  showToast("Memory updated", "success", 2500);
  const fresh = memoryCurationRow(raw);
  el.replaceWith(fresh);
}

async function deleteMemoryRow(el, raw) {
  let resp, data = {};
  try {
    resp = await fetch("/api/projects/" + encodeURIComponent(_memCur.slug)
      + "/memories/" + encodeURIComponent(raw.id), { method: "DELETE" });
    data = await resp.json().catch(() => ({}));
  } catch (e) { _memcError(el, "Delete failed: " + e); return; }
  if (!resp.ok || data.error) { _memcError(el, data.error || ("HTTP " + resp.status)); return; }
  _memCur.rows = _memCur.rows.filter(r => r !== raw);
  _memCur.total = Math.max(0, _memCur.total - 1);
  showToast("Memory forgotten", "warn", 2500);
  renderMemoryCuration();
}

// ---- Knowledge graph v2 (layered/weighted; dependency-free force layout) ----
// Data comes from GET /api/projects/{slug}/graph2 — nodes {id,label,type,degree,
// weight}, edges {src,dst,relation,weight,layer} (src/dst, NOT source/target).
// Combined "Combine with…" views still use the old /api/graph payload,
// normalized into the same shape.
const GRAPH2_COLOR = { file: "var(--g-file)", task: "var(--g-task)",
                       agent: "var(--g-agent)", concept: "var(--g-concept)" };
const SVGNS = "http://www.w3.org/2000/svg";
let graph2Data = { nodes: [], edges: [] };  // raw (unfiltered) payload
let graphProjects = null;   // combined-view slugs (null = single-project view)
let graphLayer = "domain";  // "domain" (Knowledge) | "run" (Run plumbing) | "" (All)
let graphMinWeight = 1;

// ---- KG polish: label filter (dims non-matching) + node-focus mode ----
let _graphEls = { nodes: {}, edges: [] };   // id -> {circle,label,text}; [{el,src,dst}]
let _graphNeighbors = {};                   // id -> Set(neighbor ids)
let graphFocusId = null;                    // focused node id (null = no focus)

function applyGraphView() {
  const q = ($("graph-filter") ? $("graph-filter").value : "").trim().toLowerCase();
  const focus = graphFocusId;
  const nb = focus ? (_graphNeighbors[focus] || new Set()) : null;
  Object.entries(_graphEls.nodes).forEach(([id, els]) => {
    const dim = !!q && !id.toLowerCase().includes(q) && !els.text.includes(q);
    const fade = !!focus && id !== focus && !nb.has(id);
    [els.circle, els.label].forEach(el => {
      el.classList.toggle("g-dim", dim);
      el.classList.toggle("g-fade", fade);
    });
    els.circle.classList.toggle("g-focus", focus === id);
  });
  _graphEls.edges.forEach(e => {
    e.el.classList.toggle("g-fade", !!focus && e.src !== focus && e.dst !== focus);
  });
}

function clearGraphFocus() {
  if (!graphFocusId) return false;
  graphFocusId = null;
  applyGraphView();
  return true;
}

// Old /api/graph payload (source/target, no weights) -> graph2 shape, so the
// combined "Combine with…" view renders through the same pipeline.
function normalizeOldGraph(body) {
  body = body || {};
  const edges = (Array.isArray(body.edges) ? body.edges : []).map(e => ({
    src: e.source, dst: e.target, relation: e.relation || "related_to",
    weight: e.weight != null ? e.weight : 1, layer: e.layer || "domain",
  }));
  // promote nodes that an agent is assigned to / produced by into the "agent" type
  const agentNames = new Set(edges
    .filter(e => e.relation === "assigned_to" || e.relation === "produced_result_by")
    .map(e => e.dst));
  const deg = {};
  edges.forEach(e => {
    deg[e.src] = (deg[e.src] || 0) + 1;
    deg[e.dst] = (deg[e.dst] || 0) + 1;
  });
  const nodes = (Array.isArray(body.nodes) ? body.nodes : []).map(n => ({
    ...n, id: n.id, label: n.label || n.id,
    type: agentNames.has(n.id) ? "agent" : (n.type || "concept"),
    degree: n.degree != null ? n.degree : (deg[n.id] || 0),
    weight: null,
  }));
  return { nodes, edges };
}

async function loadGraph() {
  const slug = selectedProject();
  if (!slug || slug === "multi") return;
  renderCombineRow();
  const param = combinedProjectsParam(slug, [...combineSelected]);
  let data = null, combinedProjects = null;
  if (param) {   // combined read-only view still rides the old /api/graph
    try {
      const resp = await fetch("/api/graph?projects=" + encodeURIComponent(param));
      if (resp.ok) {
        const body = await resp.json();
        if (body && Array.isArray(body.nodes) && Array.isArray(body.edges)) {
          const declared = _combinedDeclared(body, body.nodes);
          if (declared !== null) {
            data = normalizeOldGraph(body);
            combinedProjects = declared.length ? declared : param.split(",");
          }
        }
      }
    } catch (e) { /* fall back to the single-project fetch below */ }
  }
  if (!data) {   // single-project view — the layered graph2 endpoint
    try {
      const resp = await fetch("/api/projects/" + encodeURIComponent(slug) + "/graph2");
      if (resp.ok) {
        const body = await resp.json();
        data = { nodes: body.nodes || [], edges: body.edges || [] };
      }
    } catch (e) { /* fall through to the legacy endpoint */ }
    if (!data) {   // legacy server without graph2
      try { data = normalizeOldGraph(await (await fetch("/api/graph?project=" + encodeURIComponent(slug))).json()); }
      catch (e) { return; }
    }
    combinedProjects = null;
  }
  graph2Data = data;
  graphProjects = (combinedProjects && combinedProjects.length > 1) ? combinedProjects : null;
  setCombineBanner(graphProjects ? graphProjects.length : 0);
  renderGraph();
}

function renderGraph() {
  const svg = $("graph-svg");
  svg.innerHTML = "";
  const vm = graph2ViewModel(graph2Data, { layer: graphLayer, minWeight: graphMinWeight });
  const nodes = vm.nodes.slice(0, 150);   // layout cost cap
  const ids = new Set(nodes.map(n => n.id));
  if (graphFocusId && !ids.has(graphFocusId)) graphFocusId = null;
  const edges = vm.edges.filter(e => ids.has(e.src) && ids.has(e.dst));
  $("graph-stats").textContent = vm.statsLabel;

  if (!nodes.length) {
    const t = document.createElementNS(SVGNS, "text");
    t.setAttribute("x", 400); t.setAttribute("y", 260); t.setAttribute("text-anchor", "middle");
    t.setAttribute("class", "nlabel");
    t.textContent = graph2Data.nodes.length
      ? "Nothing in this layer — try All or a lower min weight."
      : "No knowledge yet — run a task.";
    svg.appendChild(t); $("graph-legend").innerHTML = ""; return;
  }

  const W = 800, H = 520;
  const pos = layout(nodes, edges, W, H);

  _graphEls = { nodes: {}, edges: [] };
  _graphNeighbors = {};
  edges.forEach(e => {
    const a = pos[e.src], b = pos[e.dst];
    const line = document.createElementNS(SVGNS, "line");
    line.setAttribute("x1", a.x); line.setAttribute("y1", a.y);
    line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
    line.setAttribute("class", "edge" + (e.layer === "run" ? " edge-run" : ""));
    if (e.weight > 1) line.style.strokeWidth = Math.min(4, 1 + e.weight * 0.4) + "px";
    svg.appendChild(line);
    _graphEls.edges.push({ el: line, src: e.src, dst: e.dst });
    (_graphNeighbors[e.src] = _graphNeighbors[e.src] || new Set()).add(e.dst);
    (_graphNeighbors[e.dst] = _graphNeighbors[e.dst] || new Set()).add(e.src);
  });

  // combined view: ring each node with its source project's color
  const pcolors = graphProjects ? projectColorMap(graphProjects) : null;

  nodes.forEach(n => {
    const p = pos[n.id];
    const c = document.createElementNS(SVGNS, "circle");
    c.setAttribute("cx", p.x); c.setAttribute("cy", p.y); c.setAttribute("r", n.r);
    c.setAttribute("fill", GRAPH2_COLOR[n.colorKey] || GRAPH2_COLOR.concept);
    c.setAttribute("class", "node");
    if (pcolors) {
      const pj = itemProjects(n)[0];
      if (pj && pcolors[pj]) {
        c.setAttribute("stroke", pcolors[pj]);
        c.setAttribute("stroke-width", "2.5");
      }
    }
    c.addEventListener("click", () => {
      // node-focus mode: click highlights the neighborhood, click again clears
      graphFocusId = graphFocusId === n.id ? null : n.id;
      openNodePanel(n.id);
      applyGraphView();
    });
    svg.appendChild(c);
    const label = document.createElementNS(SVGNS, "text");
    label.setAttribute("x", p.x + n.r + 3); label.setAttribute("y", p.y + 3);
    label.setAttribute("class", "nlabel");
    label.textContent = n.label.length > 22 ? n.label.slice(0, 21) + "…" : n.label;
    svg.appendChild(label);
    _graphEls.nodes[n.id] = { circle: c, label, text: n.label.toLowerCase() };
  });
  applyGraphView();

  let legend = vm.legend
    .map(t => `<span><i style="background:${GRAPH2_COLOR[t]}"></i>${t}</span>`).join("");
  if (pcolors) {
    legend += Object.entries(pcolors).map(([s, c]) =>
      `<span><i style="background:transparent;border:2px solid ${c}"></i>${escapeHtml(s)}</span>`).join("");
  }
  $("graph-legend").innerHTML = legend;
}

// The clicked node's 1-hop neighborhood from the ALREADY-LOADED payload —
// used for combined views and as the fallback when the server fetch fails.
function localNeighborhood(id) {
  const edges = (graph2Data.edges || []).filter(e => e.src === id || e.dst === id);
  const keep = new Set([id]);
  edges.forEach(e => { keep.add(e.src); keep.add(e.dst); });
  return { node: id, nodes: (graph2Data.nodes || []).filter(n => keep.has(n.id)), edges };
}

// Side panel: label/type/degree + incident edges with relation/weight/layer/
// last_ts provenance (server neighborhood carries last_ts; local fallback not).
async function openNodePanel(id) {
  const slug = selectedProject();
  let hood = null;
  if (!graphProjects && slug && slug !== "multi") {
    try {
      const resp = await fetch("/api/projects/" + encodeURIComponent(slug)
        + "/graph2/node/" + encodeURIComponent(id) + "?depth=1");
      if (resp.ok) hood = await resp.json();
    } catch (e) { /* fall back to the loaded payload */ }
  }
  let vm = nodePanelModel(hood);
  if (vm.empty) vm = nodePanelModel(localNeighborhood(id));
  renderNodePanel(vm, id);
}

function renderNodePanel(vm, id) {
  const el = $("node-detail");
  if (vm.empty) {
    el.classList.add("muted");
    el.textContent = "No relations recorded for “" + (id || "this node") + "”.";
    return;
  }
  el.classList.remove("muted");
  const pj = graphProjects
    ? itemProjects((graph2Data.nodes || []).find(n => n.id === vm.id)) : [];
  const projTag = pj.length
    ? ` <span class="proj-tag" title="Project this node belongs to">${escapeHtml(pj.join(" · "))}</span>` : "";
  const rows = vm.edges.length ? vm.edges.map(e =>
    `<div class="np-edge"><span class="np-dir" aria-hidden="true">${e.dir}</span> ` +
    `<b>${escapeHtml(e.otherLabel)}</b> <span class="np-rel">${escapeHtml(e.relation)}</span>` +
    `<span class="np-meta">w${e.weight} · ${escapeHtml(e.layer)}` +
    (e.lastTs != null ? " · " + escapeHtml(new Date(e.lastTs * 1000).toLocaleDateString()) : "") +
    `</span></div>`).join("")
    : "<span class='muted'>no relations</span>";
  const openFile = vm.fileTask
    ? `<button type="button" class="open-file" data-task="${escapeAttr(vm.fileTask)}" data-path="${escapeAttr(vm.id)}">Open in Files →</button>`
    : "";
  el.innerHTML = `<b>${escapeHtml(vm.label)}</b> ` +
    `<span class="muted">(${escapeHtml(vm.type)} · degree ${vm.degree})</span>${projTag}` +
    `<div class="np-edges">${rows}</div>${openFile}`;
  const of = el.querySelector(".open-file");
  if (of) of.onclick = async () => { await openFilesView(of.dataset.task); selectFile(of.dataset.path, null); };
}

// ---- graph2 node search (GET .../graph2/search?q=) — pick a hit to focus it ----
let _gSearchTimer = null;
let _gSearchSeq = 0;

function _hideGraphSearch() {
  $("graph-search-results").classList.add("hidden");
  $("graph-search-results").innerHTML = "";
}

function scheduleGraphSearch() {
  clearTimeout(_gSearchTimer);
  const q = $("graph-search").value.trim();
  if (q.length < 2) { _hideGraphSearch(); return; }
  _gSearchTimer = setTimeout(() => runGraphSearch(q), 250);
}

async function runGraphSearch(q) {
  const slug = selectedProject();
  if (!slug || slug === "multi") return;
  const seq = ++_gSearchSeq;
  let hits = [];
  try {
    const resp = await fetch("/api/projects/" + encodeURIComponent(slug)
      + "/graph2/search?q=" + encodeURIComponent(q));
    if (resp.ok) hits = (await resp.json()).nodes || [];
  } catch (e) { /* show nothing */ }
  if (seq !== _gSearchSeq) return;   // a newer query superseded this one
  const box = $("graph-search-results");
  if (!hits.length) {
    box.innerHTML = `<p class="muted gsr-none">No nodes match “${escapeHtml(q)}”.</p>`;
    box.classList.remove("hidden");
    return;
  }
  box.innerHTML = "";
  hits.forEach(n => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "gsr-item";
    b.setAttribute("role", "option");
    b.innerHTML = `<span class="gsr-label">${escapeHtml(n.label || n.id)}</span>` +
      `<span class="gsr-type">${escapeHtml(n.type || "concept")}</span>`;
    b.onclick = () => focusGraphNode(n.id);
    box.appendChild(b);
  });
  box.classList.remove("hidden");
}

function focusGraphNode(id) {
  _hideGraphSearch();
  $("graph-search").value = "";
  if (_graphEls.nodes[id]) {   // visible under the current layer/weight -> spotlight it
    graphFocusId = id;
    applyGraphView();
  }
  openNodePanel(id);           // panel works even when the node is filtered out
}

function layout(nodes, edges, W, H) {
  const pos = {};
  const n = nodes.length;
  nodes.forEach((nd, i) => {
    const ang = (i / n) * Math.PI * 2;
    pos[nd.id] = { x: W / 2 + Math.cos(ang) * 180 + (i % 7) * 4, y: H / 2 + Math.sin(ang) * 150 + (i % 5) * 4, vx: 0, vy: 0 };
  });
  const k = Math.sqrt((W * H) / n) * 0.55;
  let temp = W / 8;
  for (let iter = 0; iter < 260; iter++) {
    for (let a = 0; a < n; a++) {
      const pa = pos[nodes[a].id]; let fx = 0, fy = 0;
      for (let b = 0; b < n; b++) {
        if (a === b) continue;
        const pb = pos[nodes[b].id];
        let dx = pa.x - pb.x, dy = pa.y - pb.y;
        let d = Math.hypot(dx, dy) || 0.01;
        const rep = (k * k) / d;
        fx += (dx / d) * rep; fy += (dy / d) * rep;
      }
      pa.fx = fx; pa.fy = fy;
    }
    edges.forEach(e => {
      const pa = pos[e.src], pb = pos[e.dst];
      let dx = pa.x - pb.x, dy = pa.y - pb.y;
      let d = Math.hypot(dx, dy) || 0.01;
      const att = (d * d) / k;
      const ox = (dx / d) * att, oy = (dy / d) * att;
      pa.fx -= ox; pa.fy -= oy; pb.fx += ox; pb.fy += oy;
    });
    nodes.forEach(nd => {
      const p = pos[nd.id];
      // gentle pull to center
      p.fx += (W / 2 - p.x) * 0.02; p.fy += (H / 2 - p.y) * 0.02;
      const disp = Math.hypot(p.fx, p.fy) || 0.01;
      p.x += (p.fx / disp) * Math.min(disp, temp);
      p.y += (p.fy / disp) * Math.min(disp, temp);
      p.x = Math.max(24, Math.min(W - 24, p.x));
      p.y = Math.max(24, Math.min(H - 24, p.y));
    });
    temp *= 0.97;
  }
  return pos;
}

// ---- Workspace files (filterable by task) ----
let filesTask = "";

async function populateTaskOptions() {
  let tasks = [];
  try { tasks = await (await fetch("/api/tasks")).json(); } catch (e) { /* ignore */ }
  const sel = $("files-task");
  sel.innerHTML = '<option value="">All tasks</option>' +
    tasks.map(t => `<option value="${escapeAttr(t.id)}">${escapeHtml(t.id)}</option>`).join("");
  sel.value = filesTask;
}

async function loadFiles(taskOverride) {
  if (taskOverride !== undefined) filesTask = taskOverride;
  await populateTaskOptions();
  await renderFileList();
}

async function renderFileList() {
  const url = filesTask ? "/api/workspace?task=" + encodeURIComponent(filesTask) : "/api/workspace";
  let items = [];
  try { items = await (await fetch(url)).json(); } catch (e) { return; }
  const ul = $("file-list");
  const pre = $("file-content");
  const total = items.reduce((s, it) => s + (it.size || 0), 0);
  $("files-stats").textContent = `${items.length} file${items.length === 1 ? "" : "s"} · ${fmtSize(total)}`;
  $("file-download").classList.add("hidden");
  $("file-name").textContent = "No file selected"; $("file-name").classList.add("muted");
  currentFile = null;
  resetFileView();
  if (!items.length) {
    ul.innerHTML = '<li class="muted">No files for this selection.</li>';
    pre.textContent = "No files yet — run a task that writes code."; pre.classList.add("muted");
    return;
  }
  ul.innerHTML = "";
  items.forEach(it => {
    const li = document.createElement("li");
    li.className = "file";
    li.innerHTML = `${escapeHtml(it.path)}<span class="f-size">${fmtSize(it.size)}</span>`;
    makeActivatable(li, () => selectFile(it.path, li), `View file ${it.path}`);
    ul.appendChild(li);
  });
}

// ---- markdown preview (Files view) ----
// Markdown files open rendered by default with a Preview | Raw toggle; every
// other file shows the raw <pre> exactly as before. The choice persists for
// the session (sessionStorage).
const MD_FILE_RE = /\.(md|markdown)$/i;
let fileMode = sessionStorage.getItem("ada-file-mode") === "raw" ? "raw" : "preview";
let currentFile = null;   // { path, content, truncated, isMd } | null

// Show the raw <pre> and hide the preview + toggle (non-md files, transient states).
function resetFileView() {
  $("file-mode").classList.add("hidden");
  $("file-preview").classList.add("hidden");
  $("file-content").classList.remove("hidden");
}

function renderFileView() {
  const pre = $("file-content");
  if (!currentFile || !currentFile.isMd) {
    resetFileView();
    if (currentFile) {
      pre.textContent = (currentFile.content || "(empty)") + (currentFile.truncated ? "\n\n…[truncated]" : "");
    }
    return;
  }
  const seg = $("file-mode");
  seg.classList.remove("hidden");
  seg.querySelectorAll(".seg-opt").forEach(b =>
    b.setAttribute("aria-pressed", b.dataset.v === fileMode ? "true" : "false"));
  const preview = fileMode === "preview";
  $("file-preview").classList.toggle("hidden", !preview);
  pre.classList.toggle("hidden", preview);
  if (preview) {
    $("file-preview").innerHTML = currentFile.content
      ? renderMarkdown(currentFile.content)
        + (currentFile.truncated ? '<p class="md-truncated">…[truncated]</p>' : "")
      : '<p class="muted">(empty)</p>';
  } else {
    pre.textContent = (currentFile.content || "(empty)") + (currentFile.truncated ? "\n\n…[truncated]" : "");
  }
}

async function selectFile(path, li) {
  document.querySelectorAll("#file-list .file").forEach(x => x.classList.remove("active"));
  if (li) li.classList.add("active");
  const taskQ = filesTask ? "&task=" + encodeURIComponent(filesTask) : "";
  $("file-name").textContent = path; $("file-name").classList.remove("muted");
  const dl = $("file-download");
  dl.href = "/api/workspace/download?path=" + encodeURIComponent(path) + taskQ;
  dl.classList.remove("hidden");
  const pre = $("file-content");
  pre.classList.remove("muted");
  resetFileView();
  pre.textContent = "Loading…";
  const url = "/api/workspace/file?path=" + encodeURIComponent(path) + taskQ;
  try {
    const data = await (await fetch(url)).json();
    currentFile = {
      path, content: data.content || "", truncated: !!data.truncated,
      isMd: MD_FILE_RE.test(path),
    };
    renderFileView();
  } catch (e) {
    currentFile = null;
    resetFileView();
    pre.textContent = "Could not load file.";
  }
}

// ---- Run tab: start/stop the project's app inside its checkout ----
let _appPoll = null;
function _appUrl(path) {
  return "/api/projects/" + encodeURIComponent(selectedProject()) + "/app" + (path || "");
}
async function loadRunPanel() {
  clearInterval(_appPoll); _appPoll = null;
  const detect = $("run-detect");
  let st = null;
  try {
    const resp = await fetch(_appUrl());
    if (resp.status === 404 || resp.status === 501) {
      detect.textContent = "Running the project is not available on this server build.";
      ["run-info", "app-start", "app-stop", "app-status", "run-url", "app-logs", "run-detect-tag"].forEach(id => $(id).classList.add("hidden"));
      return;
    }
    st = await resp.json();
  } catch (e) {
    detect.textContent = "Could not reach the server.";
    return;
  }
  renderRunPanel(st);
  if (st && st.running) _startAppPoll();
}
function renderRunPanel(st) {
  const detect = $("run-detect");
  $("run-detect-tag").classList.toggle("hidden", !(st && st.runnable));
  if (!st || !st.runnable) {
    detect.className = "runp-na";
    detect.textContent = (st && st.reason) ||
      "Nothing runnable detected in this project's checkout — no FastAPI/Flask entrypoint, manage.py, or npm start script.";
    ["run-info", "app-start", "app-stop", "app-status", "run-url", "app-logs"].forEach(id => $(id).classList.add("hidden"));
    return;
  }
  detect.className = "runp-detect";
  detect.textContent = (st.detected || "App") +
    " detected — start it in the project's checkout to try the delivered work before merging.";
  $("run-info").classList.remove("hidden");
  $("run-cmd").textContent = st.cmd || "";
  $("run-dir").textContent = st.cwd || "";
  const running = !!st.running;
  $("app-start").classList.toggle("hidden", running);
  $("app-stop").classList.toggle("hidden", !running);
  $("app-status").classList.toggle("hidden", !running);
  $("app-status").textContent = "running" + (st.pid ? " · pid " + st.pid : "");
  const url = $("run-url");
  url.classList.toggle("hidden", !running || !st.url);
  if (st.url) { url.href = st.url; url.textContent = "Open " + st.url + " →"; }
  $("app-logs").classList.toggle("hidden", !running);
}
function _startAppPoll() {
  clearInterval(_appPoll);
  const tick = async () => {
    if (currentMainView !== "project" || currentTab !== "run") { clearInterval(_appPoll); _appPoll = null; return; }
    try {
      const d = await (await fetch(_appUrl("/logs"))).json();
      const pre = $("app-logs");
      const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
      pre.textContent = (d.lines || []).join("\n");
      if (stick) pre.scrollTop = pre.scrollHeight;
      if (!d.running) { clearInterval(_appPoll); _appPoll = null; loadRunPanel(); }
    } catch (e) { /* transient */ }
  };
  tick();
  _appPoll = setInterval(tick, 2000);
}
async function startApp() {
  $("app-start").disabled = true;
  try {
    const resp = await fetch(_appUrl("/start"), { method: "POST" });
    const d = await resp.json().catch(() => ({}));
    if (!resp.ok || d.error) showToast("Could not start: " + (d.error || ("HTTP " + resp.status)), "error", 6000);
    else { showToast("Project started", "success"); renderRunPanel(d); _startAppPoll(); }
  } catch (e) { showToast("Start failed: " + e, "error"); }
  $("app-start").disabled = false;
}
async function stopApp() {
  $("app-stop").disabled = true;
  try {
    await fetch(_appUrl("/stop"), { method: "POST" });
    showToast("Project stopped", "warn");
  } catch (e) { /* ignore */ }
  $("app-stop").disabled = false;
  clearInterval(_appPoll); _appPoll = null;
  loadRunPanel();
}

// =====================================================================
// Feature wave: playbooks, schedules, spend analytics, command palette,
// notification center, KB upload, A/B replay, GitHub status, first-run tour.
// =====================================================================

// ---- Playbooks: strip on the project Overview + typed launch dialog ----
let _playbooks = null;        // cached /api/playbooks catalog
let _pbCurrent = null;        // playbook open in the dialog

async function loadPlaybooks() {
  if (_playbooks === null) {
    try {
      const resp = await fetch("/api/playbooks");
      _playbooks = resp.ok ? await resp.json() : [];
    } catch (e) { _playbooks = []; }
  }
  renderPlaybookStrip();
}

function renderPlaybookStrip() {
  const strip = $("pb-strip"), wrap = $("pb-cards");
  if (!strip || !wrap) return;
  const list = Array.isArray(_playbooks) ? _playbooks : [];
  const usable = !!selectedProject() && selectedProject() !== "multi";
  strip.classList.toggle("hidden", !list.length || !usable);
  if (!list.length || !usable) { wrap.innerHTML = ""; return; }
  wrap.innerHTML = "";
  list.forEach(pb => {
    const card = document.createElement("div");
    card.className = "pb-card";
    card.innerHTML = `<span class="pb-name">${escapeHtml(pb.name)}</span>` +
      `<span class="pb-desc">${escapeHtml(pb.description || "")}</span>`;
    makeActivatable(card, () => openPlaybookModal(pb), `Start playbook ${pb.name}`);
    wrap.appendChild(card);
  });
}

function openPlaybookModal(pb) {
  _pbCurrent = pb;
  $("pbm-name").textContent = pb.name || pb.id;
  $("pbm-desc").textContent = pb.description || "";
  $("pbm-error").classList.add("hidden");
  $("pbm-error").textContent = "";
  $("pbm-start").disabled = false;
  const m = playbookFormModel(pb, {});   // prefill defaults
  $("pbm-fields").innerHTML = m.fields.map(f => {
    const id = "pbf-" + escapeAttr(f.key);
    let control;
    if (f.control === "select") {
      control = `<select id="${id}" class="select pbm-control" data-key="${escapeAttr(f.key)}">` +
        f.choices.map(c => `<option value="${escapeAttr(c)}"${c === f.value ? " selected" : ""}>${escapeHtml(c)}</option>`).join("") +
        `</select>`;
    } else if (f.control === "number") {
      control = `<input id="${id}" type="number" step="1" class="num pbm-control" data-key="${escapeAttr(f.key)}" value="${escapeAttr(f.value)}" />`;
    } else {
      control = `<input id="${id}" type="text" class="title-input pbm-control" data-key="${escapeAttr(f.key)}" value="${escapeAttr(f.value)}" />`;
    }
    return `<div class="pbm-field"><label class="pm-label" for="${id}">${escapeHtml(f.label)}` +
      `${f.required && !f.value ? ' <span class="pbm-req" title="required">*</span>' : ""}</label>${control}</div>`;
  }).join("") || '<p class="muted">This playbook takes no parameters.</p>';
  openModalEl("playbook-modal");
}

function closePlaybookModal() { closeModalEl("playbook-modal"); _pbCurrent = null; }

function _pbmFail(msg) {
  const el = $("pbm-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

async function startPlaybook() {
  if (!_pbCurrent) return;
  const slug = selectedProject();
  if (!slug || slug === "multi") { _pbmFail("Select a project first."); return; }
  const values = {};
  document.querySelectorAll("#pbm-fields .pbm-control").forEach(el => { values[el.dataset.key] = el.value; });
  const m = playbookFormModel(_pbCurrent, values);
  if (!m.ok) { _pbmFail(m.errors.join(" · ")); return; }
  const b = parseFloat($("budget").value);
  const body = {
    params: m.params, project: slug,
    model: segState.model, effort: segState.effort,
    budget: (b && b > 0) ? b : null,
  };
  const btn = $("pbm-start");
  btn.disabled = true;
  $("pbm-error").classList.add("hidden");
  let resp, data = {};
  try {
    resp = await fetch("/api/playbooks/" + encodeURIComponent(_pbCurrent.id) + "/run", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    data = await resp.json().catch(() => ({}));
  } catch (e) { _pbmFail("Request failed: " + e); btn.disabled = false; return; }
  if (!resp.ok || data.error) { _pbmFail(data.error || ("HTTP " + resp.status)); btn.disabled = false; return; }
  closePlaybookModal();
  showToast("Playbook started · " + (data.title || _pbCurrent.name), "success");
  state.budgetUsd = body.budget || serverConfig.budget_usd || 0;
  attachToRun(data.task_id, data.title || _pbCurrent.name);   // same stream as a normal launch
  loadQueue();
}

// ---- Schedules manager (project Tasks tab) ----
async function loadSchedules() {
  const slug = selectedProject();
  const card = $("schedules-card");
  if (!card) return;
  const applicable = !!slug && slug !== "multi";
  card.classList.toggle("hidden", !applicable);
  if (!applicable) return;
  let rows = [];
  try {
    const resp = await fetch("/api/schedules?project=" + encodeURIComponent(slug));
    if (resp.ok) rows = await resp.json();
  } catch (e) { /* leave empty */ }
  renderSchedules(Array.isArray(rows) ? rows : []);
}

function renderSchedules(rows) {
  const ul = $("sched-list");
  if (!rows.length) { ul.innerHTML = '<li class="muted">No schedules yet — add one below.</li>'; return; }
  const now = Date.now() / 1000;
  ul.innerHTML = "";
  rows.forEach(row => {
    const m = scheduleRowModel(row, now);
    const li = document.createElement("li");
    li.className = "sched-item" + (m.enabled ? "" : " sched-off");
    li.innerHTML =
      `<div class="sched-main"><span class="sched-title" title="${escapeAttr(m.prompt)}">${escapeHtml(m.title)}</span>` +
      `<div class="sched-meta"><span>${escapeHtml(m.everyLabel)}</span>` +
      `<span class="sched-next">${escapeHtml(m.nextLabel)}</span>` +
      (m.budgetLabel ? `<span class="r-cost">${escapeHtml(m.budgetLabel)}/run</span>` : "") +
      (m.lastTaskId ? `<button type="button" class="link-btn sched-last" title="Open the last run this schedule started">last run →</button>` : "") +
      `</div></div>` +
      `<span class="sched-acts">` +
      `<label class="sched-toggle" title="${m.enabled ? "Disable" : "Enable"} this schedule">` +
      `<input type="checkbox" class="sched-enabled"${m.enabled ? " checked" : ""} aria-label="Schedule enabled" /> on</label>` +
      `<button type="button" class="q-act sched-delete" title="Delete this schedule" aria-label="Delete schedule">✕</button></span>`;
    const last = li.querySelector(".sched-last");
    if (last) last.onclick = () => openTask(m.lastTaskId, null);
    li.querySelector(".sched-enabled").onchange = (e) => patchScheduleEnabled(m.id, e.target.checked);
    li.querySelector(".sched-delete").onclick = () => deleteSchedule(m.id, m.title);
    ul.appendChild(li);
  });
}

function _scFail(msg) {
  const el = $("sc-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

// QoL: recurrence mode — "Every N hours" (interval) | "Cron" (5-field expression).
let schedMode = "hours";
function setSchedMode(mode) {
  schedMode = mode === "cron" ? "cron" : "hours";
  $("sc-mode").querySelectorAll(".seg-opt").forEach(b =>
    b.setAttribute("aria-pressed", b.dataset.v === schedMode ? "true" : "false"));
  $("sc-every-wrap").classList.toggle("hidden", schedMode === "cron");
  $("sc-cron-wrap").classList.toggle("hidden", schedMode !== "cron");
  $("sc-cron-hint").classList.toggle("hidden", schedMode !== "cron");
}

async function createSchedule() {
  const m = scheduleFormModel({
    prompt: $("sc-prompt").value, title: $("sc-title").value,
    mode: schedMode, cron: $("sc-cron").value,
    every_hours: $("sc-every").value, budget_usd: $("sc-budget").value,
  });
  if (!m.ok) { _scFail(m.errors.join(" · ")); return; }
  $("sc-error").classList.add("hidden");
  const btn = $("sc-create");
  btn.disabled = true;
  let resp, data = {};
  try {
    resp = await fetch("/api/schedules", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...m.body, project: selectedProject() }),
    });
    data = await resp.json().catch(() => ({}));
  } catch (e) { _scFail("Request failed: " + e); btn.disabled = false; return; }
  btn.disabled = false;
  if (!resp.ok || data.error) { _scFail(data.error || ("HTTP " + resp.status)); return; }
  $("sc-prompt").value = ""; $("sc-title").value = "";
  showToast("Schedule created", "success");
  loadSchedules();
}

async function patchScheduleEnabled(sid, enabled) {
  try {
    const resp = await fetch("/api/schedules/" + encodeURIComponent(sid), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    if (!resp.ok) showToast("Could not update the schedule", "warn");
  } catch (e) { showToast("Could not update the schedule", "warn"); }
  loadSchedules();
}

async function deleteSchedule(sid, title) {
  try {
    const resp = await fetch("/api/schedules/" + encodeURIComponent(sid), { method: "DELETE" });
    if (resp.ok) showToast(`Deleted schedule "${title}"`, "warn");
    else showToast("Delete failed", "error");
  } catch (e) { showToast("Delete failed", "error"); }
  loadSchedules();
}

// ---- Spend analytics (All activity) ----
async function loadSpend() {
  let ov = null, oc = null;
  try {
    const [r1, r2] = await Promise.all([
      fetch("/api/analytics/overview?days=30"), fetch("/api/analytics/outcomes"),
    ]);
    if (r1.ok) ov = await r1.json();
    if (r2.ok) oc = await r2.json();
  } catch (e) { /* leave the section as-is */ }
  if (ov) renderSpend(spendOverviewModel(ov));
  if (oc) renderOutcomes(spendOutcomesModel(oc));
}

function renderSpend(m) {
  $("sp-window").textContent = m.windowDays;
  $("sp-total").textContent = m.totalLabel;
  $("sp-runs").textContent = m.runs;
  $("sp-tokens").textContent = m.runs ? m.tokensLabel : "—";
  const bars = $("sp-bars");
  if (!m.bars.length) {
    bars.innerHTML = '<p class="muted">No spend in this window.</p>';
  } else {
    bars.innerHTML = m.bars.map(b =>
      `<span class="sp-bar" style="height:${b.hPct}%" title="${escapeAttr(b.label)}"></span>`).join("");
  }
  const pt = $("sp-projects");
  if (!m.projects.length) {
    pt.innerHTML = '<p class="muted">No spend recorded yet.</p>';
  } else {
    pt.innerHTML = `<table class="dsa-table"><thead><tr><th scope="col">Project</th>` +
      `<th scope="col">USD</th><th scope="col">Runs</th><th scope="col">Avg quality</th></tr></thead><tbody>` +
      m.projects.map(p => `<tr><td class="dsa-name">${escapeHtml(p.project)}</td>` +
        `<td class="sp-usd">${escapeHtml(p.usdLabel)}</td><td>${p.runs}</td>` +
        `<td>${escapeHtml(p.qualityLabel)}</td></tr>`).join("") + `</tbody></table>`;
  }
}

function renderOutcomes(m) {
  $("sp-outcomes").innerHTML =
    `<table class="dsa-table"><thead><tr><th scope="col">Outcome</th>` +
    `<th scope="col">USD</th><th scope="col">Count</th></tr></thead><tbody>` +
    m.rows.map(r => `<tr><td>${escapeHtml(r.label)}</td>` +
      `<td class="sp-usd">${escapeHtml(r.value)}</td><td>${r.count}</td></tr>`).join("") +
    `</tbody></table>`;
}

// ---- Per-run cost breakdown (task detail, collapsible) ----
async function loadRunCost(taskId) {
  if (!taskId) return;
  let data = null;
  try {
    const resp = await fetch("/api/analytics/run/" + encodeURIComponent(taskId));
    if (resp.ok) data = await resp.json();
  } catch (e) { return; }
  if (!data) return;
  const m = runCostModel(data);
  const card = $("cost-card");
  if (m.empty) { card.classList.add("hidden"); return; }
  state.subtaskCosts = m.bySubtask;
  $("cost-table").innerHTML =
    `<table class="dsa-table cost-table"><thead><tr><th scope="col">Subtask</th>` +
    `<th scope="col">Agent</th><th scope="col">USD</th><th scope="col">Tokens</th></tr></thead><tbody>` +
    m.rows.map(r => `<tr><td class="dsa-name">${escapeHtml(r.subtask)}</td>` +
      `<td>${escapeHtml(r.agent)}</td><td class="sp-usd">${escapeHtml(r.usdLabel)}</td>` +
      `<td class="sp-tok">${escapeHtml(r.tokensLabel)}</td></tr>`).join("") +
    `</tbody></table>` +
    `<p class="cost-totals muted">attributed ${escapeHtml(m.totalLabel)}` +
    (m.runTotalLabel ? ` · run total ${escapeHtml(m.runTotalLabel)}` : "") +
    (m.unattributedLabel ? ` · unattributed ${escapeHtml(m.unattributedLabel)}` : "") + `</p>`;
  card.classList.remove("hidden");
  if (!$("timeline-card").classList.contains("hidden")) renderTimeline();  // add cost annotations
}

// ---- A/B replay smoke (All activity) ----
async function runAB() {
  const knob = $("ab-knob").value;
  const values = $("ab-values").value.split(",").map(s => s.trim()).filter(Boolean);
  const el = $("ab-result");
  if (values.length < 2) {
    el.classList.remove("muted");
    el.innerHTML = '<p class="pm-error">Enter at least two distinct values, comma-separated.</p>';
    return;
  }
  const btn = $("ab-run");
  btn.disabled = true;
  el.classList.remove("muted");
  el.textContent = "Running replay arms — fully offline, this can take a minute…";
  let resp, data = {};
  try {
    resp = await fetch("/api/ab", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ knob, values }),
    });
    data = await resp.json().catch(() => ({}));
  } catch (e) { el.innerHTML = `<p class="pm-error">A/B failed: ${escapeHtml(String(e))}</p>`; btn.disabled = false; return; }
  btn.disabled = false;
  if (!resp.ok || data.error) {
    el.innerHTML = `<p class="pm-error">A/B failed: ${escapeHtml(data.error || ("HTTP " + resp.status))}</p>`;
    return;
  }
  const m = abTableModel(data);
  el.innerHTML =
    `<table class="cmp-table"><thead><tr><th scope="col">${escapeHtml(m.knob)}</th>` +
    `<th scope="col">Pass rate</th><th scope="col">Quality</th><th scope="col">Mean cost</th>` +
    `<th scope="col">Mean wall</th><th scope="col">Attempts</th></tr></thead><tbody>` +
    m.rows.map(r => `<tr${r.best ? ' class="ab-best"' : ""}><td>${escapeHtml(r.value)}${r.best ? " ★" : ""}</td>` +
      `<td>${escapeHtml(r.passLabel)}</td><td>${escapeHtml(r.qualityLabel)}</td>` +
      `<td>${escapeHtml(r.costLabel)}</td><td>${escapeHtml(r.wallLabel)}</td>` +
      `<td>${r.attempts != null ? r.attempts : "—"}</td></tr>`).join("") +
    `</tbody></table>` +
    (m.verdict ? `<p class="ab-verdict">${escapeHtml(m.verdict)} <span class="muted">(replay smoke — validates mechanics, not real quality)</span></p>` : "");
}

// ---- GitHub status line (global Settings console) ----
async function loadGithubStatus() {
  let st = null;
  try {
    const resp = await fetch("/api/github/status");
    if (resp.ok) st = await resp.json();
  } catch (e) { return; }
  if (!st) return;
  const cards = [...document.querySelectorAll("#gs-groups .gs-card")];
  const ghCard = cards.find(c => {
    const h = c.querySelector(".section-label");
    return h && /github/i.test(h.textContent);
  });
  if (!ghCard) return;
  let line = ghCard.querySelector(".gh-status-line");
  if (!line) {
    line = document.createElement("div");
    line.className = "gh-status-line";
    const h = ghCard.querySelector(".section-label");
    h.insertAdjacentElement("afterend", line);
  }
  const repos = Object.entries(st.repos || {});
  line.innerHTML =
    `<span class="gh-state ${st.enabled ? "gh-on" : "gh-off"}">${st.enabled ? "enabled" : "disabled"}</span>` +
    `<span>${repos.length} repo${repos.length === 1 ? "" : "s"}` +
    (repos.length ? `: ${repos.map(([r, s]) => escapeHtml(r + " → " + s)).join(", ")}` : "") + `</span>` +
    `<span>label <code>${escapeHtml(st.label || "ada")}</code></span>` +
    `<span>${st.tracked || 0} tracked run${st.tracked === 1 ? "" : "s"}</span>` +
    `<span class="muted gh-token-note">token via <code>ADA_GITHUB_TOKEN</code> env — never stored in settings</span>`;
}

// ---- KB upload: drop zone + file picker (Knowledge · Memory) ----
const KB_MAX_BYTES = 2 * 1024 * 1024;

function _kbResult(name, text, isError) {
  const li = document.createElement("li");
  li.className = "kb-result" + (isError ? " kb-err" : "");
  li.innerHTML = `<code>${escapeHtml(name)}</code> <span>${escapeHtml(text)}</span>`;
  $("kb-results").prepend(li);
}

async function uploadKbFiles(files) {
  const slug = selectedProject();
  if (!slug || slug === "multi") return;
  for (const file of files) {
    if (file.size > KB_MAX_BYTES) { _kbResult(file.name, "too large (2MB max)", true); continue; }
    const fd = new FormData();
    fd.append("file", file, file.name);
    let resp, data = {};
    try {
      resp = await fetch("/api/projects/" + encodeURIComponent(slug) + "/kb/upload",
                         { method: "POST", body: fd });
      data = await resp.json().catch(() => ({}));
    } catch (e) { _kbResult(file.name, "upload failed", true); continue; }
    if (resp.status === 413) { _kbResult(file.name, "too large (2MB max)", true); continue; }
    if (!resp.ok || data.error) { _kbResult(file.name, data.error || ("HTTP " + resp.status), true); continue; }
    _kbResult(file.name, `ingested · ${data.chunks} chunk${data.chunks === 1 ? "" : "s"}`, false);
  }
}

function wireKbDrop() {
  const drop = $("kb-drop"), input = $("kb-file");
  if (!drop || !input) return;
  const browse = () => input.click();
  $("kb-browse").onclick = (e) => { e.stopPropagation(); browse(); };
  drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); browse(); }
  });
  drop.addEventListener("click", (e) => { if (!e.target.closest("button")) browse(); });
  input.onchange = () => { uploadKbFiles([...input.files]); input.value = ""; };
  ["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.add("kb-over");
  }));
  ["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.remove("kb-over");
  }));
  drop.addEventListener("drop", (e) => {
    const files = [...((e.dataTransfer && e.dataTransfer.files) || [])];
    if (files.length) uploadKbFiles(files);
  });
}

// ---- Command palette (cmd+K / ctrl+K): global search + static commands ----
let _palModel = { groups: [], flat: [], empty: true };
let _palIdx = 0;
let _palTimer = null;
let _palSeq = 0;

function openPalette() {
  $("palette-input").value = "";
  renderPaletteHint("Type at least 2 characters to search across every project.");
  _palModel = { groups: [], flat: [], empty: true };
  _palIdx = 0;
  openModalEl("palette-modal");
  $("palette-input").focus();
}
function closePalette() { closeModalEl("palette-modal"); }

function renderPaletteHint(text) {
  $("palette-results").innerHTML = `<p class="muted pal-hint">${escapeHtml(text)}</p>`;
}

function schedulePaletteSearch() {
  clearTimeout(_palTimer);
  const q = $("palette-input").value.trim();
  if (q.length < 2) {
    _palModel = paletteResultsModel(q, [], projectList);
    _palIdx = 0;
    if (_palModel.empty) renderPaletteHint("Type at least 2 characters to search across every project.");
    else renderPalette();
    return;
  }
  _palTimer = setTimeout(() => runPaletteSearch(q), 250);
}

async function runPaletteSearch(q) {
  const seq = ++_palSeq;
  let hits = [];
  try {
    const resp = await fetch("/api/search?q=" + encodeURIComponent(q) + "&limit=20");
    if (resp.ok) hits = await resp.json();
  } catch (e) { /* show commands only */ }
  if (seq !== _palSeq) return;   // a newer query superseded this one
  _palModel = paletteResultsModel(q, hits, projectList);
  _palIdx = 0;
  if (_palModel.empty) renderPaletteHint(`No results for “${q}”.`);
  else renderPalette();
}

function renderPalette() {
  const box = $("palette-results");
  let idx = 0;
  box.innerHTML = _palModel.groups.map(g =>
    `<div class="pal-group"><div class="pal-group-label">${escapeHtml(g.label)}</div>` +
    g.items.map(it => {
      const i = idx++;
      const chip = it.project ? `<span class="proj-tag">${escapeHtml(it.project)}</span>` : "";
      // QoL: "≈" marks hits found by semantic similarity rather than token overlap
      const sem = it.semantic
        ? `<span class="pal-sem" title="semantic match" aria-label="semantic match">≈</span>` : "";
      const main = it.type === "command"
        ? `<span class="pal-title">${escapeHtml(it.label)}</span>`
        : `${sem}<span class="pal-title">${escapeHtml(it.title || "(untitled)")}</span>` +
          (it.snippet ? `<span class="pal-snippet">${escapeHtml(it.snippet)}</span>` : "");
      return `<div class="pal-item${i === _palIdx ? " active" : ""}" data-idx="${i}" role="option"` +
        ` aria-selected="${i === _palIdx ? "true" : "false"}">${main}${chip}</div>`;
    }).join("") + `</div>`).join("");
  box.querySelectorAll(".pal-item").forEach(el => {
    el.onclick = () => activatePaletteItem(_palModel.flat[parseInt(el.dataset.idx, 10)]);
    el.onmousemove = () => {
      const i = parseInt(el.dataset.idx, 10);
      if (i !== _palIdx) { _palIdx = i; _palHighlight(); }
    };
  });
}

function _palHighlight() {
  document.querySelectorAll("#palette-results .pal-item").forEach(el => {
    const on = parseInt(el.dataset.idx, 10) === _palIdx;
    el.classList.toggle("active", on);
    el.setAttribute("aria-selected", on ? "true" : "false");
    if (on) el.scrollIntoView({ block: "nearest" });
  });
}

async function activatePaletteItem(it) {
  if (!it) return;
  closePalette();
  if (it.type === "command") {
    if (it.action === "project") selectProject(it.slug, "overview");
    else if (it.action === "new-project") openProjectModal();
    else if (it.action === "settings") showGlobalSettings();
    else if (it.action === "activity") showActivity();
    return;
  }
  const ref = it.ref || {};
  const proj = it.project && it.project !== "multi" ? it.project : null;
  if (it.type === "task") {
    if (proj) selectProject(proj, "tasks");
    openTask(ref.task_id || ref.id, null);
  } else if (it.type === "memory" || it.type === "kb") {
    if (proj) selectProject(proj);
    currentKnow = "memory";
    selectProjectTab("knowledge", true);
  } else if (it.type === "file") {
    if (proj) selectProject(proj);
    await openFilesView(ref.task || "");
    if (ref.path) selectFile(ref.path, null);
  }
}

function paletteKeydown(e) {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const n = _palModel.flat.length;
    if (!n) return;
    _palIdx = (_palIdx + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
    _palHighlight();
  } else if (e.key === "Enter") {
    e.preventDefault();
    activatePaletteItem(_palModel.flat[_palIdx]);
  }
}

// ---- Notification center (session-scope; captured from run events) ----
let _notifs = [];
let _notifSeq = 0;

function notify(kind, text, taskId) {
  _notifs.push({ id: "n" + (++_notifSeq), ts: Date.now() / 1000, kind,
                 text: String(text || ""), taskId: taskId || state.taskId || "", read: false });
  if (_notifs.length > 100) _notifs = _notifs.slice(-100);
  renderNotifs();
}

function renderNotifs() {
  const m = notifCenterModel(_notifs, Date.now() / 1000);
  const badge = $("notif-badge");
  badge.classList.toggle("hidden", !m.unread);
  badge.textContent = m.badge;
  const ul = $("notif-list");
  if (m.empty) { ul.innerHTML = '<li class="muted">Nothing yet — run a task.</li>'; return; }
  ul.innerHTML = "";
  m.rows.forEach(r => {
    const li = document.createElement("li");
    li.className = "notif-item " + r.cls + (r.read ? " notif-read" : "");
    li.innerHTML = `<span class="notif-icon" aria-hidden="true">${escapeHtml(r.icon)}</span>` +
      `<span class="notif-body"><span class="notif-text">${escapeHtml(r.text)}</span>` +
      `<span class="notif-when">${escapeHtml(r.timeLabel)}${r.taskId ? " · " + escapeHtml(r.taskId) : ""}</span></span>`;
    makeActivatable(li, () => {
      const n = _notifs.find(x => x.id === r.id);
      if (n) n.read = true;
      toggleNotifPanel(false);
      if (r.taskId) openTask(r.taskId, null);
      renderNotifs();
    }, "Open task for notification: " + r.text);
    ul.appendChild(li);
  });
}

function toggleNotifPanel(open) {
  const panel = $("notif-panel");
  const show = open !== undefined ? open : panel.classList.contains("hidden");
  panel.classList.toggle("hidden", !show);
  $("notif-bell").setAttribute("aria-expanded", show ? "true" : "false");
  if (show) renderNotifs();
}

function markAllNotifsRead() {
  _notifs.forEach(n => { n.read = true; });
  renderNotifs();
}

// ---- First-run tour: 4-step spotlight over the empty state ----
let _tour = null;   // { steps, idx, overlay, pop, ring }

function maybeStartTour() {
  const m = tourStepsModel(projectList.length, localStorage.getItem("ada-tour-done"));
  if (!m.show || _tour) return;
  const overlay = document.createElement("div");
  overlay.className = "tour-overlay";
  const ring = document.createElement("div");
  ring.className = "tour-ring hidden";
  const pop = document.createElement("div");
  pop.className = "tour-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "First-run tour");
  overlay.appendChild(ring);
  overlay.appendChild(pop);
  document.body.appendChild(overlay);
  _tour = { steps: m.steps, idx: 0, overlay, pop, ring };
  renderTourStep();
}

function renderTourStep() {
  if (!_tour) return;
  const step = _tour.steps[_tour.idx];
  const last = _tour.idx === _tour.steps.length - 1;
  _tour.pop.innerHTML =
    `<span class="kicker">Step ${_tour.idx + 1} of ${_tour.steps.length}</span>` +
    `<h3 class="tour-title">${escapeHtml(step.title)}</h3>` +
    `<p class="tour-body">${escapeHtml(step.body)}</p>` +
    `<div class="tour-actions"><button type="button" class="btn-primary tour-next">${last ? "Done" : "Next →"}</button>` +
    `<button type="button" class="ghost-btn tour-skip">Skip tour</button></div>`;
  _tour.pop.querySelector(".tour-next").onclick = () => {
    if (last) endTour();
    else { _tour.idx++; renderTourStep(); }
  };
  _tour.pop.querySelector(".tour-skip").onclick = endTour;
  // position the popover + spotlight ring near the step's target
  const target = step.target ? $(step.target) : null;
  const rect = target ? target.getBoundingClientRect() : null;
  if (rect && rect.width && rect.height) {
    Object.assign(_tour.ring.style, {
      left: (rect.left - 6) + "px", top: (rect.top - 6) + "px",
      width: (rect.width + 12) + "px", height: (rect.height + 12) + "px",
    });
    _tour.ring.classList.remove("hidden");
    const popLeft = Math.min(Math.max(12, rect.right + 18), window.innerWidth - 332);
    const popTop = Math.min(Math.max(12, rect.top), window.innerHeight - 220);
    Object.assign(_tour.pop.style, { left: popLeft + "px", top: popTop + "px", transform: "none" });
  } else {
    _tour.ring.classList.add("hidden");
    Object.assign(_tour.pop.style, { left: "50%", top: "38%", transform: "translate(-50%,-50%)" });
  }
  _tour.pop.querySelector(".tour-next").focus({ preventScroll: true });
}

function endTour() {
  if (!_tour) return;
  localStorage.setItem("ada-tour-done", "1");
  _tour.overlay.remove();
  _tour = null;
}

// ---- wire up ----
$("run-btn").onclick = runTask;
$("approve-btn").onclick = approvePlan;
$("discard-btn").onclick = discardPlan;
$("refine-btn").onclick = refinePlan;
// re-engage (continue a completed task)
$("continue-clear").onclick = clearContinue;
$("reengage-btn").onclick = () => setContinue(state.docsId, state.docsId);
$("modal-reengage").onclick = () => setContinue(currentDocsId, currentDocsId);
$("pe-instruction").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); refinePlan(); } });
$("cancel-btn").onclick = cancelRun;
$("mem-refresh").onclick = loadMemory;
$("graph-refresh").onclick = loadGraph;
$("files-refresh").onclick = () => loadFiles();
$("files-task").onchange = () => { filesTask = $("files-task").value; renderFileList(); };
// markdown Preview | Raw toggle — the choice sticks for the session
$("file-mode").querySelectorAll(".seg-opt").forEach(b => b.onclick = () => {
  fileMode = b.dataset.v;
  sessionStorage.setItem("ada-file-mode", fileMode);
  renderFileView();
});
$("tasks-refresh").onclick = loadRecent;
$("prompt").addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") runTask(); });
$("docs-link").onclick = (e) => { e.preventDefault(); if (state.docsId) openDocs(state.docsId); };
$("modal-close").onclick = () => closeModalEl("modal");
$("modal").onclick = (e) => { if (e.target === $("modal")) closeModalEl("modal"); };
$("agent-modal-close").onclick = closeAgentModal;
$("agent-modal").onclick = (e) => { if (e.target === $("agent-modal")) closeAgentModal(); };
// full per-subtask transcript panel (opens on top of the agent modal)
$("am-transcript").onclick = () => { if (state.openAgentId) openTranscript(state.openAgentId); };
$("transcript-modal-close").onclick = closeTranscript;
$("transcript-modal").onclick = (e) => { if (e.target === $("transcript-modal")) closeTranscript(); };
// F4: Reviews & Permissions panel + project settings
$("review-btn").onclick = openReviewPanel;
$("review-modal-close").onclick = closeReviewPanel;
$("review-modal").onclick = (e) => { if (e.target === $("review-modal")) closeReviewPanel(); };
document.querySelectorAll("#review-modal .tab").forEach(t => t.onclick = () => selectReviewTab(t.dataset.rtab));
$("ph-policy-save").onclick = saveProjectPolicy;
$("proj-archive").onclick = toggleArchiveProject;
$("proj-delete").onclick = deleteProject;
$("roster-modal-close").onclick = closeRosterModal;
$("roster-modal").onclick = (e) => { if (e.target === $("roster-modal")) closeRosterModal(); };
// a11y: Escape closes the topmost dialog; Tab is trapped inside an open dialog
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("palette-modal").classList.contains("hidden")) closePalette();
  else if (!$("playbook-modal").classList.contains("hidden")) closePlaybookModal();
  else if (!$("notif-panel").classList.contains("hidden")) toggleNotifPanel(false);
  else if (!$("project-modal").classList.contains("hidden")) closeProjectModal();
  else if (!$("review-modal").classList.contains("hidden")) closeReviewPanel();
  else if (!$("roster-modal").classList.contains("hidden")) closeRosterModal();
  else if (!$("transcript-modal").classList.contains("hidden")) closeTranscript();
  else if (!$("agent-modal").classList.contains("hidden")) closeAgentModal();
  else if (!$("modal").classList.contains("hidden")) closeModalEl("modal");
  else if (knowVisible("graph")) clearGraphFocus();   // node-focus mode: Esc clears
});
// cmd+K / ctrl+K opens the command palette from anywhere
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
    e.preventDefault();
    if ($("palette-modal").classList.contains("hidden")) openPalette();
    else closePalette();
  }
});
["modal", "agent-modal", "transcript-modal", "roster-modal", "project-modal", "review-modal",
 "playbook-modal", "palette-modal"].forEach(id =>
  $(id).addEventListener("keydown", (e) => _trapModalTab($(id), e)));
document.querySelectorAll("#modal .tab").forEach(t => t.onclick = () => selectTab(t.dataset.doc));
// project tab bar + knowledge sub-tabs + sidebar global nav
document.querySelectorAll("#project-tabs .ptab").forEach(b => b.onclick = () => selectProjectTab(b.dataset.tab));
document.querySelectorAll("#know-tabs .ktab").forEach(b => b.onclick = () => showKnowledge(b.dataset.know));
$("nav-home").onclick = showHome;                   // the console's entry screen
$("nav-activity").onclick = showActivity;
$("nav-agents").onclick = showAgents;
$("nav-gsettings").onclick = showGlobalSettings;    // global settings console
$("gs-refresh").onclick = loadGlobalSettings;
$("agents-refresh").onclick = () => loadAgents(true);
// away wave: home + workspaces + custom agents + graph2 controls
$("home-refresh").onclick = loadHome;
$("home-spend-link").onclick = showActivity;
$("new-workspace").onclick = () => {
  const row = $("ws-create-row");
  row.classList.toggle("hidden");
  if (!row.classList.contains("hidden")) $("ws-create-name").focus();
};
$("ws-create-go").onclick = createWorkspace;
$("ws-create-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); createWorkspace(); }
});
$("ws-refresh").onclick = refreshWorkspaceView;
$("ws-edit").onclick = () => {
  const entry = workspaceEntry(currentWorkspace) || {};
  const form = $("ws-edit-form");
  const opening = form.classList.contains("hidden");
  form.classList.toggle("hidden", !opening);
  if (opening) {
    $("ws-name-input").value = entry.name || "";
    $("ws-desc-input").value = entry.description || "";
    $("ws-edit-error").classList.add("hidden");
    $("ws-name-input").focus();
  }
};
$("ws-edit-cancel").onclick = () => $("ws-edit-form").classList.add("hidden");
$("ws-save").onclick = saveWorkspaceMeta;
$("ws-assign-btn").onclick = assignWorkspaceProject;
$("ws-deps-save").onclick = saveWorkspaceDeps;
$("ws-run").onclick = runWorkspace;
$("ws-prompt").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") runWorkspace();
});
$("agent-new").onclick = () => openAgentForm(null);
$("af-save").onclick = saveAgentForm;
$("af-cancel").onclick = closeAgentForm;
$("graph-layer").querySelectorAll(".seg-opt").forEach(b => b.onclick = () => {
  graphLayer = b.dataset.v;
  $("graph-layer").querySelectorAll(".seg-opt").forEach(x =>
    x.setAttribute("aria-pressed", x === b ? "true" : "false"));
  renderGraph();   // client-side re-filter — no refetch
});
$("graph-minw").onchange = () => {
  graphMinWeight = parseInt($("graph-minw").value, 10) || 1;
  renderGraph();
};
$("graph-search").addEventListener("input", scheduleGraphSearch);
$("graph-search").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { _hideGraphSearch(); e.stopPropagation(); }
});
document.addEventListener("click", (e) => {   // click-away closes the node-search dropdown
  if (!e.target.closest(".graph-search-wrap")) _hideGraphSearch();
});
wireSeg("model-seg", "model");
wireSeg("effort-seg", "effort");
$("app-start").onclick = startApp;
$("app-stop").onclick = stopApp;

$("multi-open").onclick = toggleMultiBox;   // F3: cross-project fan-out composer
$("new-project").onclick = openProjectModal;
$("empty-new").onclick = openProjectModal;  // first-run empty state
$("project-modal-close").onclick = closeProjectModal;
$("pm-cancel").onclick = closeProjectModal;
$("pm-submit").onclick = submitProjectModal;
$("project-modal").onclick = (e) => { if (e.target === $("project-modal")) closeProjectModal(); };
[["pm-name"], ["pm-source"], ["pm-import-name"], ["pm-ref"]].forEach(([id]) =>
  $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submitProjectModal(); } }));
$("q-pause").onclick = togglePause;
$("q-concurrency").onchange = setConcurrency;
// in-run control + feedback + dashboard
$("pause-btn").onclick = pauseResume;
$("steer-btn").onclick = steerRun;
$("dash-refresh").onclick = loadDashboard;
$("cmp-btn").onclick = compareRuns;
// cookie-session login gate (S8) + sidebar sign-out
$("login-form").addEventListener("submit", (e) => { e.preventDefault(); submitLogin(); });
$("signout-btn").onclick = signOut;
// feature wave: playbooks · schedules · A/B · palette · notifications · KB · graph · timeline
$("playbook-modal-close").onclick = closePlaybookModal;
$("pbm-cancel").onclick = closePlaybookModal;
$("pbm-start").onclick = startPlaybook;
$("playbook-modal").onclick = (e) => { if (e.target === $("playbook-modal")) closePlaybookModal(); };
$("sc-create").onclick = createSchedule;
$("sched-refresh").onclick = loadSchedules;
$("sc-prompt").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); createSchedule(); } });
// QoL: schedule recurrence mode toggle + cron enter-to-create
$("sc-mode").querySelectorAll(".seg-opt").forEach(b => b.onclick = () => setSchedMode(b.dataset.v));
$("sc-cron").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); createSchedule(); } });
// QoL: workspace-cleanup card (project Settings)
const _gcKeepInput = () => {   // blank input -> undefined -> the server's default
  const v = parseInt($("gc-keep").value, 10);
  return isFinite(v) && v >= 0 ? v : undefined;
};
$("gc-refresh").onclick = () => loadGcReport(_gcKeepInput());
$("gc-keep").addEventListener("change", () => loadGcReport(_gcKeepInput()));
$("gc-clean").onclick = runGcCleanup;
// QoL: memory-curation "Load more" paging
$("memc-more").onclick = () => loadMemoryCuration(false);
$("ab-run").onclick = runAB;
$("ab-values").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); runAB(); } });
$("palette-input").addEventListener("input", schedulePaletteSearch);
$("palette-input").addEventListener("keydown", paletteKeydown);
$("palette-modal").onclick = (e) => { if (e.target === $("palette-modal")) closePalette(); };
$("notif-bell").onclick = () => toggleNotifPanel();
$("notif-read-all").onclick = markAllNotifsRead;
document.addEventListener("click", (e) => {   // click-away closes the notification panel
  if ($("notif-panel").classList.contains("hidden")) return;
  if (!e.target.closest("#notif-panel") && !e.target.closest("#notif-bell")) toggleNotifPanel(false);
});
wireKbDrop();
$("graph-filter").addEventListener("input", applyGraphView);
$("tl-zoom").addEventListener("input", () => {
  tlZoom = parseFloat($("tl-zoom").value) || 1;
  applyTimelineZoom();
});
$("fb-accept").onclick = () => sendFeedback({ accepted: true });
$("fb-reject").onclick = () => sendFeedback({ accepted: false });
document.querySelectorAll("#fb-stars button").forEach(b => b.onclick = () => {
  _fbRating = parseInt(b.dataset.r);
  document.querySelectorAll("#fb-stars button").forEach(x => x.classList.toggle("on", parseInt(x.dataset.r) <= _fbRating));
  sendFeedback({});
});
function bootApp() {
  if (_appBooted) return;
  _appBooted = true;
  loadConfig();
  loadProjects().then(() => {
    // Home-first landing: the console's entry screen aggregates attention items,
    // live work, spend and workspaces. Opening/attaching a task switches to its
    // detail; projects are one sidebar click away.
    showHome();
    if (!currentProject) maybeStartTour();   // first-run tour (no projects yet)
  });
  loadQueue();
  setInterval(loadQueue, 4000);  // keep the queue panel + chip fresh
  setInterval(() => {            // keep sidebar dots, activity strips & header status live
    refreshProjectPulse(false);
    if (currentMainView === "activity") loadProjectsActivity();
  }, 5000);
  setInterval(() => {            // Home refreshes every ~15s while visible
    if (currentMainView === "home") loadHome();
  }, 15000);
}

// S8 boot gate: ask the (always-open) auth status endpoint whether a login is
// needed before the app touches any protected /api/* route.
(async function initAuth() {
  let status = null;
  try { status = await (await _fetch("/api/auth/status")).json(); }
  catch (e) { /* status unreachable — boot; real calls will surface the error */ }
  if (status && status.auth_required) $("signout-btn").classList.remove("hidden");
  if (authGate(status) === "login") showLogin();
  else bootApp();
})();
