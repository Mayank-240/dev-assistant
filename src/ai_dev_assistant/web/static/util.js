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
      case "plan_update": {  // adaptive replan added a subtask mid-run (e.g. a repair)
        if (d.id == null || state.agentData[d.id]) break;
        state.agentData[d.id] = makeAgentRecord(d);
        state.total += 1;
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
        if (a) a.steps.push({ kind: d.kind || "text", tool: d.tool, input: d.input,
                              text: d.text, is_error: !!d.is_error });
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
      case "subtask_retry": {  // failed review being re-run — it is no longer finished
        if (d.id == null) break;
        state.reviewed.delete(d.id);
        const a = state.agentData[d.id];
        if (a) a.status = "running";
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
    if (s.kind === "tool_result") return "↳ " + (s.tool ? s.tool + ": " : "") + (s.text || "");
    if (s.kind === "thinking") return "… " + (s.text || "");
    return s.text || "";
  }

  // Display clip for compact stream lines (steps are persisted unclipped-ish at ~4k chars;
  // the card ticker and modal activity list only show the head — the full-transcript
  // panel shows everything).
  function clipText(s, n) {
    s = String(s == null ? "" : s);
    n = n || 300;
    return s.length <= n ? s : s.slice(0, n) + "…";
  }

  // ===========================================================
  // Full per-subtask transcript panel (GET /api/runs/{task}/transcript/{subtask}).
  // ===========================================================

  // One transcript step -> render model {cls, prefix, text}. Text is plain — the
  // caller escapes before injecting into HTML.
  function transcriptLineModel(s) {
    s = s || {};
    const kind = s.kind || "text";
    if (kind === "tool")
      return { cls: "tl-tool", prefix: "▸ ", text: (s.tool || "?") + (s.input ? " " + s.input : "") };
    if (kind === "tool_result")
      return { cls: "tl-tool-result" + (s.is_error ? " tl-err" : ""), prefix: "↳ ",
               text: (s.tool ? s.tool + " → " : "") + (s.text || "") };
    if (kind === "thinking") return { cls: "tl-thinking", prefix: "", text: s.text || "" };
    if (kind === "result") return { cls: "tl-final", prefix: "", text: s.text || "" };
    return { cls: "tl-text", prefix: "", text: s.text || "" };
  }

  // Endpoint payload {agent, count, steps} -> view model for the panel.
  function transcriptViewModel(data) {
    data = data || {};
    const steps = Array.isArray(data.steps) ? data.steps : [];
    const count = data.count != null ? Number(data.count) : steps.length;
    return {
      agent: data.agent || "",
      count,
      countLabel: count + (count === 1 ? " step" : " steps"),
      lines: steps.map(transcriptLineModel),
    };
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
    const decision = s.decision === "accepted" || s.decision === "rejected"
      || s.decision === "rolled_back" ? s.decision : null;
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
      decisionLabel: decision ? decision.replace(/_/g, " ") : "",
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
  // Global settings console — pure models for GET /api/settings.
  // app.js only does DOM work from these; both are Node-tested.
  // ===========================================================

  // One field from GET /api/settings -> render model for its row + control.
  // type: "bool|int|float|str|choice" -> control: checkbox|number|text|select.
  // Text fields are plain — the caller escapes before injecting into HTML.
  function fieldControlModel(f) {
    f = f || {};
    const type = String(f.type || "str");
    const control = type === "bool" ? "checkbox"
      : type === "choice" ? "select"
      : (type === "int" || type === "float") ? "number" : "text";
    const source = f.source === "override" || f.source === "env" ? f.source : "default";
    return {
      key: String(f.key || ""),
      label: String(f.label || f.key || ""),
      help: String(f.help || ""),
      type, control,
      choices: Array.isArray(f.choices) ? f.choices.map(String) : [],
      value: f.value != null ? f.value : "",
      checked: type === "bool" ? !!f.value : false,
      step: type === "int" ? "1" : type === "float" ? "any" : "",
      source,
      sourceLabel: source === "env" ? ".env" : source,
      sourceCls: "gs-src-" + source,
      showReset: source === "override",
      restart: !!f.restart_required,
    };
  }

  // GET /api/settings body -> { info: [{label, value}], groups: [{name, fields}] }.
  // Tolerates missing/partial payloads (renders empty rather than throwing).
  function settingsGroupsModel(payload) {
    payload = payload || {};
    const info = (payload.info && typeof payload.info === "object") ? payload.info : {};
    const infoRows = [
      { label: "backend", value: String(info.llm_backend || "—") },
      { label: "data dir", value: String(info.data_dir || "—") },
      { label: "projects", value: info.projects != null ? String(info.projects) : "—" },
    ];
    const groups = (Array.isArray(payload.groups) ? payload.groups : []).map(g => ({
      name: String((g && g.name) || ""),
      fields: (g && Array.isArray(g.fields) ? g.fields : []).map(fieldControlModel),
    }));
    return { info: infoRows, groups };
  }

  // Raw control input -> { ok, value } or { ok:false, error } for PATCH.
  // Mirrors the server's coercion so obvious mistakes fail before the request.
  function coerceFieldInput(field, raw) {
    field = field || {};
    const type = String(field.type || "str");
    const label = field.label || field.key || "value";
    if (type === "bool") return { ok: true, value: !!raw };
    const s = String(raw == null ? "" : raw).trim();
    if (type === "int") {
      if (!/^-?\d+$/.test(s)) return { ok: false, error: label + " must be a whole number" };
      return { ok: true, value: parseInt(s, 10) };
    }
    if (type === "float") {
      const n = Number(s);
      if (!s || !isFinite(n)) return { ok: false, error: label + " must be a number" };
      return { ok: true, value: n };
    }
    if (type === "choice") {
      const choices = Array.isArray(field.choices) ? field.choices : [];
      if (!choices.includes(s)) {
        return { ok: false, error: label + " must be one of: " + choices.join(", ") };
      }
      return { ok: true, value: s };
    }
    return { ok: true, value: s };
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

  // ===========================================================
  // Feature wave — pure models: command palette, schedules, spend
  // analytics, A/B replay table, notification center, playbook
  // forms and the first-run tour. All Node-tested; no DOM access.
  // ===========================================================

  // Relative time: seconds-epoch -> "in 2h 10m" / "3m ago" / "due now" / "just now".
  function fmtRelTime(ts, now) {
    if (ts == null || !isFinite(Number(ts))) return "—";
    const diff = Number(ts) - (now != null ? Number(now) : Date.now() / 1000);
    const abs = Math.abs(diff);
    const unit = abs < 60 ? null
      : abs < 3600 ? Math.floor(abs / 60) + "m"
      : abs < 86400 ? Math.floor(abs / 3600) + "h" + (Math.floor((abs % 3600) / 60) ? " " + Math.floor((abs % 3600) / 60) + "m" : "")
      : Math.floor(abs / 86400) + "d";
    if (diff >= 0) return unit ? "in " + unit : "due now";
    return unit ? unit + " ago" : "just now";
  }

  // ---- Command palette: static commands + /api/search hits -> grouped model ----
  const _PALETTE_KIND_LABEL = {
    command: "Commands", task: "Tasks", memory: "Memories",
    kb: "Knowledge base", file: "Files",
  };
  // query: raw input; hits: /api/search rows; projects: /api/projects list.
  // Returns { groups: [{kind,label,items}], flat, empty }. Text is plain —
  // the caller escapes before injecting. `flat` drives arrow-key navigation.
  function paletteResultsModel(query, hits, projects) {
    const q = String(query || "").trim().toLowerCase();
    const commands = [];
    if (q) {
      visibleProjects(projects).forEach(p => {
        const name = String(p.name || p.slug);
        if (name.toLowerCase().includes(q) || String(p.slug).toLowerCase().includes(q)
            || "go to project".includes(q)) {
          commands.push({ type: "command", action: "project", slug: String(p.slug),
                          label: "Go to project " + name });
        }
      });
      [["new-project", "New project"], ["settings", "Settings"],
       ["activity", "All activity"]].forEach(([action, label]) => {
        if (label.toLowerCase().includes(q)) commands.push({ type: "command", action, label });
      });
    }
    const byKind = {};
    (Array.isArray(hits) ? hits : []).forEach(h => {
      if (!h || !h.kind) return;
      (byKind[h.kind] = byKind[h.kind] || []).push({
        type: String(h.kind), project: String(h.project || ""),
        title: String(h.title || ""), snippet: String(h.snippet || ""),
        semantic: !!h.semantic,   // vector-similarity hit -> subtle "≈" badge
        ref: (h.ref && typeof h.ref === "object") ? h.ref : {},
      });
    });
    const groups = [];
    if (commands.length) groups.push({ kind: "command", label: _PALETTE_KIND_LABEL.command, items: commands });
    ["task", "memory", "kb", "file"].concat(Object.keys(byKind)).forEach(k => {
      if (byKind[k]) {
        groups.push({ kind: k, label: _PALETTE_KIND_LABEL[k] || k, items: byKind[k] });
        delete byKind[k];
      }
    });
    const flat = [];
    groups.forEach(g => g.items.forEach(it => flat.push(it)));
    return { groups, flat, empty: !flat.length };
  }

  // ---- Schedules: one /api/schedules row -> render model ----
  function _fmtEvery(hours) {
    const h = Number(hours) || 0;
    if (h >= 1) {
      const s = h % 1 === 0 ? String(h) : h.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
      return "every " + s + "h";
    }
    return "every " + Math.round(h * 60) + "m";
  }
  function scheduleRowModel(row, now) {
    row = row || {};
    const enabled = !!row.enabled;
    const title = String(row.title || "").trim() || String(row.prompt || "").slice(0, 80);
    const cron = String(row.cron || "").trim();   // cron rows render their expression
    return {
      id: String(row.id || ""),
      title,
      prompt: String(row.prompt || ""),
      enabled,
      cron,
      everyLabel: cron ? "cron " + cron : _fmtEvery(row.every_hours),
      nextLabel: !enabled ? "paused"
        : row.next_run_at == null ? "due now"
        : "next " + fmtRelTime(row.next_run_at, now),
      lastTaskId: String(row.last_task_id || ""),
      budgetLabel: Number(row.budget_usd) > 0 ? "$" + Number(row.budget_usd).toFixed(2) : "",
    };
  }

  // Create-form values -> { ok, errors, body } for POST /api/schedules.
  // Mirrors the server's gates (prompt required, every_hours >= 0.25). With
  // mode "cron" the body carries {cron} INSTEAD of {every_hours}; the cron
  // check here is a shallow shape gate (5 fields) — the server's parser is the
  // real validator and its 400 message is surfaced inline by the caller.
  const CRON_HINT = "m h dom mon dow — e.g. 0 9 * * 1-5";
  function scheduleFormModel(f) {
    f = f || {};
    const errors = [];
    const prompt = String(f.prompt || "").trim();
    if (!prompt) errors.push("prompt is required");
    const mode = f.mode === "cron" ? "cron" : "hours";
    let recurrence;
    if (mode === "cron") {
      const cron = String(f.cron || "").trim().replace(/\s+/g, " ");
      if (!cron) errors.push("cron expression is required (" + CRON_HINT + ")");
      else if (cron.split(" ").length !== 5)
        errors.push("cron needs exactly 5 fields (" + CRON_HINT + ")");
      recurrence = { cron };
    } else {
      const every = Number(String(f.every_hours == null ? "" : f.every_hours).trim());
      if (!isFinite(every) || every < 0.25) errors.push("every_hours must be a number ≥ 0.25 (15 minutes)");
      recurrence = { every_hours: every };
    }
    let budget = 0;
    const rawB = String(f.budget_usd == null ? "" : f.budget_usd).trim();
    if (rawB) {
      budget = Number(rawB);
      if (!isFinite(budget) || budget < 0) errors.push("budget must be a non-negative number");
    }
    return {
      ok: !errors.length, errors, mode,
      body: {
        prompt, title: String(f.title || "").trim() || null,
        ...recurrence, budget_usd: budget || 0,
      },
    };
  }

  // ---- Spend analytics: /api/analytics/overview body -> dashboard model ----
  function spendOverviewModel(ov) {
    ov = ov || {};
    const byDay = Array.isArray(ov.by_day) ? ov.by_day : [];
    const max = Math.max(0, ...byDay.map(d => Number(d && d.usd) || 0));
    return {
      windowDays: Number(ov.window_days) || 30,
      totalLabel: "$" + Number(ov.total_usd || 0).toFixed(2),
      runs: Number(ov.runs) || 0,
      tokensLabel: fmtTok(Number(ov.tokens_in) || 0) + " in + " + fmtTok(Number(ov.tokens_out) || 0) + " out",
      bars: byDay.map(d => {
        const usd = Number(d && d.usd) || 0;
        return {
          date: String((d && d.date) || ""),
          usd,
          hPct: max > 0 ? Math.max(usd > 0 ? 4 : 0, Math.round((usd / max) * 100)) : 0,
          label: (d && d.date) + " · $" + usd.toFixed(2) + " · " + ((d && d.runs) || 0)
            + " run" + ((d && d.runs) === 1 ? "" : "s"),
        };
      }),
      projects: (Array.isArray(ov.by_project) ? ov.by_project : []).map(p => ({
        project: String((p && p.project) || ""),
        usdLabel: "$" + Number((p && p.usd) || 0).toFixed(2),
        runs: Number(p && p.runs) || 0,
        qualityLabel: (p && p.avg_quality != null) ? String(p.avg_quality) : "—",
      })),
    };
  }

  // /api/analytics/outcomes body -> labeled ratio rows (null -> em dash).
  function spendOutcomesModel(oc) {
    oc = oc || {};
    const r = (v) => v != null ? "$" + Number(v).toFixed(4) : "—";
    return {
      rows: [
        { label: "per completed run", value: r(oc.usd_per_completed_run),
          count: Number(oc.completed_runs) || 0 },
        { label: "per passed subtask", value: r(oc.usd_per_passed_subtask),
          count: Number(oc.passed_subtasks) || 0 },
        { label: "per accepted subtask", value: r(oc.usd_per_accepted_subtask),
          count: Number(oc.accepted_subtasks) || 0 },
      ],
      totalLabel: "$" + Number(oc.total_usd || 0).toFixed(2),
    };
  }

  // /api/analytics/run/{id} body -> per-subtask cost table model.
  function runCostModel(data) {
    data = data || {};
    const subs = Array.isArray(data.subtasks) ? data.subtasks : [];
    const totals = data.totals || {};
    const bySubtask = {};
    subs.forEach(s => { if (s && s.subtask) bySubtask[s.subtask] = Number(s.usd) || 0; });
    return {
      empty: !subs.length,
      rows: subs.map(s => ({
        subtask: String((s && s.subtask) || ""),
        agent: String((s && s.agent) || "—"),
        usdLabel: fmtCost(s && s.usd),
        tokensLabel: fmtTok(Number(s && s.tokens_in) || 0) + " in + "
          + fmtTok(Number(s && s.tokens_out) || 0) + " out",
      })),
      totalLabel: fmtCost(totals.usd),
      runTotalLabel: data.run_total_usd != null ? fmtCost(data.run_total_usd) : "",
      unattributedLabel: (data.unattributed_usd != null && data.unattributed_usd > 0.00005)
        ? fmtCost(data.unattributed_usd) : "",
      bySubtask,
    };
  }

  // ---- A/B replay: POST /api/ab response -> table + verdict model ----
  function abTableModel(rep) {
    rep = rep || {};
    const arms = Array.isArray(rep.arms) ? rep.arms : [];
    return {
      knob: String(rep.knob || ""),
      verdict: arms.length && rep.best != null
        ? "Best arm: " + rep.knob + " = " + rep.best : "",
      rows: arms.map(a => ({
        value: String(a.value),
        best: rep.best != null && a.value === rep.best,
        passLabel: Math.round((Number(a.pass_rate) || 0) * 100) + "%",
        qualityLabel: a.mean_quality != null ? String(a.mean_quality) : "n/a",
        costLabel: "$" + Number(a.mean_cost_usd || 0).toFixed(4),
        wallLabel: (Number(a.mean_wall_s) || 0).toFixed(1) + "s",
        attempts: (a.report && a.report.attempts != null) ? Number(a.report.attempts) : null,
      })),
    };
  }

  // ---- Notification center (session-scope, client-side) ----
  const _NOTIF_ICON = { ask: "?", permission: "⚠", done: "✓", error: "✗", start: "▶" };
  // items: [{id, ts (s), kind, text, taskId, read}] -> newest-first rows + badge.
  function notifCenterModel(items, now) {
    const rows = (Array.isArray(items) ? items : []).slice()
      .sort((a, b) => (Number(b && b.ts) || 0) - (Number(a && a.ts) || 0))
      .map(n => ({
        id: String((n && n.id) || ""),
        taskId: String((n && n.taskId) || ""),
        kind: String((n && n.kind) || "info"),
        icon: _NOTIF_ICON[n && n.kind] || "·",
        text: String((n && n.text) || ""),
        timeLabel: fmtRelTime(n && n.ts, now),
        read: !!(n && n.read),
        cls: "nf-" + String((n && n.kind) || "info"),
      }));
    const unread = rows.filter(r => !r.read).length;
    return {
      rows, unread, empty: !rows.length,
      badge: unread ? (unread > 9 ? "9+" : String(unread)) : "",
    };
  }

  // ---- Playbooks: catalog entry + raw field values -> typed form model ----
  // Mirrors the server's param validation (str/int/choice, required, default)
  // so obvious mistakes fail before the request. Blank defaulted/optional
  // params are omitted from `params` (the server applies its own default).
  function playbookFormModel(pb, values) {
    pb = pb || {};
    values = values || {};
    const errors = [];
    const params = {};
    const fields = (Array.isArray(pb.params) ? pb.params : []).map(spec => {
      spec = spec || {};
      const key = String(spec.key || "");
      const type = spec.type === "int" || spec.type === "choice" ? spec.type : "str";
      const required = spec.required !== false;
      const hasDefault = "default" in spec && spec.default != null;
      const raw = key in values ? values[key] : (hasDefault ? spec.default : "");
      const s = String(raw == null ? "" : raw).trim();
      let error = "";
      if (!s) {
        if (required && !hasDefault) error = "required";
      } else if (type === "int") {
        if (!/^-?\d+$/.test(s)) error = "must be a whole number";
        else params[key] = parseInt(s, 10);
      } else if (type === "choice") {
        const choices = (spec.choices || []).map(String);
        if (!choices.includes(s)) error = "must be one of: " + choices.join(", ");
        else params[key] = s;
      } else {
        params[key] = s;
      }
      if (error) errors.push((spec.label || key) + " — " + error);
      return {
        key, type, required,
        label: String(spec.label || key),
        control: type === "choice" ? "select" : type === "int" ? "number" : "text",
        choices: (spec.choices || []).map(String),
        value: raw == null ? "" : String(raw),
        error,
      };
    });
    return { fields, ok: !errors.length, errors, params };
  }

  // ===========================================================
  // QoL wave — memory curation, workspace GC, subtask rollback.
  // Pure view models; the endpoints live in web/server.py.
  // ===========================================================

  // One GET /api/projects/{slug}/memories row -> curation-row model.
  // Long content is clamped to a preview with an expand affordance; text is
  // plain — the caller escapes before injecting. createdAt stays numeric
  // (epoch seconds) so date formatting is the caller's locale concern.
  const MEMORY_CLAMP_CHARS = 240;
  function memoryRowModel(m) {
    m = m || {};
    const content = String(m.content || "");
    const clamped = content.length > MEMORY_CLAMP_CHARS;
    return {
      id: m.id != null ? Number(m.id) : null,
      scope: String(m.scope || ""),
      key: String(m.key || ""),
      content,
      clamped,
      preview: clamped ? clipText(content, MEMORY_CLAMP_CHARS) : content,
      createdAt: (m.created_at != null && isFinite(Number(m.created_at)))
        ? Number(m.created_at) : null,
    };
  }

  // Paging state for the curation list: total (server) vs. loaded (client).
  function memoryPageModel(total, loaded) {
    total = Math.max(0, Number(total) || 0);
    loaded = Math.max(0, Number(loaded) || 0);
    const remaining = Math.max(0, total - loaded);
    return {
      countLabel: total + (total === 1 ? " memory" : " memories"),
      hasMore: remaining > 0,
      nextOffset: loaded,
      moreLabel: remaining > 0 ? "Load more (" + remaining + " remaining)" : "",
    };
  }

  // GET /api/projects/{slug}/gc report -> {count, label, items, emptyText}.
  // Worktrees and branches merge into one list; keep_days (echoed by the
  // server) feeds both the retention input and the empty-state copy.
  function gcSummary(report) {
    report = report || {};
    const keep = Math.max(0, Number(report.keep_days) || 0);
    const items = [];
    (Array.isArray(report.worktrees) ? report.worktrees : []).forEach(w => items.push({
      taskId: String((w && w.task_id) || ""), kind: "worktree",
      detail: String((w && w.path) || ""),
      status: String((w && w.status) || ""),
      ageLabel: (w && w.age_days != null) ? Number(w.age_days).toFixed(1) + "d old" : "",
    }));
    (Array.isArray(report.branches) ? report.branches : []).forEach(b => items.push({
      taskId: String((b && b.task_id) || ""), kind: "branch",
      detail: String((b && b.branch) || ""),
      status: String((b && b.status) || ""),
      ageLabel: (b && b.age_days != null) ? Number(b.age_days).toFixed(1) + "d old" : "",
    }));
    const count = items.length;
    return {
      count, items, keepDays: keep,
      label: count ? "Clean up " + count + " item" + (count === 1 ? "" : "s") : "",
      emptyText: count ? "" : "Nothing to clean — task workspaces are kept "
        + keep + " day" + (keep === 1 ? "" : "s") + " after completion.",
    };
  }

  // POST /api/projects/{slug}/gc response -> one result line + skipped rows.
  function gcResultModel(res) {
    res = res || {};
    const removed = (res.removed && typeof res.removed === "object") ? res.removed : {};
    const rw = Array.isArray(removed.worktrees) ? removed.worktrees.length : 0;
    const rb = Array.isArray(removed.branches) ? removed.branches.length : 0;
    const skipped = (Array.isArray(res.skipped) ? res.skipped : []).map(s => ({
      taskId: String((s && s.task_id) || ""),
      reason: String((s && s.reason) || ""),
    }));
    return {
      removedCount: rw + rb,
      skipped,
      text: "Removed " + rw + " worktree" + (rw === 1 ? "" : "s")
        + " · " + rb + " branch" + (rb === 1 ? "" : "es")
        + (skipped.length ? " · " + skipped.length + " skipped" : ""),
    };
  }

  // One review-subtask row -> rollback affordance. Only an accepted subtask
  // can be rolled back (the server reverts its recorded accepted_commit and
  // records decision "rolled_back"); a rolled-back row shows its state.
  function rollbackStateModel(s) {
    s = s || {};
    const decision = String(s.decision || "");
    return {
      canRollback: decision === "accepted",
      rolledBack: decision === "rolled_back",
      label: decision === "rolled_back" ? "rolled back" : "Roll back",
    };
  }

  // ---- S8: cookie-session auth gate ----
  // /api/auth/status response -> which screen boots first. Only a server that
  // requires auth AND hasn't authorized this browser gates on the login screen;
  // anything else (auth off, valid cookie, unreachable status) boots the app.
  function authGate(status) {
    status = status || {};
    return status.auth_required && !status.authorized ? "login" : "app";
  }

  // Login form: raw input -> trimmed token + inline error ("" when submittable).
  function loginFormModel(raw) {
    const token = String(raw == null ? "" : raw).trim();
    return { token, ok: !!token, error: token ? "" : "Enter the API token." };
  }

  // Failed POST /api/login -> the error line (403 is the only expected rejection).
  function loginErrorMessage(httpStatus) {
    if (httpStatus === 403) return "That token was not accepted — check it and try again.";
    return "Sign-in failed (HTTP " + httpStatus + ").";
  }

  // ---- First-run tour: 4 spotlight steps, shown once with 0 projects ----
  function tourStepsModel(projectCount, done) {
    return {
      show: !done && (Number(projectCount) || 0) === 0,
      steps: [
        { id: "welcome", target: "", title: "Welcome to AI Dev Assistant",
          body: "Describe work in plain English and a team of specialist agents plans, executes, reviews and documents it. This 20-second tour shows you around." },
        { id: "projects", target: "project-list", title: "Projects are the navigation",
          body: "Every task, memory and file lives inside a project. Create an empty one or import an existing repository to get started." },
        { id: "composer", target: "empty-new", title: "Composer & playbooks",
          body: "Each project's Overview has a freeform composer plus one-click playbooks — pre-tuned templates like “raise test coverage” or “upgrade a dependency”." },
        { id: "settings", target: "nav-gsettings", title: "Settings",
          body: "Assistant-wide configuration lives here: models, budgets, guardrails, schedules and GitHub automation." },
      ],
    };
  }

  // ===========================================================
  // Away wave — pure models: Home aggregation, workspaces
  // (sidebar grouping, deps editor, run composer), knowledge
  // graph v2 and the custom-agent editor. All Node-tested.
  // ===========================================================

  // Benchmark delta -> arrow model. null/0 -> flat dot; positive up; negative down.
  function _deltaArrow(v) {
    if (v == null || !isFinite(Number(v)) || Number(v) === 0) return { dir: "flat", label: "" };
    const n = Number(v);
    return { dir: n > 0 ? "up" : "down", label: (n > 0 ? "▲ +" : "▼ ") + n };
  }

  // GET /api/home body -> the whole home-screen render model. Tolerates a
  // missing/partial payload; text fields are plain (caller escapes).
  function homeModel(p) {
    p = p || {};
    const attention = (Array.isArray(p.attention) ? p.attention : []).map(a => {
      a = a || {};
      const kind = a.kind === "permission" ? "permission" : "ask";
      return {
        taskId: String(a.task_id || ""), project: String(a.project || ""),
        kind, kindLabel: kind === "permission" ? "permission request" : "question",
        agent: String(a.agent || ""),
        text: String(a.question || a.request || ""),
        options: Array.isArray(a.options) ? a.options.map(String) : [],
      };
    });
    const live = [];
    (Array.isArray(p.running) ? p.running : []).forEach(r => {
      r = r || {};
      const pr = (r.progress && typeof r.progress === "object") ? r.progress : null;
      live.push({
        taskId: String(r.task_id || ""), title: String(r.title || r.task_id || ""),
        project: String(r.project || ""), state: "running",
        progressLabel: (pr && pr.total != null)
          ? (pr.passed != null ? pr.passed : 0) + "/" + pr.total + " subtasks" : "",
      });
    });
    (Array.isArray(p.queued) ? p.queued : []).forEach(q => {
      q = q || {};
      live.push({
        taskId: String(q.task_id || ""), title: String(q.title || q.task_id || ""),
        project: String(q.project || ""), state: "queued",
        progressLabel: q.position != null ? "#" + q.position : "",
      });
    });
    const recent = (Array.isArray(p.recent) ? p.recent : []).map(r => {
      r = r || {};
      return {
        taskId: String(r.task_id || ""), title: String(r.title || r.task_id || ""),
        project: String(r.project || ""), status: String(r.status || ""),
        qualityLabel: r.quality != null ? "quality " + r.quality + "/100" : "",
        costLabel: r.cost_usd != null ? fmtCost(r.cost_usd) : "",
      };
    });
    const sp = (p.spend && typeof p.spend === "object") ? p.spend : {};
    const topProject = (Array.isArray(sp.by_project) && sp.by_project.length)
      ? { project: String(sp.by_project[0].project || ""),
          usdLabel: "$" + Number(sp.by_project[0].usd || 0).toFixed(2) }
      : null;
    const spend = {
      totalLabel: "$" + Number(sp.total_usd || 0).toFixed(2),
      runs: Number(sp.runs) || 0,
      windowDays: Number(sp.window_days) || 30,
      topProject,
    };
    const b = (p.benchmarks && typeof p.benchmarks === "object") ? p.benchmarks : {};
    const latest = (b.latest && typeof b.latest === "object") ? b.latest : null;
    const delta = (b.delta && typeof b.delta === "object") ? b.delta : {};
    const series = Array.isArray(b.series) ? b.series : [];
    const benchmarks = {
      available: !!latest,
      passLabel: latest && latest.pass_rate != null
        ? Math.round(Number(latest.pass_rate) * 100) + "%" : "—",
      qualityLabel: latest && latest.quality_mean != null
        ? String(latest.quality_mean) : "—",
      passDelta: _deltaArrow(delta.pass_rate),
      qualityDelta: _deltaArrow(delta.quality_mean),
      bars: series.map(s => {
        const pr = Number((s && s.pass_rate) || 0);
        return {
          sha: String((s && s.sha) || "").slice(0, 7),
          hPct: Math.max(4, Math.round(pr * 100)),
          label: String((s && s.sha) || "").slice(0, 7) + " · "
            + Math.round(pr * 100) + "%",
        };
      }),
    };
    const workspaces = (Array.isArray(p.workspaces) ? p.workspaces : []).map(w => {
      w = w || {};
      const n = Number(w.projects) || 0;
      return { slug: String(w.slug || ""), name: String(w.name || w.slug || ""),
               projectsLabel: n + " project" + (n === 1 ? "" : "s") };
    });
    const c = (p.counts && typeof p.counts === "object") ? p.counts : {};
    return {
      attention, attentionCount: attention.length, prominent: attention.length > 0,
      live, recent, spend, benchmarks, workspaces,
      counts: { projects: Number(c.projects) || 0, workspaces: Number(c.workspaces) || 0,
                custom_agents: Number(c.custom_agents) || 0 },
      errors: (Array.isArray(p.errors) ? p.errors : []).map(String),
    };
  }

  // /api/projects items (with the additive "workspace" field) -> sidebar groups:
  // { groups: [{workspace, projects: [...]}], ungrouped: [...] }. Group order is
  // first appearance; the scratch "default" project is filtered out.
  function sidebarGroups(projects) {
    const groups = [];
    const byWs = {};
    const ungrouped = [];
    visibleProjects(projects).forEach(p => {
      const ws = p.workspace ? String(p.workspace) : "";
      if (!ws) { ungrouped.push(p); return; }
      if (!byWs[ws]) { byWs[ws] = { workspace: ws, projects: [] }; groups.push(byWs[ws]); }
      byWs[ws].projects.push(p);
    });
    return { groups, ungrouped };
  }

  // Workspaces tab: /api/workspaces entries + /api/projects list -> cards with
  // resolved member projects (registry order) and the leftover ungrouped list.
  function workspacesViewModel(workspaces, projects) {
    const visible = visibleProjects(projects || []);
    const bySlug = {};
    visible.forEach(p => { bySlug[p.slug] = p; });
    const grouped = new Set();
    const cards = (Array.isArray(workspaces) ? workspaces : []).map(w => {
      const members = (w.project_slugs || []).filter(s => bySlug[s]).map(s => {
        grouped.add(s);
        return { slug: s, name: bySlug[s].name || s };
      });
      return {
        slug: w.slug, name: w.name || w.slug, description: w.description || "",
        members, countLabel: members.length === 1 ? "1 project" : members.length + " projects",
      };
    });
    const ungrouped = visible.filter(p => !grouped.has(p.slug))
      .map(p => ({ slug: p.slug, name: p.name || p.slug }));
    return { workspaces: cards, ungrouped };
  }

  // Workspace entry {project_slugs, default_deps} -> deps-editor rows: one row
  // per member with the OTHER members as upstream options (selected = current dep).
  function wsDepsModel(entry) {
    entry = entry || {};
    const members = Array.isArray(entry.project_slugs) ? entry.project_slugs.map(String) : [];
    const deps = (entry.default_deps && typeof entry.default_deps === "object")
      ? entry.default_deps : {};
    const rows = members.map(slug => ({
      slug,
      upstreams: members.filter(o => o !== slug).map(o => ({
        slug: o,
        selected: (Array.isArray(deps[slug]) ? deps[slug] : []).includes(o),
      })),
    }));
    return { rows, editable: members.length > 1 };
  }

  // Workspace-run composer values -> { ok, errors, body } for
  // POST /api/workspaces/{ws}/run. `subset` is the checked member slugs;
  // checking every member (or none of the checkboxes rendered) sends no subset.
  function wsRunPayload(f) {
    f = f || {};
    const errors = [];
    const prompt = String(f.prompt || "").trim();
    if (!prompt) errors.push("prompt is required");
    const members = Array.isArray(f.members) ? f.members.map(String) : [];
    const subset = (Array.isArray(f.subset) ? f.subset.map(String) : [])
      .filter(s => members.includes(s));
    if (Array.isArray(f.subset) && members.length && !subset.length) {
      errors.push("pick at least one member project");
    }
    const body = { prompt };
    const title = String(f.title || "").trim();
    if (title) body.title = title;
    const effort = String(f.effort || "").trim();
    if (effort) body.effort = effort;
    const rawB = String(f.budget == null ? "" : f.budget).trim();
    if (rawB) {
      const budget = Number(rawB);
      if (!isFinite(budget) || budget < 0) errors.push("budget must be a non-negative number");
      else if (budget > 0) body.budget = budget;
    }
    if (subset.length && subset.length < members.length) body.subset = subset;
    return { ok: !errors.length, errors, body };
  }

  // ---- Knowledge graph v2 (GET .../graph2) ----
  // NOTE the payload: nodes {id,label,type,degree,weight}, edges use src/dst
  // (NOT source/target); labels are display text, ids canonical.
  const GRAPH2_TYPES = ["file", "task", "agent", "concept"];
  function _graph2ColorKey(type) {
    if (type === "file") return "file";
    if (type === "task" || type === "subtask") return "task";
    if (type === "agent") return "agent";
    return "concept";
  }

  // Client-side layer/min-weight filter + node size/color mapping. Mirrors
  // export_view semantics: unfiltered keeps isolated nodes; a filter keeps only
  // nodes incident to surviving edges.
  function graph2ViewModel(payload, opts) {
    payload = payload || {};
    opts = opts || {};
    const layer = opts.layer === "domain" || opts.layer === "run" ? opts.layer : "";
    const minWeight = Math.max(1, Number(opts.minWeight) || 1);
    const allNodes = Array.isArray(payload.nodes) ? payload.nodes : [];
    const allEdges = Array.isArray(payload.edges) ? payload.edges : [];
    const edges = allEdges.filter(e => e
      && (!layer || (e.layer || "domain") === layer)
      && (Number(e.weight) || 1) >= minWeight);
    let nodes;
    if (!layer && minWeight <= 1) {
      nodes = allNodes.slice();          // include isolated nodes
    } else {
      const keep = new Set();
      edges.forEach(e => { keep.add(e.src); keep.add(e.dst); });
      nodes = allNodes.filter(n => n && keep.has(n.id));
    }
    const mapped = nodes.map(n => {
      const w = n.weight != null ? Number(n.weight) : (Number(n.degree) || 0);
      return {
        ...n,
        id: String(n.id), label: String(n.label || n.id),
        type: String(n.type || "concept"),
        colorKey: _graph2ColorKey(n.type),
        degree: Number(n.degree) || 0, weight: w,
        r: Math.min(18, 6 + Math.sqrt(Math.max(0, w)) * 2),
      };
    });
    const present = new Set(mapped.map(n => n.colorKey));
    return {
      nodes: mapped,
      edges: edges.map(e => ({ src: String(e.src), dst: String(e.dst),
                               relation: String(e.relation || "related_to"),
                               weight: Number(e.weight) || 1,
                               layer: String(e.layer || "domain") })),
      legend: GRAPH2_TYPES.filter(t => present.has(t)),
      statsLabel: mapped.length + "/" + allNodes.length + " nodes · "
        + edges.length + "/" + allEdges.length + " edges",
    };
  }

  // GET .../graph2/node/{id}?depth=1 body ({node, nodes, edges}) -> side panel:
  // the focused node's label/type/degree plus its incident edges with
  // relation/weight/layer/last_ts provenance. lastTs stays numeric (epoch s).
  function nodePanelModel(hood) {
    hood = hood || {};
    const id = String(hood.node || "");
    const nodes = Array.isArray(hood.nodes) ? hood.nodes : [];
    const self = nodes.find(n => n && n.id === id)
      || nodes.find(n => n && String(n.label || "") === id) || null;
    if (!self) return { empty: true, id, label: id, type: "", degree: 0, edges: [], fileTask: "" };
    const label = (nid) => {
      const n = nodes.find(x => x && x.id === nid);
      return n ? String(n.label || n.id) : String(nid);
    };
    let fileTask = "";
    const edges = (Array.isArray(hood.edges) ? hood.edges : [])
      .filter(e => e && (e.src === self.id || e.dst === self.id))
      .map(e => {
        const out = e.src === self.id;
        if (!out && e.relation === "produced_file") fileTask = String(e.src);
        return {
          dir: out ? "→" : "←",
          other: out ? String(e.dst) : String(e.src),
          otherLabel: label(out ? e.dst : e.src),
          relation: String(e.relation || "related_to"),
          weight: Number(e.weight) || 1,
          layer: String(e.layer || "domain"),
          lastTs: (e.last_ts != null && isFinite(Number(e.last_ts))) ? Number(e.last_ts) : null,
        };
      });
    return {
      empty: false, id: String(self.id), label: String(self.label || self.id),
      type: String(self.type || "concept"), degree: Number(self.degree) || 0,
      edges, fileTask,
    };
  }

  // ---- Agents: roster listing + custom-agent editor ----
  // GET /api/agents body {builtin, custom, tools} -> normalized model.
  // Tolerates the legacy flat-array shape (treated as all-builtin, no tools).
  function agentsListModel(body) {
    if (Array.isArray(body)) {
      return { builtin: body, custom: [], tools: [],
               names: body.map(a => String((a && a.name) || "")).filter(Boolean) };
    }
    body = body || {};
    const builtin = Array.isArray(body.builtin) ? body.builtin : [];
    const custom = Array.isArray(body.custom) ? body.custom : [];
    return {
      builtin, custom,
      tools: (Array.isArray(body.tools) ? body.tools : []).map(String),
      names: [...builtin, ...custom].map(a => String((a && a.name) || "")).filter(Boolean),
    };
  }

  const AGENT_EFFORTS = ["", "low", "medium", "high", "xhigh", "max"];
  const AGENT_NAME_RE = /^[a-z][a-z0-9_]{0,39}$/;

  // Stored spec (or null for a blank form) + toolbox names -> form field values.
  function agentFormModel(spec, tools) {
    spec = spec || {};
    const chosen = new Set(Array.isArray(spec.tools) ? spec.tools : []);
    return {
      heading: spec.name ? "Edit agent — " + spec.name : "New agent",
      editing: !!spec.name,
      name: String(spec.name || ""),
      description: String(spec.description || ""),
      when_to_use: String(spec.when_to_use || ""),
      system_prompt: String(spec.system_prompt || ""),
      effort: AGENT_EFFORTS.includes(spec.effort) ? spec.effort : "",
      model: String(spec.model || ""),
      tools: (Array.isArray(tools) ? tools : []).map(t => ({
        name: String(t), checked: chosen.has(t),
      })),
    };
  }

  // Form values -> { ok, errors, spec } for POST /api/agents {spec}. Mirrors the
  // server's gates (slug name, non-empty texts, toolbox-only tools, known effort)
  // so obvious mistakes fail before the request; the server's 400 is still the
  // real validator and is surfaced inline by the caller.
  function agentFormValidate(f, tools) {
    f = f || {};
    const errors = [];
    const name = String(f.name || "").trim().toLowerCase();
    if (!AGENT_NAME_RE.test(name)) {
      errors.push("name must be a slug — lowercase letters/digits/underscores, starting with a letter");
    }
    ["description", "when_to_use", "system_prompt"].forEach(k => {
      if (!String(f[k] || "").trim()) errors.push(k.replace(/_/g, " ") + " is required");
    });
    const toolbox = new Set((Array.isArray(tools) ? tools : []).map(String));
    const chosen = (Array.isArray(f.tools) ? f.tools : []).map(String);
    if (!chosen.length) errors.push("pick at least one tool");
    const unknown = chosen.filter(t => toolbox.size && !toolbox.has(t));
    if (unknown.length) errors.push("unknown tools: " + unknown.join(", "));
    const effort = String(f.effort || "");
    if (!AGENT_EFFORTS.includes(effort)) errors.push("effort must be inherit or one of low/medium/high/xhigh/max");
    return {
      ok: !errors.length, errors,
      spec: {
        name,
        description: String(f.description || "").trim(),
        when_to_use: String(f.when_to_use || "").trim(),
        system_prompt: String(f.system_prompt || "").trim(),
        tools: chosen,
        effort: AGENT_EFFORTS.includes(effort) ? effort : "",
        model: String(f.model || "").trim(),
      },
    };
  }

  // ===========================================================
  // Closing wave — benchmarks panel (All activity), knowledge-graph
  // distillation and named-user identity. Pure models; the endpoints
  // live in web/server.py. All Node-tested; no DOM access.
  // ===========================================================

  const BENCH_EMPTY_TEXT = "No benchmark history — run an eval with --record-history.";

  // One trend_report entry (latest or history row) -> display labels.
  // ts stays numeric (epoch seconds) — date formatting is the caller's concern.
  function _benchEntryRow(h) {
    h = h || {};
    const mean = h.quality_mean != null ? String(h.quality_mean) : "—";
    return {
      ts: (h.ts != null && isFinite(Number(h.ts))) ? Number(h.ts) : null,
      sha: String(h.git_sha || h.sha || "").slice(0, 7),
      suite: String(h.suite || ""),
      passLabel: h.pass_rate != null ? Math.round(Number(h.pass_rate) * 100) + "%" : "—",
      qualityLabel: mean,
      qualityMinLabel: h.quality_min != null ? String(h.quality_min) : "—",
      qualityCell: h.quality_mean != null
        ? mean + (h.quality_min != null ? " / " + h.quality_min : "") : "—",
      costLabel: h.cost_usd != null ? "$" + Number(h.cost_usd).toFixed(2) : "—",
    };
  }

  // GET /api/benchmarks (trend_report) -> the All-activity Benchmarks section:
  // latest scorecard + deltas, series bars (heights clamped 0-100; non-zero
  // values get a 4% visibility floor) and the compact history table.
  function benchModel(p) {
    p = p || {};
    const latest = (p.latest && typeof p.latest === "object") ? p.latest : null;
    if (!latest) {
      return { available: false, emptyText: BENCH_EMPTY_TEXT,
               latest: null, bars: [], history: [] };
    }
    const delta = (p.delta && typeof p.delta === "object") ? p.delta : {};
    const clampH = (v) => {
      const h = Math.max(0, Math.min(100, Math.round(Number(v) || 0)));
      return h > 0 ? Math.max(4, h) : 0;
    };
    const dNum = (v, scale, dp) => (v == null || !isFinite(Number(v)))
      ? null : Number((Number(v) * scale).toFixed(dp));
    const runs = Number(latest.runs) || 0;
    return {
      available: true,
      emptyText: "",
      latest: Object.assign(_benchEntryRow(latest), {
        runsLabel: runs + " run" + (runs === 1 ? "" : "s"),
        durationLabel: fmtDuration(latest.duration_s),
        deltas: {   // vs the previous history entry; pass delta in percentage points
          pass: _deltaArrow(dNum(delta.pass_rate, 100, 0)),
          quality: _deltaArrow(dNum(delta.quality_mean, 1, 1)),
          qualityMin: _deltaArrow(dNum(delta.quality_min, 1, 1)),
          cost: _deltaArrow(dNum(delta.cost_usd, 1, 2)),
        },
      }),
      bars: (Array.isArray(p.series) ? p.series : []).map(s => {
        s = s || {};
        const passH = clampH(Number(s.pass_rate) * 100);
        const qualityH = clampH(s.quality_mean);
        const sha = String(s.sha || "").slice(0, 7);
        return {
          sha, passH, qualityH,
          label: sha + " · " + Math.round((Number(s.pass_rate) || 0) * 100) + "% pass"
            + " · quality " + (s.quality_mean != null ? s.quality_mean : "—")
            + (s.cost_usd != null ? " · $" + Number(s.cost_usd).toFixed(2) : ""),
        };
      }),
      // the server records history newest-last; the table reads newest-first
      history: (Array.isArray(p.history) ? p.history : []).slice().reverse().map(_benchEntryRow),
    };
  }

  // POST .../graph2/distill {dry_run:true} report -> the report panel model.
  // A too-small graph arrives as an empty report with a `reason` string.
  function distillReportModel(r) {
    r = r || {};
    const merges = (Array.isArray(r.merges) ? r.merges : []).map(m => {
      m = m || {};
      return {
        keep: String(m.keep || ""), drop: String(m.drop || ""),
        reason: String(m.reason || ""),
        label: String(m.keep || "") + " ← " + String(m.drop || ""),
      };
    });
    const prunes = (Array.isArray(r.prunes) ? r.prunes : []).map(e => {
      e = e || {};
      const bits = [];
      if (e.weight != null) bits.push("w" + e.weight);
      if (e.age_days != null) bits.push(Math.round(Number(e.age_days)) + "d old");
      return {
        edge: String(e.src || "") + " → " + String(e.dst || "")
          + (e.relation ? " (" + e.relation + ")" : ""),
        detail: bits.join(" · "),
      };
    });
    const orphans = (Array.isArray(r.orphans) ? r.orphans : []).map(String);
    const empty = !merges.length && !prunes.length && !orphans.length;
    const reason = String(r.reason || "");
    const n = (len, one, many) => len + " " + (len === 1 ? one : many);
    return {
      empty, reason,
      emptyText: empty ? (reason || "Nothing to distill.") : "",
      merges, prunes, orphans,
      summary: empty ? "" : [
        n(merges.length, "merge", "merges"),
        n(prunes.length, "prune", "prunes"),
        n(orphans.length, "orphan", "orphans"),
      ].join(" · "),
      canApply: !empty,
    };
  }

  // POST .../graph2/distill {dry_run:false} response -> the applied-result line.
  function distillResultModel(res) {
    res = res || {};
    const merged = Number(res.merged) || 0;
    const pruned = Number(res.pruned) || 0;
    const orphansRemoved = Number(res.orphans_removed) || 0;
    return {
      merged, pruned, orphansRemoved,
      total: merged + pruned + orphansRemoved,
      text: "Distilled — merged " + merged + " · pruned " + pruned
        + " · removed " + orphansRemoved + " orphan" + (orphansRemoved === 1 ? "" : "s"),
    };
  }

  // GET /api/auth/status {auth_required, authorized, user} -> sidebar user chip.
  // Only a named (non-"local") user on an auth-gated server shows the chip.
  // byLabel serves the task-detail "by <name>" chip and ignores auth_required
  // (historical run payloads carry `user` regardless of the current gate).
  function userChipModel(status) {
    status = status || {};
    const user = String(status.user || "").trim();
    const named = !!user && user !== "local";
    const visible = !!status.auth_required && named;
    return {
      visible,
      name: named ? user : "",
      label: visible ? "◆ " + user : "",
      byLabel: named ? "by " + user : "",
    };
  }

  // GET /api/users body -> admin-card rows. Tolerates a bare array, a
  // {users:[...]} wrapper, and string-only entries.
  function usersListModel(body) {
    const list = Array.isArray(body) ? body
      : (body && Array.isArray(body.users)) ? body.users : [];
    const rows = list.map(u => {
      if (typeof u === "string") return { name: u, createdAt: null };
      u = u || {};
      return {
        name: String(u.name || ""),
        createdAt: (u.created_at != null && isFinite(Number(u.created_at)))
          ? Number(u.created_at) : null,
      };
    }).filter(r => r.name);
    return {
      rows, empty: !rows.length,
      countLabel: rows.length + (rows.length === 1 ? " user" : " users"),
    };
  }

  // POST /api/users response -> the one-time-token reveal model.
  function userCreateModel(res) {
    res = res || {};
    const token = String(res.token || "");
    return {
      ok: !!token,
      name: String(res.name || res.user || ""),
      token,
      warning: token ? "This token is shown once — copy it now." : "",
    };
  }

  // ============================================================
  // UI smoothing pass — pure models (workspace recent tasks, agent
  // tool-chip collapse, backend chip, graph seed positions).
  // ============================================================

  // Per-project run lists -> one merged "Recent tasks" list for the workspace
  // view. Input: [{slug, name, runs}] where runs are /api/projects/{slug}/runs
  // rows. Output rows are newest-first (created_at), deduped by id, capped
  // (default 8), shaped for the shared task-row renderer.
  function wsRecentTasksModel(perProject, cap) {
    cap = Number(cap) > 0 ? Math.floor(Number(cap)) : 8;
    const seen = new Set();
    const rows = [];
    (Array.isArray(perProject) ? perProject : []).forEach(p => {
      p = p || {};
      const slug = String(p.slug || "");
      const name = String(p.name || slug);
      (Array.isArray(p.runs) ? p.runs : []).forEach(r => {
        r = r || {};
        const id = String(r.id || "");
        if (!id || seen.has(id)) return;
        seen.add(id);
        const status = String(r.status || "");
        rows.push({
          taskId: id, title: String(r.title || id),
          project: slug, projectName: name, status,
          live: status === "running" || status === "queued",
          createdAt: (r.created_at != null && isFinite(Number(r.created_at)))
            ? Number(r.created_at) : null,
          qualityLabel: r.quality_score != null ? "quality " + r.quality_score + "/100" : "",
          costLabel: (r.cost_usd != null && Number(r.cost_usd) > 0) ? fmtCost(r.cost_usd) : "",
        });
      });
    });
    rows.sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
    const out = rows.slice(0, cap);
    return { rows: out, empty: !out.length };
  }

  // Agent-card tool list -> collapsed count-chip model ("26 tools" toggle).
  function agentToolsModel(tools) {
    const list = (Array.isArray(tools) ? tools : []).map(String);
    return {
      tools: list, count: list.length,
      label: list.length + (list.length === 1 ? " tool" : " tools"),
    };
  }

  // /api/config -> the sidebar backend chip text ("" = leave the placeholder).
  function backendLabel(cfg) {
    const b = String((cfg && (cfg.llm_backend || cfg.backend)) || "").trim();
    return b ? "backend: " + b : "";
  }

  // Deterministic golden-angle spiral: n seed positions spread over a w×h
  // canvas (margin-clamped), so the force layout starts unclustered and the
  // first painted frame has no label pileup.
  function seedLayout(n, w, h, margin) {
    n = Math.max(0, Math.floor(Number(n) || 0));
    w = Number(w) || 0; h = Number(h) || 0;
    margin = margin == null ? 24 : Number(margin);
    const cx = w / 2, cy = h / 2;
    const rx = Math.max(1, w / 2 - margin), ry = Math.max(1, h / 2 - margin);
    const GA = Math.PI * (3 - Math.sqrt(5));   // golden angle ≈ 2.39996 rad
    const pts = [];
    for (let i = 0; i < n; i++) {
      const t = n === 1 ? 0 : Math.sqrt((i + 0.5) / n);   // uniform-area radius
      const a = i * GA;
      pts.push({
        x: Math.max(margin, Math.min(w - margin, cx + Math.cos(a) * rx * t)),
        y: Math.max(margin, Math.min(h - margin, cy + Math.sin(a) * ry * t)),
      });
    }
    return pts;
  }

  const AdaUtil = {
    escapeHtml, escapeAttr, fmtTok, fmtSize, fmtCost, fmtDuration, classifyDiffLine,
    fmtRelTime, paletteResultsModel, scheduleRowModel, scheduleFormModel,
    spendOverviewModel, spendOutcomesModel, runCostModel, abTableModel,
    notifCenterModel, playbookFormModel, tourStepsModel,
    authGate, loginFormModel, loginErrorMessage,
    makeAgentRecord, initialRunAggregates, reduceRunEvent, runProgress, formatStepLine,
    clipText, transcriptLineModel, transcriptViewModel,
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
    settingsGroupsModel, fieldControlModel, coerceFieldInput,
    renderMarkdown,
    MEMORY_CLAMP_CHARS, memoryRowModel, memoryPageModel,
    gcSummary, gcResultModel, rollbackStateModel, CRON_HINT,
    homeModel, sidebarGroups, workspacesViewModel, wsDepsModel, wsRunPayload,
    graph2ViewModel, nodePanelModel, GRAPH2_TYPES,
    agentsListModel, agentFormModel, agentFormValidate, AGENT_EFFORTS,
    benchModel, distillReportModel, distillResultModel,
    userChipModel, usersListModel, userCreateModel,
    wsRecentTasksModel, agentToolsModel, backendLabel, seedLayout,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = AdaUtil;
  if (global) global.AdaUtil = AdaUtil;
})(typeof window !== "undefined" ? window : globalThis);
