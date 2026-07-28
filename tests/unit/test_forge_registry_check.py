"""Unit tests for the forge_registry_check convention linters — the gate that
turns behaviorally-wrong-but-structurally-valid generated graphs into a gate
failure (which the emit feedback loop then self-repairs)."""

import importlib.util
from pathlib import Path

import pytest

# Load the tool impl directly (it lives in a tool dir, not an importable package).
_IMPL = Path(__file__).resolve().parents[2] / "aitelier/tools/forge_registry_check/impl.py"
_spec = importlib.util.spec_from_file_location("forge_registry_check_impl", _IMPL)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
forge_registry_check = _mod.forge_registry_check


@pytest.fixture(autouse=True)
def _stub_live_tools(monkeypatch):
    # The reviewer-reads-maker check is pure graph analysis; stub the live
    # registry so the test needs no running app.
    monkeypatch.setattr(_mod, "_live_tools",
                        lambda: {"web_search", "write", "draft_commit", "run_tests"})


def _write(tmp_path, graph):
    import yaml
    p = tmp_path / "g.yaml"
    p.write_text(yaml.safe_dump(graph), encoding="utf-8")
    return str(p)


def _maker_reviewer(reviewer_reads_maker: bool):
    reviewer_ctx = [{"source": {"config": "g", "output": "task.md"}}]
    if reviewer_reads_maker:
        reviewer_ctx.append({"source": {"step": "draft"}})
    return {
        "name": "g", "description": "x", "begin": "draft",
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "node_reached", "node": "done", "result": "completed"}]},
        "steps": [
            {"id": "draft", "step_type": "agent", "agent_config": "d",
             "transitions": [{"to": "review"}]},
            {"id": "review", "step_type": "agent", "agent_config": "r",
             "context": reviewer_ctx,
             # A reviewer declares its verdict as a fixed content slot, the way every
             # real one does — otherwise it trips `routing_file_unguaranteed`, which
             # is a different (real) defect and not what these tests are about.
             "output": {"mode": "content", "fixed": {"verdict": {"file": "v.json"}}},
             "transitions": [
                 {"to": "done", "match": {"from_file": "v.json", "field": "passed", "value": True}},
                 {"to": "draft", "match": {"from_file": "v.json", "field": "passed", "value": False},
                  "max_loop": 3}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    }


def test_reviewer_that_ignores_its_maker_is_flagged(tmp_path):
    res = forge_registry_check(graph_path=_write(tmp_path, _maker_reviewer(False)))
    assert res["passed"] is False
    assert any("judges blind" in v for v in res["violations"])
    # the actionable `error` (fed back to the emitter) names the fix
    assert "step: draft" in res["error"]


def test_reviewer_that_reads_its_maker_passes(tmp_path):
    res = forge_registry_check(graph_path=_write(tmp_path, _maker_reviewer(True)))
    assert res["passed"] is True
    assert res["error"] == ""


def test_error_field_summarizes_violations_for_feedback(tmp_path):
    # A hallucinated tool → the error field must carry the reason (the tool-gate
    # feedback path injects only tool_result["error"]).
    bad = _maker_reviewer(True)
    bad["steps"].insert(1, {"id": "fetch", "step_type": "tool",
                            "tool_name": "totally_not_a_real_tool",
                            "transitions": [{"to": "review"}]})
    res = forge_registry_check(graph_path=_write(tmp_path, bad))
    assert res["passed"] is False
    assert "totally_not_a_real_tool" in res["error"]


def _fanout(agg_scope=None):
    """A graph: make → loop → [verify] → make, then aggregate reads verify."""
    agg_src = {"step": "verify"}
    if agg_scope:
        agg_src["scope"] = agg_scope
    return {
        "name": "g", "description": "x", "begin": "make",
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "node_reached", "node": "done", "result": "completed"}]},
        "steps": [
            {"id": "make", "step_type": "agent", "agent_config": "m",
             "transitions": [{"to": "loop"}]},
            {"id": "loop", "step_type": "loop",
             "loop": {"source": {"step": "make", "file": "m.json", "field": "execution_order"},
                      "item_as": "item", "max_iterations": 5},
             "transitions": [{"to": "verify", "max_loop": 5}, {"to": "aggregate"}]},
            # The verdict is a fixed content slot: the give-up variant of this
            # fixture routes on `v.json`, and a step must guarantee what it routes on.
            {"id": "verify", "step_type": "agent", "agent_config": "v",
             "output": {"mode": "content", "fixed": {"verdict": {"file": "v.json"}}},
             "transitions": [{"to": "loop", "max_loop": 5}]},
            {"id": "aggregate", "step_type": "agent", "agent_config": "a",
             "context": [{"source": agg_src}],
             "transitions": [{"to": "done"}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    }


def test_aggregator_without_scope_is_fine_engine_defaults_to_all(tmp_path):
    # skillflow >=1.5.24 routes by position: an out-of-loop reader gets ALL
    # items by default, so a missing scope is no longer a defect.
    res = forge_registry_check(graph_path=_write(tmp_path, _fanout(agg_scope=None)))
    assert res["passed"] is True


def test_aggregator_with_scope_all_passes(tmp_path):
    res = forge_registry_check(graph_path=_write(tmp_path, _fanout(agg_scope="all")))
    assert res["passed"] is True


def test_explicit_scope_task_on_out_of_loop_reader_is_flagged(tmp_path):
    # The engine silently overrides an outside reader's scope:task to all-items;
    # a declaration that lies about behavior is a violation.
    res = forge_registry_check(graph_path=_write(tmp_path, _fanout(agg_scope="task")))
    assert res["passed"] is False
    assert any("scope: task" in v or "scope: all" in v for v in res["violations"])


def test_invalid_scope_value_is_flagged(tmp_path):
    res = forge_registry_check(graph_path=_write(tmp_path, _fanout(agg_scope="al")))
    assert res["passed"] is False
    assert any("invalid scope" in v for v in res["violations"])


def test_in_loop_reader_of_body_producer_is_not_flagged(tmp_path):
    # verify (in the body) reading make (pre-loop) or a sibling body step with
    # default scope:task is correct — only OUT-of-loop readers need scope:all.
    g = _fanout(agg_scope="all")
    # add an in-loop reader: verify reads a sibling body producer without scope:all
    g["steps"].insert(3, {"id": "verify2", "step_type": "agent", "agent_config": "v2",
                          "context": [{"source": {"step": "verify"}}],
                          "transitions": [{"to": "loop", "max_loop": 5}]})
    # reroute loop→verify2→verify→loop so both are in the body
    g["steps"][1]["transitions"][0]["to"] = "verify2"
    g["steps"][3]["transitions"] = [{"to": "verify"}]  # verify2 → verify
    res = forge_registry_check(graph_path=_write(tmp_path, g))
    # verify2 (in body) reading verify (in body) must NOT be flagged
    assert not any("verify2" in v and "scope: all" in v for v in res["violations"])


def test_giveup_edge_target_is_not_body_reach_back_semantics(tmp_path):
    """The gate hole from the 1.5.23 review: a post-loop aggregator ALSO reachable
    from a body step via a give-up edge must NOT be classified in-body (reach-back
    topology, now taken from skillflow's own loop_body_map). Consequently an
    explicit scope:task there is a lying annotation and gets flagged."""
    g = _fanout(agg_scope="task")
    # add a give-up edge: verify --(passed:false, budget spent)--> aggregate
    verify = next(s for s in g["steps"] if s["id"] == "verify")
    verify["transitions"] = [
        {"to": "loop", "match": {"from_file": "v.json", "field": "p", "value": True},
         "max_loop": 5},
        {"to": "aggregate", "match": {"from_file": "v.json", "field": "p", "value": False},
         "max_loop": 3},
    ]
    res = forge_registry_check(graph_path=_write(tmp_path, g))
    # aggregate is OUT of body despite the drain edge → the scope:task lie fires
    assert any("scope: task" in v and "aggregate" in v for v in res["violations"]), \
        res["violations"]
    # and with the annotation omitted, the same topology passes (engine default)
    g2 = _fanout(agg_scope=None)
    v2 = next(s for s in g2["steps"] if s["id"] == "verify")
    v2["transitions"] = verify["transitions"]
    res2 = forge_registry_check(graph_path=_write(tmp_path, g2))
    assert res2["passed"] is True, res2["violations"]


# ── Failure class: where a rejected graph goes back to ────────────────────

def _graph(steps, terminal="done"):
    return {"name": "g", "description": "x", "begin": steps[0]["id"],
            "end_conditions": {"combinator": "or", "conditions": [
                {"type": "node_reached", "node": terminal, "result": "completed"}]},
            "steps": steps}


def _gate(step_id="done"):
    return {"id": step_id, "step_type": "gate", "transitions": [{"to": None}]}


def _writer(step_id, to, files=("result.md",)):
    return {"id": step_id, "step_type": "agent", "agent_config": "host",
            "output": {"mode": "content",
                       "fixed": {k: {"file": f} for k, f in enumerate(files)}},
            "transitions": [{"to": to}]}


class TestFailureClass:
    def test_a_clean_graph_has_no_class(self, tmp_path):
        g = _graph([_writer("make", "done"), _gate()])
        res = forge_registry_check(graph_path=_write(tmp_path, g))
        assert res["passed"] is True
        assert res["failure_class"] == ""

    def test_an_unknown_tool_is_classed_for_rebuild(self, tmp_path):
        g = _graph([{"id": "make", "step_type": "tool", "tool_name": "not_a_real_tool",
                     "transitions": [{"to": "done", "match": {"passed": True}}]}, _gate()])
        res = forge_registry_check(graph_path=_write(tmp_path, g))
        assert res["failure_class"] == "unknown_tool"
        assert "tool plan" in res["error"]

    def test_a_shape_defect_is_classed_as_emit_fixable(self, tmp_path):
        """A bad terminal is a defect in the emitted file — repairable in place."""
        g = _graph([_writer("make", "done"),
                    {"id": "done", "step_type": "agent", "agent_config": "host"}])
        res = forge_registry_check(graph_path=_write(tmp_path, g))
        assert res["failure_class"] == "emit_fixable"


# ── The role table (S16) ──────────────────────────────────────────────────

class TestRoleTable:
    def _graph_with_role(self):
        return _graph([{"id": "make", "step_type": "agent", "agent_config": "maker",
                        "transitions": [{"to": "done"}],
                        "output": {"mode": "content", "fixed": {"o": {"file": "r.md"}}}},
                       _gate()])

    def test_top_level_roles_pass(self, tmp_path):
        import yaml
        rt = tmp_path / "rt.yaml"
        rt.write_text(yaml.safe_dump({"maker": {"model": "host"}}), encoding="utf-8")
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph_with_role()),
                                   role_table=str(rt))
        assert res["passed"] is True

    def test_an_entries_wrapper_is_unwrapped_not_reported_as_missing(self, tmp_path):
        """The roles ARE defined — one level too deep. Do not say they are absent."""
        import yaml
        rt = tmp_path / "rt.yaml"
        rt.write_text(yaml.safe_dump({"entries": {"maker": {"model": "host"}}}),
                      encoding="utf-8")
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph_with_role()),
                                   role_table=str(rt))
        assert res["passed"] is True, res["violations"]

    def test_a_genuinely_missing_role_still_fails(self, tmp_path):
        import yaml
        rt = tmp_path / "rt.yaml"
        rt.write_text(yaml.safe_dump({"someone_else": {"model": "host"}}), encoding="utf-8")
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph_with_role()),
                                   role_table=str(rt))
        assert res["passed"] is False
        assert any("maker" in v for v in res["violations"])


