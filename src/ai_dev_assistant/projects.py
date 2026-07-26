"""Project registry — first-class projects that own a repo checkout, indexing state, and policy.

Each project owns its own memory.db and knowledge_graph.json (under
data_dir/projects/<slug>/), plus a durable repo checkout:

- greenfield: ``workspace_dir/<slug>/repo`` (git init + empty initial commit);
- git URL import: a clone under ``workspace_dir/<slug>/repo``;
- local import: the user's own directory, operated on IN PLACE — this module
  never mutates it (no commits, no branch switches, no writes; git queries are
  read-only). See docs/PLAN.md decision #2 for the safety contract.

Metadata lives in the JSON registry at settings.registry_path. Legacy thin
entries ({slug, name, created_at}) still load; they are normalized on read.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .knowledge.extract import parse_js_outline, parse_python_outline
from .knowledge.repo_map import onboard

logger = logging.getLogger("ada.projects")

DEFAULT_PROJECT = "default"

_GIT_IDENT = ["-c", "user.email=ada@local", "-c", "user.name=AI Dev Assistant"]
_SKIP_PARTS = {"__pycache__", ".git", "node_modules", ".venv", "dist", "build",
               ".pytest_cache", ".ada_data", ".ada_worktrees", ".ada_deps"}
_SOURCE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".c", ".cpp", ".h"}
_JS_EXT = {".js", ".ts", ".tsx", ".jsx"}

# Defaults applied when normalizing registry entries (legacy thin entries included).
_ENTRY_DEFAULTS: dict[str, Any] = {
    "origin": "greenfield",   # "greenfield" | "local" | "git"
    "source": "",
    "root": "",               # absolute path of the project's repo checkout ("" = none)
    "default_branch": "",
    "archived": False,
    "last_indexed_commit": "",
}


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:48] or "project"


# ---------------------------------------------------------------------------
# Git helpers (read-only unless explicitly provisioning OUR OWN checkout)
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def _git_out(args: list[str], cwd: Path) -> str:
    """stdout of a read-only git query, or "" on any failure."""
    try:
        res = _git(args, cwd)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def _is_git_repo(path: Path) -> bool:
    try:
        return _git(["rev-parse", "--git-dir"], path).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _looks_like_git_url(source: str) -> bool:
    return "://" in source or source.startswith("git@") or source.endswith(".git")


# ---------------------------------------------------------------------------
# Registry (JSON file at settings.registry_path)
# ---------------------------------------------------------------------------

def _normalize(entry: dict[str, Any]) -> dict[str, Any]:
    """A full rich entry from a possibly-thin/partial registry record."""
    out = dict(entry)
    out.setdefault("slug", slugify(str(entry.get("name", ""))))
    out.setdefault("name", out["slug"])
    out.setdefault("created_at", 0.0)
    for key, default in _ENTRY_DEFAULTS.items():
        out.setdefault(key, default)
    if not isinstance(out.get("policy"), dict):
        out["policy"] = {}
    else:
        out["policy"] = dict(out["policy"])
    return out


def _read_registry(settings: Settings) -> list[dict[str, Any]]:
    path = settings.registry_path
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_registry(settings: Settings, items: list[dict[str, Any]]) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.registry_path.write_text(json.dumps(items, ensure_ascii=False, indent=2))


def list_projects(settings: Settings) -> list[dict[str, Any]]:
    """All known projects (rich, normalized dicts), default-first.

    Ensures the 'default' project always exists. The default is the zero-setup
    scratch project: it gets NO checkout (root "").
    """
    items = [_normalize(p) for p in _read_registry(settings)]
    if not any(p.get("slug") == DEFAULT_PROJECT for p in items):
        items.insert(0, _normalize({"slug": DEFAULT_PROJECT, "name": "Default", "created_at": time.time()}))
        _write_registry(settings, items)
    return items


def get_project(settings: Settings, slug: str) -> dict[str, Any] | None:
    """The normalized registry entry for ``slug``, or None."""
    return next((p for p in list_projects(settings) if p.get("slug") == slug), None)


def _update_entry(settings: Settings, slug: str, **changes: Any) -> dict[str, Any]:
    items = list_projects(settings)
    for i, p in enumerate(items):
        if p.get("slug") == slug:
            items[i] = {**p, **changes}
            _write_registry(settings, items)
            return items[i]
    raise ValueError(f"unknown project: {slug}")


def _register(settings: Settings, entry: dict[str, Any]) -> dict[str, Any]:
    items = list_projects(settings)
    if any(p.get("slug") == entry["slug"] for p in items):
        raise ValueError(f"project '{entry['slug']}' already exists")
    items.append(entry)
    _write_registry(settings, items)
    (settings.projects_dir / entry["slug"]).mkdir(parents=True, exist_ok=True)
    return entry


# ---------------------------------------------------------------------------
# Lifecycle: create / import
# ---------------------------------------------------------------------------

def create_project(settings: Settings, name: str) -> dict[str, Any]:
    """Create (or return existing) greenfield project by name.

    Provisions the data dir and ``workspace_dir/<slug>/repo`` with ``git init``
    plus an empty initial commit; the entry's root points there. The 'default'
    scratch project never gets a checkout.
    """
    slug = slugify(name)
    existing = get_project(settings, slug)
    if existing:
        return existing
    entry = _normalize({"slug": slug, "name": name.strip() or slug, "created_at": time.time()})
    if slug != DEFAULT_PROJECT:
        repo = (settings.workspace_dir / slug / "repo").resolve()
        repo.mkdir(parents=True, exist_ok=True)
        res = _git(["init", "-q"], repo)
        if res.returncode != 0:
            raise RuntimeError(f"git init failed: {(res.stderr or res.stdout).strip()[:300]}")
        res = _git([*_GIT_IDENT, "commit", "-q", "--allow-empty", "-m", "ada: project created"], repo)
        if res.returncode != 0:
            raise RuntimeError(f"initial commit failed: {(res.stderr or res.stdout).strip()[:300]}")
        entry["root"] = str(repo)
        entry["default_branch"] = _git_out(["branch", "--show-current"], repo)
    return _register(settings, entry)


def import_project(settings: Settings, source: str, *, name: str | None = None, ref: str = "") -> dict[str, Any]:
    """Import an existing repo as a project.

    - Git URL (``://``, ``git@`` or a ``.git`` suffix): clone (at ``ref`` if
      given) into ``workspace_dir/<slug>/repo`` — origin "git".
    - Existing local directory: origin "local", OPERATE IN PLACE — root is the
      user's directory and it is NEVER mutated (must already be a git repo;
      ValueError otherwise). Its current branch is recorded as default_branch.
    """
    source = (source or "").strip()
    if not source:
        raise ValueError("import_project: empty source")
    if _looks_like_git_url(source):
        return _import_git(settings, source, name=name, ref=ref)
    return _import_local(settings, source, name=name)


def _import_local(settings: Settings, source: str, *, name: str | None) -> dict[str, Any]:
    src = Path(source).expanduser().resolve()
    if not src.is_dir():
        raise ValueError(f"local source does not exist: {src}")
    if not _is_git_repo(src):
        raise ValueError(f"not a git repository: {src} (local imports operate in place and require git)")
    slug = slugify(name or src.name)
    entry = _normalize({
        "slug": slug,
        "name": (name or src.name).strip() or slug,
        "created_at": time.time(),
        "origin": "local",
        "source": str(src),
        "root": str(src),  # in place: the user's dir IS the checkout — read-only here
        "default_branch": _git_out(["branch", "--show-current"], src),
    })
    return _register(settings, entry)


def _import_git(settings: Settings, source: str, *, name: str | None, ref: str) -> dict[str, Any]:
    derived = re.sub(r"\.git$", "", source.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1])
    slug = slugify(name or derived)
    if get_project(settings, slug) is not None:
        raise ValueError(f"project '{slug}' already exists")
    dest = (settings.workspace_dir / slug / "repo").resolve()
    if dest.exists() and any(dest.iterdir()):
        raise ValueError(f"checkout path already exists and is not empty: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    res = _git(["clone", source, str(dest)], dest.parent)
    if res.returncode != 0:
        raise ValueError(f"git clone failed: {(res.stderr or res.stdout).strip()[:300]}")
    if ref:
        res = _git(["-c", "advice.detachedHead=false", "checkout", ref], dest)
        if res.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise ValueError(f"git checkout {ref!r} failed: {(res.stderr or res.stdout).strip()[:300]}")
    entry = _normalize({
        "slug": slug,
        "name": (name or derived).strip() or slug,
        "created_at": time.time(),
        "origin": "git",
        "source": source,
        "root": str(dest),
        "default_branch": _git_out(["branch", "--show-current"], dest) or ref,
    })
    return _register(settings, entry)


# ---------------------------------------------------------------------------
# Checkout / status / indexing
# ---------------------------------------------------------------------------

def project_checkout(settings: Settings, slug: str) -> Path | None:
    """The project's repo root as a Path, or None when it has no checkout
    (e.g. the legacy 'default' scratch project)."""
    entry = get_project(settings, slug)
    if entry is None:
        return None
    root = entry.get("root") or ""
    return Path(root) if root else None


def project_status(settings: Settings, slug: str) -> dict[str, Any]:
    """Read-only repo state. Safe on a missing/absent repo: returns empties."""
    entry = get_project(settings, slug) or _normalize({"slug": slug, "name": slug})
    out: dict[str, Any] = {
        "slug": entry["slug"],
        "branch": "",
        "head": "",
        "dirty": False,
        "origin": entry["origin"],
        "root": entry["root"],
        "last_indexed_commit": entry["last_indexed_commit"],
        "archived": bool(entry["archived"]),
    }
    root = Path(entry["root"]) if entry["root"] else None
    if root is None or not root.is_dir() or not _is_git_repo(root):
        return out
    out["branch"] = _git_out(["branch", "--show-current"], root)
    out["head"] = _git_out(["rev-parse", "HEAD"], root)  # "" on an unborn branch
    try:
        res = _git(["status", "--porcelain"], root)
        out["dirty"] = res.returncode == 0 and bool(res.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def _enrich_kg_file(kg: Any, rel: str, text: str, ext: str) -> None:
    """Re-enrich the KG for one changed source file (mirrors extract.py facts)."""
    kg.add_node(rel, "file")
    if ext == ".py":
        symbols, imports = parse_python_outline(text)
        for sym, kind in symbols:
            kg.add_fact(rel, "defines", f"{rel}::{sym}", obj_type=kind, kind=kind)
        for mod, level in imports:
            if mod and not level:
                kg.add_fact(rel, "imports", mod.split(".")[0])
    elif ext in _JS_EXT:
        for sym, kind in parse_js_outline(text):
            kg.add_fact(rel, "defines", f"{rel}::{sym}", obj_type=kind, kind=kind)


def refresh_index(settings: Settings, slug: str, kb: Any, kg: Any) -> dict[str, Any]:
    """Incremental onboarding for the project's checkout. Read-only on the repo.

    Empty last_indexed_commit -> full onboard (knowledge.repo_map.onboard);
    HEAD unchanged -> noop; otherwise re-ingest only the source files changed in
    ``git diff --name-only <last>..HEAD`` and re-enrich the KG for them.
    Returns {"files": n, "from": old, "to": head, "mode": "full"|"incremental"|"noop"}.
    """
    noop = {"files": 0, "from": "", "to": "", "mode": "noop"}
    entry = get_project(settings, slug)
    if entry is None:
        return noop
    root = Path(entry["root"]) if entry["root"] else None
    if root is None or not root.is_dir():
        return noop
    head = _git_out(["rev-parse", "HEAD"], root)
    if not head:  # not a repo, or no commits yet
        return noop
    last = entry.get("last_indexed_commit") or ""

    if last == head:
        return {"files": 0, "from": last, "to": head, "mode": "noop"}

    changed: list[str] | None = None
    if last:
        try:
            res = _git(["diff", "--name-only", f"{last}..{head}"], root)
        except (OSError, subprocess.SubprocessError):
            res = None
        if res is not None and res.returncode == 0:
            changed = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        # else: last commit unknown (rewritten history) — fall through to full onboard

    if changed is None:
        counts = onboard(kb, kg, root, f"project:{slug}")
        kg.save()
        _update_entry(settings, slug, last_indexed_commit=head)
        return {"files": counts.get("files", 0), "from": last, "to": head, "mode": "full"}

    n = 0
    for rel in changed:
        parts = Path(rel).parts
        if any(part in _SKIP_PARTS for part in parts):
            continue
        ext = Path(rel).suffix.lower()
        if ext not in _SOURCE_EXT:
            continue
        p = root / rel
        if not p.is_file():  # deleted in the diff range
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        kb.reingest(f"repo:{rel}", text)  # idempotent per-doc replacement
        _enrich_kg_file(kg, rel, text, ext)
        n += 1
    if n:
        kg.save()
    _update_entry(settings, slug, last_indexed_commit=head)
    return {"files": n, "from": last, "to": head, "mode": "incremental"}


# ---------------------------------------------------------------------------
# Policy / archive / delete
# ---------------------------------------------------------------------------

def set_policy(settings: Settings, slug: str, policy: dict) -> dict[str, Any]:
    """Merge ``policy`` into the project's policy and persist. Returns the entry."""
    entry = get_project(settings, slug)
    if entry is None:
        raise ValueError(f"unknown project: {slug}")
    merged = {**entry.get("policy", {}), **(policy or {})}
    return _update_entry(settings, slug, policy=merged)


