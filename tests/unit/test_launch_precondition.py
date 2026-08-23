"""A launch that cannot reach its first step must be refused, not reported started.

jinyong-usable, 2026-08-23: POST /api/runs started dpe_game and answered
201 {"status":"started","run_id":"4d315a96-…"}. Thirty seconds later the run was
`failed`, and the entire trace was one line in the scheduler tick log —
`outcome=claim_terminal … Required context source resolved to no content:
finalize`. dpe_default_v2 step "1" imports
meta_conversation/finalize/step1_goals.json with `required: true`; the generic
launcher never runs meta_conversation, so that artifact did not exist and step 1
could not be claimed on that tick or any other. Nothing was wrong with the run
except that it had no first move — and the caller had been told a build was under
way.

The sibling entry path (core/project_submit.py:seed_and_trigger) has refused the
same missing artifact since a brief-less DPE run spun on that message for 47
minutes. These pin the same refusal on the generic path, generically: any
required input a config imports from ANOTHER config's run.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml
from skillflow.compose import compose_graph
from skillflow.graph import GraphResolver, PipelineGraph

from core.run_launcher import start_config_run

CONFIGS = Path(__file__).resolve().parents[2] / "configs"


def _dpe_game_graph() -> PipelineGraph:
    """The real composed dpe_game graph — the config the live launch used.

    Composed here the way the host composes it at boot (base + the game_harness
    overlay) so the precondition under test is the SHIPPED one: a synthetic graph
    carrying a hand-written cross-config source would keep passing after step 1
    stopped importing the goals.
    """
    base = PipelineGraph.from_yaml(CONFIGS / "dpe_default.yaml").to_dict()
    addon = yaml.safe_load(
        (CONFIGS / "addons" / "game_harness.yaml").read_text(encoding="utf-8"))
    merged = compose_graph(base, [addon])
    merged["name"] = "dpe_game"
    return PipelineGraph._from_dict(merged)


def _manifest(config_name: str, **over):
    base = dict(config_name=config_name, seed_file="project_brief.md",
                scheduler_owned=True, registers_generated_pipeline=False,
                registers_generated_addon=False, repo_mode="code")
    base.update(over)
    return SimpleNamespace(**base)


def _launch(config_name: str, graph: PipelineGraph, workspace_root: Path,
            manifest=None):
    """Run the generic launch path over *graph*, with the project workspace
    rooted at *workspace_root*. Returns (result, skillflow stub, workspace stub)."""
    db = MagicMock()
    db.get_project.return_value = None
    ws = MagicMock()
    registry = MagicMock()
    registry.get.return_value = manifest or _manifest(config_name)
    sf = MagicMock()
    sf._get_resolver.return_value = GraphResolver(graph)
    sf._workspace.get_project_path.return_value = workspace_root
    sf.get_run.return_value = {"status": "running"}
    with patch("api.dependencies.get_skillflow", return_value=sf), \
         patch("api.dependencies.get_config_registry", return_value=registry), \
         patch("core.scheduler.wake_scheduler"):
        return start_config_run(db, ws, config_name, "pid_x"), sf, ws


def _finalize_goals(workspace_root: Path, body: str = '{"goals": ["ship it"]}'):
    p = workspace_root / "meta_conversation" / "finalize" / "step1_goals.json"
    p.parent.mkdir(parents=True)
    p.write_text(body, encoding="utf-8")


def test_dpe_game_without_a_meta_conversation_creates_no_run(tmp_path):
    """The live case: no step1_goals.json, so no run — and the caller is told why."""
    result, sf, ws = _launch("dpe_game", _dpe_game_graph(), tmp_path)

    assert result["status"] == "error"
    sf.get_or_create_run.assert_not_called()
    # Nothing half-built either: the refusal lands before the repo is created.
    ws.setup_workspace.assert_not_called()
    # The message has to carry all four facts the tick log withheld: what was
    # being started, what is missing, who produces it, and what to do about it.
    msg = result["message"]
    assert "dpe_game" in msg
    assert "step1_goals.json" in msg
    assert "meta_conversation" in msg and "finalize" in msg
    assert "butler" in msg


def test_dpe_game_starts_once_finalize_has_produced_the_goals(tmp_path):
    """The guard refuses an absent input, not a cross-config input."""
    _finalize_goals(tmp_path)
    result, sf, ws = _launch("dpe_game", _dpe_game_graph(), tmp_path)

    assert result["status"] == "started"
    sf.get_or_create_run.assert_called_once()


def test_an_empty_goals_file_is_not_a_brief(tmp_path):
    """Absence is what skillflow means by "resolved to no content" — an artifact
    that exists and says nothing fails the step exactly like a missing one, so
    the launch has to read it the same way."""
    _finalize_goals(tmp_path, body="")
    result, sf, ws = _launch("dpe_game", _dpe_game_graph(), tmp_path)

    assert result["status"] == "error"
    sf.get_or_create_run.assert_not_called()


def test_a_config_with_no_cross_config_inputs_is_unaffected(tmp_path):
    """code_review reads only its own seed, into an empty workspace, and starts.

    The guard is a precondition check, not a "the workspace looks bare" check —
    most configs are launched into exactly this state.
    """
    graph = PipelineGraph.from_yaml(CONFIGS / "code_review.yaml")
    result, sf, ws = _launch(
        "code_review", graph, tmp_path,
        manifest=_manifest("code_review", seed_file="review_request.md",
                           scheduler_owned=False))

    assert result["status"] == "started"
    sf.get_or_create_run.assert_called_once()


def test_a_required_source_naming_the_running_config_is_not_a_precondition(tmp_path):
    """A `{config: <itself>}` source is the run's own seed — which this very call
    writes, moments after the check. Refusing it would ground every seeded config
    the day someone marks its seed `required: true`."""
    graph = PipelineGraph._from_dict({
        "name": "selfish",
        "begin": "only",
        "steps": [{
            "id": "only",
            "step_type": "agent",
            "agent_config": "someone",
            "context": [{"source": {"config": "selfish", "output": "task.md",
                                    "required": True}}],
            "transitions": [{"to": None}],
        }],
    })
    result, sf, ws = _launch(
        "selfish", graph, tmp_path,
        manifest=_manifest("selfish", seed_file="task.md", scheduler_owned=False))

    assert result["status"] == "started"
    sf.get_or_create_run.assert_called_once()
