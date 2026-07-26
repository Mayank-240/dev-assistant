"""Semantic code index: chunking, incremental reindex, retrieval, degradation."""

from ai_dev_assistant.config import Settings
from ai_dev_assistant.knowledge.code_index import CodeIndex, chunk_text


def _settings(tmp_path):
    return Settings(embeddings_backend="hash", data_dir=tmp_path / "data",
                    docs_dir=tmp_path / "docs", workspace_dir=tmp_path / "ws")


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


class StubEmbedder:
    """2-dim embedder: axis 0 = auth-ish text, axis 1 = everything else."""

    dim = 2

    def embed(self, texts):
        out = []
        for t in texts:
            lowered = t.lower()
            authish = any(w in lowered for w in ("login", "authentication", "credential", "sign"))
            out.append([1.0, 0.0] if authish else [0.0, 1.0])
        return out


class DownEmbedder:
    dim = 2

    def embed(self, texts):
        raise RuntimeError("embedder down")


# ---- chunking ----

def test_chunk_text_windows_and_overlap():
    text = "\n".join(f"line {i}" for i in range(1, 131))  # 130 lines
    chunks = chunk_text(text)
    assert [(s, e) for s, e, _t in chunks] == [(1, 60), (51, 110), (101, 130)]
    # 10-line overlap: window 2 starts inside window 1
    assert chunks[1][2].splitlines()[0] == "line 51"
    assert chunks[0][2].splitlines()[-1] == "line 60"


def test_chunk_text_short_file_single_window():
    chunks = chunk_text("a\nb\nc")
    assert chunks == [(1, 3, "a\nb\nc")]
    assert chunk_text("   \n\n  ") == []


def test_index_skips_binaries_lockfiles_and_minified(tmp_path):
    ws = tmp_path / "repo"
    _write(ws, "ok.py", "def fine():\n    return 1\n")
    (ws / "blob.bin").write_bytes(b"\x00\x01\x02payload")
    _write(ws, "uv.lock", "[package]\nname = 'x'\n")
    _write(ws, "min.js", "var x=1;" * 400)  # single >2000-char line
    idx = CodeIndex(_settings(tmp_path), "p1")
    counts = idx.index_workspace(ws)
    assert counts == {"indexed": 1, "skipped": 3, "unchanged": 0}
    assert {h["path"] for h in idx.search("fine return", top_k=5)} == {"ok.py"}


# ---- incremental reindex ----

def test_reindex_skips_unchanged_files(tmp_path):
    ws = tmp_path / "repo"
    _write(ws, "a.py", "def alpha():\n    pass\n")
    _write(ws, "b.py", "def beta():\n    pass\n")
    idx = CodeIndex(_settings(tmp_path), "p1")
    assert idx.index_workspace(ws)["indexed"] == 2
    counts = idx.index_workspace(ws)
    assert counts == {"indexed": 0, "skipped": 0, "unchanged": 2}


def test_reindex_picks_up_edits(tmp_path):
    ws = tmp_path / "repo"
    p = _write(ws, "a.py", "def alpha():\n    pass\n")
    idx = CodeIndex(_settings(tmp_path), "p1")
    idx.index_workspace(ws)
    p.write_text("def gamma_rewritten():\n    return 42\n")  # size changes too
    counts = idx.index_workspace(ws)
    assert counts["indexed"] == 1 and counts["unchanged"] == 0
    hits = idx.search("gamma rewritten", top_k=3)
    assert hits and hits[0]["path"] == "a.py"
    assert "gamma_rewritten" in hits[0]["text"]
    assert all("alpha" not in h["text"] for h in hits), "stale chunks must be replaced"


def test_reindex_prunes_deleted_files(tmp_path):
    ws = tmp_path / "repo"
    p = _write(ws, "gone.py", "def vanishing_function():\n    pass\n")
    _write(ws, "keep.py", "def keeper():\n    pass\n")
    idx = CodeIndex(_settings(tmp_path), "p1")
    idx.index_workspace(ws)
    assert idx.search("vanishing function", top_k=5)
    p.unlink()
    idx.index_workspace(ws)
    assert all(h["path"] != "gone.py" for h in idx.search("vanishing function", top_k=5))


# ---- retrieval ----