def archive_project(settings: Settings, slug: str, archived: bool = True) -> dict[str, Any]:
    return _update_entry(settings, slug, archived=bool(archived))


def delete_project(settings: Settings, slug: str) -> None:
    """Remove the project's data dir and — ONLY for non-local origins — its
    checkout under workspace_dir/<slug>. A user's own directory (origin
    "local") is NEVER deleted. Refuses to delete the default project.
    Also unassigns the project from its workspace, if it belongs to one.
    """
    if slug == DEFAULT_PROJECT:
        raise ValueError("refusing to delete the default project")
    entry = get_project(settings, slug)
    if entry is None:
        return
    from . import workspaces  # late import: workspaces imports this module

    ws_slug = workspaces.workspace_of(settings, slug)
    if ws_slug is not None:
        workspaces.unassign_project(settings, ws_slug, slug)
    data = settings.projects_dir / slug
    if data.is_dir():
        shutil.rmtree(data, ignore_errors=True)
    if entry.get("origin") != "local":
        ws = (settings.workspace_dir / slug).resolve()
        # belt-and-braces: only ever delete inside our own workspace_dir
        if ws.is_dir() and settings.workspace_dir.resolve() in ws.parents:
            shutil.rmtree(ws, ignore_errors=True)
    items = [p for p in list_projects(settings) if p.get("slug") != slug]
    _write_registry(settings, items)


