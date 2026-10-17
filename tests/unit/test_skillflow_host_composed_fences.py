"""AItelierSkillFlow composes BOTH run-control fences on every shared method.

The Coding Mode controller fence (core.coding_workspace) and the frozen-bundle
served refusal (core.pipeline_frozen_bundle.launch_block) were added to the same
SkillFlow methods by separate branches. A textual merge of the two leaves two
`def`s of one name in the class body, and Python keeps only the later one: one
protection silently disappears while both branches' own tests still pass.
"""
import ast
import inspect
from pathlib import Path

import core.skillflow_host as host

FENCED = {"create_run": ("fence_create", "launch_block"),
          "resume_run": ("fence_control", "_refuse_unserved_bundle"),
          "approve_checkpoint": ("fence_control", "_refuse_unserved_bundle"),
          "reject_checkpoint": ("fence_control", "_refuse_unserved_bundle"),
          "reactivate_run": ("fence_control", "_refuse_unserved_bundle")}


def _class_defs():
    tree = ast.parse(Path(host.__file__).read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AItelierSkillFlow")
    return [n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def test_no_method_is_defined_twice():
    names = _class_defs()
    assert sorted(n for n in set(names) if names.count(n) > 1) == []


def test_each_shared_method_carries_both_fences():
    for name, fences in FENCED.items():
        source = inspect.getsource(getattr(host.AItelierSkillFlow, name))
        for fence in fences:
            assert fence in source, (name, fence)
