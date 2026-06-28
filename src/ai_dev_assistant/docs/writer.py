"""Per-task documentation: a full plan, a full report, and a one-glance brief.

Also maintains docs/INDEX.md — the at-a-glance list of every task and its TL;DR.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..execution import ExecutionResult
from ..llm.schemas import BriefDoc
from ..orchestration.message_bus import Message
from ..orchestration.task import SubTaskState, TaskRun


def _plan_md(run: TaskRun) -> str:
    lines = [f"# Plan — {run.id}", "", f"**Task:** {run.prompt}", "", "## Approach", "", run.plan.summary, "", "## Subtasks", ""]
    for st in run.plan.subtasks:
        deps = ", ".join(st.depends_on) or "—"
        lines += [
            f"### {st.id}: {st.title}",
            f"- **Agent:** {st.agent}",
            f"- **Why this agent:** {st.rationale}",
            f"- **Depends on:** {deps}",
            "- **Acceptance criteria:**",
            *([f"  - {c}" for c in st.acceptance_criteria] or ["  - (none)"]),
            "",
        ]
    return "\n".join(lines)


def _report_md(run: TaskRun, messages: list[Message], execution: ExecutionResult | None) -> str:
    lines = [f"# Report — {run.id}", "", f"**Task:** {run.prompt}", ""]
    passed = sum(1 for s in run.subtasks.values() if s.status.value == "passed")
    lines += [f"**Outcome:** {passed}/{len(run.subtasks)} subtasks passed.", ""]

    if execution is not None:
        status = "PASSED ✅" if execution.passed else ("TIMED OUT ⏱" if execution.timed_out else "FAILED ❌")
        lines += [
            "## Test execution", "",
            f"- **Command:** `{execution.command}`",
            f"- **Exit code:** {execution.return_code} — **{status}**",
            f"- **Duration:** {execution.duration:.1f}s", "",
            "```", (execution.stdout or "").strip()[-3000:] or "(no stdout)", "```", "",
        ]
        if execution.stderr:
            lines += ["**stderr:**", "```", execution.stderr.strip()[-1500:], "```", ""]

    lines += ["## Subtask results", ""]
    for st in run.subtasks.values():
        lines += [
            f"### {st.id}: {st.spec.title}  —  `{st.status.value}`",
            f"- **Agent:** {st.agent}  |  **Attempts:** {st.attempts}",
        ]
        if st.verdict:
            lines.append(f"- **Verdict:** {'PASS' if st.verdict.passed else 'FAIL'} (score {st.verdict.score})")
            if st.verdict.reasons:
                lines += ["- **Reasons:**", *[f"  - {r}" for r in st.verdict.reasons]]
            if st.verdict.suggestions and not st.verdict.passed:
                lines += ["- **Suggestions:**", *[f"  - {s}" for s in st.verdict.suggestions]]
        if st.error:
            lines.append(f"- **Error:** {st.error}")
        lines += ["", "**Result:**", "", st.result or "_(no result)_", ""]

    lines += ["## Inter-agent messages", ""]
    if messages:
        for m in messages:
            to = m.recipient or "all"
            lines.append(f"- `{m.sender} → {to}`: {m.content}")
    else:
        lines.append("_(none recorded)_")
    lines.append("")
    return "\n".join(lines)


def _brief_md(run: TaskRun, brief: BriefDoc) -> str:
    lines = [
        f"# Brief — {run.id}",
        "",
        f"**Task:** {run.prompt}",
        f"**Status:** {brief.status}",
        "",
        "## TL;DR",
        "",
        brief.tldr,
        "",
        "## Key points",
        "",
        *[f"- {p}" for p in brief.key_points],
        "",
    ]
    return "\n".join(lines)


def write_task_docs(
    settings: Settings,
    run: TaskRun,
    brief: BriefDoc,
    messages: list[Message],
    execution: ExecutionResult | None = None,
    activity: dict[str, list[dict[str, Any]]] | None = None,
) -> Path:
    out_dir = settings.docs_dir / run.id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.md").write_text(_plan_md(run))
    (out_dir / "report.md").write_text(_report_md(run, messages, execution))
    (out_dir / "brief.md").write_text(_brief_md(run, brief))
    # Full per-agent activity log (the complete step stream), for the detail popup.
    (out_dir / "activity.json").write_text(json.dumps(activity or {}, ensure_ascii=False))
    _append_index(settings, run, brief)
    return out_dir


def _append_index(settings: Settings, run: TaskRun, brief: BriefDoc) -> None:
    index = settings.docs_dir / "INDEX.md"
    header = "# Task Index\n\nAt-a-glance list of every task. Click a brief for the quick story.\n"
    if not index.exists():
        index.write_text(header + "\n")
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(run.created_at))
    passed = sum(1 for s in run.subtasks.values() if s.status.value == "passed")
    line = (
        f"- **[{run.id}]({run.id}/brief.md)** ({stamp}, {passed}/{len(run.subtasks)} ok) — "
        f"{brief.tldr}\n"
    )
    with index.open("a") as fh:
        fh.write(line)
