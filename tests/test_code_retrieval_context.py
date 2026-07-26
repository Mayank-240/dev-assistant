"""Semantic code retrieval in subtask prompts (engine wiring of knowledge/code_index).

Engine-level, offline (FakeProvider from test_engine_e2e). CASSETTE-CRITICAL:
only a run on a real repo workspace (project checkout / repo-backed) may add the
"Relevant code (indexed)" part; greenfield runs — which is what the replay eval
records — must assemble prompts byte-identical whether the setting is on or off.
"""

from __future__ import annotations

import dataclasses
import subprocess

from test_engine_e2e import FakeProvider, _settings
from test_workspace_context import _CaptureProvider, _engine

from ai_dev_assistant import projects
from ai_dev_assistant.config import Settings

# The FakeProvider plan's s2 is "Implement change / Write the code." — the fixture
# file leans into those tokens so the hybrid (hash-embedding + lexical) leg ranks it.
_REPO_FILE = "impl.py"
_REPO_TEXT = (
    "# Implement change: write the code for the greeting feature here.\n"
    "def implement_change(code: str) -> str:\n"
    '    """Write the code change with input validation."""\n'
    "    return code.strip()\n"
)


def _seed_git_repo(path):
    path.mkdir(parents=True, exist_ok=True)

    def _git(*args):
        subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                        "-c", "user.name=t", *args], check=True, capture_output=True)

    _git("init")
    (path / _REPO_FILE).write_text(_REPO_TEXT)
    _git("add", "-A")
    _git("commit", "-m", "init")
    return path


def _repo_settings(tmp_path, **overrides) -> Settings:
    """Settings bound to a registered project with a real git checkout."""
    settings = dataclasses.replace(
        _settings(tmp_path),
        # keep the objective/static gates out of this test's way (offline + fast);
        # both arms of every comparison share these knobs, so identity still holds
        objective_review=False, lint_check=False, objective_static=False,
        **overrides)
    repo = _seed_git_repo(tmp_path / "checkout")
    projects._register(settings, projects._normalize(
        {"slug": "proj-r", "name": "proj-r", "created_at": 0.0, "root": str(repo)}))
    return dataclasses.replace(settings, project="proj-r")


async def _run(settings: Settings) -> list[str]:
    fake = _CaptureProvider()
    engine = _engine(settings, fake)
    try:
        await engine.run("Add input validation and document it", on_event=lambda _e: None)
    finally:
        await engine.aclose()
    return fake.prompts


async def test_repo_run_gets_indexed_code_part(tmp_path):
    settings = _repo_settings(tmp_path)
    prompts = await _run(settings)
    joined = "\n<<<>>>\n".join(prompts)
    assert "Relevant code (indexed)" in joined
    assert '<untrusted source="code-index">' in joined   # never instruction-trusted
    assert f"--- {_REPO_FILE}:" in joined                # path:line-span headers
    # ordering: the indexed-code part sits AFTER the repo-map part in the same prompt
    hit = next(p for p in prompts if "Relevant code (indexed)" in p)
    assert (hit.index("Workspace map (most relevant files first)")
            < hit.index("Relevant code (indexed)"))
    # and the index landed next to the project's memory.db
    assert (settings.projects_dir / "proj-r" / "code_index.db").is_file()


async def test_setting_off_disables_code_retrieval_on_repo_runs(tmp_path):
    prompts = await _run(_repo_settings(tmp_path, code_retrieval=False))
    assert prompts  # the run itself completed
    assert all("Relevant code (indexed)" not in p for p in prompts)
    assert all("code-index" not in p for p in prompts)


async def test_greenfield_prompts_byte_identical_with_setting_on_or_off(tmp_path):
    """Replay-cassette guarantee: without a repo workspace the setting is inert —
    prompts with code_retrieval on equal a control run with it off, byte for byte."""
    on = await _run(_settings(tmp_path / "feature"))  # code_retrieval defaults to True
    off = await _run(dataclasses.replace(_settings(tmp_path / "control"),
                                         code_retrieval=False))
    assert len(on) == len(off) == 3
    assert sorted(on) == sorted(off)  # byte-identical, order-independent
    assert all("Relevant code (indexed)" not in p for p in on)
    assert all("code-index" not in p for p in on)
