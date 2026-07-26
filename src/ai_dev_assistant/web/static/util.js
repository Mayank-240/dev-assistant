/* Pure, dependency-free helpers shared by app.js and the Node test suite.
   Loaded before app.js in index.html (window.AdaUtil); require()-able from Node
   via the UMD-ish guard at the bottom (tests/js/util.test.mjs). No DOM access here. */
(function (global) {
  "use strict";

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function escapeAttr(s) { return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;"); }

  function fmtTok(n) { n = n || 0; return n < 1000 ? String(n) : (n / 1000).toFixed(1) + "k"; }

  function fmtSize(n) { return n < 1024 ? n + " B" : (n / 1024).toFixed(1) + " KB"; }

  function fmtCost(n) { return "$" + Number(n || 0).toFixed(4); }

  function fmtDuration(sec) {
    if (sec == null || !isFinite(sec) || sec < 0) return "—";
    if (sec < 60) return sec.toFixed(1) + "s";
    return Math.floor(sec / 60) + "m " + Math.floor(sec % 60) + "s";
  }

  // Classify one line of a unified `git diff` for display.
  // Order matters: "+++"/"---" are file headers, not additions/deletions.
  function classifyDiffLine(l) {
    l = String(l);
    if (l.startsWith("+++") || l.startsWith("---") || l.startsWith("diff --git")) return "file";
    if (l.startsWith("@@")) return "hunk";
    if (l.startsWith("+")) return "add";
    if (l.startsWith("-")) return "del";
    return "ctx";
  }

  // ===========================================================
  // Run-event aggregation — the pure "model" half of the run view.
  // app.js feeds every websocket event through reduceRunEvent and
  // only does DOM updates from the resulting state.
  // ===========================================================

  // Full per-agent record for one subtask (drives the card + detail popup).
  function makeAgentRecord(st) {
    st = st || {};
    return {
      id: st.id, agent: st.agent, title: st.title || "",
      depends_on: st.depends_on || [], status: "queued",
      score: null, passed: null, attempts: null, reasons: [],
      steps: [], result: null, start: null, end: null,
    };
  }

  // Fresh accumulator for one run's event stream.
  function initialRunAggregates() {
    return {
      total: 0,               // subtasks in the plan
      reviewed: new Set(),    // subtask ids that finished review
      agentData: {},          // subtask id -> record (makeAgentRecord)
      timeline: {},           // subtask id -> { agent, start, end }
      messages: 0,            // inter-agent messages seen
      costUsd: null,          // latest cumulative run cost reported
      phase: "plan",          // plan | execute | verify | document | done
      children: null,         // F3: fan-out children (makeChildRecord[]), null = not a fan-out
    };
  }

  // ===========================================================
  // F3 — cross-project fan-out: children of a parent task.
  // ===========================================================

  // One child task of a cross-project parent (drives a card in the children grid).
  function makeChildRecord(c) {
    c = c || {};
    return {
      slug: String(c.slug || ""), task_id: String(c.task_id || ""),
      status: c.status || "queued", quality: c.quality != null ? c.quality : null,
      cost_usd: c.cost_usd != null ? c.cost_usd : null, branch: String(c.branch || ""),
    };
  }

  // Find a child by task_id (preferred) or slug in the state's children array.
  function _childOf(state, d) {
    if (!state.children) return null;
    return state.children.find(c =>
      (d.task_id && c.task_id === d.task_id) || (d.slug && c.slug === d.slug)) || null;
  }

  // /api/tasks/{id}/children rows -> child records (for the historical parent view).
  function childrenFromRows(rows) {
    return (rows || []).map(r => makeChildRecord({
      slug: r.slug || r.project, task_id: r.id,
      status: r.status || "queued", quality: r.quality_score,
      cost_usd: r.cost_usd, branch: r.task_branch,
    }));
  }

  // Child records -> render models for the children grid. Text fields are plain —
  // the caller escapes before injecting into HTML.
  function childrenGridModel(children) {
    return (children || []).map(c => {
      const status = c.status || "queued";
      const bad = status === "failed" || status === "cancelled"
        || status === "over_budget" || status === "interrupted" || status === "error";
      const cls = status === "running" ? "running"
        : status === "completed" ? "passed"
        : status === "partial" ? "partial"
        : bad ? "failed" : "queued";
      const pill = status === "running" ? "pill-running"
        : status === "completed" ? "pill-done"
        : status === "partial" ? "pill-warn"
        : bad ? "pill-err" : "";
      return {
        slug: c.slug, taskId: c.task_id, status, cls, pill,
        statusLabel: status,
        qualityLabel: c.quality != null ? "quality " + c.quality + "/100" : "quality —",
        costLabel: c.cost_usd != null ? fmtCost(c.cost_usd) : "$—",
        branch: c.branch || "",
        open: status === "running" || status === "queued" ? "live" : "docs",
      };
    });
  }

  // Fold one websocket event into the aggregates (mutates + returns state).
  // Unknown event types are ignored, so it is safe to call for every event.
  function reduceRunEvent(state, ev) {
    if (!state || !ev) return state;
    const d = ev.data || {};
    switch (ev.type) {
      case "status":
        if (ev.message && /Documenting/i.test(ev.message)) state.phase = "document";
        break;
      case "plan": {
        if (d.children && d.children.length) {  // F3: fan-out parent plan
          state.children = d.children.map(makeChildRecord);
          state.total = state.children.length;
          state.reviewed = new Set();
          state.phase = "execute";
          break;
        }
        const subs = d.subtasks || [];
        state.total = subs.length;
        state.reviewed = new Set();
        subs.forEach(st => { state.agentData[st.id] = makeAgentRecord(st); });
        state.phase = "execute";
        break;
      }
      case "child_start": {
        if (!state.children) state.children = [];
        let c = _childOf(state, d);
        if (!c) { c = makeChildRecord(d); state.children.push(c); }
        c.status = "running";
        if (d.task_id) c.task_id = d.task_id;
        break;
      }
      case "child_done": {
        if (!state.children) state.children = [];
        let c = _childOf(state, d);
        if (!c) { c = makeChildRecord(d); state.children.push(c); }
        c.status = d.status || "completed";
        if (d.quality != null) c.quality = d.quality;
        if (d.cost_usd != null) c.cost_usd = d.cost_usd;
        if (d.branch) c.branch = d.branch;
        if (d.task_id) c.task_id = d.task_id;
        state.reviewed.add(c.task_id || c.slug);
        break;
      }
      case "subtask_start": {
        if (d.id == null) break;
        if (!state.timeline[d.id]) state.timeline[d.id] = { agent: d.agent, start: ev.ts, end: ev.ts };
        const a = state.agentData[d.id];
        if (a) { a.status = "running"; a.start = ev.ts; }
        break;
      }
      case "agent_step": {
        const a = state.agentData[d.id];
        if (a) a.steps.push({ kind: d.kind || "text", tool: d.tool, input: d.input, text: d.text });
        break;
      }
      case "subtask_review": {
        if (d.id == null) break;
        if (state.timeline[d.id]) state.timeline[d.id].end = ev.ts;
        state.reviewed.add(d.id);
        const a = state.agentData[d.id];
        if (a) {
          a.status = d.passed ? "passed" : "failed";
          a.passed = !!d.passed;
          a.score = d.score;
          a.attempts = d.attempts != null ? d.attempts : a.attempts;
          a.reasons = d.reasons || [];
          if (d.result) a.result = d.result;
          a.end = ev.ts;
          a.objective_note = d.objective_note || "";
          a.cost = d.cost || null;
        }
        if ("cost_usd" in d) state.costUsd = Number(d.cost_usd);
        break;
      }
      case "message":
        state.messages += 1;
        break;
      case "diff": {
        const a = state.agentData[d.id];
        if (a) a.diff = { added: d.added || [], modified: d.modified || [] };
        break;
      }
      case "execution":
        state.phase = "verify";
        break;
      case "done":
        state.phase = "done";
        if (d.cost_usd !== undefined) state.costUsd = Number(d.cost_usd);
        break;
    }
    return state;
  }

  // Progress readout for the run's progress bar.
  function runProgress(state) {
    const total = (state && state.total) || 0;
    const done = (state && state.reviewed) ? state.reviewed.size : 0;
    const unit = (state && state.children) ? "projects" : "subtasks";  // F3 fan-out
    return {
      done, total,
      pct: total ? Math.round((done / total) * 100) : 0,
      label: total ? done + "/" + total + " " + unit : "",
    };
  }

  // One activity-stream line for an agent step (card stream + detail popup).
  function formatStepLine(s) {
    s = s || {};
    if (s.kind === "tool") return "→ " + (s.tool || "") + (s.input ? " " + s.input : "");
    if (s.kind === "thinking") return "… " + (s.text || "");
    return s.text || "";
  }

  // ---- W5: cost-vs-budget meter model ----
  // No (or invalid) cap -> hidden. severity: "" | "warn" (>=80%) | "over" (>=100%).
  function computeBudgetMeter(cost, budget) {
    const cap = Number(budget || 0);
    if (!cap || !isFinite(cap) || cap <= 0) return { visible: false };
    const c = Number(cost || 0);
    const pct = Math.min(100, Math.round((c / cap) * 100));
    const severity = pct >= 100 ? "over" : pct >= 80 ? "warn" : "";
    return { visible: true, pct, severity, text: fmtCost(c) + " / $" + cap.toFixed(2) };
  }

  // ---- timeline rows: {id: {agent, start, end}} -> sorted render model ----
  function timelineRows(timeline) {
    const entries = Object.entries(timeline || {});
    if (!entries.length) return [];
    const starts = entries.map(([, v]) => v.start);
    const ends = entries.map(([, v]) => (v.end != null ? v.end : v.start));
    const t0 = Math.min(...starts), t1 = Math.max(...ends);
    const span = Math.max(0.001, t1 - t0);
    return entries
      .sort((a, b) => a[1].start - b[1].start)
      .map(([id, v]) => {
        const end = v.end != null ? v.end : v.start;
        const dur = end - v.start;
        return {
          id, agent: v.agent, dur,
          leftPct: ((v.start - t0) / span) * 100,
          widthPct: Math.max((dur / span) * 100, 1),
          durLabel: dur.toFixed(1) + "s",
        };
      });
  }

  // ---- W5: run-comparison table model (Dashboard) ----
  // Missing fields render as an em dash so the table never shows "undefined".
  function compareRowModel(a, b) {
    a = a || {}; b = b || {};
    const rows = [
      ["Status", r => (r.status || "—") + (r.run_status && r.run_status !== r.status ? " · " + r.run_status : "")],
      ["Quality", r => r.quality_score != null ? r.quality_score + "/100" : "—"],
      ["Subtasks passed", r => (r.subtasks_passed != null && r.subtasks_total != null)
        ? r.subtasks_passed + "/" + r.subtasks_total : "—"],
      ["Tests", r => r.tests || "—"],
      ["Cost", r => r.cost_usd != null ? fmtCost(r.cost_usd) : "—"],
      ["Tokens", r => (r.input_tokens != null || r.output_tokens != null)
        ? fmtTok(r.input_tokens || 0) + " in + " + fmtTok(r.output_tokens || 0) + " out" : "—"],
      ["Duration", r => fmtDuration(r.duration_s)],
      ["Sessions", r => r.sessions_spawned != null
        ? r.sessions_spawned + " spawned · " + (r.sessions_reaped || 0) + " reaped" : "—"],
    ];
    return rows.map(([label, f]) => ({ label, a: f(a), b: f(b) }));
  }

  // ---- W5: statuses a stopped run may be retried/resumed from (mirrors the server's gate) ----
  const RESUMABLE_STATUSES = new Set(["interrupted", "failed", "over_budget", "cancelled", "partial"]);
  function isResumable(status) { return RESUMABLE_STATUSES.has(status); }

  // ---- F1: project status line model (branch @ short-head · dirty · archived) ----
  // Input: GET /api/projects/{slug}/status body (or null). Output drives the compact
  // line under the project selector; text is plain (caller escapes before injecting).
  function projectStatusLine(st) {
    if (!st || (!st.branch && !st.head)) return { visible: false, text: "", dirty: false, archived: false };
    const head = String(st.head || "").slice(0, 7);
    const branch = st.branch || "(no branch)";
    return {
      visible: true,
      text: branch + (head ? " @ " + head : ""),
      dirty: !!st.dirty,
      archived: !!st.archived,
    };
  }

  // ---- F4: per-project activity strip model ----
  // Input: GET /api/projects/{slug}/activity body (or null).
  // state: "idle" | "queued" | "running"; text is the human summary line.
  function activityStripModel(act) {
    const running = (act && act.running) || [];
    const queued = (act && act.queued) || [];
    const state = running.length ? "running" : queued.length ? "queued" : "idle";
    let text = "idle";
    if (running.length) {
      const first = running[0].title || running[0].id || "task";
      text = "running · " + first + (running.length > 1 ? " (+" + (running.length - 1) + " more)" : "");
      if (queued.length) text += " · " + queued.length + " queued";
    } else if (queued.length) {
      text = queued.length + " queued";
    }
    return { state, text, running: running.length, queued: queued.length };
  }

  // ===========================================================
  // F4 — Reviews & Permissions panel + project home (pure models).
  // ===========================================================

  // Escaped, colored HTML for a unified diff (reuses classifyDiffLine).
  // Returns "" for empty input; the caller wraps it in a <pre>.
  function renderDiffHtml(text) {
    if (!text) return "";
    return String(text).split("\n").map(l => {
      const esc = escapeHtml(l);
      const kind = classifyDiffLine(l);
      return kind === "ctx" ? esc : `<span class="d-${kind}">${esc}</span>`;
    }).join("\n");
  }

  // One subtask row from GET /api/tasks/{id}/review -> render model for a review card.
  function reviewCardModel(s) {
    s = s || {};
    const v = (s.verdict && typeof s.verdict === "object") ? s.verdict : {};
    const passed = v.passed != null ? !!v.passed : null;
    const rawCriteria = Array.isArray(v.criteria) ? v.criteria : [];
    const criteria = rawCriteria.map(c => {
      if (typeof c === "string") return { name: c, met: null };
      c = c || {};
      const met = c.met != null ? !!c.met : (c.passed != null ? !!c.passed : null);
      return { name: String(c.name || c.criterion || ""), met };
    }).filter(c => c.name);
    const decision = s.decision === "accepted" || s.decision === "rejected" ? s.decision : null;
    return {
      id: String(s.id || ""), title: String(s.title || ""), agent: String(s.agent || ""),
      status: String(s.status || ""),
      badge: passed === true ? "passed" : passed === false ? "failed" : "pending",
      score: v.score != null ? v.score : null,
      reasons: Array.isArray(v.reasons) ? v.reasons : [],
      suggestions: Array.isArray(v.suggestions) ? v.suggestions : [],
      criteria,
      changed: Array.isArray(s.changed) ? s.changed : [],
      attempts: s.attempts != null ? s.attempts : null,
      decision,
      mergeShort: String(s.merge_commit || "").slice(0, 7),
      hasDiff: !!s.diff,
      canAccept: !!s.merge_commit && decision !== "accepted",
      canReject: decision !== "rejected",
    };
  }

  // GET /api/tasks/{id}/permissions body -> render model for the Permissions tab.
  function permissionsModel(p) {
    p = p || {};
    const policy = (p.policy && typeof p.policy === "object") ? p.policy : {};
    const policyRows = Object.keys(policy).sort().map(k => {
      const v = policy[k];
      return { key: k, value: (v && typeof v === "object") ? JSON.stringify(v) : String(v) };
    });
    const tba = (p.tools_by_agent && typeof p.tools_by_agent === "object") ? p.tools_by_agent : {};
    const agents = Object.keys(tba).sort().map(a => ({
      agent: a, tools: Array.isArray(tba[a]) ? tba[a] : [],
    }));
    const denied = (Array.isArray(p.denied) ? p.denied : []).map(d => ({
      ts: (d && d.ts != null) ? d.ts : null,
      agent: String((d && d.agent) || ""),
      tool: String((d && d.tool) || ""),
      outcome: String((d && d.outcome) || ""),
    }));
    return {
      policyRows, agents, denied, deniedEmpty: !denied.length,
      reviewTarget: String(p.review_target || ""), taskBranch: String(p.task_branch || ""),
    };
  }

  // Project policy dict -> editor field values (strings, ready for inputs).
  function policyFormModel(policy) {
    policy = policy || {};
    return {
      budget_usd: policy.budget_usd != null ? String(policy.budget_usd) : "",
      effort: String(policy.effort || ""),
      git_mode: String(policy.git_mode || ""),
      protected_paths: Array.isArray(policy.protected_paths)
        ? policy.protected_paths.join("\n") : "",
    };
  }

  // Editor field values -> {ok, errors, policy} for PATCH /api/projects/{slug}.
  // Blank fields are omitted (leave the stored value alone); protected_paths is
  // always sent so clearing the textarea clears the policy.
  function parsePolicyForm(f) {
    f = f || {};
    const errors = [];
    const policy = {};
    const b = String(f.budget_usd == null ? "" : f.budget_usd).trim();
    if (b) {
      const n = Number(b);
      if (!isFinite(n) || n < 0) errors.push("budget_usd must be a non-negative number");
      else policy.budget_usd = n;
    }
    const effort = String(f.effort || "").trim();
    if (effort) policy.effort = effort;
    const gm = String(f.git_mode || "").trim();
    if (gm && gm !== "merge" && gm !== "branch") {
      errors.push('git_mode must be "merge" or "branch"');
    } else if (gm) {
      policy.git_mode = gm;
    }
    policy.protected_paths = String(f.protected_paths || "")
      .split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
    return { ok: !errors.length, errors, policy };
  }

  // ===========================================================
  // Project-first restructure: pure models for the topbar identity
  // line, composer delivery control, recent-task scoping and the
  // Project Home header.
  // ===========================================================

  // Topbar identity: "<project name> · <branch> @ <shorthead>" (+ dirty/archived
  // flags). Falls back to the bare project name when there is no repo status.
  // Text fields are plain — the caller escapes before injecting.
  function projectTaglineModel(name, st) {
    const line = projectStatusLine(st);
    const n = String(name || "");
    return {
      name: n,
      branchText: line.visible ? line.text : "",
      text: line.visible ? n + " · ⎇ " + line.text : n,
      dirty: line.dirty,
      archived: line.archived,
    };
  }

  // Composer delivery control. A project WITH a repo (non-empty root) is governed
  // by its policy git_mode -> render the hint line; only the scratch default
  // (no repo) keeps the per-run git_finalize checkbox.
  function deliveryControlModel(entry) {
    entry = entry || {};
    if (!entry.root) return { control: "checkbox", gitMode: "", text: "" };
    const gitMode = (entry.policy && entry.policy.git_mode) || "branch";
    return {
      control: "hint",
      gitMode,
      text: "Delivery: ada/<task-id> branch for review — policy: " + gitMode,
    };
  }

  // Recent-tasks scoping: by default only the selected project's rows.
  // Rows without a project value (legacy servers / docs-dir fallback) are
  // never hidden — they cannot be scoped.
  function filterTasksByProject(items, slug, showAll) {
    const list = Array.isArray(items) ? items : [];
    if (showAll || !slug) return list.slice();
    return list.filter(it => (!it || it.project == null) ? true : it.project === slug);
  }

  // Project Home header: origin badge + root path, plus the purposeful
  // empty state for the scratch default (no repo bound).
  function projectHomeHeaderModel(entry) {
    entry = entry || {};
    const root = String(entry.root || "");
    return {
      name: String(entry.name || entry.slug || ""),
      origin: String(entry.origin || "greenfield"),
      root,
      scratch: !root,
      emptyText: !root
        ? "Scratch project — tasks run greenfield. Create or import a project to bind a repository."
        : "",
    };
  }

  // ===========================================================
  // Project-first IA: sidebar project rows, project tab state,
  // merged task-list rows and the composer heading/delivery model.
  // ===========================================================

  // Projects the UI renders. The backend's scratch "default" project still
  // exists server-side but is never shown or auto-selected in the UI.
  function visibleProjects(projects) {
    return (Array.isArray(projects) ? projects : [])
      .filter(p => p && p.slug && p.slug !== "default");
  }

  // Empty-state flag: no real (non-default) projects yet -> the main area
  // shows the first-run "Create your first project" hero instead of a project.
  function projectsEmptyState(projects) {
    return visibleProjects(projects).length === 0;
  }

  // /api/projects list + {slug: activity body} -> sidebar nav rows.
  // `multi` = { activity, hasRuns } for the "⋔ Across projects" pseudo-entry,
  // appended only when there is (or was) cross-project work to show.
  // The scratch "default" project is filtered out (visibleProjects).
  // Text fields are plain — the caller escapes before injecting.
  function sidebarProjectRows(projects, actBySlug, currentSlug, multi) {
    const rows = visibleProjects(projects).map(p => {
      const m = activityStripModel((actBySlug || {})[p.slug]);
      return {
        slug: String(p.slug || ""), name: String(p.name || p.slug || ""),
        state: m.state, archived: !!p.archived,
        current: p.slug === currentSlug, multi: false,
      };
    });
    const mm = multi || {};
    const multiModel = activityStripModel(mm.activity);
    if (multiModel.running || multiModel.queued || mm.hasRuns) {
      rows.push({
        slug: "multi", name: "⋔ Across projects", state: multiModel.state,
        archived: false, current: currentSlug === "multi", multi: true,
      });
    }
    return rows;
  }

  // Which tabs a project shows, with the active one resolved. The "multi"
  // pseudo-entry only has its cross-project task list.
  const _TAB_LABELS = {
    overview: "Overview", tasks: "Tasks", run: "Run", knowledge: "Knowledge", settings: "Settings",
  };
  function projectTabsModel(slug, active) {
    const all = slug === "multi"
      ? ["tasks"] : ["overview", "tasks", "run", "knowledge", "settings"];
    const act = all.includes(active) ? active : all[0];
    return all.map(t => ({ id: t, label: _TAB_LABELS[t], selected: t === act }));
  }

  // /api/projects/{slug}/runs (or /api/tasks rows) + /activity -> one merged
  // task list: queued first, then running, then history — deduped by id, with
  // the live status winning and stored metrics folded in.
  function projectTaskRows(runs, activity) {
    const act = activity || {};
    const rows = [];
    const idx = {};
    const push = (r) => { idx[r.id] = rows.length; rows.push(r); };
    (act.queued || []).forEach(q => push({
      id: q.id, title: q.title || q.id, status: "queued",
      position: q.position != null ? q.position : null,
      quality: null, cost: null, tests: null, run_status: null, live: true,
    }));
    (act.running || []).forEach(r => {
      if (r.id in idx) return;
      push({ id: r.id, title: r.title || r.id, status: "running", position: null,
             quality: null, cost: null, tests: null, run_status: null, live: true });
    });
    (runs || []).forEach(r => {
      if (r.id in idx) {  // live row wins on status; fold the stored metrics in
        const row = rows[idx[r.id]];
        if (row.quality == null && r.quality_score != null) row.quality = r.quality_score;
        if (row.cost == null && r.cost_usd != null) row.cost = r.cost_usd;
        if (!row.title || row.title === row.id) row.title = r.title || row.title;
        return;
      }
      const status = r.status || "";
      push({
        id: r.id, title: r.title || r.id, status, position: null,
        quality: r.quality_score != null ? r.quality_score : null,
        cost: r.cost_usd != null ? r.cost_usd : null,
        tests: r.tests || null, run_status: r.run_status || null,
        live: status === "running" || status === "queued",
      });
    });
    return rows;
  }

  // Composer heading + delivery control for the selected project entry.
  function composerModel(entry) {
    entry = entry || {};
    const name = entry.name || entry.slug || "this project";
    return {
      heading: "What should we work on in " + name + "?",
      delivery: deliveryControlModel(entry),
    };
  }

  // Quality scores (chronological) -> SVG polyline geometry for a sparkline.
  // Nulls are skipped; fewer than 2 points -> not drawable (show the number instead).
  function sparklinePoints(values, w, h, pad) {
    w = w || 120; h = h || 28; pad = pad == null ? 2 : pad;
    const vals = (values || []).filter(v => v != null).map(Number).filter(v => isFinite(v));
    if (vals.length < 2) {
      return { drawable: false, points: "", last: vals.length ? vals[0] : null,
               min: null, max: null };
    }
    const min = Math.min(...vals), max = Math.max(...vals);
    const span = max - min || 1;
    const step = (w - pad * 2) / (vals.length - 1);
    const points = vals.map((v, i) => {
      const x = pad + i * step;
      const y = h - pad - ((v - min) / span) * (h - pad * 2);
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    return { drawable: true, points, last: vals[vals.length - 1], min, max };
  }

  // ===========================================================
  // Combined knowledge across projects (read-only) — pure models
  // for the "Combine with…" chips, the query param, per-item
  // project tags and the combined-view banner.
  // ===========================================================

  // "Combine with…" chips: every OTHER visible, non-archived project.
  // Text fields are plain — the caller escapes before injecting.
  function combineChipsModel(projects, activeSlug, selected) {
    const sel = new Set(Array.isArray(selected) ? selected : []);
    return visibleProjects(projects)
      .filter(p => !p.archived && p.slug !== activeSlug)
      .map(p => ({
        slug: String(p.slug),
        name: String(p.name || p.slug),
        selected: sel.has(p.slug),
      }));
  }

  // Active project + selected chips -> the `projects=` query value (active
  // first, deduped). "" means no combination -> use the single-project fetch.
  function combinedProjectsParam(activeSlug, selected) {
    const out = [];
    const seen = new Set();
    [activeSlug].concat(Array.isArray(selected) ? selected : []).forEach(s => {
      if (s && !seen.has(s)) { seen.add(s); out.push(s); }
    });
    return out.length > 1 ? out.join(",") : "";
  }

  // Which project(s) a combined-view item came from. Reads the additive
  // "sources" (list) or "project" (string) response fields; tolerates plain
  // single-project items (returns []).
  function itemProjects(item) {
    if (!item) return [];
    if (Array.isArray(item.sources) && item.sources.length) return item.sources.map(String);
    if (item.project) return [String(item.project)];
    return [];
  }

  // Memory hits / graph nodes -> rows with a per-item project tag. Tags only
  // exist in combined mode; tag text is plain (caller escapes).
  function taggedItemRows(items, combined) {
    return (Array.isArray(items) ? items : []).map(it => {
      const projects = combined ? itemProjects(it) : [];
      return { item: it, projects, tag: projects.join(" · ") };
    });
  }

  // Combined-view banner: visible only when actually spanning 2+ projects.
  function combinedBannerModel(projectCount) {
    const n = Number(projectCount) || 0;
    if (n < 2) return { visible: false, text: "" };
    return { visible: true, text: "Combined view — read-only across " + n + " projects" };
  }

  // Stable per-project accent color for combined graph nodes (cycled palette).
  const PROJECT_COLORS = ["#C96442", "#5a9bc4", "#3f9f7a", "#9a7bd0",
                          "#bf9540", "#c2607f", "#5b93a6", "#8fae4f"];
  function projectColorMap(slugs) {
    const map = {};
    (Array.isArray(slugs) ? slugs : []).forEach(s => {
      if (s && !(s in map)) {
        map[s] = PROJECT_COLORS[Object.keys(map).length % PROJECT_COLORS.length];
      }
    });
    return map;
  }

  // ===========================================================
  // Markdown preview renderer (Files view) — security-first.
  // The ENTIRE input is HTML-escaped first (escapeHtml); every transform
  // below operates on that escaped text, so raw input HTML can never
  // survive into the output. Unsupported syntax degrades to escaped
  // plain text. No external libraries.
  // ===========================================================

  const MD_MAX_CHARS = 500 * 1024;   // cap for synchronous rendering
  const MD_TRUNC_NOTICE = '<p class="md-truncated">(truncated for preview)</p>';

  // href here is already-escaped text. Strip whitespace/control chars that
  // browsers ignore when sniffing the scheme, then allow only http(s),
  // mailto and in-page fragments. Everything else stays plain text.
  function _mdSafeHref(href) {
    const probe = String(href).replace(/[\s\u0000-\u001f]+/g, "");
    return /^(?:https?:\/\/|mailto:|#)/i.test(probe);
  }

  // Inline transforms on ESCAPED text: code spans and links are stashed
  // behind \u0000<idx>\u0000 placeholders (the input had all \u0000 stripped,
  // so placeholders are always ours) so bold/italic can't chew into them.
  function _mdInline(s, stash) {
    const put = (html) => { stash.push(html); return "\u0000" + (stash.length - 1) + "\u0000"; };
    s = s.replace(/`([^`\n]+)`/g, (m, code) => put("<code>" + code + "</code>"));
    s = s.replace(/\[([^\]\n]*)\]\(([^()\s]+)\)/g, (m, text, href) =>
      _mdSafeHref(href)
        ? put('<a href="' + href + '" target="_blank" rel="noopener">') + text + put("</a>")
        : m);
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
    return s;
  }

  // One list item's escaped text -> { cls, html } (task checkboxes supported).
  function _mdListItem(text, inline) {
    const task = text.match(/^\[( |x|X)\]\s+(.*)$/);
    if (!task) return { cls: "", html: inline(text) };
    const checked = task[1].toLowerCase() === "x" ? " checked" : "";
    return {
      cls: ' class="md-task"',
      html: '<input type="checkbox" disabled' + checked + "> " + inline(task[2]),
    };
  }

  // Consecutive list lines -> <ul>/<ol> with ONE nesting level (indent >= 2).
  function _mdListHtml(items, inline) {
    const base = items[0].indent;
    const top = [];
    items.forEach(it => {
      if (it.indent >= base + 2 && top.length) top[top.length - 1].children.push(it);
      else top.push({ indent: it.indent, ordered: it.ordered, text: it.text, children: [] });
    });
    const open = (o) => (o ? "<ol>" : "<ul>");
    const close = (o) => (o ? "</ol>" : "</ul>");
    const li = (it) => { const r = _mdListItem(it.text, inline); return "<li" + r.cls + ">" + r.html; };
    let html = open(top[0].ordered);
    top.forEach(t => {
      html += li(t);
      if (t.children.length) {
        const co = t.children[0].ordered;
        html += open(co) + t.children.map(c => li(c) + "</li>").join("") + close(co);
      }
      html += "</li>";
    });
    return html + close(top[0].ordered);
  }

  const _MD_LIST_RE = /^(\s*)([-*]|\d+\.)\s+(.*)$/;
  const _MD_HR_RE = /^ {0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/;

  // Does this escaped line begin a non-paragraph block? (terminates paragraphs)
  function _mdBlockStart(l) {
    return /^#{1,6}\s/.test(l) || /^```/.test(l) || /^&gt;/.test(l)
      || _MD_LIST_RE.test(l) || _MD_HR_RE.test(l);
  }

  // Markdown text -> safe HTML for the Files-view preview. Pure; Node-testable.
  function renderMarkdown(text) {
    let src = String(text == null ? "" : text)
      .replace(/\u0000/g, "")            // reserve \u0000 for our placeholders
      .replace(/\r\n?/g, "\n");
    let truncated = false;
    if (src.length > MD_MAX_CHARS) {
      const cut = src.lastIndexOf("\n", MD_MAX_CHARS);
      src = src.slice(0, cut > 0 ? cut : MD_MAX_CHARS);
      truncated = true;
    }
    const lines = escapeHtml(src).split("\n");   // <-- security boundary
    const n = lines.length;
    const out = [];
    const stash = [];
    const inline = (s) => _mdInline(s, stash);
    let i = 0;
    while (i < n) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      // fenced code block (``` with optional language)
      let m = line.match(/^```([A-Za-z0-9_+-]*)\s*$/);
      if (m) {
        const cls = m[1] ? ' class="lang-' + m[1] + '"' : "";
        const buf = [];
        i++;
        while (i < n && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++;                                       // closing fence (or EOF)
        out.push("<pre><code" + cls + ">" + buf.join("\n") + "</code></pre>");
        continue;
      }
      // heading
      m = line.match(/^(#{1,6})\s+(.*)$/);
      if (m) {
        const lvl = m[1].length;
        out.push("<h" + lvl + ">" + inline(m[2].replace(/\s+#+\s*$/, "")) + "</h" + lvl + ">");
        i++; continue;
      }
      // horizontal rule
      if (_MD_HR_RE.test(line)) { out.push("<hr>"); i++; continue; }
      // blockquote (escaped ">" is "&gt;")
      if (/^&gt;/.test(line)) {
        const buf = [];
        while (i < n && /^&gt;/.test(lines[i])) { buf.push(lines[i].replace(/^&gt;\s?/, "")); i++; }
        out.push("<blockquote><p>" + buf.map(inline).join("<br>") + "</p></blockquote>");
        continue;
      }
      // GFM table: header row with pipes + a |---|---| separator line
      if (line.includes("|") && i + 1 < n) {
        const sep = lines[i + 1];
        if (/^[|\s:-]+$/.test(sep) && sep.includes("-") && sep.includes("|")) {
          const cells = (s) => {
            let t = s.trim();
            if (t.startsWith("|")) t = t.slice(1);
            if (t.endsWith("|")) t = t.slice(0, -1);
            return t.split("|").map(c => inline(c.trim()));
          };
          const head = cells(line);
          i += 2;
          const rows = [];
          while (i < n && lines[i].includes("|") && lines[i].trim()) { rows.push(cells(lines[i])); i++; }
          out.push("<table><thead><tr>"
            + head.map(c => "<th>" + c + "</th>").join("")
            + "</tr></thead><tbody>"
            + rows.map(r => "<tr>" + r.map(c => "<td>" + c + "</td>").join("") + "</tr>").join("")
            + "</tbody></table>");
          continue;
        }
      }
      // unordered / ordered list (one nesting level)
      if (_MD_LIST_RE.test(line)) {
        const items = [];
        while (i < n) {
          const lm = lines[i].match(_MD_LIST_RE);
          if (!lm) break;
          items.push({ indent: lm[1].length, ordered: /^\d/.test(lm[2]), text: lm[3] });
          i++;
        }
        out.push(_mdListHtml(items, inline));
        continue;
      }
      // paragraph — consume until a blank line or the start of another block
      const buf = [line];
      i++;
      while (i < n && lines[i].trim() && !_mdBlockStart(lines[i])) { buf.push(lines[i]); i++; }
      out.push("<p>" + inline(buf.join("\n")) + "</p>");
    }
    let html = out.join("\n").replace(/\u0000(\d+)\u0000/g, (m, k) => stash[+k] || "");
    if (truncated) html += "\n" + MD_TRUNC_NOTICE;
    return html;
  }

  const AdaUtil = {
    escapeHtml, escapeAttr, fmtTok, fmtSize, fmtCost, fmtDuration, classifyDiffLine,
    makeAgentRecord, initialRunAggregates, reduceRunEvent, runProgress, formatStepLine,
    computeBudgetMeter, timelineRows, compareRowModel, RESUMABLE_STATUSES, isResumable,
    projectStatusLine, activityStripModel,
    projectTaglineModel, deliveryControlModel, filterTasksByProject, projectHomeHeaderModel,
    renderDiffHtml, reviewCardModel, permissionsModel, policyFormModel, parsePolicyForm,
    sparklinePoints,
    makeChildRecord, childrenFromRows, childrenGridModel,
    sidebarProjectRows, projectTabsModel, projectTaskRows, composerModel,
    visibleProjects, projectsEmptyState,
    combineChipsModel, combinedProjectsParam, itemProjects, taggedItemRows,
    combinedBannerModel, projectColorMap,
    renderMarkdown,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = AdaUtil;
  if (global) global.AdaUtil = AdaUtil;
})(typeof window !== "undefined" ? window : globalThis);
