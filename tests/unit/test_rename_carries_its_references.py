"""Registration renames the graph; it must rewrite what that rename invalidates.

The emitter writes self-referential context sources under the name it knows — the
human pipeline name or its slug:

    - id: interview
      context:
        - source: {config: skill_packager, output: task.md}

Registration renames the graph to `gen_skill_packager` and used to leave that
source pointing at a config that will never exist. Measured across the registered
pipelines when this was found: NINE of eleven carried a dead self-reference
(`cac40_daily`, `deepsearch` ×2, `math_competition_solver`, `mcp_server_builder`,
`markdown_link_fix`, `reference_verification` ×2, `skill_packager`).

Benign purely by luck — `_inject_seed_context` puts a correct source at position 0,
so the dead one resolves to nothing and is skipped. It becomes a hard failure the
moment an emitter marks the dead source `required: true`.
"""
from core.pipeline_registry import (
    _dedupe_context_sources, _inject_seed_context, _rewrite_self_config_refs)


def _graph(name, sources):
    return {"name": name, "begin": "start",
            "steps": [{"id": "start", "step_type": "agent", "agent_config": "r",
                       "context": [{"source": s} for s in sources]}]}


def _sources(data):
    return [c["source"] for c in data["steps"][0]["context"]]


class TestSelfReferencesAreRewritten:
    def test_the_slug_form_is_rewritten(self):
        """What actually shipped: the emitter used the bare slug."""
        d = _graph("skill_packager", [{"config": "skill_packager", "output": "task.md"}])
        assert _rewrite_self_config_refs(d, "gen_skill_packager") == \
            ["start:skill_packager"]
        assert _sources(d) == [{"config": "gen_skill_packager", "output": "task.md"}]

    def test_the_graphs_own_declared_name_is_rewritten_too(self):
        """The emitter may name the graph one thing and reference another spelling."""
        d = _graph("Skill Packager Pipeline",
                   [{"config": "Skill Packager Pipeline", "output": "task.md"}])
        _rewrite_self_config_refs(d, "gen_skill_packager")
        assert _sources(d)[0]["config"] == "gen_skill_packager"

    def test_a_reference_to_a_DIFFERENT_config_is_left_alone(self):
        """A pipeline that reads another pipeline's output is a real, supported
        thing. Only this graph's own pre-rename identity is rewritten."""
        d = _graph("skill_packager", [
            {"config": "skill_packager", "output": "task.md"},
            {"config": "dpe_default_v2", "output": "brief.md"},
        ])
        _rewrite_self_config_refs(d, "gen_skill_packager")
        assert _sources(d) == [
            {"config": "gen_skill_packager", "output": "task.md"},
            {"config": "dpe_default_v2", "output": "brief.md"},
        ]

    def test_an_already_correct_reference_is_not_touched(self):
        d = _graph("gen_skill_packager",
                   [{"config": "gen_skill_packager", "output": "task.md"}])
        assert _rewrite_self_config_refs(d, "gen_skill_packager") == []

    def test_a_graph_with_no_name_does_not_rewrite_everything(self):
        """`old` empty + slug present must not turn unrelated sources into self-refs."""
        d = _graph(None, [{"config": "other_config", "output": "x.md"}])
        assert _rewrite_self_config_refs(d, "gen_skill_packager") == []
        assert _sources(d)[0]["config"] == "other_config"


class TestTheRewriteComposesWithSeeding:
    def test_rewrite_then_seed_leaves_exactly_one_of_each_source(self):
        """The end-to-end shape. Rewriting collapses the dead source onto the seed's
        config, so the dedupe has to run after seeding or the agent reads the same
        file twice."""
        d = _graph("skill_packager", [{"config": "skill_packager", "output": "task.md"}])
        _rewrite_self_config_refs(d, "gen_skill_packager")
        d["name"] = "gen_skill_packager"
        _inject_seed_context(d, "gen_skill_packager")
        _dedupe_context_sources(d)
        assert _sources(d) == [
            {"config": "gen_skill_packager", "output": "seed_input.md"},
            {"config": "gen_skill_packager", "output": "task.md"},
        ]

    def test_an_exact_duplicate_is_dropped(self):
        d = _graph("g", [{"config": "gen_g", "output": "a.md"},
                         {"config": "gen_g", "output": "a.md"}])
        assert _dedupe_context_sources(d) == ["start"]
        assert len(_sources(d)) == 1

    def test_dedupe_keeps_distinct_sources_and_their_order(self):
        d = _graph("g", [{"config": "gen_g", "output": "a.md"},
                         {"config": "gen_g", "output": "b.md"},
                         {"step": "earlier"}])
        assert _dedupe_context_sources(d) == []
        assert len(_sources(d)) == 3
        assert _sources(d)[1]["output"] == "b.md"

    def test_a_step_with_no_context_is_untouched(self):
        d = {"name": "g", "begin": "s",
             "steps": [{"id": "s", "step_type": "tool", "tool_name": "t"}]}
        assert _dedupe_context_sources(d) == []
        assert "context" not in d["steps"][0]
