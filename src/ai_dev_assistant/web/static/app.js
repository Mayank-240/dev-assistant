const $ = (id) => document.getElementById(id);

// Editorial colored-dot palette (no emoji) — one stable hue per specialist.
const AGENT_STYLE = {
  architect:        { color: "#7b86c9" },
  researcher:       { color: "#5a9bc4" },
  coder:            { color: "#3f9f7a" },
  test_engineer:    { color: "#8fae4f" },
  debugger:         { color: "#cf6a4f" },
  refactorer:       { color: "#9a7bd0" },
  security_auditor: { color: "#c2607f" },
  devops:           { color: "#5b93a6" },
  database:         { color: "#6f86c9" },
  frontend:         { color: "#c074a8" },
  performance:      { color: "#bf9540" },
  integrator:       { color: "#5fae84" },
  documenter:       { color: "#c79248" },
};
const agentStyle = (n) => AGENT_STYLE[n] || { color: "#8a8374" };
const PHASES = ["plan", "execute", "verify", "document", "done"];

const state = {
  agents: {},        // subtask id -> card element
  agentData: {},     // subtask id -> full per-agent record (for the detail popup)
  openAgentId: null, // id of the agent whose detail popup is open (for live updates)
  timer: null,
  startedAt: 0,
  docsId: null,      // run.id used for the docs folder
};

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

function fmtTok(n) { n = n || 0; return n < 1000 ? String(n) : (n / 1000).toFixed(1) + "k"; }

