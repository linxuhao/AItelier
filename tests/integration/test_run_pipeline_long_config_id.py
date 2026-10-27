"""Normal registered run_pipeline with a long imported repo-less config.

Real DB / SDK / registry state in an isolated home (the shared `runtime`
fixture); generate_run_id, the project validator and start_config_run are all
real. Nothing is driven: no provider is called and no driver loop is attached.

The ordinary import uses an exact 64-character name, the longest the existing
config-name contract (core.tool_guards.bad_config_name) accepts. The originally
observed 68-character generated name is admitted by NEITHER import nor
export/get/edit any more; it stays covered at the launcher-helper level by
tests/unit/test_run_launcher_project_ids.py and is refused at import by
tests/unit/test_pipeline_import_config_name.py.
"""
import re
from pathlib import Path

import yaml

from api import mcp_router
from core import pipeline_bundle as pb, project_id as pid_mod
from core.tool_guards import bad_config_name
from tests.integration.test_nested_callback_seed import runtime  # noqa: F401  (fixture)

ORIGINAL_68 = "gen_sdk100_live_checkpoint_20261027_b0a871b25d544edc88d9578958f66e93"
# Exact 64: same prefix, hex tail cut to what the 64-character contract leaves.
LONG_NAME = ORIGINAL_68[:64]


def _run_pipeline_tool():
    captured = {}

    def tool(name, kind, description):
        def deco(fn):
            captured[name] = fn
            return fn
        return deco
    mcp_router._register_run_tools(tool)
    return captured["run_pipeline"]


def test_long_imported_config_launches_with_valid_project_and_exact_identity(runtime, monkeypatch):
    sf, db, ws, registry, outbound = runtime
    # No event loop means _start_driver attaches nothing: the run stays at its start.
    monkeypatch.setattr(mcp_router, "_MAIN_LOOP", None)
    assert len(ORIGINAL_68) == 68 and bad_config_name(ORIGINAL_68) is not None
    assert len(LONG_NAME) == 64
    assert bad_config_name(LONG_NAME) is None  # the real config-name guard accepts it

    graph = {
        "name": LONG_NAME,
        "x-aitelier": {"repo_mode": "none", "scheduler_owned": False,
                       "seed_file": "task.md", "output_step": "docs"},
        "begin": "docs",
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "node_reached", "node": "done", "result": "completed"}]},
        "steps": [
            {"id": "docs", "step_type": "tool", "tool_name": "skillflow_docs_list",
             "transitions": [{"to": "done"}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    }
    native_bundle = {
        pb.BUNDLE_KEY: pb.BUNDLE_VERSION, "config_name": LONG_NAME,
        "graph_yaml": yaml.safe_dump(graph), "roles": {}, "tools": {},
    }
    imported = pb.import_pipeline(sf, registry, native_bundle)
    assert imported["config_name"] == LONG_NAME
    manifest = registry.get(LONG_NAME)
    assert manifest is not None and manifest.config_name == LONG_NAME
    assert manifest.repo_mode == "none"

    run_pipeline = _run_pipeline_tool()
    first = run_pipeline(LONG_NAME, seed_text="TASK_DATA {}", checkpoints="ask")
    second = run_pipeline(LONG_NAME, seed_text="TASK_DATA {}", checkpoints="ask")
    assert "error" not in first and "error" not in second, (first, second)
    assert first["project_id"] != second["project_id"]
    assert first["run_id"] != second["run_id"]

    for out in (first, second):
        pid = out["project_id"]
        assert pid_mod.is_valid(pid) and pid_mod.require_valid(pid) == pid
        assert pid.isascii() and len(pid) <= 64
        assert re.search(r"-[0-9a-f]{8}$", pid)
        assert pid.startswith("gen-sdk100-live-checkpoint-20261027-")
        assert out["config"] == LONG_NAME and out["checkpoints"] == "ask"
        # Real SDK and DB rows exist and carry the exact, untruncated config identity.
        run = sf.get_run(out["run_id"])
        assert run is not None and run["graph_name"] == LONG_NAME
        assert sf._get_project_id(out["run_id"]) == pid
        row = db.get_project(pid)
        assert row is not None and row["config_name"] == LONG_NAME
        assert row["repo_type"] == "none" and not row.get("repo_path")
    # The registered identity itself was not altered by launching.
    persisted = yaml.safe_load(Path(imported["path"]).read_text())
    assert persisted["name"] == LONG_NAME
    assert registry.get(LONG_NAME).config_name == LONG_NAME
    assert persisted["steps"][0]["step_type"] == "tool"
    assert persisted["steps"][0]["tool_name"] == "skillflow_docs_list"
    assert outbound == []  # Neither a live nor scripted provider was called.
