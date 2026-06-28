"""BaseAgent: a capability profile plus an agentic run, delegated to the LLM provider.

The provider owns the actual tool-use loop (Anthropic API or Claude Agent SDK); the
agent just supplies its persona, model, allowed tools, and the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..llm.provider import LLMProvider, StepFn
from ..tools.registry import ToolBox


@dataclass
class AgentProfile:
    name: str
    description: str          # what this agent is
    when_to_use: str          # guidance the orchestrator uses for routing
    tools: list[str] = field(default_factory=list)
    effort: str | None = None


class BaseAgent:
    def __init__(self, profile: AgentProfile, system_prompt: str, model: str) -> None:
        self.profile = profile
        self.system_prompt = system_prompt
        self.model = model

    @property
    def name(self) -> str:
        return self.profile.name

    async def run(
        self,
        *,
        task_text: str,
        context: str,
        toolbox: ToolBox,
        provider: LLMProvider,
        workdir: str | None = None,
        on_step: StepFn | None = None,
    ) -> str:
        prompt = task_text if not context else f"{task_text}\n\n--- Context ---\n{context}"
        return await provider.run_agent(
            system_prompt=self.system_prompt,
            prompt=prompt,
            toolbox=toolbox,
            allowed_tools=self.profile.tools,
            model=self.model,
            effort=self.profile.effort,
            workdir=workdir,
            on_step=on_step,
        )
