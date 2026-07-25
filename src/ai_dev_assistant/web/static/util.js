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
    };
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
        const subs = d.subtasks || [];
        state.total = subs.length;
        state.reviewed = new Set();
        subs.forEach(st => { state.agentData[st.id] = makeAgentRecord(st); });
        state.phase = "execute";
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
    return {
      done, total,
      pct: total ? Math.round((done / total) * 100) : 0,
      label: total ? done + "/" + total + " subtasks" : "",
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

  const AdaUtil = {
    escapeHtml, escapeAttr, fmtTok, fmtSize, fmtCost, fmtDuration, classifyDiffLine,
    makeAgentRecord, initialRunAggregates, reduceRunEvent, runProgress, formatStepLine,
    computeBudgetMeter, timelineRows, compareRowModel, RESUMABLE_STATUSES, isResumable,
    projectStatusLine, activityStripModel,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = AdaUtil;
  if (global) global.AdaUtil = AdaUtil;
})(typeof window !== "undefined" ? window : globalThis);