def review_target(settings: Settings, slug: str) -> str:
    """The branch acceptance merges into (F2/decision #2).

    In-place (local-origin) projects get an ``ada/integration`` branch — git refuses
    commits to the user's checked-out branch from another worktree, and the safety
    contract forbids touching their working tree; the user merges when ready.
    """
    p = get_project(settings, slug) or {}
    if p.get("origin") == "local":
        return "ada/integration"
    return p.get("default_branch") or "main"


# Policy keys applied onto Settings for a run (project policy → Settings field).
_POLICY_MAP = {
    "budget_usd": "budget_usd",
    "effort": "agent_effort",
    "git_mode": "git_mode",
    "sandbox": "sandbox",
    "allow_web": "allow_web",
    "max_plan_subtasks": "max_plan_subtasks",
    "worktree_per_subtask": "worktree_per_subtask",
    "subtask_max_seconds": "subtask_max_seconds",
    "attention_timeout": "attention_timeout",
}


def accept_commit(settings: Settings, slug: str, sha: str) -> dict:
    """Accept one subtask commit into the project's review target (decision #5).

    Owned checkouts (greenfield/clone) with the target checked out and clean:
    cherry-pick directly in the checkout so the working tree advances with the
    branch. In-place projects (and any other case): temp-worktree cherry-pick
    into the target branch — the user's checkout is never touched.
    """
    from . import vcs

    p = get_project(settings, slug)
    if not p or not p.get("root"):
        return {"merged": False, "conflict": False, "error": "project has no repository"}
    root = Path(p["root"])
    target = review_target(settings, slug)
    if p.get("origin") != "local":
        st = project_status(settings, slug)
        if st.get("branch") == target and not st.get("dirty"):
            return vcs.cherry_pick_in_checkout(root, sha)
    return vcs.cherry_pick_merge(root, sha, target)


