"""Playbooks — parameterized task templates that turn one click + a few params
into a reliable, pre-tuned run.

A Playbook bundles a carefully written prompt template, the parameters it
needs, and the Settings overrides that make the run behave well for that kind
of task (effort, budget, retries...). ``render()`` validates the caller's
params and produces a payload shaped for the existing run pipeline:

    {"prompt": <str>, "title": <str>, "settings_overrides": <dict>}

``settings_overrides`` keys are real ``Settings`` field names, so the server
can apply them exactly like its other per-run overrides (dataclasses.replace),
layered under any explicit request choices. ``catalog()`` is JSON-safe and
ready to back a future ``GET /api/playbooks``.

This module has no dependencies on the rest of the package and edits nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from string import Formatter
from typing import Any

__all__ = ["Playbook", "PLAYBOOKS", "get_playbook", "render", "catalog"]

_PARAM_TYPES = ("str", "int", "choice")


def _template_fields(template: str) -> set[str]:
    """Root field names referenced by a str.format template."""
    fields: set[str] = set()
    for _, name, _, _ in Formatter().parse(template):
        if name is None:
            continue
        root = name.split(".")[0].split("[")[0]
        fields.add(root)  # "" for positional {} — caught by the caller
    return fields


@dataclass(frozen=True)
class Playbook:
    id: str
    name: str
    description: str
    # Each param: {key, label, type: "str"|"int"|"choice", choices?, default?, required?}
    params: tuple[dict, ...]
    prompt_template: str          # .format(**params)
    settings_overrides: dict = field(default_factory=dict)  # Settings field -> value
    suggested_title: str = ""     # also a .format template

    def __post_init__(self) -> None:
        declared: set[str] = set()
        for spec in self.params:
            key = spec.get("key")
            if not key or not isinstance(key, str):
                raise ValueError(f"playbook '{self.id}': param without a 'key': {spec!r}")
            if key in declared:
                raise ValueError(f"playbook '{self.id}': duplicate param key '{key}'")
            ptype = spec.get("type")
            if ptype not in _PARAM_TYPES:
                raise ValueError(
                    f"playbook '{self.id}': param '{key}' has invalid type {ptype!r} "
                    f"(expected one of {_PARAM_TYPES})")
            if ptype == "choice" and not spec.get("choices"):
                raise ValueError(f"playbook '{self.id}': choice param '{key}' has no 'choices'")
            declared.add(key)
        for label, template in (("prompt_template", self.prompt_template),
                                ("suggested_title", self.suggested_title)):
            undeclared = _template_fields(template) - declared
            if undeclared:
                names = ", ".join(sorted(repr(n) for n in undeclared))
                raise ValueError(
                    f"playbook '{self.id}': {label} references undeclared placeholder(s): {names}")


# ---------------------------------------------------------------------------
# Param validation / coercion
# ---------------------------------------------------------------------------

def _coerce(pid: str, spec: dict, raw: Any) -> Any:
    key, ptype = spec["key"], spec["type"]
    if ptype == "int":
        # bool is an int subclass — reject it explicitly.
        if isinstance(raw, bool):
            raise ValueError(f"playbook '{pid}': param '{key}' must be an integer, got {raw!r}")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, float) and raw.is_integer():
            return int(raw)
        if isinstance(raw, str):
            try:
                return int(raw.strip())
            except ValueError:
                pass
        raise ValueError(
            f"playbook '{pid}': param '{key}' must be an integer, got {raw!r}")
    if not isinstance(raw, str):
        raise ValueError(
            f"playbook '{pid}': param '{key}' must be a string, got {type(raw).__name__}")
    value = raw.strip()
    if ptype == "choice":
        choices = tuple(spec.get("choices") or ())
        if value not in choices:
            raise ValueError(
                f"playbook '{pid}': param '{key}' must be one of {list(choices)}, got {raw!r}")
    return value


def _resolve_params(pb: Playbook, params: dict) -> dict[str, Any]:
    params = dict(params or {})
    declared = {spec["key"] for spec in pb.params}
    unknown = sorted(set(params) - declared)
    if unknown:
        raise ValueError(
            f"playbook '{pb.id}': unknown param(s) {unknown}; expected {sorted(declared)}")
    values: dict[str, Any] = {}
    for spec in pb.params:
        key = spec["key"]
        raw = params.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if "default" in spec:
                raw = spec["default"]
            elif spec.get("required", True):
                raise ValueError(
                    f"playbook '{pb.id}': missing required param '{key}' ({spec.get('label', key)})")
            else:
                raw = ""
        values[key] = _coerce(pb.id, spec, raw)
    return values


# ---------------------------------------------------------------------------
# The playbooks. The prompts are the product: each states the deliverable,
# gives the orchestrator explicit acceptance-criteria guidance, and sets
# guardrails the reviewer can hold agents to.
# ---------------------------------------------------------------------------

_RAISE_COVERAGE = Playbook(
    id="raise-coverage",
    name="Raise test coverage",
    description="Write meaningful tests for uncovered behavior in a module or "
                "path until a coverage target is met.",
    params=(
        {"key": "path", "label": "Module or path to cover", "type": "str", "required": True},
        {"key": "target_percent", "label": "Coverage target (percent)", "type": "int",
         "default": 85, "required": False},
    ),
    prompt_template="""\
