"""An agent's tool call may not pick its own root.

skillflow's `execute_tool` does `kwargs.setdefault("project_root", <injected>)`,
so a `project_root` supplied in the agent's arguments would win over the host's.
The host strips the host-owned names before forwarding.
"""
from core.dpe_pipeline import _strip_agent_roots, _AGENT_RESERVED_ARGS


def test_reserved_root_arguments_are_dropped():
    out = _strip_agent_roots({"query": "x", "project_root": "/etc", "workspace_root": "/tmp",
                              "step_dir": "/a", "out_dir": "/b", "limit": 3})
    assert out == {"query": "x", "limit": 3}


def test_ordinary_arguments_pass_through_untouched():
    assert _strip_agent_roots({"path": "a.py", "start_line": 3}) == {"path": "a.py", "start_line": 3}


def test_non_dict_params_become_empty():
    assert _strip_agent_roots(None) == {} and _strip_agent_roots("x") == {}


def test_the_reserved_set_names_every_root_the_inventory_guards():
    assert {"project_root", "workspace_root"} <= set(_AGENT_RESERVED_ARGS)
