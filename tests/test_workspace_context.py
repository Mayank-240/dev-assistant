"""Workspace-aware context: sibling recall/KB hits in subtask prompts.

Engine-level, offline (FakeProvider from test_engine_e2e). The feature is
CASSETTE-CRITICAL: a project outside any workspace must assemble prompts
byte-identical to the pre-feature behavior — proven here by comparing captured
prompts against a control run with ``workspace_of`` forced to None.
"""

from __future__ import annotations

import dataclasses
import re
from unittest import mock

from test_engine_e2e import FakeProvider, _settings

import ai_dev_assistant.engine as engine_mod
from ai_dev_assistant import projects, workspaces
from ai_dev_assistant.config import Settings
from ai_dev_assistant.engine import Engine
from ai_dev_assistant.knowledge.base import KnowledgeBase
from ai_dev_assistant.memory.store import MemoryStore


class _CaptureProvider(FakeProvider):
    """FakeProvider that records every agent prompt (task text + assembled context)."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    async def run_agent(self, *, prompt, **kw):
        self.prompts.append(prompt)
        return await super().run_agent(prompt=prompt, **kw)


def _register(settings: Settings, *slugs: str) -> None:
    """Thin registry entries (no git checkout) — enough for workspace membership."""
    for slug in slugs:
        projects._register(settings, projects._normalize(
            {"slug": slug, "name": slug, "created_at": 0.0}))


def _seed_sibling(settings: Settings, slug: str, memory: str, kb_doc: str = "") -> None:
    store = MemoryStore(dataclasses.replace(settings, project=slug))
    try:
        store.remember("longterm", memory)
        if kb_doc:
            KnowledgeBase(store.vectors).ingest(f"seed:{slug}", kb_doc)
    finally:
        store.close()


def _engine(settings: Settings, fake: FakeProvider) -> Engine:
    engine = Engine(settings)
    engine.provider = fake
    engine.orchestrator._provider = fake
    engine.reviewer._provider = fake
    return engine


async def _run(settings: Settings) -> list[str]:
    fake = _CaptureProvider()
    engine = _engine(settings, fake)
    try:
        await engine.run("Add input validation and document it", on_event=lambda _e: None)
    finally:
        await engine.aclose()
    return fake.prompts


# The FakeProvider plan's s2 is "Implement change / Write the code." — seed sibling
# knowledge with heavy token overlap so hybrid recall clears the min-score bars.
_B_MEMORY = "[DO] Implement change: write the code with strict input validation."
_B_KB = "Implement change guidance: the code change should reuse the shared validators."


async def test_run_recalls_sibling_memory_with_attribution(tmp_path):
    settings = dataclasses.replace(_settings(tmp_path), project="proj-a")
    _register(settings, "proj-a", "proj-b")
    _seed_sibling(settings, "proj-b", _B_MEMORY, _B_KB)
    workspaces.create_workspace(settings, "Acme Suite", projects=["proj-a", "proj-b"])

    prompts = await _run(settings)

    joined = "\n<<<>>>\n".join(prompts)
    assert "Related knowledge from workspace 'Acme Suite'" in joined
    assert "[proj-b] " in joined                       # per-item attribution
    assert '<untrusted source="workspace-sibling">' in joined  # wrapped like KB hits
    # the sibling section sits AFTER the project's own KB part would (own parts first):
    hit = next(p for p in prompts if "workspace 'Acme Suite'" in p)
    assert hit.index("Overall task") < hit.index("Related knowledge from workspace")


async def test_no_workspace_prompts_byte_identical_to_pre_feature(tmp_path):
    """Replay-cassette guarantee: with no workspace, prompts equal a control run
    whose workspace hook is disabled entirely (pre-feature behavior)."""
    with mock.patch.object(engine_mod, "workspace_of", lambda *_a, **_k: None):
        before = await _run(_settings(tmp_path / "control"))
    after = await _run(_settings(tmp_path / "feature"))
    assert len(before) == len(after) == 3
    assert sorted(before) == sorted(after)  # byte-identical, order-independent
    assert all("workspace-sibling" not in p for p in after)


async def test_setting_off_disables_sibling_context(tmp_path):
    settings = dataclasses.replace(_settings(tmp_path), project="proj-a",
                                   workspace_context=False)
    _register(settings, "proj-a", "proj-b")
    _seed_sibling(settings, "proj-b", _B_MEMORY, _B_KB)
    workspaces.create_workspace(settings, "Acme Suite", projects=["proj-a", "proj-b"])

    prompts = await _run(settings)
    assert all("Related knowledge from workspace" not in p for p in prompts)
    assert all("workspace-sibling" not in p for p in prompts)


async def test_sibling_without_stores_is_silent_and_creates_no_files(tmp_path):
    settings = dataclasses.replace(_settings(tmp_path), project="proj-a")
    _register(settings, "proj-a", "proj-c")  # proj-c: registered, never seeded
    workspaces.create_workspace(settings, "Acme Suite", projects=["proj-a", "proj-c"])

    prompts = await _run(settings)  # must not raise

    assert all("Related knowledge from workspace" not in p for p in prompts)
    sib_dir = settings.projects_dir / "proj-c"
    assert not (sib_dir / "memory.db").exists()  # read side must never create it
    assert list(sib_dir.iterdir()) == []         # no other side-effect files either


async def test_at_most_three_siblings_are_queried(tmp_path):
    settings = dataclasses.replace(_settings(tmp_path), project="proj-a")
    slugs = [f"proj-s{i}" for i in range(1, 6)]
    _register(settings, "proj-a", *slugs)
    for slug in slugs:
        _seed_sibling(settings, slug, f"shared validation helper design note from {slug}")
    workspaces.create_workspace(settings, "Big WS", projects=["proj-a", *slugs])

    part = _engine(settings, FakeProvider())._workspace_sibling_context(
        "shared validation helper design")
    assert part is not None and part[0] == "Related knowledge from workspace 'Big WS'"
    mentioned = set(re.findall(r"\[(proj-s\d)\]", part[1]))
    assert mentioned == {"proj-s1", "proj-s2", "proj-s3"}  # first 3 siblings only


async def test_sibling_section_char_cap_respected(tmp_path):
    settings = dataclasses.replace(_settings(tmp_path), project="proj-a")
    slugs = ["proj-s1", "proj-s2", "proj-s3"]
    _register(settings, "proj-a", *slugs)
    long_text = "shared validation helper design guidance. " * 20  # ~840 chars
    for slug in slugs:  # 2 memories + 1 KB doc each -> far more than the cap allows
        store = MemoryStore(dataclasses.replace(settings, project=slug))
        try:
            store.remember("longterm", f"{slug} first: {long_text}")
            store.remember("longterm", f"{slug} second: {long_text}")
            KnowledgeBase(store.vectors).ingest(f"seed:{slug}", f"{slug} doc: {long_text}")
        finally:
            store.close()
    workspaces.create_workspace(settings, "Big WS", projects=["proj-a", *slugs])

    part = _engine(settings, FakeProvider())._workspace_sibling_context(
        "shared validation helper design")
    assert part is not None
    bullets = [l for l in part[1].splitlines() if l.startswith("- [")]
    assert bullets  # something was borrowed …
    body_len = sum(len(l) + 1 for l in bullets)
    assert body_len <= 1500  # … but the whole section stays under the cap
