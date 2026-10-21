"""A rejected checkpoint must rewind to where the GRAPH says, not to itself.

skillflow resolves the target as `redirect_to or step_id` and never reads the
graph, so `checkpoint_reject_to` only works if the CALLER passes it. AItelier
has four call sites; one of them had the logic and three did not, which is how
the blind-vision gate's rejection ended up addressed to the gate itself instead
of to the PM (jinyong-hud, 2026-08-27). The source guard at the bottom is the
part that actually prevents a recurrence: a new caller written without
`redirect_to` fails here rather than silently in a live run.
"""
import ast
import pathlib

import pytest

from core.run_driver import checkpoint_reject_target
from core.skillflow_host import AItelierSkillFlow

ROOT = pathlib.Path(__file__).resolve().parents[2]


class _Node:
    def __init__(self, target):
        self.checkpoint_reject_to = target


class _Resolver:
    def __init__(self, node):
        self._node = node

    def get_node(self, step_id):
        return self._node


class _SF:
    def __init__(self, node=None, raises=False):
        self._node, self._raises = node, raises

    def _get_resolver(self, graph_name):
        if self._raises:
            raise RuntimeError("no such graph")
        return _Resolver(self._node)


def test_declared_target_is_returned():
    assert checkpoint_reject_target(_SF(_Node("3")), "dpe_game", "5_vision_human") == "3"


def test_no_declared_target_means_rerun_in_place():
    # "" is what skillflow's default already means — re-run the checkpoint step.
    assert checkpoint_reject_target(_SF(_Node("")), "dpe_game", "cp") == ""
    assert checkpoint_reject_target(_SF(_Node(None)), "dpe_game", "cp") == ""


def test_missing_node_or_broken_resolver_never_raises():
    # A reject must not fail because the graph could not be read; falling back
    # to "" degrades to skillflow's own default rather than losing the reject.
    assert checkpoint_reject_target(_SF(None), "dpe_game", "cp") == ""
    assert checkpoint_reject_target(_SF(raises=True), "gone", "cp") == ""


def _assert_host_redirect_forwarding(tmp_path, positional):
    from skillflow.core import StepResult
    from skillflow.graph import PipelineGraph, StepNode, Transition

    tmp_path.mkdir(parents=True, exist_ok=True)
    sf = AItelierSkillFlow(str(tmp_path / "sf.db"),
                          workspace_base=str(tmp_path / "ws"),
                          projects_base=str(tmp_path / "projects"))
    try:
        sf.register_graph(PipelineGraph(name="redirect_proof", begin="maker", steps=[
            StepNode(id="maker", transitions=[Transition(to="gate")]),
            StepNode(id="gate", checkpoint=True, checkpoint_reject_to="maker",
                     transitions=[Transition(to="done")]),
            StepNode(id="done")]))
        rid = sf.create_run("redirect_proof", project_id="redirect-proof")
        sf.start_run(rid)
        for step_id in ("maker", "gate"):
            sf.advance_run(rid)
            claimed = sf.claim_next_step(rid)
            assert claimed is not None and claimed.step_id == step_id
            sf.confirm_step(claimed.token, StepResult(outputs={"text": step_id}))
        sf.advance_run(rid)
        assert sf.get_run(rid)["status"] == "paused"
        target = checkpoint_reject_target(sf, "redirect_proof", "gate", rid)
        assert target == "maker"
        if positional:
            sf.reject_checkpoint(rid, "gate", "revise maker", target)
        else:
            sf.reject_checkpoint(rid, "gate", "revise maker", redirect_to=target)
        assert sf.get_run(rid)["current_node"] == "maker", "redirect_to was dropped"
        assert sf.get_run(rid)["status"] == "running"
        claimed = sf.claim_next_step(rid)
        assert claimed is not None and claimed.step_id == "maker"
        # The SDK renders cumulative feedback; raw latest rejection stays exact.
        feedback = claimed.inputs["_feedback"]
        assert isinstance(feedback, str) and feedback.count("revise maker") == 1
        assert claimed.inputs["_rejection"] == "revise maker"
    finally:
        sf._conn.close()


@pytest.mark.parametrize("positional", [False, True])
def test_host_forwarding_witness_detects_dropped_redirect(tmp_path, monkeypatch, positional):
    original = AItelierSkillFlow.reject_checkpoint

    def drop_redirect(self, run_id, step_id, feedback, redirect_to=""):
        return original(self, run_id, step_id, feedback)

    monkeypatch.setattr(AItelierSkillFlow, "reject_checkpoint", drop_redirect)
    with pytest.raises(AssertionError, match="redirect_to was dropped"):
        _assert_host_redirect_forwarding(tmp_path, positional)


@pytest.mark.parametrize("relpath", ["api", "core"])
def test_every_reject_checkpoint_caller_passes_redirect_to(relpath, tmp_path):
    """No caller may leave the rewind target to skillflow's default by accident.

    Omitting `redirect_to` is occasionally right — but only deliberately, and
    the helper expresses that as an explicit "". An omission is indistinguishable
    from a bug, so require the keyword or a tested transparent host forwarder.
    """
    offenders = []
    code = AItelierSkillFlow.reject_checkpoint.__code__
    forward_call = ast.dump(ast.parse(
        "super().reject_checkpoint(run_id, *args, **kwargs)", mode="eval").body)
    for path in sorted((ROOT / relpath).rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        host_forward = None
        if path.resolve() == pathlib.Path(code.co_filename).resolve():
            method = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                           and n.lineno == code.co_firstlineno), None)
            if method is not None and isinstance(method.body[-1], ast.Return):
                host_forward = method.body[-1].value
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "reject_checkpoint"):
                continue
            # `def reject_checkpoint(...)` handlers are not calls; only calls
            # onto an object (sf.reject_checkpoint) reach skillflow.
            if any(kw.arg == "redirect_to" for kw in node.keywords):
                continue
            if len(node.args) >= 4:      # positional redirect_to
                continue
            if node is host_forward and ast.dump(node) == forward_call:
                # Only this exact method/call earns an exception, after real SDK proof.
                for positional in (False, True):
                    _assert_host_redirect_forwarding(tmp_path / str(positional), positional)
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, (
        "reject_checkpoint called without redirect_to — the graph's "
        "checkpoint_reject_to will be ignored at: " + ", ".join(offenders))
