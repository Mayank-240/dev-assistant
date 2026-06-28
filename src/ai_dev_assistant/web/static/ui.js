/* ============================================================
   ui.js — theme (light / dark / system) + Run-view layout switch.
   Loaded before app.js; only touches presentation, never the
   backend wiring. Preferences persist in localStorage.
   ============================================================ */
(function () {
  "use strict";

  var root = document.documentElement;
  var mq = window.matchMedia("(prefers-color-scheme: dark)");

  function pref() { return localStorage.getItem("ada-theme") || "system"; }
  function resolve(p) { return p === "system" ? (mq.matches ? "dark" : "light") : p; }

  function apply(p) {
    root.setAttribute("data-theme", resolve(p));
    root.setAttribute("data-theme-pref", p);
    var btn = document.getElementById("theme-btn");
    if (btn) {
      var icon = p === "light" ? "☀" : p === "dark" ? "☾" : "◐";
      var label = p === "system" ? "Auto" : p === "light" ? "Light" : "Dark";
      btn.textContent = icon + " " + label;
    }
  }

  function cycle() {
    var order = ["light", "dark", "system"];
    var next = order[(order.indexOf(pref()) + 1) % 3];
    localStorage.setItem("ada-theme", next);
    apply(next);
  }

  // follow the OS when in "system" mode
  mq.addEventListener("change", function () { if (pref() === "system") apply("system"); });

  // ---- run-view layout ----
  function applyLayout(name) {
    var rv = document.getElementById("run-view");
    if (rv) rv.setAttribute("data-layout", name);
    document.querySelectorAll("#layout-seg .lseg").forEach(function (b) {
      b.classList.toggle("active", b.dataset.layout === name);
    });
    localStorage.setItem("ada-layout", name);
  }

  // ---- sidebar: drag-to-resize + collapse toggle (both persisted) ----
  var SB_MIN = 200, SB_MAX = 560, SB_DEFAULT = 296;

  function sbLayout() { return document.querySelector(".layout"); }

  function setSidebarWidth(px) {
    var w = Math.max(SB_MIN, Math.min(SB_MAX, Math.round(px)));
    var l = sbLayout();
    if (l) l.style.setProperty("--sidebar-w", w + "px");
    return w;
  }

  function setCollapsed(on) {
    var l = sbLayout();
    if (l) l.classList.toggle("sidebar-collapsed", on);
    var b = document.getElementById("sidebar-toggle");
    if (b) { b.textContent = on ? "»" : "«"; b.setAttribute("aria-expanded", on ? "false" : "true"); }
    localStorage.setItem("ada-sidebar-collapsed", on ? "1" : "0");
  }

  function initSidebar() {
    var savedW = parseInt(localStorage.getItem("ada-sidebar-w"), 10);
    if (savedW) setSidebarWidth(savedW);
    setCollapsed(localStorage.getItem("ada-sidebar-collapsed") === "1");

    var toggle = document.getElementById("sidebar-toggle");
    if (toggle) toggle.addEventListener("click", function () {
      var l = sbLayout();
      setCollapsed(!(l && l.classList.contains("sidebar-collapsed")));
    });

    var rez = document.getElementById("sidebar-resizer");
    var sb = document.querySelector(".sidebar");
    if (!rez || !sb) return;

    rez.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      var startX = e.clientX, startW = sb.getBoundingClientRect().width;
      rez.classList.add("dragging");
      document.body.classList.add("resizing");
      try { rez.setPointerCapture(e.pointerId); } catch (_) {}

      function move(ev) { setSidebarWidth(startW + (ev.clientX - startX)); }
      function up(ev) {
        rez.removeEventListener("pointermove", move);
        rez.removeEventListener("pointerup", up);
        rez.classList.remove("dragging");
        document.body.classList.remove("resizing");
        try { rez.releasePointerCapture(ev.pointerId); } catch (_) {}
        var l = sbLayout(), cur = l && l.style.getPropertyValue("--sidebar-w");
        if (cur) localStorage.setItem("ada-sidebar-w", String(parseInt(cur, 10)));
      }
      rez.addEventListener("pointermove", move);
      rez.addEventListener("pointerup", up);
    });

    // double-click the divider to reset to the default width
    rez.addEventListener("dblclick", function () {
      setSidebarWidth(SB_DEFAULT);
      localStorage.setItem("ada-sidebar-w", String(SB_DEFAULT));
    });
  }

  function init() {
    apply(pref());
    var btn = document.getElementById("theme-btn");
    if (btn) btn.addEventListener("click", cycle);

    initSidebar();

    var layout = localStorage.getItem("ada-layout") || "focus";
    applyLayout(layout);
    document.querySelectorAll("#layout-seg .lseg").forEach(function (b) {
      b.addEventListener("click", function () { applyLayout(b.dataset.layout); });
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
