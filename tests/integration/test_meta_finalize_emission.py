"""Meta finalize emission — the meta_conversation `finalize` tool step now
produces the project artifacts (project_brief.md, spec.md, step1_goals.json) as
the graph's declared output, instead of host glue (seed_and_trigger).

Two layers:
  - Unit: the emit_project_artifacts tool transforms gather_state + transcript
    into the three artifacts in the right locations.
  - E2E: a REAL meta_conversation skillflow run, driven through the gather step
    (mocked agent) to its checkpoint, then approve_meta — asserting the run
    self-completes via the node_reached end-condition and the artifacts land.
"""

import json
from pathlib import Path

import pytest
import yaml

from skillflow import SkillFlow, PipelineGraph
import skillflow as _skillflow_pkg
from skillflow.tool_loader import ToolLoader

from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager
from core import meta_run
from aitelier.tools.emit_project_artifacts.impl import emit_project_artifacts

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_BRIEF = {
    "project_name": "Card Game",
    "description": "A 2-player card game.",
    "user_stories": ["As a player, I want to draw cards, so that I can play."],
    "goals": ["Playable MVP"],
    "non_goals": ["Multiplayer netcode"],
    "tech_constraints": ["Unity / C#"],
    "target_users": "casual players",
    "success_criteria": "press Play and play a hand",
}
_RULE = "RULE 7: a flush beats a straight; ties split the pot exactly."


# ── Unit: the tool itself ────────────────────────────────────────────────

class TestEmitToolUnit:
    def _seed(self, ws: Path):
        gather = ws / "meta_conversation" / "gather"
        gather.mkdir(parents=True)
        (gather / "gather_state.json").write_text(
            json.dumps({"need_input": False, "brief": _BRIEF}), encoding="utf-8")
        meta = ws / "meta"; meta.mkdir(parents=True)
        (meta / "conversation.md").write_text(
            f"User: build a card game\nAssistant: ok\nUser: {_RULE}\n", encoding="utf-8")

    def test_emits_brief_spec_and_goals(self, tmp_path):
        ws = tmp_path / "p"
        self._seed(ws)
        out_dir = ws / "meta_conversation" / "finalize"
        res = emit_project_artifacts(
            workspace_root=str(ws), out_dir=str(out_dir))

        assert res["emitted"] is True
        # Brief slot
        brief_md = (ws / "project" / "project_brief.md").read_text(encoding="utf-8")
        assert "Card Game" in brief_md and "draw cards" in brief_md
        # Spec — verbatim rule survives un-summarized
        spec_md = (ws / "project" / "spec.md").read_text(encoding="utf-8")
        assert _RULE in spec_md
        # step1_goals.json into the finalize step dir (DPE cross-config source)
        goals = json.loads((out_dir / "step1_goals.json").read_text(encoding="utf-8"))
        assert goals["project_name"] == "Card Game"
        assert goals["user_stories"] and goals["mvp_goals"] == ["Playable MVP"]

    def test_no_spec_when_transcript_absent(self, tmp_path):
        ws = tmp_path / "p"
        gather = ws / "meta_conversation" / "gather"; gather.mkdir(parents=True)
        (gather / "gather_state.json").write_text(
            json.dumps({"need_input": False, "brief": _BRIEF}), encoding="utf-8")
        emit_project_artifacts(workspace_root=str(ws),
                               out_dir=str(ws / "meta_conversation" / "finalize"))
        assert (ws / "project" / "project_brief.md").exists()
        assert not (ws / "project" / "spec.md").exists()  # nothing to write

    def test_raises_on_brief_without_user_stories(self, tmp_path):
        """Fix E: the SOLE producer must fail loud on an incomplete brief so the
        require_completed end-condition can't complete the run on an empty brief."""
        ws = tmp_path / "p"
        gather = ws / "meta_conversation" / "gather"; gather.mkdir(parents=True)
        (gather / "gather_state.json").write_text(
            json.dumps({"need_input": False, "brief": {"project_name": "X"}}),
            encoding="utf-8")
        with pytest.raises(ValueError):
            emit_project_artifacts(workspace_root=str(ws),
                                   out_dir=str(ws / "meta_conversation" / "finalize"))
        # No empty brief was written.
        assert not (ws / "project" / "project_brief.md").exists()

    def test_raises_on_corrupt_gather_state(self, tmp_path):
        ws = tmp_path / "p"
        gather = ws / "meta_conversation" / "gather"; gather.mkdir(parents=True)
        (gather / "gather_state.json").write_text("{ not json", encoding="utf-8")
        with pytest.raises(ValueError):
            emit_project_artifacts(workspace_root=str(ws),
                                   out_dir=str(ws / "meta_conversation" / "finalize"))


# ── E2E: real skillflow meta run, gather → approve → self-complete ───────

