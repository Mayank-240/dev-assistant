"""Security + capability tests for the agent-facing ToolBox (tools/registry.py)."""

from pathlib import Path

from ai_dev_assistant.tools import registry as registry_mod
from ai_dev_assistant.tools.registry import ToolBox, ToolContext, _valid_requirement


def make_box(base: Path, **overrides) -> ToolBox:
    # File/exec tools never touch memory/kb/kg/bus, so stubs are fine here.
    ctx = ToolContext(memory=None, kb=None, kg=None, bus=None, agent_name="tester",
                      task_scope="t", base_dir=base, workspace=base, **overrides)
    return ToolBox(ctx)


# ---- path safety ----

def test_read_file_rejects_dotdot_escape(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "outside.txt").write_text("secret stuff")
    out = make_box(ws).dispatch("read_file", {"path": "../outside.txt"})
    assert out.startswith("DENIED: ")
    assert "secret stuff" not in out


def test_read_file_rejects_absolute_path(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    out = make_box(ws).dispatch("read_file", {"path": "/etc/passwd"})
    assert out.startswith("DENIED: ")
    assert "root:" not in out


def test_read_file_rejects_symlink_out_of_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "outside.txt").write_text("secret stuff")
    (ws / "link.txt").symlink_to(tmp_path / "outside.txt")
    out = make_box(ws).dispatch("read_file", {"path": "link.txt"})
    assert out.startswith("DENIED: ")
    assert "secret stuff" not in out