Raise test coverage for {path} to at least {target_percent} percent line coverage.

Deliverable: new or extended tests that pin down the currently-untested behavior of {path}, \
plus a short summary of before/after coverage and any lines that remain uncovered with a \
one-line justification each.

Method:
1. Measure current coverage for {path} first and list the uncovered functions and branches.
2. Read each uncovered region and write tests that assert its observable contract: return \
values, raised exceptions, boundary and error paths, and side effects callers rely on. \
Prioritize branches with real behavioral consequences over trivial lines.
3. Re-run coverage and iterate until the target is met or every remaining uncovered line is \
genuinely untestable from the public surface (document those).

Acceptance criteria (plan against these):
- Line coverage for {path} is at or above {target_percent} percent, or the final report \
justifies each residual uncovered line.
- Every new test asserts specific behavior (values, exception types, state changes); a test \
that only calls code and asserts nothing, or asserts on incidental formatting, does not count.
- The full pre-existing test suite still passes.

Guardrails:
- No snapshot spam: never bulk-capture current output as the expected value; each assertion \
must express intended behavior a maintainer would defend.
- Do not modify the code under test to make it easier to cover. Exception: if a test exposes \
a genuine bug, fix it minimally and add a regression test that names the bug.
- Do not delete, skip, or weaken any existing test.
- Do not touch files outside {path} and the test tree.""",
    settings_overrides={"agent_effort": "high", "verify_run_tests": True},
    suggested_title="Raise coverage of {path} to {target_percent}%",
)

_UPGRADE_DEPENDENCY = Playbook(
    id="upgrade-dependency",
    name="Upgrade a dependency",
    description="Upgrade one package to a target version, fix all resulting "
                "breakage, and keep the test suite green throughout.",
    params=(
        {"key": "package", "label": "Package name", "type": "str", "required": True},
        {"key": "target_version", "label": "Target version", "type": "str",
         "default": "latest", "required": False},
    ),
    prompt_template="""\
Upgrade the dependency {package} to version {target_version} and leave the project fully \
working on the new version.

Deliverable: updated dependency pins (manifest and lockfile if present), all call sites \
migrated off removed or changed APIs, and a green test suite. Include a short migration note \
listing each breaking change encountered and how it was resolved.

Method:
1. Establish a baseline: run the test suite before changing anything and record any \
pre-existing failures — you are not responsible for those, but you must not add to them.
2. Read the changelog or release notes of {package} between the current and target versions; \
list the breaking changes that apply to this codebase before editing code.
3. Bump the pin, then fix breakage in dependency order: imports and constructors first, then \
behavior changes, then deprecation warnings that the new version turns into errors.
4. Re-run the suite after each fix cluster; finish with a full clean run.

Acceptance criteria (plan against these):
- {package} resolves to {target_version} in the dependency manifest (and lockfile if the \
project has one).
- The full test suite passes, minus only failures documented in the pre-upgrade baseline.
- No compatibility shims that pin old behavior forever (no vendored copies of the old \
package, no broad warning suppressions).

