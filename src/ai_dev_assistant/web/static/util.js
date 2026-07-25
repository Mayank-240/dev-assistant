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

  const AdaUtil = { escapeHtml, escapeAttr, fmtTok, fmtSize, fmtCost, fmtDuration, classifyDiffLine };

  if (typeof module !== "undefined" && module.exports) module.exports = AdaUtil;
  if (global) global.AdaUtil = AdaUtil;
})(typeof window !== "undefined" ? window : globalThis);