def accept_subtask(settings: Settings, slug: str, task_id: str, subtask_id: str) -> dict:
    """Accept one subtask AND record the commit the acceptance created.

    ``accept_commit`` alone returns the new cherry-pick commit but persists
    nothing; this wrapper looks up the subtask's merge commit in the run store,
    accepts it, and stores decision "accepted" + ``accepted_commit`` on
    subtask_reviews — which is what ``rollback_accept`` reverts later.
    Same return shape as ``accept_commit``.
    """
    from .orchestration.run_store import RunStore

    store = RunStore(settings.data_dir / "runs.db")
    try:
        st = store.get_subtask_states(task_id).get(subtask_id) or {}
        sha = str(st.get("merge_commit") or "").strip()
        if not sha:
            return {"merged": False, "conflict": False,
                    "error": "subtask has no merge commit to accept"}
        res = accept_commit(settings, slug, sha)
        if res.get("merged"):
            store.set_subtask_review(task_id, subtask_id, "accepted", "",
                                     accepted_commit=str(res.get("commit") or ""))
        return res
    finally:
        store.close()


def rollback_accept(settings: Settings, slug: str, task_id: str, subtask_id: str) -> dict:
    """Revert a previously accepted subtask's commit on the review target.

    Finds the ``accepted_commit`` recorded at acceptance time and reverts exactly
    that commit. Owned checkouts with the target checked out and clean: revert
    directly in the checkout (working tree moves back with the branch). In-place
    projects (and any other case): the revert lands on the review target
    (``ada/integration``) via a temporary worktree — the user's checked-out
    branch and working tree are never touched, mirroring how acceptance flows.

    Only the requested subtask's commit is reverted; if git cannot revert it
    cleanly (e.g. later accepts depend on it) the operation aborts, the repo is
    left clean, and ``{"ok": False, "error": ...}`` is returned — never forced.
    Success records decision "rolled_back" + ``rollback_commit`` in
    subtask_reviews and returns ``{"ok": True, "reverted": sha,
    "rollback_commit": sha}``.
    """
    from . import vcs
    from .orchestration.run_store import RunStore

    p = get_project(settings, slug)
    if not p or not p.get("root"):
        return {"ok": False, "error": "project has no repository"}
    store = RunStore(settings.data_dir / "runs.db")
    try:
        review = store.get_subtask_reviews(task_id).get(subtask_id) or {}
        sha = str(review.get("accepted_commit") or "").strip()
        if not sha:
            return {"ok": False,
                    "error": "no recorded accept commit for this subtask — nothing to roll back"}
        decision = str(review.get("decision") or "")
        if decision != "accepted":
            return {"ok": False,
                    "error": f"subtask is not in an accepted state (decision: {decision or 'none'})"}
        root = Path(p["root"])
        target = review_target(settings, slug)
        res: dict | None = None
        if p.get("origin") != "local":
            st = project_status(settings, slug)
            if st.get("branch") == target and not st.get("dirty"):
                res = vcs.revert_in_checkout(root, sha)
        if res is None:
            res = vcs.revert_merge(root, sha, target)
        if not res.get("reverted"):
            err = res.get("error") or (
                "revert conflict — later accepted commits may depend on this one"
                if res.get("conflict") else "revert failed")
            out = {"ok": False, "error": err, "conflict": bool(res.get("conflict"))}
            if res.get("files"):
                out["files"] = res["files"]
            return out
        commit = str(res.get("commit") or "")
        store.set_subtask_rollback(task_id, subtask_id, commit)
        return {"ok": True, "reverted": sha, "rollback_commit": commit, "target": target}
    finally:
        store.close()