Guardrails:
- Change only what the upgrade forces: the dependency files and the call sites the new \
version breaks. Do not refactor, reformat, or upgrade other packages opportunistically \
unless the upgrade strictly requires a peer bump — document any such forced bump.
- Never delete or weaken a failing test to get green; fix the code under test instead. If a \
test asserts obsolete behavior of {package} itself, update it to assert the new documented \
behavior and say so in the migration note.""",
    settings_overrides={"agent_effort": "high", "max_retries": 2},
    suggested_title="Upgrade {package} to {target_version}",
)

_SECURITY_AUDIT = Playbook(
    id="security-audit",
    name="Security audit",
    description="Audit a path for concrete vulnerabilities (injection, path "
                "traversal, secrets, authz), report findings, and fix the "
                "clear-cut ones.",
    params=(
        {"key": "scope", "label": "Path to audit", "type": "str",
         "default": ".", "required": False},
        {"key": "mode", "label": "Mode", "type": "choice",
         "choices": ["audit-and-fix", "audit-only"],
         "default": "audit-and-fix", "required": False},
    ),
    prompt_template="""\
Perform a security audit of {scope}. Mode: {mode}. Route the analysis work to the most \
security-capable reviewer/auditor agents available; this is a read-heavy task and reading \
carefully is most of the job.

Deliverable: a findings report, one entry per finding, each with: file and line, \
vulnerability class, severity (critical/high/medium/low), a concrete exploit sketch (how an \
attacker reaches it — not a hypothetical), and the recommended fix. In audit-and-fix mode, \
additionally apply fixes for the clear-cut findings; in audit-only mode, change no code and \
deliver the report alone.

Hunt for concrete, reachable vulnerabilities in these classes, in priority order:
1. Injection: SQL/command/template injection, shell invocation with untrusted input, unsafe \
eval or deserialization (pickle, yaml.load, etc.).
2. Path traversal: user-influenced paths that escape an intended root; missing resolve-and- \
verify checks before file reads/writes.
3. Secrets: hardcoded credentials, tokens, or keys in source, config, or test fixtures; \
secrets leaking into logs or error messages.
4. Broken authorization: endpoints or operations that skip permission checks, confuse \
authentication with authorization, or trust client-supplied identity.
5. SSRF and unsafe network fetches of user-supplied URLs.

Acceptance criteria (plan against these):
- Every finding cites a real file and line in {scope} and explains attacker reachability; \
speculative or style-only findings are explicitly labeled as low-confidence, not mixed in.
- A finding of "no issues" in a class is backed by naming the places checked.
- In audit-and-fix mode: each applied fix is minimal, targeted at one finding, and the test \
suite still passes; findings that need design decisions are reported, not half-fixed.

Guardrails:
- Fix only clear-cut findings (e.g. parameterize a query, resolve-and-check a path, move a \
secret to configuration). Anything requiring an architecture change is report-only.
- Do not refactor, rename, or reformat beyond the minimal fix. Do not touch files outside \
{scope} except a shared configuration file a secret-removal fix requires.
- Never commit or echo real secret values into the report; redact them and reference the \
location only.""",
    settings_overrides={"agent_effort": "high", "reviewer_effort": "xhigh"},
    suggested_title="Security audit of {scope}",
)

_REFACTOR_MODULE = Playbook(
    id="refactor-module",
    name="Refactor a module",
    description="Behavior-preserving refactor of a path toward a stated goal; "
                "tests stay green and no public API breaks.",
    params=(
        {"key": "path", "label": "Module or path to refactor", "type": "str", "required": True},
        {"key": "goal", "label": "Refactoring goal", "type": "str", "required": True},
    ),
    prompt_template="""\
Refactor {path} with this goal: {goal}. This is strictly behavior-preserving: after the \
refactor, every caller and every test observes identical behavior.

Deliverable: the refactored code, an unchanged-or-stronger passing test suite, and a short \
note mapping old structure to new (what moved where and why it serves the goal).