# ── Fail-open shapes (S4, S18) ────────────────────────────────────────────

class TestFailOpen:
    def test_a_success_path_that_only_writes_a_verdict_is_rejected(self, tmp_path):
        """R1's shape: verify -> done_gate, with the answer step on the give-up branch."""
        steps = [
            {"id": "verify", "step_type": "agent", "agent_config": "reviewer",
             "output": {"mode": "content",
                        "fixed": {"verdict": {"file": "review_verdict.json"}}},
             "transitions": [{"to": "done", "match": {"passed": True}},
                             {"to": "answer"}]},
            _writer("answer", None, ("final_answer.md",)),
            _gate(),
        ]
        res = forge_registry_check(graph_path=_write(tmp_path, _graph(steps)))
        assert res["passed"] is False
        assert any("deliverable" in v for v in res["violations"])

    def test_a_deliverable_before_the_gate_passes(self, tmp_path):
        steps = [
            {"id": "verify", "step_type": "agent", "agent_config": "reviewer",
             "output": {"mode": "content",
                        "fixed": {"verdict": {"file": "review_verdict.json"}}},
             "transitions": [{"to": "answer", "match": {"passed": True}}]},
            _writer("answer", "done", ("final_answer.md",)),
            _gate(),
        ]
        res = forge_registry_check(graph_path=_write(tmp_path, _graph(steps)))
        assert res["passed"] is True, res["violations"]

    def test_a_write_mode_predecessor_is_not_second_guessed(self, tmp_path):
        """mode: write doesn't enumerate files — the DELIVERABLE check must not
        invent a violation from that.

        (It does now need a `validation`, which is a different rule — assert on the
        one this test is about rather than on the global verdict.)
        """
        steps = [{"id": "make", "step_type": "agent", "agent_config": "host",
                  "output": {"mode": "write"},
                  "validation": [{"files": ["*"], "tool": "file_exists"}],
                  "transitions": [{"to": "done"}]},
                 _gate()]
        res = forge_registry_check(graph_path=_write(tmp_path, _graph(steps)))
        assert res["passed"] is True, res["violations"]

    def test_the_deliverable_check_still_ignores_write_mode_without_validation(self, tmp_path):
        """Same graph minus the validation: exactly ONE violation, and not this one."""
        steps = [{"id": "make", "step_type": "agent", "agent_config": "host",
                  "output": {"mode": "write"}, "transitions": [{"to": "done"}]},
                 _gate()]
        res = forge_registry_check(graph_path=_write(tmp_path, _graph(steps)))
        assert not any("deliverable" in v for v in res["violations"]), res["violations"]
        assert all("declares no `validation`" in v for v in res["violations"])

    def test_an_unrouted_fallible_tool_is_rejected(self, tmp_path):
        """The commit step that 'succeeded' while committing nothing."""
        steps = [_writer("make", "commit"),
                 {"id": "commit", "step_type": "tool", "tool_name": "draft_commit",
                  "transitions": [{"to": "done"}]},
                 _gate()]
        res = forge_registry_check(graph_path=_write(tmp_path, _graph(steps)))
        assert res["passed"] is False
        assert any("unconditional" in v for v in res["violations"])

    def test_a_routed_fallible_tool_passes(self, tmp_path):
        steps = [_writer("make", "commit"),
                 {"id": "commit", "step_type": "tool", "tool_name": "draft_commit",
                  "transitions": [{"to": "done", "match": {"passed": True}},
                                  {"to": "make", "match": {"passed": False}, "max_loop": 2}]},
                 _gate()]
        res = forge_registry_check(graph_path=_write(tmp_path, _graph(steps)))
        assert res["passed"] is True, res["violations"]

    def test_a_verify_prefixed_tool_counts_as_fallible(self, tmp_path):
        steps = [_writer("make", "vstep"),
                 {"id": "vstep", "step_type": "tool", "tool_name": "verify_things",
                  "transitions": [{"to": "done"}]},
                 _gate()]
        res = forge_registry_check(graph_path=_write(tmp_path, _graph(steps)))
        assert any("unconditional" in v for v in res["violations"])