def test_search_returns_semantically_right_chunk_with_stub_embedder(tmp_path):
    ws = tmp_path / "repo"
    _write(ws, "auth.py", "def handle_login(credentials):\n    return check(credentials)\n")
    _write(ws, "math.py", "def add(a, b):\n    return a + b\n")
    idx = CodeIndex(_settings(tmp_path), "p1", embedder=StubEmbedder())
    idx.index_workspace(ws)
    # Query shares no tokens with auth.py — only the semantic (cosine) leg can rank it.
    hits = idx.search("how does the user authentication flow work", top_k=2)
    assert hits and hits[0]["path"] == "auth.py"
    assert {"path", "start_line", "end_line", "text", "score"} <= set(hits[0])
    assert hits[0]["start_line"] == 1 and hits[0]["end_line"] == 2


def test_lexical_fallback_when_embedder_down(tmp_path):
    ws = tmp_path / "repo"
    _write(ws, "pay.py", "def charge_invoice(customer):\n    pass\n")
    _write(ws, "other.py", "def unrelated():\n    pass\n")
    idx = CodeIndex(_settings(tmp_path), "p1", embedder=DownEmbedder())
    counts = idx.index_workspace(ws)  # embedding fails -> lexical-only rows, no raise
    assert counts["indexed"] == 2
    hits = idx.search("charge invoice customer", top_k=2)
    assert hits and hits[0]["path"] == "pay.py"


# ---- retrieval_context ----

def test_retrieval_context_format_and_budget(tmp_path):
    ws = tmp_path / "repo"
    _write(ws, "svc/api.py", "def list_widgets():\n    return WIDGETS\n")
    _write(ws, "svc/db.py", "\n".join(f"widgets_row_{i} = {i}" for i in range(200)))
    idx = CodeIndex(_settings(tmp_path), "p1")
    idx.index_workspace(ws)

    block = idx.retrieval_context("widgets", budget_chars=400)
    assert block is not None and len(block) <= 400
    lines = block.splitlines()
    assert lines[0] == "relevant code (from the project code index; may be stale):"
    assert any(l.startswith("--- svc/") and l.endswith(" ---") and ":" in l for l in lines[1:])
    # header format is exactly "--- <path>:<start>-<end> ---"
    header = next(l for l in lines[1:] if l.startswith("--- "))
    path_span = header[4:-4]
    path, span = path_span.rsplit(":", 1)
    start, end = span.split("-")
    assert path.startswith("svc/") and int(start) >= 1 and int(end) >= int(start)


def test_retrieval_context_none_when_empty(tmp_path):
    idx = CodeIndex(_settings(tmp_path), "p1")
    assert idx.retrieval_context("anything at all") is None
    # tiny budget that can't fit a single chunk line -> None, not a broken fragment
    ws = tmp_path / "repo"
    _write(ws, "a.py", "def something_reasonably_long_named():\n    pass\n")
    idx.index_workspace(ws)
    assert idx.retrieval_context("something", budget_chars=60) is None


# ---- resilience ----

def test_corrupt_db_deleted_and_rebuilt(tmp_path):
    settings = _settings(tmp_path)
    db = settings.projects_dir / "p1" / "code_index.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"this is not a sqlite database, not even close" * 10)
    idx = CodeIndex(settings, "p1")  # must not raise
    ws = tmp_path / "repo"
    _write(ws, "a.py", "def resilient():\n    pass\n")
    assert idx.index_workspace(ws)["indexed"] == 1
    assert idx.search("resilient", top_k=1)


def test_index_and_search_never_raise_on_garbage_input(tmp_path):
    idx = CodeIndex(_settings(tmp_path), "p1")
    assert idx.index_workspace(tmp_path / "does-not-exist") == {
        "indexed": 0, "skipped": 0, "unchanged": 0}
    assert idx.search("") == []
    assert idx.retrieval_context("") is None


def test_max_files_cap_limits_work(tmp_path):
    ws = tmp_path / "repo"
    for i in range(5):
        _write(ws, f"f{i}.py", f"def func_{i}():\n    pass\n")
    idx = CodeIndex(_settings(tmp_path), "p1")
    counts = idx.index_workspace(ws, max_files=3)
    assert counts["indexed"] == 3
