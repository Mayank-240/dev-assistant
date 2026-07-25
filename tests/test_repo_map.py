import re

from ai_dev_assistant.knowledge.repo_map import build_repo_map


def _mini_repo(tmp_path):
    (tmp_path / "core.py").write_text(
        "def connect():\n    pass\n\nclass Engine:\n    pass\n"
    )
    (tmp_path / "a.py").write_text("import core\n\ndef run_a():\n    pass\n")
    (tmp_path / "b.py").write_text("from core import Engine\n\ndef run_b():\n    pass\n")
    (tmp_path / "c.py").write_text("import core\n\ndef run_c():\n    pass\n")
    (tmp_path / "lonely.py").write_text("def unused_helper():\n    pass\n")
    (tmp_path / "payments.py").write_text(
        "def charge_stripe_customer():\n    pass\n\ndef refund_payment():\n    pass\n"
    )
    (tmp_path / "README.md").write_text("# demo project\n")
    (tmp_path / "index.js").write_text(
        "export function renderWidget() {}\nclass WidgetStore {}\n"
    )
    return tmp_path


def _line_index(text: str, path: str) -> int:
    for i, line in enumerate(text.splitlines()):
        if line == path or line.startswith(path + " "):
            return i
    raise AssertionError(f"{path} not listed in map:\n{text}")


def test_heavily_imported_module_ranks_above_unimported(tmp_path):
    out = build_repo_map(_mini_repo(tmp_path))
    assert _line_index(out, "core.py") < _line_index(out, "lonely.py")
    # symbol annotation on the ranked line
    core_line = out.splitlines()[_line_index(out, "core.py")]
    assert "connect" in core_line and "Engine" in core_line


def test_query_boosts_matching_files(tmp_path):
    root = _mini_repo(tmp_path)
    plain = build_repo_map(root)
    boosted = build_repo_map(root, 6000, "stripe payment refund handling")
    # without a query the import-graph hub wins; with the query, payments.py does
    assert _line_index(plain, "core.py") < _line_index(plain, "payments.py")
    assert _line_index(boosted, "payments.py") < _line_index(boosted, "core.py")


def test_respects_max_chars_and_never_ends_mid_line(tmp_path):
    root = _mini_repo(tmp_path)
    out = build_repo_map(root, 200)
    assert 0 < len(out) <= 200
    # omission is explicit (a count), never a silent mid-line cut
    last = out.splitlines()[-1]
    assert re.search(r"…\d+ more files omitted \(", last)


def test_larger_budget_lists_files_and_still_summarizes_rest(tmp_path):
    root = _mini_repo(tmp_path)
    out = build_repo_map(root, 350)
    assert len(out) <= 350
    lines = out.splitlines()
    listed = [ln for ln in lines if ln.endswith(".py") or " — " in ln]
    assert listed, f"expected at least one ranked file line:\n{out}"
    if "more files omitted" in out:
        assert lines[-1].startswith("…")


def test_non_python_files_still_listed(tmp_path):
    out = build_repo_map(_mini_repo(tmp_path))
    assert _line_index(out, "README.md") >= 0
    js_line = out.splitlines()[_line_index(out, "index.js")]
    assert "renderWidget" in js_line  # JS symbols come from the regex outline


def test_backward_compatible_call_shapes(tmp_path):
    root = _mini_repo(tmp_path)
    assert build_repo_map(root)  # workspace only (engine call shape)
    assert build_repo_map(root, 500)  # workspace + max_chars
    assert build_repo_map(tmp_path / "does-not-exist") == ""