function setMetricsFrom(data) {
  if ("kg_nodes" in data) $("m-kg").textContent = data.kg_nodes;
  if ("kg_edges" in data) $("m-edges").textContent = data.kg_edges;
  if ("messages" in data) $("m-msgs").textContent = data.messages;
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
  state.timeline = {};
  state.agentData = {}; state.openAgentId = null;
  state.total = 0; state.reviewed = new Set();
  setPhase("plan");
  $("progress-bar").style.width = "0%"; $("progress-label").textContent = "";
  state.agents = {};
  state.docsId = null;
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
    <div class="ac-top"><span class="chip">${st.id}</span><span class="ac-agent"><span class="ac-dot" style="background:${agentStyle(st.agent).color}"></span>${st.agent}</span></div>
    <div class="ac-title">${escapeHtml(st.title)}</div>
    <div class="ac-deps">${deps}</div>
    <div class="ac-state"><span class="dot"></span><span class="ac-statetext">queued</span><span class="ac-score"></span></div>
    <div class="ac-stream"></div>
  `;
  card.title = "Click to see this agent's full activity";
  card.addEventListener("click", () => openAgentModal(st.id));
  return card;
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
  const total = state.total || 0;
  const done = state.reviewed ? state.reviewed.size : 0;
  $("progress-bar").style.width = (total ? Math.round((done / total) * 100) : 0) + "%";
  $("progress-label").textContent = total ? `${done}/${total} subtasks` : "";
}

// ---- plan DAG ----
function renderPlanDag(subtasks) {
  const svg = $("plan-dag");
  svg.innerHTML = "";
  if (!subtasks || !subtasks.length) { $("plan-dag-card").classList.add("hidden"); return; }
  $("plan-dag-card").classList.remove("hidden");

  const defs = document.createElementNS(SVGNS, "defs");
  defs.innerHTML = '<marker id="dag-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#3a4256"></path></marker>';
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
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 1000);
}

// ---- per-agent detail popup ----
function openAgentModal(id) {
  const a = state.agentData[id];
  if (!a) return;
  state.openAgentId = id;
  renderAgentModal(a);
  $("agent-modal").classList.remove("hidden");
}
function closeAgentModal() {
  $("agent-modal").classList.add("hidden");
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

  const dur = (a.start != null && a.end != null) ? (a.end - a.start).toFixed(1) + "s"
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
    stream.innerHTML = a.steps.map(s => {
      const cls = "as-" + (s.kind || "text");
      let txt;
      if (s.kind === "tool") txt = "→ " + (s.tool || "") + (s.input ? " " + s.input : "");
      else if (s.kind === "thinking") txt = "… " + (s.text || "");
      else txt = s.text || "";
      return `<div class="${cls}">${escapeHtml(txt)}</div>`;
    }).join("");
    stream.scrollTop = stream.scrollHeight;
  }
}

// ---- controls / toasts / timeline ----
function getControls() {
  const b = parseFloat($("budget").value);
  const t = $("task-title").value.trim();
  return {
    effort: $("effort").value, budget: (b && b > 0) ? b : null, title: t || null,
    project: selectedProject(), memory_scope: $("mem-scope").value,
  };
}

// ---- projects (scope memory + knowledge graph) ----
function selectedProject() { return $("project").value || "default"; }

async function loadProjects() {
  let list = [];
  try { list = await (await fetch("/api/projects")).json(); } catch (e) { list = []; }
  if (!list.length) list = [{ slug: "default", name: "Default" }];
  const sel = $("project");
  const saved = localStorage.getItem("ada-project") || sel.value || "default";
  sel.innerHTML = list.map(p => `<option value="${p.slug}">${escapeHtml(p.name)}</option>`).join("");
  sel.value = list.some(p => p.slug === saved) ? saved : list[0].slug;
}

async function createProject() {
  const name = (window.prompt("New project name:") || "").trim();
  if (!name) return;
  let created;
  try {
    created = await (await fetch("/api/projects", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
    })).json();
  } catch (e) { showToast("Could not create project", "error"); return; }
  if (created && created.slug) {
    await loadProjects();
    $("project").value = created.slug;
    localStorage.setItem("ada-project", created.slug);
    showToast("Project created · " + (created.name || created.slug), "success");
    if (currentView === "memory") loadMemory();
    if (currentView === "graph") loadGraph();
  }
}

function onProjectChange() {
  localStorage.setItem("ada-project", selectedProject());
  if (currentView === "memory") loadMemory();
  if (currentView === "graph") loadGraph();
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

  const sec = $("queue-section"), list = $("queue-list");
  if (!total) { sec.classList.add("hidden"); list.innerHTML = ""; return; }
  sec.classList.remove("hidden");
  list.innerHTML = "";

  running.forEach(r => {
    const li = document.createElement("li");
    li.className = "q-item q-running";
    li.innerHTML = `<span class="q-pos">●</span><span class="q-title">${escapeHtml(r.title || r.id)}</span>` +
      `<span class="q-acts"><button class="q-act q-cancel" title="Cancel run">Cancel</button></span>`;
    li.querySelector(".q-cancel").onclick = (e) => { e.stopPropagation(); cancelTask(r.id); };
    li.onclick = (e) => { if (!e.target.closest("button")) attachToRun(r.id, r.title); };
    list.appendChild(li);
  });

  pending.forEach((p, i) => {
    const li = document.createElement("li");
    li.className = "q-item";
    const upDis = i === 0 ? "disabled" : "";
    const downDis = i === pending.length - 1 ? "disabled" : "";
    li.innerHTML = `<span class="q-pos">${p.position}</span><span class="q-title">${escapeHtml(p.title || p.id)}</span>` +
      `<span class="q-acts">` +
      `<button class="q-act q-up" ${upDis} title="Move up">↑</button>` +
      `<button class="q-act q-down" ${downDis} title="Move down">↓</button>` +
      `<button class="q-act q-now" title="Run next">Run next</button>` +
      `<button class="q-act q-remove" title="Remove from queue">✕</button></span>`;
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
function renderTimeline() {
  const rows = Object.entries(state.timeline || {});
  if (!rows.length) return;
  const starts = rows.map(([, v]) => v.start), ends = rows.map(([, v]) => v.end || v.start);
  const t0 = Math.min(...starts), t1 = Math.max(...ends), span = Math.max(0.001, t1 - t0);
  const el = $("timeline"); el.innerHTML = "";
  rows.sort((a, b) => a[1].start - b[1].start).forEach(([id, v]) => {
    const st = agentStyle(v.agent);
    const left = ((v.start - t0) / span) * 100;
    const width = (((v.end || v.start) - v.start) / span) * 100;
    const dur = (v.end || v.start) - v.start;
    const row = document.createElement("div"); row.className = "tl-row";
    row.innerHTML = `<span class="tl-label"><span class="tl-dot" style="background:${st.color}"></span>${id} · ${v.agent}</span>` +
      `<div class="tl-track"><div class="tl-bar" style="left:${left}%;width:${Math.max(width, 1)}%;background:${st.color}"></div></div>` +
      `<span class="tl-dur">${dur.toFixed(1)}s</span>`;
    el.appendChild(row);
  });
}

function handleEvent(ev) {
  const d = ev.data || {};
  switch (ev.type) {
    case "status":
      if (d.backend) $("backend-badge").textContent = "backend: " + d.backend;
      if (ev.message && /Documenting/i.test(ev.message)) setPhase("document");
      feed(ev.message);
      break;
    case "queued":
      // task is waiting its turn in the queue
      $("status-pill").className = "pill";
      $("status-pill").textContent = d.position ? `queued · #${d.position}` : "queued";
      setConn("queued", "badge-idle");
      $("pipeline").classList.add("hidden");
      $("progress-row").classList.add("hidden");
      loadQueue();
      break;
    case "plan":
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
      $("agents-title").classList.remove("hidden");
      (d.subtasks || []).forEach(st => {
        $("agents").appendChild(makeAgentCard(st));
        state.agents[st.id] = true;
        state.agentData[st.id] = {
          id: st.id, agent: st.agent, title: st.title || "",
          depends_on: st.depends_on || [], status: "queued",
          score: null, passed: null, attempts: null, reasons: [],
          steps: [], result: null, start: null, end: null,
        };
      });
      $("m-agents").textContent = (d.subtasks || []).length;
      state.total = (d.subtasks || []).length;
      state.reviewed = new Set();
      renderPlanDag(d.subtasks || []);
      setPhase("execute");
      updateProgress();
      feed(ev.message);
      break;
    case "subtask_start": {
      setAgentState(d.id, "running", "running");
      setPlanNodeState(d.id, "running");
      if (!state.timeline[d.id]) state.timeline[d.id] = { agent: d.agent, start: ev.ts, end: ev.ts };
      const a = state.agentData[d.id];
      if (a) { a.status = "running"; a.start = ev.ts; }
      if (state.openAgentId === d.id) renderAgentModal(state.agentData[d.id]);
      feed("▶ " + ev.message);
      break;
    }
    case "agent_step": {
      const a = state.agentData[d.id];
      if (a) a.steps.push({ kind: d.kind || "text", tool: d.tool, input: d.input, text: d.text });
      if (state.openAgentId === d.id) renderAgentModal(state.agentData[d.id]);
      const card = $("agent-" + d.id);
      if (!card) break;
      const stream = card.querySelector(".ac-stream");
      if (!stream) break;
      const line = document.createElement("div");
      line.className = "as-" + (d.kind || "text");
      if (d.kind === "tool") line.textContent = "→ " + d.tool + (d.input ? " " + d.input : "");
      else if (d.kind === "thinking") line.textContent = "… " + (d.text || "");
      else line.textContent = d.text || "";
      stream.appendChild(line);
      while (stream.childElementCount > 6) stream.removeChild(stream.firstChild);
      stream.scrollTop = stream.scrollHeight;
      break;
    }
    case "subtask_review": {
      setAgentState(d.id, d.passed ? "passed" : "failed", d.passed ? "passed" : "failed", d.score);
      setPlanNodeState(d.id, d.passed ? "passed" : "failed");
      if (state.timeline[d.id]) state.timeline[d.id].end = ev.ts;
      if (state.reviewed) state.reviewed.add(d.id);
      const a = state.agentData[d.id];
      if (a) {
        a.status = d.passed ? "passed" : "failed";
        a.passed = d.passed; a.score = d.score;
        a.attempts = d.attempts ?? a.attempts;
        a.reasons = d.reasons || [];
        if (d.result) a.result = d.result;
        a.end = ev.ts;
      }
      if (a) { a.objective_note = d.objective_note || ""; a.cost = d.cost || null; }
      if (state.openAgentId === d.id) renderAgentModal(state.agentData[d.id]);
      updateProgress();
      setMetricsFrom(d);
      feed((d.passed ? "✓ " : "✗ ") + ev.message + (d.objective_note ? "  ·  " + d.objective_note : ""));
      break;
    }
    case "message":
      $("m-msgs").textContent = (parseInt($("m-msgs").textContent) || 0) + 1;
      feed("✉ " + d.sender + " → " + (d.recipient || "all") + ": " + d.content, "msg");
      break;
    case "sessions":
      $("m-sessions").textContent = d.created ?? 0;
      $("m-reaped").textContent = d.reaped ?? 0;
      feed(ev.message);
      break;
    case "budget":
      showToast("⚠ " + (ev.message || "Budget exceeded"), "warn", 7000);
      feed("⚠ " + ev.message);
      break;
    case "diff": {
      const added = (d.added || []).length, mod = (d.modified || []).length;
      feed(`✎ ${d.id}: +${added} file(s), ~${mod} changed`);
      const a = state.agentData[d.id];
      if (a) a.diff = { added: d.added || [], modified: d.modified || [] };
      break;
    }
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
      if (d.cost_usd !== undefined) $("m-cost").textContent = "$" + Number(d.cost_usd).toFixed(4);
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
      stopTimer();
      setConn("done", "badge-done");
      $("run-btn").disabled = false;
      loadRecent();
      loadQueue();
      if (currentView === "memory") loadMemory();
      if (currentView === "graph") loadGraph();
      if (currentView === "files") loadFiles();
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
      feed("✗ " + (ev.message || "error"));
      stopTimer();
      $("cancel-btn").classList.add("hidden");
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

function runTask() {
  const prompt = $("prompt").value.trim();
  if (!prompt) { $("prompt").focus(); return; }
  if ($("review-plan").checked) requestPlan(prompt);
  else launchRun({ prompt, ...getControls() }, prompt);
}

async function requestPlan(prompt) {
  switchView("run");
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
    const opts = names.map(n => `<option value="${n}"${n === st.agent ? " selected" : ""}>${n}</option>`).join("");
    row.innerHTML = `<span class="chip">${st.id}</span>` +
      `<input class="pe-title" value="${escapeAttr(st.title)}" />` +
      `<span class="pe-agent-wrap"><span class="pe-dot" style="background:${agentStyle(st.agent).color}"></span><select class="pe-agent">${opts}</select></span>` +
      `<button class="pe-remove" title="Remove step">✕</button>`;
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
  $("empty").classList.remove("hidden");
  $("run-btn").disabled = false;
}

async function launchRun(body, prompt) {
  switchView("run");
  $("run-btn").disabled = true;
  resetRunView(prompt);
  setConn("connecting…", "badge-idle");
  let taskId;
  try {
    taskId = (await (await fetch("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    })).json()).task_id;
  } catch (e) { feed("✗ could not start run: " + e); $("run-btn").disabled = false; return; }
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
  switchView("run");
  resetRunView(prompt || id);
  setConn("connecting…", "badge-idle");
  connectWS(id);
}

let _agentNames = null;
async function getAgentNames() {
  if (_agentNames) return _agentNames;
  try { _agentNames = (await (await fetch("/api/agents")).json()).map(a => a.name); }
  catch (e) { _agentNames = ["researcher", "coder", "documenter"]; }
  return _agentNames;
}

async function cancelRun() {
  if (!state.taskId) return;
  $("cancel-btn").disabled = true;
  feed("⛔ cancel requested…");
  showToast("Cancel requested…", "warn");
  try { await fetch(`/api/run/${state.taskId}/cancel`, { method: "POST" }); } catch (e) { /* ignore */ }
}

function escapeAttr(s) { return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;"); }

// ---- recent tasks + doc viewer ----
async function loadRecent() {
  try {
    const items = await (await fetch("/api/tasks")).json();
    const ul = $("recent-list");
    if (!items.length) { ul.innerHTML = '<li class="muted">none yet</li>'; return; }
    ul.innerHTML = "";
    items.forEach(it => {
      const li = document.createElement("li");
      li.className = "item";
      li.dataset.id = it.id;
      const status = it.status || "";
      const cost = (it.cost_usd != null && it.cost_usd > 0) ? "$" + Number(it.cost_usd).toFixed(4) : "";
      const tests = it.tests === "passed" ? '<span class="r-tests-pass">tests ✓</span>'
                  : it.tests === "failed" ? '<span class="r-tests-fail">tests ✗</span>' : "";
      const meta = `<div class="r-meta">${status ? `<span class="r-status ${status}"><span class="r-status-dot"></span>${status}</span>` : ""}` +
                   `${cost ? `<span class="r-cost">${cost}</span>` : ""}${tests}</div>`;
      const titleText = it.title || it.tldr || it.id;
      const cancelBtn = it.status === "running"
        ? `<button class="r-act r-cancel" title="Cancel this run">Cancel</button>` : "";
      li.innerHTML = `<div class="r-title">${escapeHtml(titleText)}</div>` +
        (it.tldr ? `<div class="r-tldr">${escapeHtml(it.tldr)}</div>` : "") +
        `${meta}<div class="r-actions"><span class="r-files">Files →</span>` +
        `${cancelBtn}<button class="r-act r-delete" title="Delete this task">Delete</button></div>`;
      li.onclick = () => { if (it.status === "running") attachToRun(it.id, it.prompt); else openTask(it.id, it); };
      li.querySelector(".r-files").onclick = (e) => {
        e.stopPropagation();
        switchView("files");
        loadFiles(it.id);
      };
      const cb = li.querySelector(".r-cancel");
      if (cb) cb.onclick = (e) => { e.stopPropagation(); cancelTask(it.id); };
      li.querySelector(".r-delete").onclick = (e) => { e.stopPropagation(); deleteTask(it.id, titleText); };
      ul.appendChild(li);
    });
  } catch (e) { /* ignore */ }
}

async function cancelTask(id) {
  try {
    await fetch(`/api/run/${id}/cancel`, { method: "POST" });
    showToast("Cancelling run…", "warn");
  } catch (e) { showToast("Cancel failed", "error"); }
  setTimeout(loadRecent, 700);
}

function deleteTask(id, label) {
  // Optimistic + deferred: hide the row now, actually delete after a grace window
  // unless the user clicks Undo. No backend restore needed — the DELETE never fires if undone.
  const row = document.querySelector(`#recent-list li[data-id="${CSS.escape(id)}"]`);
  if (row) row.style.display = "none";

  const wasOnScreen = (state.taskId === id || state.docsId === id);
  if (wasOnScreen) {
    state.taskId = null; state.docsId = null;
    $("run-view").classList.add("hidden");
    $("empty").classList.remove("hidden");
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
    if (row) row.style.display = "";
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
async function openDocs(id) {
  try {
    currentDocs = await (await fetch("/api/tasks/" + id)).json();
  } catch (e) { return; }
  $("modal").classList.remove("hidden");
  selectTab("brief");
}
function selectTab(which) {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.doc === which));
  $("modal-content").textContent = (currentDocs && currentDocs[which]) || "(empty)";
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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

async function openTask(id, meta) {
  switchView("run");
  let docs;
  try { docs = await (await fetch("/api/tasks/" + id)).json(); } catch (e) { docs = null; }
  if (!docs || !docs.plan) { openDocs(id); return; }   // older task without parseable docs
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
    state.agentData[st.id] = {
      id: st.id, agent: st.agent, title: st.title || "",
      depends_on: st.depends_on || [], status: "queued",
      score: null, passed: null, attempts: null, reasons: [],
      steps: (docs.activity && docs.activity[st.id]) || [],
      result: null, start: null, end: null,
    };
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
}

// ---- view tabs (Run / Agents / Memory / Graph) ----
let currentView = "run";
const VIEWS = { run: "view-run", agents: "view-agents", memory: "view-memory", graph: "view-graph", files: "view-files", dashboard: "view-dashboard" };

function switchView(name) {
  currentView = name;
  document.querySelectorAll(".vtab").forEach(b => b.classList.toggle("active", b.dataset.view === name));
  Object.entries(VIEWS).forEach(([n, id]) => $(id).classList.toggle("hidden", n !== name));
  if (name === "agents") loadAgents();
  if (name === "memory") loadMemory();
  if (name === "graph") loadGraph();
  if (name === "files") loadFiles();
  if (name === "dashboard") loadDashboard();
}

// ---- Dashboard (Tier 5) ----
async function loadDashboard() {
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
    row.innerHTML = `<span class="ds-agent-name">${n}</span>`
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
document.querySelectorAll(".vtab").forEach(b => b.onclick = () => switchView(b.dataset.view));

// ---- Agents roster ----
let agentsLoaded = false;
let rosterAgents = {};   // name -> agent profile, for the detail popup
async function loadAgents() {
  if (agentsLoaded) return;
  try {
    const agents = await (await fetch("/api/agents")).json();
    rosterAgents = {};
    const wrap = $("agents-roster");
    wrap.innerHTML = "";
    agents.forEach(a => {
      rosterAgents[a.name] = a;
      const card = document.createElement("div");
      card.className = "agent-card";
      card.title = "Click for full details";
      card.innerHTML = `
        <div class="ac-top"><span class="ac-dot" style="background:${agentStyle(a.name).color}"></span><span class="ac-agent ac-name">${a.name}</span><span class="chip chip-tools">${a.tools.length} tools</span></div>
        <div class="ac-desc">${escapeHtml(a.description)}</div>
        <div class="ac-when">${escapeHtml(a.when_to_use)}</div>
        <div class="tool-chips">${a.tools.map(t => `<span class="tool-chip">${t}</span>`).join("")}</div>`;
      card.addEventListener("click", () => openRosterModal(a.name));
      wrap.appendChild(card);
    });
    agentsLoaded = true;
  } catch (e) { /* ignore */ }
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
  $("roster-modal").classList.remove("hidden");
}
function closeRosterModal() { $("roster-modal").classList.add("hidden"); }

// ---- Memory ----
async function loadMemory() {
  try {
    const items = await (await fetch("/api/memory?project=" + encodeURIComponent(selectedProject()))).json();
    const wrap = $("memory-list");
    if (!items.length) { wrap.innerHTML = '<p class="muted">No memories yet — run a task.</p>'; return; }
    wrap.innerHTML = "";
    items.forEach(m => {
      const when = m.created_at ? new Date(m.created_at * 1000).toLocaleString() : "";
      const scope = m.mem_scope === "global" ? "global" : "project";
      const el = document.createElement("div");
      el.className = "mem-item";
      el.innerHTML = `
        <div class="mem-meta"><span class="mem-scope-tag mem-scope-${scope}">${scope}</span>
          <span class="mem-author">${escapeHtml(m.author || "agent")}</span>
          <span>· ${escapeHtml(m.subtask || m.scope)}</span><span>· ${when}</span></div>
        <div class="mem-content">${escapeHtml(m.content)}</div>`;
      wrap.appendChild(el);
    });
  } catch (e) { /* ignore */ }
}

// ---- Knowledge graph (dependency-free force layout) ----
const TYPE_COLOR = { task: "#2f7d5e", subtask: "#5a9bc4", agent: "#7b86c9", concept: "#c79248" };
const SVGNS = "http://www.w3.org/2000/svg";
let graphData = { nodes: [], edges: [] };

async function loadGraph() {
  try {
    graphData = await (await fetch("/api/graph?project=" + encodeURIComponent(selectedProject()))).json();
  } catch (e) { return; }
  renderGraph();
}

function renderGraph() {
  const svg = $("graph-svg");
  svg.innerHTML = "";
  const nodes = graphData.nodes.slice(0, 120);
  const ids = new Set(nodes.map(n => n.id));
  const edges = graphData.edges.filter(e => ids.has(e.source) && ids.has(e.target));
  $("graph-stats").textContent = `${graphData.nodes.length} nodes · ${graphData.edges.length} edges`;

  // promote nodes that an agent is assigned to / produced by into the "agent" type
  const agentNames = new Set(edges.filter(e => e.relation === "assigned_to" || e.relation === "produced_result_by").map(e => e.target));
  nodes.forEach(n => { if (agentNames.has(n.id)) n.type = "agent"; });

  if (!nodes.length) {
    const t = document.createElementNS(SVGNS, "text");
    t.setAttribute("x", 400); t.setAttribute("y", 260); t.setAttribute("text-anchor", "middle");
    t.setAttribute("class", "nlabel"); t.textContent = "No knowledge yet — run a task.";
    svg.appendChild(t); $("graph-legend").innerHTML = ""; return;
  }

  const W = 800, H = 520;
  const pos = layout(nodes, edges, W, H);
  const adj = {};
  edges.forEach(e => {
    (adj[e.source] = adj[e.source] || []).push(`→ [${e.relation}] ${e.target}`);
    (adj[e.target] = adj[e.target] || []).push(`← [${e.relation}] ${e.source}`);
  });

  edges.forEach(e => {
    const a = pos[e.source], b = pos[e.target];
    const line = document.createElementNS(SVGNS, "line");
    line.setAttribute("x1", a.x); line.setAttribute("y1", a.y);
    line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
    line.setAttribute("class", "edge");
    svg.appendChild(line);
  });

  nodes.forEach(n => {
    const p = pos[n.id];
    const r = n.type === "task" ? 13 : n.type === "subtask" ? 10 : 8;
    const c = document.createElementNS(SVGNS, "circle");
    c.setAttribute("cx", p.x); c.setAttribute("cy", p.y); c.setAttribute("r", r);
    c.setAttribute("fill", TYPE_COLOR[n.type] || TYPE_COLOR.concept);
    c.setAttribute("class", "node");
    c.addEventListener("click", () => showNode(n, adj[n.id] || []));
    svg.appendChild(c);
    const label = document.createElementNS(SVGNS, "text");
    label.setAttribute("x", p.x + r + 3); label.setAttribute("y", p.y + 3);
    label.setAttribute("class", "nlabel");
    label.textContent = n.id.length > 22 ? n.id.slice(0, 21) + "…" : n.id;
    svg.appendChild(label);
  });

  $("graph-legend").innerHTML = Object.entries(TYPE_COLOR)
    .map(([t, c]) => `<span><i style="background:${c}"></i>${t}</span>`).join("");
}

function showNode(n, facts) {
  const taskEdge = (graphData.edges || []).find(e => e.relation === "produced_file" && e.target === n.id);
  const extra = taskEdge
    ? `<div class="open-file" data-task="${escapeAttr(taskEdge.source)}" data-path="${escapeAttr(n.id)}">Open in Files →</div>`
    : "";
  $("node-detail").classList.remove("muted");
  $("node-detail").innerHTML = `<b>${escapeHtml(n.id)}</b> <span class="muted">(${n.type})</span><br>` +
    (facts.length ? facts.map(f => escapeHtml(f)).join("<br>") : "<span class='muted'>no relations</span>") + extra;
  const of = $("node-detail").querySelector(".open-file");
  if (of) of.onclick = async () => { switchView("files"); await loadFiles(of.dataset.task); selectFile(of.dataset.path, null); };
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
      const pa = pos[e.source], pb = pos[e.target];
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
    tasks.map(t => `<option value="${t.id}">${t.id}</option>`).join("");
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
    li.onclick = () => selectFile(it.path, li);
    ul.appendChild(li);
  });
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
  pre.textContent = "Loading…";
  const url = "/api/workspace/file?path=" + encodeURIComponent(path) + taskQ;
  try {
    const data = await (await fetch(url)).json();
    pre.textContent = (data.content || "(empty)") + (data.truncated ? "\n\n…[truncated]" : "");
  } catch (e) { pre.textContent = "Could not load file."; }
}
function fmtSize(n) { return n < 1024 ? n + " B" : (n / 1024).toFixed(1) + " KB"; }

// ---- wire up ----
$("run-btn").onclick = runTask;
document.querySelectorAll(".ex-chip").forEach(b => b.onclick = () => { $("prompt").value = b.textContent; $("prompt").focus(); });
$("approve-btn").onclick = approvePlan;
$("discard-btn").onclick = discardPlan;
$("refine-btn").onclick = refinePlan;
$("pe-instruction").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); refinePlan(); } });
$("cancel-btn").onclick = cancelRun;
$("mem-refresh").onclick = loadMemory;
$("graph-refresh").onclick = loadGraph;
$("files-refresh").onclick = () => loadFiles();
$("files-task").onchange = () => { filesTask = $("files-task").value; renderFileList(); };
$("refresh").onclick = loadRecent;
$("prompt").addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") runTask(); });
$("docs-link").onclick = (e) => { e.preventDefault(); if (state.docsId) openDocs(state.docsId); };
$("modal-close").onclick = () => $("modal").classList.add("hidden");
$("modal").onclick = (e) => { if (e.target === $("modal")) $("modal").classList.add("hidden"); };
$("agent-modal-close").onclick = closeAgentModal;
$("agent-modal").onclick = (e) => { if (e.target === $("agent-modal")) closeAgentModal(); };
$("roster-modal-close").onclick = closeRosterModal;
$("roster-modal").onclick = (e) => { if (e.target === $("roster-modal")) closeRosterModal(); };
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("roster-modal").classList.contains("hidden")) closeRosterModal();
  else if (!$("agent-modal").classList.contains("hidden")) closeAgentModal();
  else if (!$("modal").classList.contains("hidden")) $("modal").classList.add("hidden");
});
document.querySelectorAll(".tab").forEach(t => t.onclick = () => selectTab(t.dataset.doc));
$("project").onchange = onProjectChange;
$("new-project").onclick = createProject;
$("q-pause").onclick = togglePause;
$("q-concurrency").onchange = setConcurrency;
// in-run control + feedback + dashboard
$("pause-btn").onclick = pauseResume;
$("steer-btn").onclick = steerRun;
$("dash-refresh").onclick = loadDashboard;
$("fb-accept").onclick = () => sendFeedback({ accepted: true });
$("fb-reject").onclick = () => sendFeedback({ accepted: false });
document.querySelectorAll("#fb-stars button").forEach(b => b.onclick = () => {
  _fbRating = parseInt(b.dataset.r);
  document.querySelectorAll("#fb-stars button").forEach(x => x.classList.toggle("on", parseInt(x.dataset.r) <= _fbRating));
  sendFeedback({});
});
loadProjects();
loadRecent();
loadQueue();
setInterval(loadQueue, 4000);  // keep the queue panel + chip fresh
