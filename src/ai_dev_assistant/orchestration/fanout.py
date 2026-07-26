"""Cross-project fan-out (PLAN F3).

A cross-project task is a meta-task: one ordinary child task per target project,
run in parallel (bounded), each in its own project worktree with its own memory
scope, baseline, and branch — rolled up into a single parent report. Failure
isolation: one child failing never blocks the others. Optional stagger runs the
first child alone so its lessons (written to global memory) inform the rest.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from typing import Any, Callable

from .. import projects
from ..config import Settings
from .events import Event, status
from .run_store import RunStore, derive_title
from .task import new_task_id

logger = logging.getLogger("ada.fanout")

EventFn = Callable[[Event], None]

UPSTREAM_DELIM = "\n\n--- Upstream results ---\n"
UPSTREAM_DIGEST_CHARS = 2000  # per-upstream bound on the injected summary


def validate_project_deps(slugs: list[str], deps: dict[str, list[str]] | None,
                          ) -> dict[str, list[str]]:
    """Normalize + validate a slug -> upstream-slugs mapping against ``slugs``.

    Raises ValueError on unknown slugs, self-dependencies, or cycles. Returns a
    normalized copy (slugified keys/values, empty upstream lists dropped).
    """
    known = set(slugs)
    norm: dict[str, list[str]] = {}
    for key, ups in (deps or {}).items():
        k = projects.slugify(key)
        if k not in known:
            raise ValueError(f"unknown project in dependencies: '{key}' "
                             "(not one of the projects in this run)")
        cleaned: list[str] = []
        for up in ups or []:
            u = projects.slugify(up)
            if u not in known:
                raise ValueError(f"unknown upstream project '{up}' for '{key}' "
                                 "(not one of the projects in this run)")
            if u == k:
                raise ValueError(f"project '{key}' cannot depend on itself")
            if u not in cleaned:
                cleaned.append(u)
        if cleaned:
            norm[k] = cleaned
    return norm


def _dependency_waves(slugs: list[str],
                      deps: dict[str, list[str]]) -> list[list[str]]:
    """Topological waves (Kahn): each wave only depends on earlier waves.

    Preserves the caller's slug order within a wave. Raises ValueError on cycles.
    """
    waves: list[list[str]] = []
    done: set[str] = set()
    remaining = list(slugs)
    while remaining:
        ready = [s for s in remaining
                 if all(u in done for u in deps.get(s, []))]
        if not ready:
            raise ValueError("dependency cycle among projects: "
                             + ", ".join(sorted(remaining)))
        waves.append(ready)
        done.update(ready)
        remaining = [s for s in remaining if s not in done]
    return waves


async def run_cross_project(
    settings: Settings,
    prompt: str,
    slugs: list[str],
    *,
    title: str | None = None,
    stagger: bool = False,
    task_id: str | None = None,
    on_event: EventFn | None = None,
    engine_factory: Callable[[Settings], Any] | None = None,
    deps: dict[str, list[str]] | None = None,
) -> dict:
    """Run ``prompt`` as one child task per project and roll up the results.

    ``engine_factory`` builds the per-child Engine (tests/evals inject scripted
    providers through it); default is the real Engine.

    ``deps`` (optional) maps a project slug to the slugs it depends on. With
    deps, children run in topological waves; a dependent child's prompt gets the
    upstream results appended (slug, status, bounded summary — failures included,
    never silently skipped). Without deps the behavior — concurrency and the
    exact child prompts — is unchanged.
    """
    base_emit: EventFn = on_event or (lambda _e: None)
    parent_id = task_id or new_task_id()
    resolved = [projects.resolve(settings, s) for s in slugs]
    seen: set[str] = set()
    slugs = [s for s in resolved if not (s in seen or seen.add(s))]
    if not slugs:
        raise ValueError("cross-project run needs at least one project")
    # Validate dependencies (unknown slugs / cycles) before any run row exists.
    deps_norm = validate_project_deps(slugs, deps)
    waves = _dependency_waves(slugs, deps_norm) if deps_norm else []

    if engine_factory is None:
        from ..engine import Engine  # late import: engine pulls in the world

        engine_factory = Engine

    store = RunStore(settings.data_dir / "runs.db")
    events_path = settings.docs_dir / parent_id / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    seq = 0

    def emit(e: Event) -> None:
        nonlocal seq
        try:
            with events_path.open("a") as fh:
                fh.write(json.dumps({"seq": seq, **e.to_dict()}, ensure_ascii=False,
                                    default=str) + "\n")
        except Exception:
            pass
        seq += 1
        base_emit(e)

    final_title = (title or "").strip() or derive_title(prompt)
    store.start(parent_id, prompt, title=final_title, project="multi")
    try:
        plan_doc: dict[str, Any] = {"cross_project": slugs}
        if deps_norm:
            plan_doc["deps"] = deps_norm
        store.save_plan(parent_id, json.dumps(plan_doc))
    except Exception:
        pass

    child_ids = {s: new_task_id() for s in slugs}
    per_child_budget = (settings.budget_usd / len(slugs)) if settings.budget_usd else 0.0
    emit(Event("plan", f"Cross-project fan-out: {len(slugs)} project(s).", {
        "summary": f"Run the task in {len(slugs)} project(s) and roll up the results.",
        "children": [{"slug": s, "task_id": child_ids[s]} for s in slugs],
        "subtasks": [],
    }))

    results: dict[str, dict] = {}
    digests: dict[str, str] = {}  # slug -> bounded summary for downstream injection

    async def run_child(slug: str, *, child_prompt: str | None = None,
                        wave: int | None = None) -> dict:
        cid = child_ids[slug]
        extra = {} if wave is None else {"wave": wave}
        child_settings = projects.effective_settings(
            dataclasses.replace(settings, project=slug, budget_usd=per_child_budget), slug)
        emit(Event("child_start", f"[{slug}] child task {cid} started.",
                   {"slug": slug, "task_id": cid, **extra}))
        engine = engine_factory(child_settings)
        try:
            run, _brief, _out = await engine.run(
                prompt if child_prompt is None else child_prompt,
                task_id=cid, title=f"{final_title} [{slug}]")
            row = engine.runs.get(cid) or {}
            info = {"slug": slug, "task_id": cid,
                    "status": row.get("status") or run.rollup_status(),
                    "quality": row.get("quality_score"),
                    "cost_usd": float(row.get("cost_usd") or 0.0),
                    "branch": row.get("task_branch") or "",
                    "review_target": row.get("review_target") or "", "error": ""}
            digests[slug] = str(row.get("summary")
                                or getattr(_brief, "tldr", "") or "").strip()
        except Exception as exc:  # failure isolation: one child never sinks the rest
            logger.warning("child %s (%s) failed: %s", cid, slug, exc)
            info = {"slug": slug, "task_id": cid, "status": "failed", "quality": None,
                    "cost_usd": 0.0, "branch": "", "review_target": "",
                    "error": str(exc)[:300]}
        finally:
            try:
                await engine.aclose()
            except Exception:
                pass
        try:
            store.set_parent(cid, parent_id)  # after the child's own start() row exists
        except Exception:
            pass
        results[slug] = info
        emit(Event("child_done",
                   f"[{slug}] {info['status']}" + (f" — {info['error']}" if info["error"] else ""),
                   {**info, **extra}))
        return info

    def upstream_section(ups: list[str]) -> str:
        """The clearly-delimited context block appended to a dependent's prompt."""
        parts = []
        for u in ups:
            r = results.get(u) or {}
            st = r.get("status") or "unknown"
            entry = f"[{u}] status: {st}"
            if st == "failed":
                entry += "\nerror: " + (r.get("error") or "unknown error")
            digest = (digests.get(u) or "").strip()[:UPSTREAM_DIGEST_CHARS]
            if digest:
                entry += "\n" + digest
            parts.append(entry)
        return UPSTREAM_DELIM + "\n\n".join(parts)

    bound = max(1, settings.max_concurrent_runs)
    sem = asyncio.Semaphore(bound)

    async def bounded(slug: str, *, child_prompt: str | None = None,
                      wave: int | None = None) -> dict:
        async with sem:
            return await run_child(slug, child_prompt=child_prompt, wave=wave)

    try:
        if deps_norm:
            emit(status("Dependency order: "
                        + " -> ".join("[" + ", ".join(w) + "]" for w in waves)))
            for wi, wave_slugs in enumerate(waves, start=1):
                prompts: dict[str, str] = {}
                for s in wave_slugs:
                    ups = deps_norm.get(s) or []
                    if not ups:
                        prompts[s] = prompt
                        continue
                    failed_ups = [u for u in ups
                                  if (results.get(u) or {}).get("status") == "failed"]
                    if failed_ups:
                        emit(status(f"[{s}] upstream failure(s): "
                                    + ", ".join(failed_ups)
                                    + " — running anyway with the failure noted "
                                      "in its context."))
                    prompts[s] = prompt + upstream_section(ups)
                if stagger and wi == 1 and len(wave_slugs) > 1:
                    emit(status(f"Stagger: running '{wave_slugs[0]}' first so its "
                                "lessons inform the rest."))
                    await run_child(wave_slugs[0],
                                    child_prompt=prompts[wave_slugs[0]], wave=wi)
                    await asyncio.gather(*(bounded(s, child_prompt=prompts[s], wave=wi)
                                           for s in wave_slugs[1:]))
                else:
                    await asyncio.gather(*(bounded(s, child_prompt=prompts[s], wave=wi)
                                           for s in wave_slugs))
        elif stagger and len(slugs) > 1:
            emit(status(f"Stagger: running '{slugs[0]}' first so its lessons inform the rest."))
            await run_child(slugs[0])
            await asyncio.gather(*(bounded(s) for s in slugs[1:]))
        else:
            await asyncio.gather(*(bounded(s) for s in slugs))

        ok = sum(1 for r in results.values() if r["status"] == "completed")
        soft = sum(1 for r in results.values()
                   if r["status"] not in ("completed", "failed"))
        overall = ("completed" if ok == len(slugs)
                   else "failed" if (ok + soft) == 0 else "partial")
        total_cost = sum(r.get("cost_usd") or 0.0 for r in results.values())

        lines = [f"# Cross-project rollup — {parent_id}", "", f"**Task:** {prompt}", ""]
        if deps_norm:
            lines += ["**Dependency order:** "
                      + " -> ".join("[" + ", ".join(w) + "]" for w in waves), ""]
        lines += ["| project | status | quality | cost | branch |",
                  "|---|---|---|---|---|"]
        for s in slugs:
            r = results[s]
            q = r.get("quality")
            lines.append(f"| {s} | {r['status']} | {q if q is not None else '—'} | "
                         f"${r.get('cost_usd') or 0.0:.2f} | {r.get('branch') or '—'} |")
        (settings.docs_dir / parent_id).mkdir(parents=True, exist_ok=True)
        (settings.docs_dir / parent_id / "rollup.md").write_text("\n".join(lines) + "\n")

        tldr = (f"{ok}/{len(slugs)} project(s) completed (total ${total_cost:.2f}): "
                + "; ".join(f"{s}: {results[s]['status']}" for s in slugs))
        store.finish(parent_id, status=overall, run_status=overall,
                     subtasks_total=len(slugs), subtasks_passed=ok,
                     summary=tldr, cost_usd=total_cost)
        emit(Event("brief", tldr, {
            "tldr": tldr, "status": overall,
            "key_points": [f"{s}: {results[s]['status']}"
                           + (f" → {results[s]['branch']}" if results[s]["branch"] else "")
                           for s in slugs],
        }))
        emit(Event("done", "Cross-project run complete.", {
            "task_id": parent_id, "passed": ok, "total": len(slugs),
            "docs_dir": str(settings.docs_dir / parent_id), "over_budget": False,
            "run_status": overall, "cost_usd": total_cost,
            "children": [results[s] for s in slugs],
        }))
        return {"parent_id": parent_id, "status": overall,
                "children": [results[s] for s in slugs],
                "docs_dir": str(settings.docs_dir / parent_id)}
    except asyncio.CancelledError:
        store.set_status(parent_id, "cancelled")
        raise
    finally:
        try:
            cur = store.get(parent_id) or {}
            if cur.get("status") in (None, "running"):
                store.set_status(parent_id, "failed")
        finally:
            store.close()
