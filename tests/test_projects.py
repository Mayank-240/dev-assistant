"""Tests for first-class projects (F1): lifecycle, in-place local imports,
incremental indexing, policy, archive, delete — with the operate-in-place
safety contract asserted against real git repos."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_dev_assistant.config import Settings
from ai_dev_assistant.projects import (
    DEFAULT_PROJECT,
    archive_project,
    create_project,
    delete_project,
    get_project,
    import_project,
    list_projects,
    project_checkout,
    project_status,
    refresh_index,
    resolve,
    set_policy,
    slugify,
)

GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com"]


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run([*GIT, *args], cwd=str(cwd), capture_output=True, text=True)
    assert res.returncode == 0, f"git {args} failed: {res.stderr}"
    return res.stdout.strip()


def make_repo(path: Path, files: dict[str, str]) -> str:
    """Init a real git repo at ``path`` with ``files`` committed; returns HEAD sha."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for rel, text in files.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return git(path, "rev-parse", "HEAD")


def repo_fingerprint(path: Path) -> tuple[str, str, str]:
    """(HEAD, current branch, porcelain status) — for untouched-repo assertions."""
    return (
        git(path, "rev-parse", "HEAD"),
        git(path, "branch", "--show-current"),
        git(path, "status", "--porcelain"),
    )


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "ws",
                    docs_dir=tmp_path / "docs")


class FakeKB:
    def __init__(self) -> None:
        self.reingested: list[str] = []

    def reingest(self, doc_id: str, text: str) -> int:
        self.reingested.append(doc_id)
        return 1


class FakeKG:
    def __init__(self) -> None:
        self.nodes: list[tuple] = []
        self.facts: list[tuple] = []
        self.saved = 0

    def add_node(self, node_id, node_type="concept", **attrs):
        self.nodes.append((node_id, node_type))

    def add_fact(self, subject, relation, obj, **attrs):
        self.facts.append((subject, relation, obj))

    def facts_about(self, node_id):
        return []

    def all_triples(self):
        return []

    def save(self):
        self.saved += 1


# ---------------------------------------------------------------------------
# Registry basics / legacy compatibility
# ---------------------------------------------------------------------------

def test_list_projects_ensures_default_with_no_checkout(settings):
    items = list_projects(settings)
    default = next(p for p in items if p["slug"] == DEFAULT_PROJECT)
    assert default["root"] == ""            # scratch project: no checkout
    assert default["origin"] == "greenfield"
    assert default["policy"] == {}
    assert project_checkout(settings, DEFAULT_PROJECT) is None