# ── Review follow-ups ─────────────────────────────────────────────────────

class TestShorthandOutputs:
    def _graph_with_writer(self, fixed):
        return _graph([{"id": "make", "step_type": "agent", "agent_config": "host",
                        "output": {"mode": "content", "fixed": fixed},
                        "transitions": [{"to": "done"}]},
                       _gate()])

    @pytest.mark.parametrize("fixed", [
        {"answer": "final_answer.md"},                       # shorthand
        {"answer": {"file": "final_answer.md"}},             # long form
        {"a": "one.md", "b": {"file": "two.md"}},            # mixed
    ])
    def test_a_real_deliverable_is_recognised_in_either_form(self, tmp_path, fixed):
        """pipeline_forge's own survey step uses the shorthand — reading only the
        long form rejects a correct graph for 'writing nothing but a verdict'."""
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph_with_writer(fixed)))
        assert res["passed"] is True, res["violations"]

    def test_a_verdict_only_predecessor_is_still_caught(self, tmp_path):
        res = forge_registry_check(
            graph_path=_write(tmp_path, self._graph_with_writer({"v": "review_verdict.json"})))
        assert res["passed"] is False
        assert any("deliverable" in v for v in res["violations"])


class TestFalliblePrefixes:
    def test_test_write_is_not_treated_as_a_check(self, tmp_path, monkeypatch):
        """`test_write` writes a test file; it is not a gate, so an unconditional
        edge is correct and must not be flagged."""
        monkeypatch.setattr(_mod, "_live_tools", lambda: {"test_write", "write"})
        g = _graph([_writer("make", "tw"),
                    {"id": "tw", "step_type": "tool", "tool_name": "test_write",
                     "transitions": [{"to": "done"}]},
                    _gate()])
        res = forge_registry_check(graph_path=_write(tmp_path, g))
        assert res["passed"] is True, res["violations"]

    def test_a_check_prefixed_tool_is_still_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_mod, "_live_tools", lambda: {"check_things", "write"})
        g = _graph([_writer("make", "c"),
                    {"id": "c", "step_type": "tool", "tool_name": "check_things",
                     "transitions": [{"to": "done"}]},
                    _gate()])
        assert any("unconditional" in v
                   for v in forge_registry_check(graph_path=_write(tmp_path, g))["violations"])


