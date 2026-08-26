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


@pytest.mark.parametrize("relpath", ["api", "core"])
def test_every_reject_checkpoint_caller_passes_redirect_to(relpath):
    """No caller may leave the rewind target to skillflow's default by accident.

    Omitting `redirect_to` is occasionally right — but only deliberately, and
    the helper expresses that as an explicit "". An omission is indistinguishable
    from a bug, so require the keyword at every call site.
    """
    offenders = []
    for path in sorted((ROOT / relpath).rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
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
            offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, (
        "reject_checkpoint called without redirect_to — the graph's "
        "checkpoint_reject_to will be ignored at: " + ", ".join(offenders))