def test_legacy_thin_entry_normalizes(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.registry_path.write_text(json.dumps([
        {"slug": "oldie", "name": "Oldie", "created_at": 123.0},
    ]))
    entry = get_project(settings, "oldie")
    assert entry is not None
    assert entry["origin"] == "greenfield"
    assert entry["root"] == ""
    assert entry["default_branch"] == ""
    assert entry["archived"] is False
    assert entry["last_indexed_commit"] == ""
    assert entry["policy"] == {}
    assert entry["name"] == "Oldie" and entry["created_at"] == 123.0
    # legacy entry still resolves and lists alongside the auto-created default
    assert resolve(settings, "oldie") == "oldie"
    assert {p["slug"] for p in list_projects(settings)} == {DEFAULT_PROJECT, "oldie"}


def test_get_project_unknown_is_none(settings):
    assert get_project(settings, "nope") is None


# ---------------------------------------------------------------------------
# Greenfield create
# ---------------------------------------------------------------------------

def test_create_greenfield_provisions_repo_with_initial_commit(settings):
    entry = create_project(settings, "My App")
    assert entry["slug"] == "my-app"
    assert entry["origin"] == "greenfield"
    root = Path(entry["root"])
    assert root == (settings.workspace_dir / "my-app" / "repo").resolve()
    assert root.is_dir()
    head = git(root, "rev-parse", "HEAD")   # initial commit exists
    assert head
    assert git(root, "status", "--porcelain") == ""
    assert entry["default_branch"] == git(root, "branch", "--show-current")
    assert project_checkout(settings, "my-app") == root
    # data dir provisioned; idempotent re-create returns the same entry
    assert (settings.projects_dir / "my-app").is_dir()
    again = create_project(settings, "My App")
    assert again["root"] == entry["root"]

    status = project_status(settings, "my-app")
    assert status["head"] == head
    assert status["dirty"] is False
    assert status["origin"] == "greenfield"


# ---------------------------------------------------------------------------
# Local import: operate in place, never mutate
# ---------------------------------------------------------------------------

def test_import_local_in_place_and_never_mutates(settings, tmp_path):
    user_repo = tmp_path / "user-code"
    make_repo(user_repo, {"a.py": "def alpha():\n    return 1\n"})
    # a dirty working tree must survive import untouched too
    (user_repo / "scratch.txt").write_text("uncommitted\n")
    before = repo_fingerprint(user_repo)

    entry = import_project(settings, str(user_repo))
    assert entry["origin"] == "local"
    assert Path(entry["root"]) == user_repo.resolve()
    assert entry["source"] == str(user_repo.resolve())
    assert entry["default_branch"] == before[1]
    assert project_checkout(settings, entry["slug"]) == user_repo.resolve()

    status = project_status(settings, entry["slug"])
    assert status["branch"] == before[1]
    assert status["head"] == before[0]
    assert status["dirty"] is True          # read-only observation of the scratch file

    refresh_index(settings, entry["slug"], FakeKB(), FakeKG())

    # CRITICAL: import + status + refresh_index left the user's repo untouched
    assert repo_fingerprint(user_repo) == before


def test_import_local_non_git_dir_raises(settings, tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    (plain / "file.txt").write_text("hi")
    with pytest.raises(ValueError):
        import_project(settings, str(plain))


def test_import_local_missing_dir_raises(settings, tmp_path):
    with pytest.raises(ValueError):
        import_project(settings, str(tmp_path / "does-not-exist"))


# ---------------------------------------------------------------------------
# Git URL import
# ---------------------------------------------------------------------------

def test_import_git_url_clones_into_workspace(settings, tmp_path):
    fixture = tmp_path / "origin-repo"
    head = make_repo(fixture, {"lib.py": "def one():\n    return 1\n"})
    url = f"file://{fixture.resolve()}"     # "://" triggers git-URL detection

    entry = import_project(settings, url, name="Cloned Proj")
    assert entry["origin"] == "git"
    assert entry["source"] == url
    dest = Path(entry["root"])
    assert dest == (settings.workspace_dir / "cloned-proj" / "repo").resolve()
    assert git(dest, "rev-parse", "HEAD") == head
    assert entry["default_branch"] == git(dest, "branch", "--show-current")
    # the fixture "remote" itself is untouched
    assert git(fixture, "rev-parse", "HEAD") == head
    assert git(fixture, "status", "--porcelain") == ""


def test_import_git_url_at_ref(settings, tmp_path):
    fixture = tmp_path / "origin-repo"
    first = make_repo(fixture, {"lib.py": "x = 1\n"})
    (fixture / "lib.py").write_text("x = 2\n")
    git(fixture, "add", "-A")
    git(fixture, "commit", "-q", "-m", "second")

    entry = import_project(settings, f"file://{fixture.resolve()}", name="Pinned", ref=first)
    assert git(Path(entry["root"]), "rev-parse", "HEAD") == first


def test_import_git_bad_url_raises(settings, tmp_path):
    with pytest.raises(ValueError):
        import_project(settings, f"file://{tmp_path / 'missing'}", name="Bad")


# ---------------------------------------------------------------------------
# Indexing: full onboard -> incremental -> noop
# ---------------------------------------------------------------------------

def test_refresh_full_then_incremental_then_noop(settings, tmp_path):
    user_repo = tmp_path / "src-repo"
    make_repo(user_repo, {
        "a.py": "def alpha():\n    return 1\n",
        "b.py": "def beta():\n    return 2\n",
        "notes.md": "not source\n",
    })
    entry = import_project(settings, str(user_repo), name="Indexed")
    slug = entry["slug"]
    before = repo_fingerprint(user_repo)

    # 1) empty last_indexed_commit -> full onboard via repo_map.onboard
    kb, kg = FakeKB(), FakeKG()
    out = refresh_index(settings, slug, kb, kg)
    assert out["mode"] == "full"
    assert out["from"] == "" and out["to"] == before[0]
    assert set(kb.reingested) == {"repo:a.py", "repo:b.py"}   # only source files
    assert kg.saved >= 1
    assert get_project(settings, slug)["last_indexed_commit"] == before[0]

    # 2) a new commit touching only b.py -> incremental re-index of b.py alone
    (user_repo / "b.py").write_text("def beta():\n    return 3\n\ndef gamma():\n    return 4\n")
    (user_repo / "notes.md").write_text("still not source\n")
    git(user_repo, "add", "-A")
    git(user_repo, "commit", "-q", "-m", "change b")
    new_head = git(user_repo, "rev-parse", "HEAD")

    kb2, kg2 = FakeKB(), FakeKG()
    out2 = refresh_index(settings, slug, kb2, kg2)
    assert out2 == {"files": 1, "from": before[0], "to": new_head, "mode": "incremental"}
    assert kb2.reingested == ["repo:b.py"]
    assert ("b.py", "file") in kg2.nodes
    assert ("b.py", "defines", "b.py::gamma") in kg2.facts
    assert kg2.saved == 1
    assert get_project(settings, slug)["last_indexed_commit"] == new_head

    # 3) HEAD unchanged -> noop, nothing ingested
    kb3, kg3 = FakeKB(), FakeKG()
    out3 = refresh_index(settings, slug, kb3, kg3)
    assert out3 == {"files": 0, "from": new_head, "to": new_head, "mode": "noop"}
    assert kb3.reingested == [] and kg3.saved == 0

    # in-place repo untouched by all of the above (HEAD/branch as committed by the *user*)
    assert repo_fingerprint(user_repo) == (new_head, before[1], "")


def test_refresh_index_safe_without_checkout(settings):
    assert refresh_index(settings, DEFAULT_PROJECT, FakeKB(), FakeKG())["mode"] == "noop"
    assert refresh_index(settings, "unknown", FakeKB(), FakeKG())["mode"] == "noop"


def test_project_status_safe_on_missing_repo(settings):
    status = project_status(settings, DEFAULT_PROJECT)
    assert status["branch"] == "" and status["head"] == "" and status["dirty"] is False
    # unknown slug: empties, no raise
    status = project_status(settings, "ghost")
    assert status["head"] == "" and status["root"] == ""


# ---------------------------------------------------------------------------
# Policy / archive / delete
# ---------------------------------------------------------------------------

def test_set_policy_merges_and_persists(settings):
    create_project(settings, "Poly")
    set_policy(settings, "poly", {"budget_usd": 5.0, "git_mode": "branch"})
    entry = set_policy(settings, "poly", {"git_mode": "merge", "effort": "high"})
    assert entry["policy"] == {"budget_usd": 5.0, "git_mode": "merge", "effort": "high"}
    assert get_project(settings, "poly")["policy"] == entry["policy"]
    with pytest.raises(ValueError):
        set_policy(settings, "ghost", {"x": 1})


def test_archive_roundtrip(settings):
    create_project(settings, "Arch")
    assert archive_project(settings, "arch")["archived"] is True
    assert get_project(settings, "arch")["archived"] is True
    assert archive_project(settings, "arch", archived=False)["archived"] is False


def test_delete_refuses_default(settings):
    list_projects(settings)
    with pytest.raises(ValueError):
        delete_project(settings, DEFAULT_PROJECT)
    assert get_project(settings, DEFAULT_PROJECT) is not None


def test_delete_greenfield_removes_data_and_checkout(settings):
    entry = create_project(settings, "Gone Soon")
    root = Path(entry["root"])
    data = settings.projects_dir / "gone-soon"
    assert root.is_dir() and data.is_dir()
    delete_project(settings, "gone-soon")
    assert get_project(settings, "gone-soon") is None
    assert not data.exists()
    assert not (settings.workspace_dir / "gone-soon").exists()
    assert not root.exists()


def test_delete_local_never_removes_user_dir(settings, tmp_path):
    user_repo = tmp_path / "precious"
    head = make_repo(user_repo, {"keep.py": "x = 1\n"})
    entry = import_project(settings, str(user_repo))
    (settings.projects_dir / entry["slug"]).mkdir(parents=True, exist_ok=True)
    delete_project(settings, entry["slug"])
    assert get_project(settings, entry["slug"]) is None
    assert not (settings.projects_dir / entry["slug"]).exists()
    # the user's own directory survives, fully intact
    assert user_repo.is_dir()
    assert (user_repo / "keep.py").read_text() == "x = 1\n"
    assert git(user_repo, "rev-parse", "HEAD") == head


def test_delete_unknown_is_noop(settings):
    delete_project(settings, "never-existed")  # must not raise


# ---------------------------------------------------------------------------
# Misc contract points
# ---------------------------------------------------------------------------

def test_slugify_and_resolve_still_work(settings):
    assert slugify("My Cool Repo!") == "my-cool-repo"
    assert resolve(settings, None) == DEFAULT_PROJECT
    assert resolve(settings, "nonexistent") == DEFAULT_PROJECT
    create_project(settings, "Real One")
    assert resolve(settings, "Real One") == "real-one"


def test_import_duplicate_slug_raises(settings, tmp_path):
    repo = tmp_path / "dup"
    make_repo(repo, {"f.py": "pass\n"})
    import_project(settings, str(repo))
    with pytest.raises(ValueError):
        import_project(settings, str(repo))