class TestRoleTableNoteSurvivesAPass:
    def test_wrapper_is_reported_even_when_the_check_passes(self, tmp_path):
        """Otherwise the emitter is never told, and the wrong shape ships."""
        import yaml
        rt = tmp_path / "rt.yaml"
        rt.write_text(yaml.safe_dump({"entries": {"maker": {"model": "host"}}}),
                      encoding="utf-8")
        g = _graph([{"id": "make", "step_type": "agent", "agent_config": "maker",
                     "output": {"mode": "content", "fixed": {"r": "out.md"}},
                     "transitions": [{"to": "done"}]}, _gate()])
        res = forge_registry_check(graph_path=_write(tmp_path, g), role_table=str(rt))
        assert res["passed"] is True
        assert any("entries" in n for n in res["notes"])

    def test_no_note_when_the_table_is_already_flat(self, tmp_path):
        import yaml
        rt = tmp_path / "rt.yaml"
        rt.write_text(yaml.safe_dump({"maker": {"model": "host"}}), encoding="utf-8")
        g = _graph([{"id": "make", "step_type": "agent", "agent_config": "maker",
                     "output": {"mode": "content", "fixed": {"r": "out.md"}},
                     "transitions": [{"to": "done"}]}, _gate()])
        assert forge_registry_check(graph_path=_write(tmp_path, g),
                                    role_table=str(rt))["notes"] == []


class TestUnreachableTerminals:
    """A give-up branch that produces the answer and then dies is still a failure.

    Reproduces the real `gen_math_olympiad` shape: `verify` routes to `done_gate` on
    pass, loops back to `solve` on reject (max_loop 2), and falls through to
    `final_answer` when that budget is exhausted. `final_answer` writes the answer and
    declares `to: null` — but no end condition names it, so the run dies there and the
    "no confident solution" outcome the brief asked for never reaches the user.
    """

    @staticmethod
    def _graph(name_the_giveup_terminal: bool):
        conds = [{"type": "node_reached", "node": "done_gate", "result": "completed"}]
        if name_the_giveup_terminal:
            conds.append({"type": "node_reached", "node": "final_answer",
                          "result": "failed"})
        return {
            "name": "g", "description": "x", "begin": "solve",
            "end_conditions": {"combinator": "or", "conditions": conds},
            "steps": [
                {"id": "solve", "step_type": "agent", "agent_config": "solver",
                 "output": {"mode": "content",
                            "fixed": {"a": {"file": "answer.md"}}},
                 "transitions": [{"to": "verify"}]},
                {"id": "verify", "step_type": "agent", "agent_config": "checker",
                 "context": [{"source": {"step": "solve"}}],
                 "output": {"mode": "content",
                            "fixed": {"v": {"file": "review_verdict.json"}}},
                 "transitions": [
                     {"to": "done_gate", "match": {"from_file": "review_verdict.json",
                                                   "field": "passed", "value": True}},
                     {"to": "solve", "match": {"from_file": "review_verdict.json",
                                               "field": "passed", "value": False},
                      "max_loop": 2},
                     {"to": "final_answer"},
                 ]},
                {"id": "done_gate", "step_type": "gate",
                 "transitions": [{"to": None}]},
                {"id": "final_answer", "step_type": "agent", "agent_config": "solver",
                 "output": {"mode": "content",
                            "fixed": {"a": {"file": "final_answer.md"}}},
                 "transitions": [{"to": None}]},
            ],
        }

    def test_a_terminal_no_end_condition_names_is_flagged(self, tmp_path):
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph(False)),
                                   role_table="")
        assert res["passed"] is False
        assert any("final_answer" in v and "terminal" in v
                   for v in res["violations"]), res["violations"]
        assert res["failure_class"] == "emit_fixable"

    def test_naming_it_in_end_conditions_clears_the_violation(self, tmp_path):
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph(True)),
                                   role_table="")
        assert not [v for v in res["violations"] if "terminal (no outgoing edge)" in v], \
            res["violations"]

    def test_a_gate_with_to_null_that_IS_named_is_not_flagged(self, tmp_path):
        """The normal `done` terminal must never trip this rule.

        (This graph still fails the *deliverable-on-the-success-path* rule, which is
        a different, pre-existing check — assert on this rule's own wording.)
        """
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph(True)),
                                   role_table="")
        assert not any("done_gate" in v and "terminal (no outgoing edge)" in v
                       for v in res["violations"]), res["violations"]


