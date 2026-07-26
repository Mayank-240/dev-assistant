// Unit tests for the S8 cookie-session auth helpers in web/static/util.js:
// authGate, loginFormModel, loginErrorMessage.
// Run with: node --test tests/js/
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  authGate, loginFormModel, loginErrorMessage,
} = require("../../src/ai_dev_assistant/web/static/util.js");

// ---- authGate ----

test("authGate gates on login only when auth is required and unauthorized", () => {
  assert.equal(authGate({ auth_required: true, authorized: false }), "login");
  assert.equal(authGate({ auth_required: true, authorized: true }), "app");
  assert.equal(authGate({ auth_required: false, authorized: false }), "app");
  assert.equal(authGate({ auth_required: false, authorized: true }), "app");
});

test("authGate boots the app when status is missing or malformed", () => {
  // unreachable /api/auth/status must never lock the localhost default out
  assert.equal(authGate(null), "app");
  assert.equal(authGate(undefined), "app");
  assert.equal(authGate({}), "app");
});

// ---- loginFormModel ----

test("loginFormModel trims the token and accepts non-empty input", () => {
  const m = loginFormModel("  secret-token  ");
  assert.equal(m.token, "secret-token");
  assert.equal(m.ok, true);
  assert.equal(m.error, "");
});

test("loginFormModel rejects empty and whitespace-only input", () => {
  for (const raw of ["", "   ", "\n\t", null, undefined]) {
    const m = loginFormModel(raw);
    assert.equal(m.ok, false, `raw=${JSON.stringify(raw)}`);
    assert.equal(m.token, "");
    assert.ok(m.error);
  }
});

test("loginFormModel never mangles token content beyond trimming", () => {
  // urlsafe tokens can contain - and _; inner whitespace is preserved verbatim
  assert.equal(loginFormModel("a-b_c~x y").token, "a-b_c~x y");
});

// ---- loginErrorMessage ----

test("loginErrorMessage: 403 gets the wrong-token line", () => {
  const msg = loginErrorMessage(403);
  assert.match(msg, /not accepted/);
});

test("loginErrorMessage: other statuses fall back to a generic line with the code", () => {
  assert.match(loginErrorMessage(500), /500/);
  assert.match(loginErrorMessage(502), /502/);
  assert.doesNotMatch(loginErrorMessage(500), /not accepted/);
});
