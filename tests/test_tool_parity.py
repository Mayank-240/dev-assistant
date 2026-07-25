"""Parity check for tools/registry.py: ToolBox._handlers and _TOOL_DEFS are two
parallel structures that can silently drift — every handler must have a schema
definition and vice versa. Compares the two sets to each other (no hardcoded
tool list) so it survives tools being added or removed."""

from __future__ import annotations

from ai_dev_assistant.tools import registry as reg


def _toolbox(tmp_path) -> reg.ToolBox:
    # ToolBox.__init__ only binds handler methods; the subsystems in ToolContext are
    # untouched until a tool is dispatched, so lightweight placeholders suffice.
    ctx = reg.ToolContext(memory=None, kb=None, kg=None, bus=None,
                          agent_name="parity-test", task_scope="parity",
                          base_dir=tmp_path)
    return reg.ToolBox(ctx)


def test_handler_names_match_tool_definition_names(tmp_path):
    tb = _toolbox(tmp_path)
    handler_names = set(tb._handlers.keys())
    def_names = {d["name"] for d in reg._TOOL_DEFS}

    missing_defs = handler_names - def_names
    missing_handlers = def_names - handler_names
    assert not missing_defs, (
        f"handlers with no tool definition (model can never call them): {sorted(missing_defs)}")
    assert not missing_handlers, (
        f"tool definitions with no handler (dispatch returns unknown-tool): {sorted(missing_handlers)}")


def test_definitions_cover_every_handler_when_commands_allowed(tmp_path):
    tb = _toolbox(tmp_path)
    exposed = {d["name"] for d in tb.definitions()}
    assert exposed == set(tb._handlers.keys())


def test_definition_names_are_unique(tmp_path):
    names = [d["name"] for d in reg._TOOL_DEFS]
    assert len(names) == len(set(names)), "duplicate tool definition names"