class TestSingleConditionalEdgeOnAFallibleTool:
    """The other half of the unconditional-edge defect.

    One conditional edge routes the success case and leaves the failure matching
    nothing, so a failed check ends the run with "no matching transition". The
    dry-run smoke used to catch this by ACCIDENT — its stub emitted flags no real
    tool returns, so any flag branch failed. Now that the stub speaks the tools'
    contracts, the check has to live here, where the message can say what to do.
    """

    @staticmethod
    def _graph(transitions):
        return {
            "name": "g", "description": "x", "begin": "gate_step",
            "end_conditions": {"combinator": "or", "conditions": [
                {"type": "node_reached", "node": "done", "result": "completed"}]},
            "steps": [
                {"id": "gate_step", "step_type": "tool", "tool_name": "run_tests",
                 "transitions": transitions},
                {"id": "fix", "step_type": "agent", "agent_config": "w",
                 "output": {"mode": "content", "fixed": {"a": {"file": "out.md"}}},
                 "transitions": [{"to": "gate_step"}]},
                {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
            ],
        }

    def test_one_conditional_edge_is_flagged(self, tmp_path):
        g = self._graph([{"to": "done", "match": {"passed": True}}])
        res = forge_registry_check(graph_path=_write(tmp_path, g), role_table="")
        assert any("exactly ONE conditional transition" in v
                   for v in res["violations"]), res["violations"]

    def test_a_success_plus_failure_pair_passes(self, tmp_path):
        g = self._graph([{"to": "done", "match": {"passed": True}},
                         {"to": "fix", "match": {"passed": False}, "max_loop": 2}])
        res = forge_registry_check(graph_path=_write(tmp_path, g), role_table="")
        assert not [v for v in res["violations"] if "conditional transition" in v]

    def test_a_conditional_plus_unconditional_fallback_passes(self, tmp_path):
        g = self._graph([{"to": "done", "match": {"passed": True}}, {"to": "fix"}])
        res = forge_registry_check(graph_path=_write(tmp_path, g), role_table="")
        assert not [v for v in res["violations"] if "conditional transition" in v]

    def test_a_non_fallible_tool_is_left_alone(self, tmp_path):
        g = self._graph([{"to": "done", "match": {"passed": True}}])
        g["steps"][0]["tool_name"] = "write"          # not in the fallible set
        res = forge_registry_check(graph_path=_write(tmp_path, g), role_table="")
        assert not [v for v in res["violations"] if "conditional transition" in v]


class TestFailureBranchRejoinsSuccess:
    """The 2.2 rule was gameable, and a real generated graph gamed it.

    Told that a fallible tool needs its failure routed, the emitter produced
    `spec_apply --{applied:true}--> scaffold_maker` PLUS
    `spec_apply --(unconditional)--> spec_apply_fallback [gate] --> scaffold_maker`.
    The letter of the rule is satisfied; a `repo_apply` that never landed the code
    still advances into the next phase. Only human review caught it.
    """

    @staticmethod
    def _graph(fallback_target):
        return {
            "name": "g", "description": "x", "begin": "apply",
            "end_conditions": {"combinator": "or", "conditions": [
                {"type": "node_reached", "node": "done", "result": "completed"},
                {"type": "node_reached", "node": "gave_up", "result": "failed"}]},
            "steps": [
                {"id": "apply", "step_type": "tool", "tool_name": "repo_apply",
                 "transitions": [{"to": "next_phase", "match": {"applied": True}},
                                 {"to": "fallback"}]},
                {"id": "fallback", "step_type": "gate",
                 "transitions": [{"to": fallback_target}]},
                {"id": "next_phase", "step_type": "agent", "agent_config": "w",
                 "output": {"mode": "content", "fixed": {"a": {"file": "out.md"}}},
                 "transitions": [{"to": "done"}]},
                {"id": "gave_up", "step_type": "gate", "transitions": [{"to": None}]},
                {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
            ],
        }

    def test_a_gate_that_forwards_to_the_success_target_is_flagged(self, tmp_path):
        res = forge_registry_check(
            graph_path=_write(tmp_path, self._graph("next_phase")), role_table="")
        assert res["passed"] is False
        assert any("rejoins the SUCCESS target" in v
                   for v in res["violations"]), res["violations"]

    def test_a_failure_branch_that_actually_ends_the_run_passes(self, tmp_path):
        res = forge_registry_check(
            graph_path=_write(tmp_path, self._graph("gave_up")), role_table="")
        assert not [v for v in res["violations"] if "rejoins" in v], res["violations"]

    def test_a_direct_edge_to_the_success_target_is_flagged_too(self, tmp_path):
        """No gate in between — the same no-op, one hop shorter."""
        g = self._graph("gave_up")
        g["steps"][0]["transitions"] = [{"to": "next_phase", "match": {"applied": True}},
                                        {"to": "next_phase"}]
        res = forge_registry_check(graph_path=_write(tmp_path, g), role_table="")
        assert any("rejoins the SUCCESS target" in v for v in res["violations"])

    def test_a_non_fallible_tool_is_left_alone(self, tmp_path):
        g = self._graph("next_phase")
        g["steps"][0]["tool_name"] = "write"
        res = forge_registry_check(graph_path=_write(tmp_path, g), role_table="")
        assert not [v for v in res["violations"] if "rejoins" in v]


class TestDuplicateMaxLoopEdges:
    """Two max_loop edges on one (from, to) pair make the graph un-runnable.

    A generated graph shipped this; `forge_lint` passed it and only the smoke caught
    it, as `UNIQUE constraint failed: skillflow_edge_counts…` at boot. Our own configs
    get the rule from a pytest guard — generated ones should get it here, with a
    message that names the pair.
    """

    @staticmethod
    def _graph(second_edge_bounded: bool):
        second = {"to": "maker", "match": {"passed": False}}
        if second_edge_bounded:
            second["max_loop"] = 5
        return {
            "name": "g", "description": "x", "begin": "maker",
            "end_conditions": {"combinator": "or", "conditions": [
                {"type": "node_reached", "node": "done", "result": "completed"}]},
            "steps": [
                {"id": "maker", "step_type": "agent", "agent_config": "w",
                 "output": {"mode": "content", "fixed": {"a": {"file": "out.md"}}},
                 "transitions": [{"to": "gate_step"}]},
                {"id": "gate_step", "step_type": "tool", "tool_name": "run_tests",
                 "transitions": [{"to": "done", "match": {"passed": True}},
                                 {"to": "maker", "match": {"stalled": True},
                                  "max_loop": 3},
                                 second]},
                {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
            ],
        }

    def test_two_bounded_edges_to_the_same_target_are_flagged(self, tmp_path):
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph(True)),
                                   role_table="")
        assert res["passed"] is False
        assert any("both carry `max_loop`" in v for v in res["violations"]), \
            res["violations"]
        assert res["failure_class"] == "emit_fixable"

    def test_one_bounded_plus_one_unbounded_is_fine(self, tmp_path):
        """The invariant is about max_loop edges, not parallel edges — meta_conversation
        has had two `intent_detect → gather` edges for dozens of runs."""
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph(False)),
                                   role_table="")
        assert not [v for v in res["violations"] if "max_loop" in v], res["violations"]