Method:
1. Run the test suite first and record the baseline; if coverage of {path} is too thin to \
protect the refactor, add characterization tests for the current behavior BEFORE changing \
any code.
2. Inventory the public surface of {path}: exported names, function signatures, return \
types, raised exceptions, and side effects that external callers or tests rely on. This \
surface is frozen.
3. Refactor in small, independently green steps toward the goal; run the affected tests \
after each step, and the full suite at the end.

Acceptance criteria (plan against these):
- The full test suite passes with the same set of tests (plus any new characterization \
tests) — no test was deleted, skipped, or loosened to get there.
- The public API of {path} is intact: no renamed/removed exports, no changed signatures, \
return types, or exception types visible to callers.
- The stated goal is demonstrably met, and the mapping note explains how.

Guardrails:
- Do not change observable behavior, including error messages that callers or tests match on.
- Do not touch files outside {path} except mechanical import-path updates forced by the \
refactor — and only those lines.
- No drive-by changes: no reformatting untouched code, no dependency changes, no fixing \
unrelated issues (report them instead).""",
    settings_overrides={"agent_effort": "high", "reviewer_effort": "high"},
    suggested_title="Refactor {path}: {goal}",
)

_DOCUMENT_CODEBASE = Playbook(
    id="document-codebase",
    name="Document the codebase",
    description="Write READMEs, docstrings, and architecture notes grounded in "
                "what the code actually does.",
    params=(
        {"key": "scope", "label": "Path to document", "type": "str",
         "default": ".", "required": False},
    ),
    prompt_template="""\
Document {scope} so a competent developer new to it can navigate, extend, and safely change \
the code. Every statement must be grounded in the actual code — read before you write.

Deliverable, in priority order:
1. A README (or updates to the existing one) for {scope}: what it does, how the pieces fit, \
how to run and test it, and the two or three entry points a newcomer should read first.
2. Docstrings for the public API — every exported module, class, and function that lacks \
one, covering purpose, non-obvious parameters, return values, and raised exceptions. Skip \
private helpers whose names already say everything.
3. An architecture note where the design is non-obvious: key data flows, invariants the code \
maintains, and the reasoning behind non-obvious choices that the code itself reveals.

Acceptance criteria (plan against these):
- Spot-checking any documented claim against the code confirms it: names, signatures, \
behavior, and file paths in the docs match reality exactly.
- Docs explain WHY where the code only shows HOW; no docstring merely restates the function \
name.
- The docs build/render if the project has a docs toolchain, and the test suite is untouched \
and still green.

Guardrails:
- Change documentation only: markdown files, docstrings, and comments. Zero behavior \
changes — no code edits, however tempting; note code smells you find in the report instead.
- Never invent behavior, options, or roadmap items the code does not contain; if something \
is unclear, document it as an open question rather than guessing.
- Match the project's existing doc style and structure; extend it, do not replace it.""",
    settings_overrides={"agent_effort": "medium", "budget_usd": 3.0},
    suggested_title="Document {scope}",
)

_FIX_FAILING_TESTS = Playbook(
    id="fix-failing-tests",
    name="Fix failing tests",
    description="Diagnose and fix failing tests by repairing the code under "
                "test — never by deleting or weakening the tests.",
    params=(
        {"key": "test_filter", "label": "Test filter (optional)", "type": "str",
         "default": "(none - run the whole suite)", "required": False},
    ),
    prompt_template="""\
Find and fix the failing tests. Test selection filter: {test_filter}.

Deliverable: a green test run for the selected scope, plus a per-failure summary: the \
failing test, the root cause in the code, and the fix applied.

Method:
1. Run the selected tests and capture every distinct failure; group failures that share a \
root cause.
2. For each group, diagnose before editing: read the test to learn the intended contract, \
then read the code under test to find where reality diverges. State the root cause in one \
sentence before writing the fix.
3. Fix the code under test, re-run the group, then re-run the full selected scope to catch \
regressions between fixes.

Acceptance criteria (plan against these):
- All tests in the selected scope pass, and no previously-passing test broke.
- Each fix addresses a stated root cause in the production code; the summary makes the \
cause-to-fix link explicit.
- The test files' assertions are byte-for-byte as strong as before.

Guardrails:
- Never delete, skip, xfail, or comment out a failing test, and never weaken an assertion \
(no widening tolerances, no replacing equality with containment) to force green.
- The only permissible test edit is repairing a test that is itself objectively broken — \
wrong fixture path, use of a removed API in test setup — and each such edit must be called \
out and justified in the summary.
- Keep fixes minimal and targeted; do not refactor or reformat surrounding code.
- If a failure is environmental (missing service, network, platform), report it as such \
with evidence rather than papering over it in code.""",
    settings_overrides={"agent_effort": "high", "verify_run_tests": True},
    suggested_title="Fix failing tests",
)