def test_write_file_rejects_escape(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    out = make_box(ws).dispatch("write_file", {"path": "../evil.txt", "content": "x"})
    assert out.startswith("DENIED: ")
    assert not (tmp_path / "evil.txt").exists()


def test_secret_denylist_blocks_env_reads(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".env").write_text("API_KEY=hunter2hunter2")
    (ws / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    box = make_box(ws)
    for name in (".env", "id_rsa"):
        out = box.dispatch("read_file", {"path": name})
        assert out.startswith("DENIED: "), name
        assert "hunter2" not in out


# ---- edit_file ----

def test_edit_file_fails_on_ambiguous_old(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\ny = 1\n")
    out = make_box(ws).dispatch("edit_file", {"path": "a.py", "old": "= 1", "new": "= 2"})
    assert out.startswith("ERROR") and "2 times" in out
    assert (ws / "a.py").read_text() == "x = 1\ny = 1\n"  # unchanged


def test_edit_file_replace_all(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\ny = 1\n")
    out = make_box(ws).dispatch("edit_file", {"path": "a.py", "old": "= 1", "new": "= 2",
                                              "replace_all": True})
    assert not out.startswith("ERROR")
    assert (ws / "a.py").read_text() == "x = 2\ny = 2\n"


def test_edit_file_unique_old_still_works(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x = 1\ny = 2\n")
    out = make_box(ws).dispatch("edit_file", {"path": "a.py", "old": "y = 2", "new": "y = 3"})
    assert not out.startswith("ERROR")
    assert (ws / "a.py").read_text() == "x = 1\ny = 3\n"


# ---- read_file offset/limit ----

def test_read_file_offset_limit(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "big.txt").write_text("\n".join(f"L{i}" for i in range(1, 11)))
    out = make_box(ws).dispatch("read_file", {"path": "big.txt", "offset": 3, "limit": 2})
    assert "L3" in out and "L4" in out
    assert "L5" not in out and "L2\n" not in out
    assert "[lines 3-4 of 10]" in out


def test_read_file_wrapped_as_untrusted(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("hello")
    out = make_box(ws).dispatch("read_file", {"path": "a.txt"})
    assert "<untrusted" in out and "hello" in out


# ---- grep ----

def _grep_fixture(ws: Path) -> None:
    (ws / "mod.py").write_text("import os\n\ndef alpha_handler():\n    return 1\n")
    (ws / "other.txt").write_text("alpha_handler is mentioned here\n")


def test_grep_regex_and_context(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _grep_fixture(ws)
    out = make_box(ws).dispatch("grep", {"pattern": r"def \w+_handler", "context": 1})
    assert "mod.py:3:" in out          # regex match with line number
    assert "return 1" in out           # context line below the match
    assert "other.txt" not in out      # regex, not substring: 'def ' doesn't match there
    assert "<untrusted" in out


def test_grep_include_glob(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _grep_fixture(ws)
    out = make_box(ws).dispatch("grep", {"pattern": "alpha_handler", "include": "*.txt"})
    assert "other.txt" in out and "mod.py" not in out


def test_grep_python_fallback(tmp_path, monkeypatch):
    # Force the pure-Python path even when ripgrep is installed.
    monkeypatch.setattr(registry_mod.shutil, "which", lambda _name: None)
    ws = tmp_path / "ws"
    ws.mkdir()
    _grep_fixture(ws)
    out = make_box(ws).dispatch("grep", {"pattern": r"def \w+_handler", "context": 1})
    assert "mod.py:3:" in out and "return 1" in out and "other.txt" not in out


# ---- install_packages ----

def test_valid_requirement_accepts_plain_specs():
    for good in ("requests", "requests==2.31.0", "numpy>=1.0,<2.0", "uvicorn[standard]~=0.29"):
        assert _valid_requirement(good), good


def test_valid_requirement_rejects_dangerous_specs():
    for bad in ("--index-url", "git+https://github.com/x/y", "./local", "pkg; rm -rf /",
                "-e .", "https://evil.tld/p.whl", "..\\up", "name @ file:///tmp/x"):
        assert not _valid_requirement(bad), bad


def test_install_packages_rejects_dangerous_args(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    box = make_box(ws)
    for bad in ("--index-url", "git+https://github.com/x/y", "./local", "pkg; rm -rf /"):
        out = box.dispatch("install_packages", {"packages": [bad]})
        assert out.startswith("ERROR"), bad
        assert "refusing" in out


# ---- protected paths (F7 project policy) ----

class _AuditStub:
    def __init__(self):
        self.lines = []

    def record(self, agent, tool, args, outcome):
        self.lines.append(f"{agent} {tool} {outcome}")


def test_write_file_blocked_by_protected_globs(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    box = make_box(ws, protected_paths=("infra/**", "*.lock"))
    out = box.dispatch("write_file", {"path": "infra/main.tf", "content": "x"})
    assert out.startswith("DENIED: protected path")
    assert not (ws / "infra").exists()
    out = box.dispatch("write_file", {"path": "poetry.lock", "content": "x"})
    assert out.startswith("DENIED: protected path")
    assert not (ws / "poetry.lock").exists()


def test_protected_paths_match_nested_dirs(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    for patterns in (("infra/**",), ("infra",)):  # bare dir name and glob both block
        out = make_box(ws, protected_paths=patterns).dispatch(
            "write_file", {"path": "infra/x/y.tf", "content": "x"})
        assert out.startswith("DENIED: protected path"), patterns
    assert not (ws / "infra").exists()


def test_edit_file_blocked_by_protected_glob(tmp_path):
    ws = tmp_path / "ws"
    (ws / "infra").mkdir(parents=True)
    (ws / "infra" / "main.tf").write_text("old\n")
    box = make_box(ws, protected_paths=("infra/**",))
    out = box.dispatch("edit_file", {"path": "infra/main.tf", "old": "old", "new": "new"})
    assert out.startswith("DENIED: protected path")
    assert (ws / "infra" / "main.tf").read_text() == "old\n"  # unchanged


def test_apply_patch_refused_when_diff_touches_protected_file(tmp_path):
    ws = tmp_path / "ws"
    (ws / "infra").mkdir(parents=True)
    (ws / "infra" / "main.tf").write_text("old\n")
    patch = ("--- a/infra/main.tf\n"
             "+++ b/infra/main.tf\n"
             "@@ -1 +1 @@\n"
             "-old\n"
             "+new\n")
    out = make_box(ws, protected_paths=("infra/**",)).dispatch("apply_patch", {"patch": patch})
    assert out.startswith("DENIED: protected path")
    assert (ws / "infra" / "main.tf").read_text() == "old\n"  # refused BEFORE applying


def test_apply_patch_unprotected_paths_not_denied(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    patch = ("--- /dev/null\n"
             "+++ b/new.txt\n"
             "@@ -0,0 +1 @@\n"
             "+hello\n")
    out = make_box(ws, protected_paths=("infra/**",)).dispatch("apply_patch", {"patch": patch})
    assert not out.startswith("DENIED")


def test_unprotected_writes_unaffected_by_policy(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    box = make_box(ws, protected_paths=("infra/**", "*.lock"))
    out = box.dispatch("write_file", {"path": "src/app.py", "content": "print(1)\n"})
    assert not out.startswith("DENIED") and not out.startswith("ERROR")
    assert (ws / "src" / "app.py").read_text() == "print(1)\n"


def test_empty_protected_paths_is_a_noop(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    out = make_box(ws).dispatch("write_file", {"path": "infra/main.tf", "content": "x"})
    assert not out.startswith("DENIED")
    assert (ws / "infra" / "main.tf").read_text() == "x"


def test_protected_path_denial_is_audited(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    audit = _AuditStub()
    box = make_box(ws, protected_paths=("infra/**",), audit=audit)
    box.dispatch("write_file", {"path": "infra/main.tf", "content": "x"})
    assert any("DENIED: protected path" in line for line in audit.lines)


def test_escape_and_secret_denials_use_denied_prefix(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".env").write_text("API_KEY=x")
    box = make_box(ws)
    assert box.dispatch("write_file", {"path": "../out.txt", "content": "x"}).startswith("DENIED: ")
    assert box.dispatch("read_file", {"path": ".env"}).startswith("DENIED: ")
    assert box.dispatch("edit_file", {"path": "../out.txt", "old": "a", "new": "b"}).startswith("DENIED: ")