class TestRoleToolsMustExist:
    """A role granted a tool that does not exist runs silently without it.

    skillflow's `resolve_tool_schemas` swallows the ImportError, so
    `write_file`/`create_file`/`edit_file` — a different application's coding tools —
    registered cleanly and the maker wrote nothing while its step reported success.
    At emit time the role table is still a file on disk, so this is the only place the
    mistake can be caught before it ships.
    """

    _GRAPH = {"name": "g", "description": "x", "begin": "make",
              "end_conditions": {"combinator": "or", "conditions": [
                  {"type": "node_reached", "node": "done", "result": "completed"}]},
              "steps": [
                  {"id": "make", "step_type": "agent", "agent_config": "maker",
                   "output": {"mode": "content", "fixed": {"a": {"file": "out.md"}}},
                   "transitions": [{"to": "done"}]},
                  {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
              ]}

    def _rt(self, tmp_path, tools):
        import yaml as _y
        p = tmp_path / "rt.yaml"
        p.write_text(_y.safe_dump({"maker": {"model": "host", "tools": tools}}),
                     encoding="utf-8")
        return str(p)

    def test_an_invented_tool_is_flagged(self, tmp_path):
        res = forge_registry_check(
            graph_path=_write(tmp_path, self._GRAPH),
            role_table=self._rt(tmp_path, ["write_file", "create_file", "edit_file"]))
        assert res["passed"] is False
        assert any("do not exist in the registry" in v for v in res["violations"])
        assert res["failure_class"] == "emit_fixable"

    def test_real_registry_tools_pass(self, tmp_path):
        res = forge_registry_check(graph_path=_write(tmp_path, self._GRAPH),
                                   role_table=self._rt(tmp_path, ["web_search", "write"]))
        assert not [v for v in res["violations"] if "do not exist" in v], res["violations"]

    def test_framework_injected_names_are_exempt(self, tmp_path):
        """Nine of the ten pipelines generated so far list `write`, and they work —
        flagging the working convention would fail correct graphs."""
        res = forge_registry_check(
            graph_path=_write(tmp_path, self._GRAPH),
            role_table=self._rt(tmp_path, ["create", "edit", "write", "finish_step"]))
        assert not [v for v in res["violations"] if "do not exist" in v], res["violations"]

    def test_a_role_with_no_tools_is_fine(self, tmp_path):
        res = forge_registry_check(graph_path=_write(tmp_path, self._GRAPH),
                                   role_table=self._rt(tmp_path, []))
        assert not [v for v in res["violations"] if "do not exist" in v]


class TestWriteStepsDeclareValidation:
    """A `mode: write` step that writes nothing completes green.

    Observed four times in a row: the maker emitted its files as prose, the lifecycle
    hook logged `0 file(s)`, the step passed, and the reviewer's bounded reject loop
    burned out. A declared validation turns that into an in-place retry carrying the
    reason.
    """

    @staticmethod
    def _graph(validation):
        step = {"id": "make", "step_type": "agent", "agent_config": "maker",
                "output": {"mode": "write"}, "transitions": [{"to": "done"}]}
        if validation is not None:
            step["validation"] = validation
        return {"name": "g", "description": "x", "begin": "make",
                "end_conditions": {"combinator": "or", "conditions": [
                    {"type": "node_reached", "node": "done", "result": "completed"}]},
                "steps": [step, {"id": "done", "step_type": "gate",
                                 "transitions": [{"to": None}]}]}

    def test_a_write_step_without_validation_is_flagged(self, tmp_path):
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph(None)),
                                   role_table="")
        assert any("declares no `validation`" in v for v in res["violations"])

    def test_the_suggested_glob_satisfies_it(self, tmp_path):
        """The message has to propose something that actually works everywhere —
        `file_exists` globs, so `files: ["*"]` fits a step whose filenames are
        not known ahead of time."""
        res = forge_registry_check(
            graph_path=_write(tmp_path, self._graph([{"files": ["*"],
                                                      "tool": "file_exists"}])),
            role_table="")
        assert not [v for v in res["violations"] if "validation" in v]

    def test_content_mode_is_untouched(self, tmp_path):
        """Content mode enumerates its slots — the engine already enforces them."""
        g = self._graph(None)
        g["steps"][0]["output"] = {"mode": "content", "fixed": {"a": {"file": "o.md"}}}
        res = forge_registry_check(graph_path=_write(tmp_path, g), role_table="")
        assert not [v for v in res["violations"] if "declares no `validation`" in v]

    def test_a_tool_step_is_untouched(self, tmp_path):
        g = self._graph(None)
        g["steps"][0] = {"id": "make", "step_type": "tool", "tool_name": "write",
                         "output": {"mode": "write"}, "transitions": [{"to": "done"}]}
        res = forge_registry_check(graph_path=_write(tmp_path, g), role_table="")
        assert not [v for v in res["violations"] if "declares no `validation`" in v]


