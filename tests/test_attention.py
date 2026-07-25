"""Attention screens: agents ask clarifying questions / request permission mid-run,
the UI answers through steer, the engine resolves the waiting tool call."""

from __future__ import annotations

import asyncio

from ai_dev_assistant.config import Settings
from ai_dev_assistant.engine import Engine
from ai_dev_assistant.llm.schemas import BriefDoc, Plan, SubTask, Verdict
from ai_dev_assistant.orchestration.run_control import RunControl
from ai_dev_assistant.tools.registry import ToolBox, ToolContext


# ---- RunControl: request registry + steer-note parsing ----

async def test_runcontrol_answer_resolves_waiting_request():
    rc = RunControl()
    rid = rc.open_request("ask")
    assert rid.startswith("ask-")

    async def answer_later():
        await asyncio.sleep(0.02)
        rc.steer(f"[answer {rid}] Use SQLite")

    task = asyncio.create_task(answer_later())
    got = await rc.wait_answer(rid, timeout=2)
    await task
    assert got == "Use SQLite"
    assert rc.drain_steer() == []  # consumed, not queued as a context note


async def test_runcontrol_permission_and_plain_notes():
    rc = RunControl()
    rid = rc.open_request("permission")

    async def decide():
        await asyncio.sleep(0.02)
        rc.steer("just a normal steer note")           # queued
        rc.steer("[answer nope-99] orphan answer")      # unknown id -> queued
        rc.steer(f"[permission {rid}] DENIED: too risky")

    task = asyncio.create_task(decide())
    got = await rc.wait_answer(rid, timeout=2)
    await task
    assert got == "DENIED: too risky"
    notes = rc.drain_steer()
    assert "just a normal steer note" in notes
    assert any("orphan answer" in n for n in notes)


async def test_runcontrol_timeout_returns_none():
    rc = RunControl()
    rid = rc.open_request("ask")
    assert await rc.wait_answer(rid, timeout=0.05) is None


# ---- ToolBox: attention tools through dispatch_async ----

def _box(tmp_path, attention=None) -> ToolBox:
    return ToolBox(ToolContext(
        memory=None, kb=None, kg=None, bus=None, agent_name="coder",
        task_scope="t", base_dir=tmp_path, workspace=tmp_path,
        verify_timeout=5, redact=False, audit=None,
        allow_run_command=False, sandbox="none", sandbox_cpu=5, sandbox_mem=256,
        attention=attention,
    ))


async def test_ask_operator_roundtrip(tmp_path):
    seen = {}

    async def attention(kind, text, options):
        seen.update(kind=kind, text=text, options=options)
        return "Postgres, please"

    box = _box(tmp_path, attention)
    out = await box.dispatch_async("ask_operator",
                                   {"question": "Which DB?", "options": ["SQLite", "Postgres"]})
    assert out == "Operator answered: Postgres, please"
    assert seen == {"kind": "ask", "text": "Which DB?", "options": ["SQLite", "Postgres"]}


async def test_request_permission_decisions(tmp_path):
    for reply, needle in [("DENIED: no", "DENIED"),
                          ("ALLOW ONCE: fine", "once"),
                          ("ALLOW FOR THIS RUN: go", "entire run")]:
        async def attention(kind, text, options, _r=reply):
            return _r
        out = await _box(tmp_path, attention).dispatch_async(
            "request_permission", {"request": "edit infra/main.tf"})
        assert needle.lower() in out.lower()


async def test_attention_fallbacks(tmp_path):
    box = _box(tmp_path, attention=None)
    out = await box.dispatch_async("ask_operator", {"question": "Which DB?"})
    assert "best judgment" in out

    async def silent(kind, text, options):
        return None
    out = await _box(tmp_path, silent).dispatch_async("request_permission", {"request": "x"})
    assert "DENIED" in out


def test_sync_dispatch_guards_async_tools(tmp_path):
    out = _box(tmp_path).dispatch("ask_operator", {"question": "hm?"})
    assert out.startswith("ERROR") and "async" in out


# ---- Engine end-to-end: ask event -> steer answer -> agent sees it ----

class _Usage:
    def __init__(self):
        self.cost_usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0

    def to_dict(self):
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_usd": self.cost_usd}


class AskingProvider:
    def __init__(self) -> None:
        self.usage = _Usage()
        self.answer_seen = ""

    async def structured(self, *, system, user, schema, model, effort=None, max_tokens=4000):
        if schema is Plan:
            return Plan(summary="One step.", subtasks=[
                SubTask(id="s1", title="Pick a database", description="Choose storage.",
                        agent="coder", rationale="c", depends_on=[],
                        acceptance_criteria=["a choice is made"])])
        if schema is Verdict:
            return Verdict(passed=True, score=90, reasons=["ok"], suggestions=[])
        if schema is BriefDoc:
            return BriefDoc(tldr="Done.", key_points=["done"], status="completed")
        raise AssertionError(schema)

    async def run_agent(self, *, prompt, toolbox, on_step=None, **_kw):
        self.answer_seen = await toolbox.dispatch_async(
            "ask_operator", {"question": "Which database should I use?",
                             "options": ["SQLite", "Postgres"]})
        return f"Decided based on: {self.answer_seen}"

    async def aclose(self):
        pass


async def test_engine_ask_event_and_steer_answer(tmp_path):
    settings = Settings(
        llm_backend="anthropic", anthropic_api_key="", embeddings_backend="hash",
        data_dir=tmp_path / "data", docs_dir=tmp_path / "docs",
        workspace_dir=tmp_path / "workspace",
        session_idle_ttl=0.05, reaper_interval=0.02, max_retries=0,
        verify_run_tests=False, attention_timeout=5.0,
    )
    engine = Engine(settings)
    provider = AskingProvider()
    engine.provider = provider
    engine.orchestrator._provider = provider
    engine.reviewer._provider = provider
    engine.control = RunControl()

    asks = []

    def on_event(e):
        if e.type == "ask":
            asks.append(e)
            # the "UI": answer through steer exactly as app.js does
            engine.control.steer(f"[answer {e.data['id']}] Postgres")

    try:
        run, _b, _o = await engine.run("pick a db", on_event=on_event)
    finally:
        await engine.aclose()

    assert run.all_passed
    assert len(asks) == 1
    assert asks[0].data["agent"] == "coder"
    assert asks[0].data["question"] == "Which database should I use?"
    assert asks[0].data["options"] == ["SQLite", "Postgres"]
    assert provider.answer_seen == "Operator answered: Postgres"