def _build_meta_run(tmp_path):
    ws_base = tmp_path / "ws"
    loader = ToolLoader(Path(_skillflow_pkg.__file__).parent / "tools")
    loader.add_tools_dir(_REPO_ROOT / "aitelier" / "tools")
    # Mirror the DPE harness: make the inline tool node discoverable as native so
    # advance_run runs it inline (framework mode runs it inline regardless, but
    # this matches the proven test setup).
    _orig = loader.is_native
    loader.is_native = lambda n: n == "emit_project_artifacts" or _orig(n)

    sf = SkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
                   workspace_base=str(ws_base),
                   projects_base=str(tmp_path / "proj"),
                   stale_threshold_seconds=60)
    for f in sorted((_REPO_ROOT / "agent_configs").glob("*.yaml")):
        for name, cfg in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).items():
            try:
                sf.register_agent_config_from_dict(name, cfg)
            except Exception:
                pass
    graph = PipelineGraph.from_yaml(_REPO_ROOT / "configs" / "meta_conversation.yaml")
    sf.register_graph(graph)

    db = DBManager(str(tmp_path / "aitelier.db"))
    db.ensure_project("p", name="Meta E2E")
    ws = WorkspaceManager(base_path=str(ws_base))
    run_id = sf.create_run(graph.name, {"project_id": "p"})
    sf.start_run(run_id)
    return sf, db, ws, run_id


class TestMetaRunE2E:
    async def test_approve_emits_artifacts_and_run_completes(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        import core.agents as agents_mod
        import api.dependencies as deps
        from aitelier.runner import AgentStepRunner

        sf, db, ws, run_id = _build_meta_run(tmp_path)
        # Runner/tools resolve the workspace via get_skillflow() — point it at THIS sf.
        monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
        ws_root = Path(sf._workspace.get_project_path("p"))

        # The butler maintains the transcript; seed it so spec.md has content.
        (ws_root / "meta").mkdir(parents=True, exist_ok=True)
        (ws_root / "meta" / "conversation.md").write_text(
            f"User: build a card game\nUser: {_RULE}\n", encoding="utf-8")

        # Mocked agent: emit the step's generated write_* action carrying the
        # canned content (mirrors the DPE real-runner harness). intent_detect →
        # new_project; gather → finalized brief.
        def response_for(step_id, tool_schemas):
            if step_id == "intent_detect":
                content = json.dumps({"intent": "new_project"})
            elif step_id == "gather":
                content = json.dumps({"need_input": False, "brief": _BRIEF})
            else:
                content = "{}"
            writes = [k for k in tool_schemas if k.startswith("write_")]
            tool = writes[0] if writes else "write"
            return json.dumps({"thoughts": "ok",
                               "actions": [{"tool": tool, "params": {"content": content}}]})

        cur = {"response": "{}"}

        def fake_get_agent(self, name):
            mg = MagicMock()
            mg.gateway.litellm_model = "mock-model"
            mg.run.side_effect = lambda *a, **k: cur["response"]
            return mg

        monkeypatch.setattr(agents_mod.AgentFactory, "get_agent", fake_get_agent)
        monkeypatch.setattr(agents_mod.AgentFactory, "is_native", lambda self, n: False)
        monkeypatch.setattr(agents_mod.AgentFactory, "get_max_retries", lambda self, s: 1)
        monkeypatch.setattr(agents_mod.AgentFactory, "get_max_tool_turns", lambda self, s: 2)

        runner = AgentStepRunner(db_manager=db, workspace_manager=ws,
                                 agent_factory=None, prompt_assembler=None, event_bus=None)

        # Drive to the gather checkpoint (intent_detect then gather, which pauses).
        paused = False
        for _ in range(40):
            node = sf.advance_run(run_id)
            if node is None:
                if sf.get_run(run_id)["status"] == "paused":
                    paused = True
                    break
                continue
            claimed = sf.claim_next_step(run_id)
            if claimed is None:
                continue
            cur["response"] = response_for(
                claimed.step_id, claimed.inputs.get("_tool_schemas", {}))
            result = await runner.execute(claimed)
            sf.confirm_step(claimed.token, result)
        assert paused, "run never reached the gather checkpoint"

        # Approve → finalize tool emits artifacts → end-condition completes run.
        meta_run.approve_meta(sf, run_id)

        assert sf.get_run(run_id)["status"] == "completed"
        assert (ws_root / "project" / "project_brief.md").read_text(encoding="utf-8")
        assert _RULE in (ws_root / "project" / "spec.md").read_text(encoding="utf-8")
        goals_path = ws_root / "meta_conversation" / "finalize" / "step1_goals.json"
        assert goals_path.is_file()
        # And the DPE researcher's cross-config source can now actually resolve it.
        goals = json.loads(goals_path.read_text(encoding="utf-8"))
        assert goals["project_name"] == "Card Game"
