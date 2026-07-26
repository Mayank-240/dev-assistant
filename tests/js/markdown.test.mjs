// Unit tests for renderMarkdown (web/static/util.js) — the Files-view
// markdown preview. Security model under test: the whole input is
// HTML-escaped BEFORE any markdown transform, so raw input HTML can never
// reach the output; link hrefs are allow-listed (http(s)/mailto/#).
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { renderMarkdown } = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- headings ----
test("renders h1 through h6", () => {
  const html = renderMarkdown("# One\n\n###### Six");
  assert.ok(html.includes("<h1>One</h1>"));
  assert.ok(html.includes("<h6>Six</h6>"));
});

test("seven hashes is not a heading", () => {
  const html = renderMarkdown("####### nope");
  assert.ok(!html.includes("<h7"));
  assert.ok(html.includes("<p>"));
});

test("heading text supports inline markup and stays escaped", () => {
  const html = renderMarkdown("## A **bold** <b>title</b>");
  assert.ok(html.includes("<h2>A <strong>bold</strong> &lt;b&gt;title&lt;/b&gt;</h2>"));
});

// ---- emphasis + inline code ----
test("bold and italic", () => {
  const html = renderMarkdown("**bold** and *ital*");
  assert.ok(html.includes("<strong>bold</strong>"));
  assert.ok(html.includes("<em>ital</em>"));
});

test("inline code renders and its contents are not further transformed", () => {
  const html = renderMarkdown("use `**not bold**` here");
  assert.ok(html.includes("<code>**not bold**</code>"));
  assert.ok(!html.includes("<strong>not bold</strong>"));
});

test("html inside inline code stays escaped", () => {
  const html = renderMarkdown("`<script>alert(1)</script>`");
  assert.ok(html.includes("<code>&lt;script&gt;alert(1)&lt;/script&gt;</code>"));
  assert.ok(!/<script/i.test(html));
});

// ---- fenced code blocks ----
test("fenced code block with language", () => {
  const html = renderMarkdown("```python\nprint('hi')\n```");
  assert.ok(html.includes('<pre><code class="lang-python">'));
  assert.ok(html.includes("print(&#39;hi&#39;)"));
});

test("html inside a code fence stays escaped", () => {
  const html = renderMarkdown('```\n<img src=x onerror=alert(1)>\n<script>evil()</script>\n```');
  assert.ok(html.includes("&lt;img src=x onerror=alert(1)&gt;"));
  assert.ok(html.includes("&lt;script&gt;evil()&lt;/script&gt;"));
  assert.ok(!/<img/i.test(html));
  assert.ok(!/<script/i.test(html));
});

test("markdown inside a code fence is not transformed", () => {
  const html = renderMarkdown("```\n# not a heading\n**not bold**\n```");
  assert.ok(html.includes("# not a heading"));
  assert.ok(!html.includes("<h1>"));
  assert.ok(!html.includes("<strong>"));
});

test("unclosed fence swallows to EOF without breaking", () => {
  const html = renderMarkdown("```\ncode line");
  assert.ok(html.includes("<pre><code>code line</code></pre>"));
});

// ---- lists ----
test("unordered list with one nesting level", () => {
  const html = renderMarkdown("- top\n  - nested\n- next");
  assert.ok(html.includes("<ul>"));
  assert.ok(html.includes("<li>top<ul><li>nested</li></ul></li>"));
  assert.ok(html.includes("<li>next</li>"));
});

test("ordered list", () => {
  const html = renderMarkdown("1. first\n2. second");
  assert.ok(html.includes("<ol>"));
  assert.ok(html.includes("<li>first</li>"));
  assert.ok(html.includes("<li>second</li>"));
});

test("task-list checkboxes render disabled", () => {
  const html = renderMarkdown("- [ ] todo\n- [x] done");
  assert.ok(html.includes('<li class="md-task"><input type="checkbox" disabled> todo</li>'));
  assert.ok(html.includes('<li class="md-task"><input type="checkbox" disabled checked> done</li>'));
});

// ---- tables ----
test("GFM table renders thead and tbody", () => {
  const html = renderMarkdown("| a | b |\n|---|---|\n| 1 | 2 |");
  assert.ok(html.includes("<table><thead><tr><th>a</th><th>b</th></tr></thead>"));
  assert.ok(html.includes("<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"));
});

test("table cells support inline markup and stay escaped", () => {
  const html = renderMarkdown("| x |\n|---|\n| **b** <i>i</i> |");
  assert.ok(html.includes("<td><strong>b</strong> &lt;i&gt;i&lt;/i&gt;</td>"));
});

