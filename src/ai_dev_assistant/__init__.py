"""AI Dev Assistant — a self-hosted multi-agent system on the Claude API.

An orchestrator ("boss") agent decomposes a task, routes each subtask to the
best-suited specialized agent, and runs them in parallel through a pooled set of
sessions. Agents share memory, a knowledge base, and a knowledge graph, and can
message one another. Every result is verified and documented (full + brief).
"""

__version__ = "0.1.0"