_ADD_FEATURE_TDD = Playbook(
    id="add-feature-tdd",
    name="Add a feature (TDD)",
    description="Test-driven feature work: write failing tests that specify the "
                "feature first, then implement to green.",
    params=(
        {"key": "feature", "label": "Feature description", "type": "str", "required": True},
    ),
    prompt_template="""\
Implement this feature test-first: {feature}

Deliverable: a test suite that specifies the feature's behavior, written and confirmed \
failing BEFORE implementation, followed by the minimal implementation that turns it green — \
with the rest of the suite green throughout.

Method (the order is the point — plan subtasks so tests strictly precede implementation):
1. Turn the feature description into concrete, testable behaviors: the happy path, edge \
cases (empty/boundary/invalid input), and error behavior. If the description is ambiguous, \
choose the most conventional interpretation and record the decision.
2. Write the tests for those behaviors and RUN them: they must fail for the right reason \
(missing behavior, not import errors or typos). Fix the tests until the failures are \
meaningful.
3. Implement the simplest code that makes the tests pass, working test by test. Do not \
build beyond what a test demands.
4. With everything green, do one small cleanup pass (names, duplication) keeping the suite \
green.

Acceptance criteria (plan against these):
- The tests demonstrably preceded the implementation and initially failed for behavioral \
reasons; the report shows the red-then-green sequence.
- The feature's happy path, edge cases, and error behavior each have at least one dedicated \
test with specific assertions.
- The new tests and the entire pre-existing suite pass; no existing test was modified to \
accommodate the feature unless the feature explicitly changes that contract (justify each).

Guardrails:
- No implementation before its test exists and fails — scaffolding (empty modules, \
signatures that raise NotImplementedError) is allowed so tests can import.
- Do not weaken new tests to make implementation easier; strengthen the implementation \
instead.
- Touch only the files the feature requires; no opportunistic refactoring of unrelated code.""",
    settings_overrides={"agent_effort": "high", "orchestrator_effort": "high"},
    suggested_title="Feature (TDD): {feature}",
)


PLAYBOOKS: list[Playbook] = [
    _RAISE_COVERAGE,
    _UPGRADE_DEPENDENCY,
    _SECURITY_AUDIT,
    _REFACTOR_MODULE,
    _DOCUMENT_CODEBASE,
    _FIX_FAILING_TESTS,
    _ADD_FEATURE_TDD,
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_playbook(pid: str) -> Playbook | None:
    """The playbook with id ``pid``, or None."""
    return next((pb for pb in PLAYBOOKS if pb.id == pid), None)


def render(pid: str, params: dict) -> dict:
    """Validate ``params`` against the playbook and produce a run payload.

    Returns {"prompt", "title", "settings_overrides"} ready for the run
    pipeline. Raises ValueError with a helpful message on an unknown playbook,
    a missing required param, a bad type, or an out-of-set choice.
    """
    pb = get_playbook(pid)
    if pb is None:
        known = ", ".join(p.id for p in PLAYBOOKS)
        raise ValueError(f"unknown playbook: {pid!r} (known: {known})")
    values = _resolve_params(pb, params)
    return {
        "prompt": pb.prompt_template.format(**values),
        "title": (pb.suggested_title or pb.name).format(**values),
        "settings_overrides": dict(pb.settings_overrides),
    }


def catalog() -> list[dict]:
    """JSON-safe playbook listing for a future GET /api/playbooks."""
    return [
        {
            "id": pb.id,
            "name": pb.name,
            "description": pb.description,
            "params": [dict(spec) for spec in pb.params],
            "settings_overrides": dict(pb.settings_overrides),
        }
        for pb in PLAYBOOKS
    ]
