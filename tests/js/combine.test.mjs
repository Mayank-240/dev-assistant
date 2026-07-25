// Unit tests for the project-visibility + combined-knowledge pure models in
// web/static/util.js: default-project hiding, first-run empty-state flag,
// "Combine with…" chips, the projects= query param, tagged item rows, the
// combined-view banner and per-project graph colors.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  visibleProjects, projectsEmptyState, sidebarProjectRows,
  combineChipsModel, combinedProjectsParam, itemProjects, taggedItemRows,
  combinedBannerModel, projectColorMap,
} = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- visibleProjects / projectsEmptyState (default hidden, empty-state flag) ----

test("visibleProjects hides the scratch default project", () => {
  const out = visibleProjects([
    { slug: "default", name: "Default" }, { slug: "api", name: "Notes API" },
  ]);
  assert.deepEqual(out.map(p => p.slug), ["api"]);
});

test("visibleProjects tolerates bad input", () => {
  assert.deepEqual(visibleProjects(null), []);
  assert.deepEqual(visibleProjects([null, {}, { slug: "" }]), []);
});

test("projectsEmptyState is true with no projects or only the default", () => {
  assert.equal(projectsEmptyState([]), true);
  assert.equal(projectsEmptyState([{ slug: "default", name: "Default" }]), true);
  assert.equal(projectsEmptyState(null), true);
});

test("projectsEmptyState is false once a real project exists", () => {
  assert.equal(projectsEmptyState([{ slug: "default" }, { slug: "api" }]), false);
});

test("sidebarProjectRows never renders the default project", () => {
  const rows = sidebarProjectRows(
    [{ slug: "default", name: "Default" }, { slug: "api", name: "Notes API" }],
    {}, "api", null);
  assert.deepEqual(rows.map(r => r.slug), ["api"]);
  assert.equal(rows[0].current, true);
});

test("sidebarProjectRows is empty when only the default project exists", () => {
  assert.deepEqual(sidebarProjectRows([{ slug: "default" }], {}, null, null), []);
});

// ---- combineChipsModel (active project excluded, archived + default excluded) ----

const _projects = [
  { slug: "default", name: "Default" },
  { slug: "api", name: "Notes API" },
  { slug: "web", name: "Web" },
  { slug: "old", name: "Old", archived: true },
];

test("combineChipsModel lists other non-archived projects only", () => {
  const chips = combineChipsModel(_projects, "api", []);
  assert.deepEqual(chips.map(c => c.slug), ["web"]);
  assert.equal(chips[0].name, "Web");
  assert.equal(chips[0].selected, false);
});

test("combineChipsModel marks selected chips", () => {
  const chips = combineChipsModel(_projects.concat([{ slug: "cli", name: "CLI" }]),
                                  "api", ["cli"]);
  assert.deepEqual(chips.map(c => [c.slug, c.selected]), [["web", false], ["cli", true]]);
});

test("combineChipsModel falls back to the slug as name and tolerates bad input", () => {
  assert.equal(combineChipsModel([{ slug: "x" }], "y", null)[0].name, "x");
  assert.deepEqual(combineChipsModel(null, "api", null), []);
});

// ---- combinedProjectsParam (selected set -> query param string) ----

test("combinedProjectsParam is empty with no other selections", () => {
  assert.equal(combinedProjectsParam("api", []), "");
  assert.equal(combinedProjectsParam("api", null), "");
});

test("combinedProjectsParam puts the active project first", () => {
  assert.equal(combinedProjectsParam("api", ["web", "cli"]), "api,web,cli");
});

test("combinedProjectsParam dedupes and drops empties", () => {
  assert.equal(combinedProjectsParam("api", ["api", "web", "", "web"]), "api,web");
  assert.equal(combinedProjectsParam("api", ["api"]), "");
});

// ---- itemProjects / taggedItemRows (tagged memory + graph row models) ----

test("itemProjects reads the additive project field", () => {
  assert.deepEqual(itemProjects({ project: "api" }), ["api"]);
});

test("itemProjects prefers a non-empty sources list", () => {
  assert.deepEqual(itemProjects({ sources: ["api", "web"], project: "api" }), ["api", "web"]);
});

test("itemProjects is empty for untagged items and bad input", () => {
  assert.deepEqual(itemProjects({ content: "note" }), []);
  assert.deepEqual(itemProjects(null), []);
});

test("taggedItemRows tags rows only in combined mode", () => {
  const items = [{ id: "a", project: "api" }, { id: "b" }];
  const combined = taggedItemRows(items, true);
  assert.equal(combined[0].tag, "api");
  assert.deepEqual(combined[0].projects, ["api"]);
  assert.equal(combined[1].tag, "");
  const single = taggedItemRows(items, false);
  assert.equal(single[0].tag, "");
  assert.equal(single[0].item.id, "a");
});

test("taggedItemRows joins multi-source tags and tolerates bad input", () => {
  const rows = taggedItemRows([{ sources: ["api", "web"] }], true);
  assert.equal(rows[0].tag, "api · web");
  assert.deepEqual(taggedItemRows(null, true), []);
});

// ---- combinedBannerModel ----

test("combinedBannerModel hides below two projects", () => {
  assert.equal(combinedBannerModel(0).visible, false);
  assert.equal(combinedBannerModel(1).visible, false);
  assert.equal(combinedBannerModel(null).visible, false);
});

test("combinedBannerModel announces the read-only combined view", () => {
  const m = combinedBannerModel(3);
  assert.equal(m.visible, true);
  assert.equal(m.text, "Combined view — read-only across 3 projects");
});

// ---- projectColorMap ----

test("projectColorMap assigns one stable color per project", () => {
  const map = projectColorMap(["api", "web", "api"]);
  assert.deepEqual(Object.keys(map), ["api", "web"]);
  assert.match(map.api, /^#[0-9a-fA-F]{6}$/);
  assert.notEqual(map.api, map.web);
});

test("projectColorMap cycles the palette and tolerates bad input", () => {
  const many = projectColorMap(Array.from({ length: 12 }, (_, i) => "p" + i));
  assert.equal(Object.keys(many).length, 12);
  assert.deepEqual(projectColorMap(null), {});
});