// ---- links ----
test("http(s) links get target=_blank and rel=noopener", () => {
  const html = renderMarkdown("[docs](https://example.com/a?b=c)");
  assert.ok(html.includes('<a href="https://example.com/a?b=c" target="_blank" rel="noopener">docs</a>'));
});

test("mailto and fragment links are allowed", () => {
  const html = renderMarkdown("[m](mailto:a@b.c) [f](#sec)");
  assert.ok(html.includes('href="mailto:a@b.c"'));
  assert.ok(html.includes('href="#sec"'));
});

test("javascript: links are neutralized to plain text", () => {
  const html = renderMarkdown("[click](javascript:alert(1))");
  assert.ok(!html.includes("<a "));
  assert.ok(html.includes("[click](javascript:alert(1))"));
});

test("data: and scheme-smuggling links are neutralized", () => {
  for (const bad of ["data:text/html,x", "vbscript:x", "java\tscript:alert(1)", "JaVaScRiPt:x"]) {
    const html = renderMarkdown(`[x](${bad})`);
    assert.ok(!html.includes("<a "), `expected no link for ${JSON.stringify(bad)}`);
  }
});

test("link text is escaped", () => {
  const html = renderMarkdown('[<b>x</b>](https://e.com)');
  assert.ok(html.includes(">&lt;b&gt;x&lt;/b&gt;</a>"));
});

// ---- blockquote, hr, paragraphs ----
test("blockquote", () => {
  const html = renderMarkdown("> quoted\n> lines");
  assert.ok(html.includes("<blockquote><p>quoted<br>lines</p></blockquote>"));
});

test("horizontal rule", () => {
  assert.ok(renderMarkdown("---").includes("<hr>"));
  assert.ok(renderMarkdown("***").includes("<hr>"));
});

test("blank lines split paragraphs", () => {
  const html = renderMarkdown("one\n\ntwo");
  assert.equal((html.match(/<p>/g) || []).length, 2);
});

// ---- XSS probes (belt and braces on top of the per-feature checks) ----
test("raw script tag never survives", () => {
  const html = renderMarkdown('<script>alert("xss")</script>');
  assert.ok(!/<script/i.test(html));
  assert.ok(html.includes("&lt;script&gt;"));
});

test("img onerror probe never survives", () => {
  const html = renderMarkdown('hello <img src=x onerror="alert(1)"> world');
  assert.ok(!/<img/i.test(html));
  assert.ok(html.includes("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"));
});

test("attribute breakout via link href quotes is escaped", () => {
  const html = renderMarkdown('[x](https://e.com/"onmouseover="alert(1))');
  assert.ok(!html.includes('" onmouseover="'));
});

test("event handlers cannot appear outside escaped text", () => {
  const html = renderMarkdown('**<svg onload=alert(1)>**');
  assert.ok(!/<svg/i.test(html));
});

// ---- robustness + truncation ----
test("empty and nullish input", () => {
  assert.equal(renderMarkdown(""), "");
  assert.equal(renderMarkdown(null), "");
  assert.equal(renderMarkdown(undefined), "");
});

test("input NULs are stripped so stash placeholders cannot be forged", () => {
  const html = renderMarkdown("a\u00000\u0000b `c`");
  assert.ok(html.includes("<code>c</code>"));
  assert.ok(!html.includes("<code>c</code><code>c</code>"));
});

test("input over 500KB is truncated with a notice", () => {
  const big = "# Title\n\n" + ("lorem ipsum dolor\n".repeat(40000));  // ~720KB
  const html = renderMarkdown(big);
  assert.ok(html.includes("(truncated for preview)"));
  assert.ok(html.includes("<h1>Title</h1>"));
  assert.ok(html.length < 600 * 1024);
});

test("input under the cap has no truncation notice", () => {
  assert.ok(!renderMarkdown("# small doc").includes("(truncated for preview)"));
});

// ---- degradation: unsupported syntax stays escaped plain text ----
test("unsupported syntax degrades to escaped text without throwing", () => {
  const html = renderMarkdown("term\n: definition\n\n[^1]: footnote\n\n~~strike~~ <marquee>x</marquee>");
  assert.ok(html.includes("~~strike~~"));                 // no strikethrough support -> plain text
  assert.ok(html.includes("&lt;marquee&gt;"));            // raw html always escaped
  assert.ok(!/<marquee/i.test(html));
});
