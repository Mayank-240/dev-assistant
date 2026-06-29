"""Pydantic schemas used for Claude structured outputs.

Kept deliberately simple (str / list[str] / int / bool) so they map cleanly onto
the structured-output JSON-schema subset the API supports.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SubTask(BaseModel):
    """One unit of work the orchestrator routes to a single agent."""

    id: str = Field(description="Short stable id, e.g. 's1'. Referenced by depends_on.")
    title: str = Field(description="Imperative one-line title.")
    description: str = Field(description="What this subtask must accomplish, in detail.")
    agent: str = Field(description="Name of the specialized agent best suited to this subtask.")
    rationale: str = Field(description="Why this agent was chosen for this subtask.")
    depends_on: list[str] = Field(
        default_factory=list,
        description="Ids of subtasks that must finish before this one can start.",
    )
    acceptance_criteria: list[str] = Field(
        default_factory=list,
        description="Concrete, checkable conditions the result must satisfy.",
    )


class Plan(BaseModel):
    """The orchestrator's decomposition of a task into a routed DAG of subtasks."""

    title: str = Field(
        default="",
        description="A short, human-friendly title for the overall task "
        "(3-7 words, Title Case, no trailing punctuation).",
    )
    summary: str = Field(description="One-paragraph summary of the overall approach.")
    subtasks: list[SubTask] = Field(description="The subtasks, in any order; deps define ordering.")


class CriterionResult(BaseModel):
    """Per-acceptance-criterion judgment, so a verdict says WHICH criterion failed and why."""

    criterion: str = Field(description="The acceptance criterion being judged.")
    met: bool = Field(description="Whether this specific criterion is satisfied.")
    evidence: str = Field(default="", description="The concrete evidence for the judgment.")


class Verdict(BaseModel):
    """The reviewer's verification of a single subtask result."""

    passed: bool = Field(description="True only if every acceptance criterion is met.")
    score: int = Field(description="Confidence 0-100 that the result is correct and complete.")
    reasons: list[str] = Field(default_factory=list, description="Why it passed or failed.")
    suggestions: list[str] = Field(
        default_factory=list,
        description="Concrete fixes to apply on retry if it failed.",
    )
    # Optional richer signal (Tier 3). Default-empty so older/fake providers still validate.
    criteria: list[CriterionResult] = Field(
        default_factory=list, description="Per-criterion breakdown of what passed/failed."
    )
    objective_note: str = Field(
        default="", description="Summary of objective signals (tests/lint) folded into this verdict."
    )


class RunLessons(BaseModel):
    """A structured post-mortem distilled from a finished run (Tier 4 learning loop)."""

    summary: str = Field(description="One-sentence outcome-aware summary of the run.")
    what_worked: list[str] = Field(default_factory=list, description="Approaches that succeeded.")
    what_to_avoid: list[str] = Field(default_factory=list, description="Mistakes / dead ends to avoid next time.")
    routing_notes: list[str] = Field(
        default_factory=list, description="Which agent suited (or didn't suit) which kind of subtask."
    )


class BriefDoc(BaseModel):
    """The one-glance summary of a completed task."""

    tldr: str = Field(description="Two or three sentences a human can read in 10 seconds.")
    key_points: list[str] = Field(description="The handful of things that actually happened.")
    status: str = Field(description="Overall outcome, e.g. 'completed', 'completed with caveats'.")