class TestTemplateToolVocabulary:
    """The agent follows its PROMPT over its toolset.

    A template promising `create_file(path, content)` produced a maker that emitted its
    files as prose, wrote zero of them, and whose step still reported success — four
    rounds, until the reviewer's bounded loop burned out. The role's `tools:` list was a
    symptom; the template was the cause.
    """

    @staticmethod
    def _graph():
        return {"name": "g", "description": "x", "begin": "make",
                "end_conditions": {"combinator": "or", "conditions": [
                    {"type": "node_reached", "node": "done", "result": "completed"}]},
                "steps": [
                    {"id": "make", "step_type": "agent", "agent_config": "maker",
                     "output": {"mode": "write"},
                     "validation": [{"files": ["*"], "tool": "file_exists"}],
                     "transitions": [{"to": "done"}]},
                    {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
                ]}

    def _rt(self, tmp_path, prompt, tools=()):
        import yaml as _y
        (tmp_path / "templates").mkdir(exist_ok=True)
        (tmp_path / "templates" / "maker.md").write_text(prompt, encoding="utf-8")
        p = tmp_path / "rt.yaml"
        p.write_text(_y.safe_dump({"maker": {"model": "host", "tools": list(tools),
                                             "template": "templates/maker.md"}}),
                     encoding="utf-8")
        return str(p)

    def test_a_template_promising_a_nonexistent_tool_is_flagged(self, tmp_path):
        rt = self._rt(tmp_path,
                      "Use `create_file(path, content)` and `write_file(path, content)`.")
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph()),
                                   role_table=rt)
        assert res["passed"] is False
        v = [x for x in res["violations"] if "template tells the agent" in x]
        assert v and "do not exist at all" in v[0], res["violations"]
        assert "create_file" in v[0] and "write_file" in v[0]

    def test_the_injected_write_tools_are_accepted(self, tmp_path):
        """`mode: write` really does give the agent create/edit — naming those is right."""
        rt = self._rt(tmp_path, "Use `create(file, content)` and `edit(file, old_str, new_str)`.")
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph()),
                                   role_table=rt)
        assert not [x for x in res["violations"] if "template tells the agent" in x], \
            res["violations"]

    def test_a_real_tool_the_role_was_not_granted_is_flagged_differently(self, tmp_path):
        rt = self._rt(tmp_path, "Call `web_search(query)` to research.", tools=[])
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph()),
                                   role_table=rt)
        v = [x for x in res["violations"] if "template tells the agent" in x]
        assert v and "not granted to this role" in v[0]

    def test_granting_it_clears_the_violation(self, tmp_path):
        rt = self._rt(tmp_path, "Call `web_search(query)` to research.",
                      tools=["web_search"])
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph()),
                                   role_table=rt)
        assert not [x for x in res["violations"] if "template tells the agent" in x]

    def test_prose_that_merely_mentions_a_tool_is_not_flagged(self, tmp_path):
        """Only call-shaped mentions count — this is what keeps the rule quiet."""
        rt = self._rt(tmp_path,
                      "Do not confuse this with create_file from other systems; "
                      "the `write_file` name is wrong here.")
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph()),
                                   role_table=rt)
        assert not [x for x in res["violations"] if "template tells the agent" in x], \
            res["violations"]

    def test_an_inline_system_prompt_is_scanned_too(self, tmp_path):
        """After registration the prompt is inline in roles.json, not a file."""
        import yaml as _y
        p = tmp_path / "rt.yaml"
        p.write_text(_y.safe_dump({"maker": {"model": "host", "tools": [],
                                             "system_prompt": "Use `edit_file(a, b, c)`."}}),
                     encoding="utf-8")
        res = forge_registry_check(graph_path=_write(tmp_path, self._graph()),
                                   role_table=str(p))
        assert any("edit_file" in x for x in res["violations"])


