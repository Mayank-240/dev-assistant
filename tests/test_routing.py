from ai_dev_assistant.agents.orchestrator import Orchestrator
from ai_dev_assistant.agents.registry import build_agents
from ai_dev_assistant.config import Settings
from ai_dev_assistant.llm.schemas import Plan, SubTask


def test_sanitize_remaps_unknown_agent_and_drops_bad_deps():
    agents = build_agents(Settings())
    plan = Plan(
        summary="x",
        subtasks=[
            SubTask(
                id="s1",
                title="t",
                description="d",
                agent="does-not-exist",
                rationale="r",
                depends_on=["missing", "s1"],  # dangling + self
                acceptance_criteria=[],
            ),
            SubTask(
                id="s2", title="t2", description="d2", agent="coder", rationale="r",
                depends_on=["s1"], acceptance_criteria=["works"],
            ),
        ],
    )
    fixed = Orchestrator._sanitize(plan, agents)
    s1 = fixed.subtasks[0]
    assert s1.agent == "researcher"          # remapped to default
    assert s1.depends_on == []               # dangling + self-dep removed
    assert fixed.subtasks[1].agent == "coder"  # valid agent kept
    assert fixed.subtasks[1].depends_on == ["s1"]  # valid dep kept


def test_default_roster_has_expected_agents():
    agents = build_agents(Settings())
    assert {"researcher", "coder", "documenter"} <= set(agents)