def deliver_branch(settings: Settings, slug: str, branch: str, *, message: str = "") -> dict:
    """Deliver a whole task branch into the project's review target ("Accept all").

    Same routing as ``accept_commit``: owned checkouts with the target checked
    out and clean merge directly (working tree advances); in-place projects and
    everything else go through the temp-worktree merge — the user's checkout is
    never touched. Idempotent: an already-delivered branch merges as up-to-date.
    """
    from . import vcs

    p = get_project(settings, slug)
    if not p or not p.get("root"):
        return {"merged": False, "conflict": False, "error": "project has no repository"}
    root = Path(p["root"])
    target = review_target(settings, slug)
    msg = message or f"ada: deliver {branch}"
    if p.get("origin") != "local":
        st = project_status(settings, slug)
        if st.get("branch") == target and not st.get("dirty"):
            return vcs.merge_in_checkout(root, branch, message=msg)
    return vcs.merge_branch(root, branch, target, message=msg)


def effective_settings(settings: Settings, slug: str | None = None) -> Settings:
    """Resolve per-run Settings: task override → project policy → global defaults (F7).

    Also flips worktree_per_subtask ON by default for project tasks with a checkout —
    per-subtask commits are what make per-subtask review acceptance possible
    (decision #5).
    """
    import dataclasses

    slug = slug or settings.project
    p = get_project(settings, slug)
    if p is None:
        return settings
    policy = p.get("policy") or {}
    overrides: dict = {}
    for key, field in _POLICY_MAP.items():
        if key in policy and policy[key] is not None:
            overrides[field] = policy[key]
    if "protected_paths" in policy:
        overrides["protected_paths"] = tuple(policy["protected_paths"] or ())
    if project_checkout(settings, slug) is not None and "worktree_per_subtask" not in policy:
        overrides["worktree_per_subtask"] = True
    return dataclasses.replace(settings, **overrides) if overrides else settings


def resolve(settings: Settings, slug: str | None) -> str:
    """Return a valid project slug, falling back to default for unknown/blank input."""
    if not slug:
        return DEFAULT_PROJECT
    slug = slugify(slug)
    known = {p["slug"] for p in list_projects(settings)}
    return slug if slug in known else DEFAULT_PROJECT