class TestRoutingFileIsGuaranteed:
    """A step that routes on a file must write that file.

    From a live run of `gen_math_olympiad`: `final_answer` was `mode: write` with
    `validation: {tool: file_exists, files: ["*"]}` and two edges, both matching on
    `final_verdict.json`. It wrote `final_answer.md` — which satisfies `["*"]` —
    never wrote the verdict, and the run died with "No matching transition from
    'final_answer' with flags {'wrote_files': True}". A finished proof, produced and
    thrown away.

    `["*"]` was the remediation THIS GATE suggested for the write-mode rule, so the
    gate taught the emitter the shape that broke it.
    """

    @staticmethod
    def _step(**over):
        s = {"id": "final_answer", "step_type": "agent", "agent_config": "w",
             "output": {"mode": "write"},
             "validation": [{"tool": "file_exists", "files": ["*"]}],
             "transitions": [
                 {"to": "done", "match": {"from_file": "final_verdict.json",
                                          "field": "passed", "value": True}},
                 {"to": "give_up", "match": {"from_file": "final_verdict.json",
                                             "field": "passed", "value": False}}]}
        s.update(over)
        return s

    def _graph(self, step):
        return {"name": "g", "description": "d", "begin": step["id"],
                "end_conditions": {"combinator": "or", "conditions": [
                    {"type": "node_reached", "node": "done", "result": "completed"},
                    {"type": "node_reached", "node": "give_up", "result": "failed"}]},
                "steps": [step,
                          {"id": "done", "step_type": "gate",
                           "transitions": [{"to": None}]},
                          {"id": "give_up", "step_type": "gate",
                           "transitions": [{"to": None}]}]}

    def _run(self, tmp_path, step):
        return forge_registry_check(graph_path=_write(tmp_path, self._graph(step)),
                                    role_table="")["violations"]

    def test_the_star_validation_does_not_cover_the_routing_file(self, tmp_path):
        v = self._run(tmp_path, self._step())
        assert any("route on ['final_verdict.json']" in x for x in v), v
        assert any('does not cover it' in x for x in v), v

    def test_naming_the_file_in_the_validation_satisfies_it(self, tmp_path):
        v = self._run(tmp_path, self._step(
            validation=[{"tool": "file_exists", "files": ["final_verdict.json"]}]))
        assert not [x for x in v if "route on" in x], v

    def test_a_content_fixed_slot_satisfies_it(self, tmp_path):
        """The other guarantee mechanism: the engine promotes the named slot."""
        v = self._run(tmp_path, self._step(
            output={"mode": "content",
                    "fixed": {"verdict": {"file": "final_verdict.json"}}},
            validation=[]))
        assert not [x for x in v if "route on" in x], v

    def test_the_shorthand_fixed_form_counts_too(self, tmp_path):
        v = self._run(tmp_path, self._step(
            output={"mode": "content", "fixed": {"verdict": "final_verdict.json"}},
            validation=[]))
        assert not [x for x in v if "route on" in x], v

    def test_a_step_that_routes_on_nothing_is_not_flagged(self, tmp_path):
        v = self._run(tmp_path, self._step(transitions=[{"to": "done"}]))
        assert not [x for x in v if "route on" in x], v

    def test_a_gate_step_is_exempt(self, tmp_path):
        """A gate executes nothing — it routes on a file an earlier step wrote."""
        g = self._graph({"id": "route", "step_type": "gate", "transitions": [
            {"to": "done", "match": {"from_file": "review_verdict.json",
                                     "field": "passed", "value": True}},
            {"to": "give_up"}]})
        g["begin"] = "route"
        v = forge_registry_check(graph_path=_write(tmp_path, g),
                                 role_table="")["violations"]
        assert not [x for x in v if "route on" in x], v

    def test_a_tool_step_is_exempt(self, tmp_path):
        """`test_report.json` is run_tests' contract, not the graph's."""
        g = self._graph({"id": "t", "step_type": "tool", "tool_name": "run_tests",
                         "transitions": [
                             {"to": "done", "match": {"from_file": "test_report.json",
                                                      "field": "passed", "value": True}},
                             {"to": "give_up"}]})
        g["begin"] = "t"
        v = forge_registry_check(graph_path=_write(tmp_path, g),
                                 role_table="")["violations"]
        assert not [x for x in v if "route on" in x], v


class TestTheWriteModeRemedySuggestsTheRoutingFile:
    """The gate must not teach the shape that broke a run."""

    def test_it_names_the_routing_file_when_there_is_one(self, tmp_path):
        step = {"id": "s", "step_type": "agent", "agent_config": "w",
                "output": {"mode": "write"},
                "transitions": [{"to": "done", "match": {
                    "from_file": "verdict.json", "field": "passed", "value": True}},
                    {"to": "done"}]}
        g = {"name": "g", "description": "d", "begin": "s",
             "end_conditions": {"combinator": "or", "conditions": [
                 {"type": "node_reached", "node": "done", "result": "completed"}]},
             "steps": [step, {"id": "done", "step_type": "gate",
                              "transitions": [{"to": None}]}]}
        v = forge_registry_check(graph_path=_write(tmp_path, g),
                                 role_table="")["violations"]
        msg = next(x for x in v if "declares no `validation`" in x)
        assert "['verdict.json']" in msg
        assert '["*"]' not in msg.split("validation:")[1]

    def test_the_star_hatch_survives_for_a_step_that_routes_on_nothing(self, tmp_path):
        step = {"id": "s", "step_type": "agent", "agent_config": "w",
                "output": {"mode": "write"}, "transitions": [{"to": "done"}]}
        g = {"name": "g", "description": "d", "begin": "s",
             "end_conditions": {"combinator": "or", "conditions": [
                 {"type": "node_reached", "node": "done", "result": "completed"}]},
             "steps": [step, {"id": "done", "step_type": "gate",
                              "transitions": [{"to": None}]}]}
        v = forge_registry_check(graph_path=_write(tmp_path, g),
                                 role_table="")["violations"]
        msg = next(x for x in v if "declares no `validation`" in x)
        assert '["*"]' in msg
