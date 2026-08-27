"""seed_text must reach the run, or the launch must say it cannot.

`start_config_run` wrote seed_text only `if manifest.seed_file` — and skipped it
silently otherwise. meta_conversation declares no seed_file (only dpe_default
does), so `start_config_run("meta_conversation", seed_text=<the whole brief>)`
answered {"status": "started"} and threw the brief away. The reply is
indistinguishable from success; the run only looks wrong much later, in what the
agent asks, and by then nothing points back at the launch.

Same shape as the missing-cross-config-input guard next to it: a launch that
cannot do what the caller asked is refused at launch, not silently at runtime.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from skillflow.graph import GraphResolver, PipelineGraph

from core.run_launcher import start_config_run

CONFIGS = Path(__file__).resolve().parents[2] / "configs"


def _graph() -> PipelineGraph:
    """code_review: reads only its own seed, no cross-config inputs, so the
    precondition guard never fires and seed handling is what is under test."""
    return PipelineGraph.from_yaml(CONFIGS / "code_review.yaml")


def _manifest(config_name, seed_file):
    return SimpleNamespace(config_name=config_name, seed_file=seed_file,
                           scheduler_owned=False,
                           registers_generated_pipeline=False,
                           registers_generated_addon=False, repo_mode="none")


def _launch(tmp_path, *, seed_file, **kwargs):
    db = MagicMock()
    db.get_project.return_value = None
    ws = MagicMock()
    registry = MagicMock()
    registry.get.return_value = _manifest("code_review", seed_file)
    sf = MagicMock()
    sf._get_resolver.return_value = GraphResolver(_graph())
    sf._workspace.get_project_path.return_value = tmp_path
    sf._workspace.get_config_path.return_value = tmp_path / "code_review"
    sf.get_run.return_value = {"status": "running"}
    with patch("api.dependencies.get_skillflow", return_value=sf), \
         patch("api.dependencies.get_config_registry", return_value=registry), \
         patch("core.scheduler.wake_scheduler"):
        result = start_config_run(db, ws, "code_review", "pid_x", **kwargs)
    return result, sf


def test_seed_text_into_a_config_with_nowhere_to_put_it_is_refused(tmp_path):
    result, sf = _launch(tmp_path, seed_file="", seed_text="the whole brief")

    assert result["status"] == "error"
    sf.get_or_create_run.assert_not_called()
    msg = result["message"]
    assert "code_review" in msg
    assert "seed_file" in msg
    # The caller must be able to act on this, not just learn it failed.
    assert "seed_inputs" in msg


def test_the_refusal_says_how_much_text_would_have_vanished(tmp_path):
    # The size is what makes it obvious this was not an empty accident.
    result, _ = _launch(tmp_path, seed_file=None, seed_text="x" * 4321)
    assert "4321" in result["message"]


def test_seed_text_still_lands_when_the_config_declares_a_seed_file(tmp_path):
    result, sf = _launch(tmp_path, seed_file="project_brief.md",
                         seed_text="build the thing")

    assert result["status"] == "started"
    written = tmp_path / "code_review" / "_seed" / "project_brief.md"
    assert written.read_text(encoding="utf-8") == "build the thing"


def test_a_launch_with_no_seed_text_is_unaffected(tmp_path):
    # The guard fires on the caller ASKING for something impossible, never on a
    # config that simply has no seed.
    result, sf = _launch(tmp_path, seed_file="")
    assert result["status"] == "started"
    sf.get_or_create_run.assert_called_once()


def test_empty_seed_text_is_still_a_request_and_still_refused(tmp_path):
    # "" is not None: the caller passed a seed. Dropping it silently is the same
    # bug at size zero, and treating it as no-seed would reopen the hole.
    result, sf = _launch(tmp_path, seed_file="", seed_text="")
    assert result["status"] == "error"
    sf.get_or_create_run.assert_not_called()


def test_seed_inputs_work_without_a_seed_file(tmp_path):
    # seed_inputs names its own files, so it never depended on manifest.seed_file
    # and must keep working — it is the remedy the refusal points at.
    result, sf = _launch(tmp_path, seed_file="", seed_inputs={"notes.md": "hi"})
    assert result["status"] == "started"
    assert (tmp_path / "code_review" / "_seed" / "notes.md").read_text() == "hi"
