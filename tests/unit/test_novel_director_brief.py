"""The optional novel brief follows actual publication and context resolution.

Execution adapters are mocked so tests cannot start production or paid writers.
The shipped graph, host registry, launcher, seed publisher, resolver and probe
are real. No model compliance or deployed-runtime acceptance is asserted here.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from skillflow.context import ContextResolver
from skillflow.graph import GraphResolver, PipelineGraph

from core.config_registry import ConfigManifest, ConfigRegistry
from core.run_launcher import start_config_run
from core.seed_publication import published_generation, reads_own_seed, seed_is_published

ROOT = Path(__file__).resolve().parents[2]
CONFIG = "novel_chapter"
SEED = "director_brief.md"
READERS = ("outline", "outline_review", "draft", "draft_review", "humanize",
           "finalize", "finalize_review")


def graph():
    return PipelineGraph.from_yaml(ROOT / "configs" / "novel_chapter.yaml")


def manifest():
    sf = MagicMock()
    sf._get_resolver.return_value = GraphResolver(graph())
    registry = ConfigRegistry()
    return registry.register_one(sf, CONFIG)


def launch(tmp_path, *, pid="chapter-a", **kwargs):
    """Use the public launcher with a real input workspace and graph."""
    project = tmp_path / pid
    seed = project / CONFIG / "_seed"
    sf = MagicMock()
    sf._get_resolver.return_value = GraphResolver(graph())
    sf._workspace.get_project_path.return_value = project
    sf._workspace.get_config_path.return_value = project / CONFIG
    sf.get_run.return_value = {"status": "pending"}
    seen = []

    def at_execution_boundary(*args, **kw):
        ok, why = seed_is_published(seed, SEED)
        assert ok, why
        seen.append((seed / SEED).read_text(encoding="utf-8"))
        return "run-" + pid

    sf.get_or_create_run.side_effect = at_execution_boundary
    sf.start_run.side_effect = at_execution_boundary
    db, ws, registry = MagicMock(), MagicMock(), MagicMock()
    db.get_project.return_value = None
    registry.get.return_value = manifest()
    with patch("api.dependencies.get_skillflow", return_value=sf), \
         patch("api.dependencies.get_config_registry", return_value=registry), \
         patch("core.run_isolation.ensure_for_run", return_value={"mode": "direct"}), \
         patch("core.scheduler.wake_scheduler", side_effect=at_execution_boundary):
        result = start_config_run(db, ws, CONFIG, pid, **kwargs)
    return result, project, seen


def test_registry_publishes_the_optional_input_contract():
    m = manifest()
    assert m.seed_file == SEED
    assert m.seed_default.strip()
    assert "未提供" in m.seed_default
    assert m.to_dict()["seed_default"] == m.seed_default
    assert "seed_text" in m.input_hint
    assert reads_own_seed(graph(), CONFIG, SEED)
    assert set(m.checkpoints) == {"outline_gate", "final_gate"}


@pytest.mark.parametrize("default", ["", "  \n", 123, False, []])
def test_invalid_declared_defaults_fail(default):
    with pytest.raises(ValueError, match="seed_default"):
        ConfigManifest(CONFIG, graph, seed_file=SEED, seed_default=default)


def test_a_default_without_a_seed_file_fails():
    with pytest.raises(ValueError, match="seed_file"):
        ConfigManifest(CONFIG, graph, seed_default="no extra direction")


def test_other_pipeline_has_no_default():
    sf = MagicMock()
    sf._get_resolver.return_value = GraphResolver(
        PipelineGraph.from_yaml(ROOT / "configs" / "coding_impl.yaml"))
    m = ConfigRegistry().register_one(sf, "coding_impl")
    assert m.seed_default is None
    assert m.seed_file == "plan.md"


@pytest.mark.parametrize("kwargs", [{}, {"seed_text": None}, {"seed_text": ""},
                                   {"seed_text": " \n\t"},
                                   {"seed_inputs": {SEED: " "}}])
def test_omitted_or_blank_brief_is_a_complete_default_seed(tmp_path, kwargs):
    result, project, seen = launch(tmp_path, **kwargs)
    assert result["status"] == "started"
    text = (project / CONFIG / "_seed" / SEED).read_text(encoding="utf-8")
    assert text == manifest().seed_default
    assert seen == [text, text, text]  # create, start, scheduler wake


def test_submitted_text_is_preserved_at_all_execution_boundaries(tmp_path):
    brief = "  # Director Brief\n只推进，不完成。\n" + "重要约束。" * 5000 + "\n末尾约束\n"
    result, project, seen = launch(tmp_path, seed_text=brief)
    assert result["status"] == "started"
    assert seen == [brief, brief, brief]
    content = ContextResolver(project).resolve(
        [{"source": {"config": CONFIG, "output": SEED}}], current_config=CONFIG)
    assert content[f"{CONFIG}/{SEED}"] == brief


def test_named_seed_inputs_keep_their_existing_precedence(tmp_path):
    result, _, seen = launch(tmp_path, seed_text="text input",
                             seed_inputs={SEED: "named input", "notes.md": "side note"})
    assert result["status"] == "started"
    assert seen == ["named input"] * 3


def test_same_execution_seed_is_immutable_and_retry_is_idempotent(tmp_path):
    first, project, _ = launch(tmp_path, seed_text="original")
    directory = project / CONFIG / "_seed"
    generation = published_generation(directory)
    repeat, _, seen = launch(tmp_path, seed_text="original")
    assert first["status"] == repeat["status"] == "started"
    assert seen == ["original"] * 3
    assert published_generation(directory) == generation
    changed, _, seen = launch(tmp_path, seed_text="replacement")
    assert changed["status"] == "error"
    assert "immutable" in changed["message"]
    assert seen == []
    assert (directory / SEED).read_text() == "original"
    # Omitting the brief is also a different input, never a silent inheritance.
    omitted, _, _ = launch(tmp_path)
    assert omitted["status"] == "error"


def test_next_chapter_and_other_book_do_not_inherit_an_old_brief(tmp_path):
    _, old, _ = launch(tmp_path, pid="book-a-ch1", seed_text="ONLY-CHAPTER-ONE")
    for pid, brief in (("book-a-ch2", None), ("book-b-ch1", "OTHER-BOOK")):
        result, project, _ = launch(tmp_path, pid=pid, seed_text=brief)
        assert result["status"] == "started"
        resolved = ContextResolver(project).resolve(
            [{"source": {"config": CONFIG, "output": SEED}}], current_config=CONFIG)
        assert "ONLY-CHAPTER-ONE" not in "\n".join(resolved.values())
    assert "ONLY-CHAPTER-ONE" in (old / CONFIG / "_seed" / SEED).read_text()


@pytest.mark.parametrize("step", READERS)
def test_actual_step_context_contains_brief_separate_from_bible(tmp_path, step):
    from tests.unit.test_novel_tools import _seed
    from aitelier.tools.state_probe.impl import state_probe
    book = tmp_path / "book"
    _seed(book)
    before = {p.relative_to(book): p.read_bytes() for p in book.rglob("*") if p.is_file()}
    brief = "DIRECTOR-INTENT: 推进但不完成；计划不等于事实。"
    _, project, _ = launch(tmp_path, seed_text=brief)
    state_probe(project_root=str(book), out_dir=str(project / CONFIG / "probe"))
    node = GraphResolver(graph()).get_node(step)
    resolved = ContextResolver(project).resolve(node.context, current_config=CONFIG)
    assert resolved[f"{CONFIG}/{SEED}"] == brief
    assert sum(brief in v for v in resolved.values()) == 1
    if step != "humanize":
        assert "第1章" in "\n".join(resolved.values())
    after = {p.relative_to(book): p.read_bytes() for p in book.rglob("*") if p.is_file()}
    assert after == before


def test_legacy_missing_optional_context_does_not_invent_a_brief(tmp_path):
    node = GraphResolver(graph()).get_node("outline")
    resolved = ContextResolver(tmp_path).resolve(node.context, current_config=CONFIG)
    assert f"{CONFIG}/{SEED}" not in resolved


@pytest.mark.parametrize("step", ["draft", "draft_review", "humanize", "finalize", "finalize_review"])
def test_readers_receive_rulings_that_can_supersede_the_initial_brief(step):
    node = GraphResolver(graph()).get_node(step)
    sources = [s.get("source", s) for s in node.context]
    assert any(s.get("feedback_of") == "outline" for s in sources)
    if step in {"humanize", "finalize", "finalize_review"}:
        assert any(s.get("feedback_of") == "draft" for s in sources)


@pytest.mark.parametrize("name", ["novel_outline", "novel_outline_review", "novel_draft",
                                  "novel_draft_review", "novel_humanize",
                                  "novel_finalize", "novel_finalize_review"])
def test_templates_name_the_plan_fact_and_current_ruling_boundaries(name):
    text = (ROOT / "templates" / f"{name}.md").read_text(encoding="utf-8")
    assert "director_brief.md" in text
    assert "execution project" in text
    assert "事实" in text and "裁定" in text and "取代" in text


def test_only_this_pipeline_opts_into_seed_default():
    enabled = []
    for path in (ROOT / "configs").glob("*.yaml"):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if (raw.get("x-aitelier") or {}).get("seed_default") is not None:
            enabled.append(raw["name"])
    assert enabled == [CONFIG]


def test_default_does_not_shift_existing_positional_manifest_arguments():
    # label, has_task_loop, scheduler_owned, seed_file, output_step retain order.
    m = ConfigManifest(CONFIG, graph, "label", False, True, "seed.md", "finalize")
    assert m.output_step == "finalize"
    assert m.seed_default is None


@pytest.mark.parametrize("step", ["draft", "draft_review", "humanize", "finalize_review"])
def test_changed_outline_ruling_reaches_actual_downstream_context(tmp_path, step):
    _, project, _ = launch(tmp_path, seed_text="INITIAL: end with combat")
    feedback = project / CONFIG / "_feedback"
    feedback.mkdir()
    ruling = "CURRENT-RULING: replace the combat direction with a quiet discovery."
    (feedback / "outline.md").write_text(ruling, encoding="utf-8")
    node = GraphResolver(graph()).get_node(step)
    resolved = ContextResolver(project).resolve(node.context, current_config=CONFIG)
    assert ruling in "\n".join(resolved.values())
    assert resolved[f"{CONFIG}/{SEED}"] == "INITIAL: end with combat"
