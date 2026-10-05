"""CPU-only planner controls over real temporary Git histories.

Run directly with unittest in an approved disposable CPU environment. This does
not execute Godot, import its harness, or prove runtime/context independence.
The graph-only select_scope controls stub the unrelated clock prerequisite.
The same collision test source must also run against the accepted old package
to retain a causal RED; do not copy or reimplement the old planner here.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aitelier import gate_evidence as ge
from aitelier import round_feedback as rf
from aitelier import scoped_gate as sg
from aitelier.gate_coverage import publication_scope_refusal, validate_coverage


class ProjectBoundaryTests(unittest.TestCase):
    parent = "scripts/data/appearance_registry.gd"
    child = "contracts/art.npc-consistency/interfaces/appearance_registry.gd"

    def setUp(self):
        self.delta = 0
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Boundary Fixture")
        self.git("config", "user.email", "boundary@example.invalid")
        self.write("project.godot", "[application]\n")
        self.write(self.parent, "class_name AppearanceRegistry\nextends RefCounted\n")
        scenarios = []
        for name in "abcde":
            self.write(name + ".gd", "extends Node\nvar value = 0\n")
            refs = '[ext_resource path="res://' + name + '.gd"]\n'
            if name == "a":
                refs += '[ext_resource path="res://' + self.parent + '"]\n'
            self.write(name + ".tscn", "[gd_scene]\n" + refs)
            scenarios.append({"name": name, "scene": "res://" + name + ".tscn",
                              "repeatability": name == "a", "timeline": [
                                  {"at": 4, "assert": [{"name": "N.value",
                                    "node": "N", "expr": "value == 1"}]}]})
        self.spec = rf.expand_feedback_spec({"scenarios": scenarios})

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo), *args],
            check=True, capture_output=True, text=True).stdout.strip()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def changed(self, path=None):
        path = path or self.parent
        target = self.repo / path
        self.delta += 1
        lines = [line for line in target.read_text(encoding="utf-8").splitlines()
                 if not line.startswith("var changed = ")]
        self.write(path, "\n".join(lines) + f"\nvar changed = {self.delta}\n")
        return self.commit("parent delta")

    def graph(self, revision):
        return sg._graph(*sg._snapshot(self.repo, revision))

    def feedback(self, base, head, requested=()):
        return rf.feedback_plan(self.repo, base, head, self.spec,
                                "playtest/", ["b"], ["c"], requested)

    def conservative(self, base, head):
        # Only isolate the graph; this is not a clock/context measurement.
        with patch.object(sg, "_measured_context", return_value=set()):
            return sg.select_scope(self.repo, base, head, self.spec)

    def assert_full_fallback(self, scope):
        self.assertTrue(scope["fallback_full"])
        self.assertEqual(scope["coverage"], "full")
        self.assertEqual(scope["selected_scenarios"],
                         [s["name"] for s in self.spec["scenarios"]])
        self.assertTrue(scope["selection_basis"]["fallback_reason"])

    def test_observed_nested_and_ignored_collision_forms_plan(self):
        self.write("contracts/art.npc-consistency/project.godot", "[application]\n")
        self.write("contracts/art.npc-consistency/interfaces/.gdignore", "")
        self.write(self.child, "class_name AppearanceRegistry\nextends RefCounted\n")
        base = self.commit("observed parent and contract stub topology")
        head = self.changed()
        scope = self.feedback(base, head, requested=["d"])
        self.assertEqual(scope["selected_scenarios"], ["a", "b", "c", "d", "a__repeatability"])
        self.assertEqual(scope["unselected_scenarios"], ["e"])
        self.assertEqual(scope["selection_basis"]["changed_files"], [self.parent])
        self.assertEqual(scope["selection_basis"]["requested"], ["d"])
        self.assertEqual(scope["selection_basis"]["sentinels"], ["b"])
        self.assertEqual(scope["selection_basis"]["mandatory"], ["c"])
        self.assertEqual(scope["selection_basis"]["direct_consumers"],
                         {"a": [self.parent], "a__repeatability": [self.parent]})
        self.assertFalse(scope["full_recommended"])
        self.assertIsNone(validate_coverage(scope))
        self.assertIn("UNKNOWN", scope["unselected_observations"]["e"])
        conservative = self.conservative(base, head)
        self.assertEqual(conservative["selected_scenarios"], ["a", "a__repeatability"])
        self.assertFalse(conservative["fallback_full"])
        for revision in (base, head):
            files, _ = sg._snapshot(self.repo, revision)
            self.assertIn(self.child, files)  # Snapshot/change accounting is intact.
            edges, _, opaque = self.graph(revision)
            self.assertNotIn(self.child, edges)
            self.assertIn(self.child, opaque)  # Exclusion is not child validation.
            self.assertIn(self.parent, edges["a.tscn"])

    def test_each_boundary_alone_excludes_child_global_classes(self):
        for marker in ("package/project.godot", "package/.gdignore"):
            with self.subTest(marker=marker):
                self.write(marker, "[application]\n" if marker.endswith(".godot") else "")
                self.write("package/deep/copy.gd", "class_name AppearanceRegistry\n")
                base = self.commit("one nested boundary")
                head = self.changed("a.gd")
                self.assertTrue(self.feedback(base, head)["feedback_available"])
                (self.repo / marker).unlink()
                (self.repo / "package/deep/copy.gd").unlink()
                self.commit("remove fixture boundary")

    def test_same_parent_duplicate_refuses_at_either_endpoint(self):
        clean = self.commit("clean parent")
        self.write("ordinary_sibling/copy.gd", "class_name AppearanceRegistry\n")
        duplicate = self.commit("real parent duplicate")
        for base, head in ((clean, duplicate), (duplicate, clean)):
            with self.subTest(base=base):
                with self.assertRaisesRegex(ValueError, "ambiguous class_name: AppearanceRegistry"):
                    self.feedback(base, head)
                self.assert_full_fallback(self.conservative(base, head))

    def test_root_markers_and_marker_directories_do_not_hide_parent_errors(self):
        for marker in (".gdignore", ".gdignore/entry.txt",
                       "ordinary_sibling/.gdignore/entry.txt",
                       "ordinary_sibling/project.godot/entry.txt"):
            with self.subTest(marker=marker):
                self.write(marker, "not a nested marker file\n")
                self.write("ordinary_sibling/copy.gd", "class_name AppearanceRegistry\n")
                revision = self.commit("parent duplicate with ineffective boundary")
                with self.assertRaisesRegex(ValueError, "ambiguous class_name"):
                    self.graph(revision)
                (self.repo / marker).unlink()
                # Empty directories have no Git meaning; remove them for the next shape.
                if marker == ".gdignore/entry.txt":
                    (self.repo / ".gdignore").rmdir()
                (self.repo / "ordinary_sibling/copy.gd").unlink()
                self.commit("remove ineffective fixture marker")

    def test_symlink_marker_is_refused_before_namespace_filtering(self):
        self.write("package/copy.gd", "class_name AppearanceRegistry\n")
        (self.repo / "package/.gdignore").symlink_to("../project.godot")
        revision = self.commit("unsupported marker entry")
        with self.assertRaisesRegex(ValueError, "unsupported Git entry: package/.gdignore"):
            self.graph(revision)
        self.assert_full_fallback(self.conservative(revision, revision))

    def test_boundary_addition_or_removal_does_not_erase_endpoint_duplicate(self):
        for marker in ("package/project.godot", "package/.gdignore"):
            with self.subTest(marker=marker):
                self.write("package/copy.gd", "class_name AppearanceRegistry\n")
                unbounded = self.commit("unbounded same-parent duplicate")
                self.write(marker, "")
                bounded = self.commit("boundary added")
                self.graph(bounded)
                for base, head in ((unbounded, bounded), (bounded, unbounded)):
                    with self.assertRaisesRegex(ValueError, "ambiguous class_name"):
                        self.feedback(base, head)
                (self.repo / marker).unlink()
                (self.repo / "package/copy.gd").unlink()
                self.commit("remove collision fixture")

    def test_boundary_transitions_keep_all_changes_and_unknown_dependencies(self):
        self.write("package/unique.gd", "class_name ChildOnly\n")
        self.write("a.gd", 'extends Node\nvar child = preload("res://package/unique.gd")\n')
        parent = self.commit("literal parent dependency")
        for marker in ("package/project.godot", "package/.gdignore"):
            with self.subTest(marker=marker):
                self.write(marker, "")
                bounded = self.commit("add child boundary")
                for base, head in ((parent, bounded), (bounded, parent)):
                    scope = self.feedback(base, head, requested=["a"])
                    self.assertIn(marker, scope["selection_basis"]["changed_files"])
                    self.assertIn(marker, scope["selection_basis"]["unmapped_changes"])
                    self.assertTrue(scope["full_recommended"])
                    self.assertIn("package/unique.gd",
                                  scope["selection_basis"]["opaque_dependencies"]["a"])
                    self.assert_full_fallback(self.conservative(base, head))
                (self.repo / marker).unlink()
                parent = self.commit("remove child boundary")

    def test_cross_boundary_literals_autoloads_and_scene_roots_remain_unknown(self):
        self.write("package/project.godot", "")
        self.write("package/child.gd", "class_name ChildOnly\n")
        self.write("package/child.tscn", '[gd_scene]\n')
        self.write("a.gd", 'extends Node\nvar child = preload("res://package/child.gd")\n')
        self.write("project.godot", '[autoload]\nChild="*res://package/child.gd"\n')
        base = self.commit("cross-boundary parent relationships")
        head = self.changed("a.gd")
        self.spec["scenarios"][3]["scene"] = "res://package/child.tscn"
        scope = self.feedback(base, head, requested=["a"])
        self.assertTrue(scope["full_recommended"])
        opaque = scope["selection_basis"]["opaque_dependencies"]
        self.assertIn("package/child.gd", opaque["b"])  # Autoload reaches every scenario.
        self.assertIn("package/child.tscn", opaque["d"])
        self.assert_full_fallback(self.conservative(base, head))

    def test_child_only_class_reference_cannot_silently_lose_an_edge(self):
        self.write("package/.gdignore", "")
        self.write("package/child.gd", "class_name ChildOnly\n")
        self.write("a.gd", "extends Node\nvar child: ChildOnly\n")
        base = self.commit("class outside parent namespace")
        head = self.changed("a.gd")
        scope = self.feedback(base, head)
        reasons = scope["selection_basis"]["opaque_dependencies"]["a"]["a.gd"]
        self.assertTrue(any("ChildOnly" in reason for reason in reasons))
        self.assertTrue(scope["full_recommended"])
        self.assert_full_fallback(self.conservative(base, head))

    def test_parent_class_edges_and_shared_autoload_remain_visible(self):
        self.write("b.gd", "extends Node\nvar registry: AppearanceRegistry\n")
        self.write("project.godot", '[autoload]\nRegistry="*res://' + self.parent + '"\n')
        base = self.commit("parent class and autoload")
        head = self.changed()
        edges, autoload, _ = self.graph(head)
        self.assertIn(self.parent, edges["b.gd"])
        self.assertEqual(autoload, {self.parent})
        scope = self.feedback(base, head)
        self.assertEqual(scope["selection_basis"]["shared_runtime_changes"], [self.parent])
        self.assertTrue(scope["full_recommended"])
        self.assertFalse(scope["feedback_available"])
        self.assertEqual(scope["coverage"], "full")
        conservative = self.conservative(base, head)
        self.assertEqual(conservative["coverage"], "full")
        self.assertFalse(conservative["fallback_full"])

    def test_unreachable_child_changes_stay_unmapped_and_recommend_full(self):
        self.write("package/.gdignore", "")
        self.write("package/child.gd", "class_name ChildOnly\n")
        base = self.commit("unreferenced child")
        head = self.changed("package/child.gd")
        scope = self.feedback(base, head)
        self.assertEqual(scope["selection_basis"]["changed_files"], ["package/child.gd"])
        self.assertEqual(scope["selection_basis"]["unmapped_changes"], ["package/child.gd"])
        self.assertIn("changed", scope["selection_basis"]["changed_symbols"]["package/child.gd"])
        self.assertTrue(scope["full_recommended"])
        self.assertFalse(scope["feedback_available"])
        self.assertEqual(scope["coverage"], "full")
        self.assert_full_fallback(self.conservative(base, head))

    def test_base_and_head_unknown_reasons_are_both_retained(self):
        self.write("package/.gdignore", "")
        self.write("package/child.gd", "class_name ChildOnly\n")
        self.write("a.gd", 'extends Node\nvar child = preload("res://package/child.gd")\n')
        base = self.commit("base cross-boundary dependency")
        self.write("a.gd", "extends Node\nvar child = load(dynamic_path)\n")
        head = self.commit("head dynamic dependency")
        scope = self.feedback(base, head)
        opaque = scope["selection_basis"]["opaque_dependencies"]["a"]
        self.assertIn("package/child.gd", opaque)
        self.assertTrue(any("dynamic" in reason for reason in opaque["a.gd"]))
        self.assertTrue(scope["full_recommended"])
        self.assert_full_fallback(self.conservative(base, head))

    def test_partial_publication_and_first_attempt_guards_remain_in_force(self):
        base = self.commit("clean")
        head = self.changed("a.gd")
        for requested in ((), ["a", "b", "c", "d", "e"]):
            scope = self.feedback(base, head, requested=requested)
            self.assertTrue(publication_scope_refusal(scope))
            report = {"passed": True, "purpose": rf.PURPOSE, "gate_coverage": scope,
                      "upstream_state": "passed"}
            self.assertEqual(ge.report_state(report), "partial")
            self.assertEqual(ge.release_disposition(report), "unresolved")
        with self.assertRaisesRegex(ValueError, "unknown feedback policy/request"):
            self.feedback(base, head, requested=["not-authored"])
        for artifact in ("playtest_report.json", "round_feedback_raw.json",
                         "round_feedback_request.json"):
            out = Path(self.temp.name) / artifact.removesuffix(".json")
            out.mkdir()
            first = out / artifact
            first.write_bytes(b"first failure; keep exact bytes")
            before = first.read_bytes()
            with self.assertRaisesRegex(ValueError, "first attempt"):
                rf.require_new_feedback_output(out)
            self.assertEqual(first.read_bytes(), before)


    def test_alias_child_scene_requires_conservative_feedback(self):
        self.write("package/.gdignore", "")
        self.write("package/child.gd", "class_name ChildRule\nextends RefCounted\n")
        self.write("package/child.tscn", "[gd_scene]\n")
        self.spec["scenarios"][3]["scene"] = "res://./package/child.tscn"
        base = self.commit("stationary ignored child, noncanonical requested scene")
        head = self.changed("a.gd")
        scope = self.feedback(base, head, requested=["d"])
        basis = scope["selection_basis"]
        self.assertEqual(basis["changed_files"], ["a.gd"])
        self.assertEqual(basis["shared_runtime_changes"], [])
        self.assertEqual(basis["unmapped_changes"], [])
        self.assertIn("d", scope["selected_scenarios"])
        self.assertTrue(scope["full_recommended"])
        self.assertIn("scenario root is missing: ./package/child.tscn",
                      basis["opaque_dependencies"]["d"]["./package/child.tscn"])
        self.assert_full_fallback(self.conservative(base, head))

    def test_missing_child_scene_requires_conservative_feedback(self):
        self.write("package/.gdignore", "")
        self.write("package/child.gd", "class_name ChildRule\nextends RefCounted\n")
        self.write("package/child.tscn", "[gd_scene]\n")
        self.spec["scenarios"][3]["scene"] = "res://package/missing.tscn"
        base = self.commit("stationary ignored child, missing requested scene")
        head = self.changed("a.gd")
        scope = self.feedback(base, head, requested=["d"])
        basis = scope["selection_basis"]
        self.assertEqual(basis["changed_files"], ["a.gd"])
        self.assertEqual(basis["shared_runtime_changes"], [])
        self.assertEqual(basis["unmapped_changes"], [])
        self.assertIn("d", scope["selected_scenarios"])
        self.assertTrue(scope["full_recommended"])
        self.assertIn("scenario root is missing: package/missing.tscn",
                      basis["opaque_dependencies"]["d"]["package/missing.tscn"])
        self.assert_full_fallback(self.conservative(base, head))

    def test_feedback_reuses_literal_scene_root_policy(self):
        base = self.commit("ordinary parent scenes")
        head = self.changed("a.gd")
        for root in ("res://d.tscn", None, "d.tscn"):
            with self.subTest(root=root):
                self.spec["scenarios"][3]["scene"] = root
                scope = self.feedback(base, head, requested=["d"])
                self.assertIn("d", scope["selected_scenarios"])
                if root == "res://d.tscn":
                    self.assertFalse(scope["full_recommended"])
                    self.assertEqual(scope["selection_basis"]["opaque_dependencies"], {})
                else:
                    self.assertTrue(scope["full_recommended"])
                    self.assertIn("scenario has no literal scene root: d",
                                  scope["selection_basis"]["opaque_dependencies"]["d"]["<scene-root>"])
                    self.assert_full_fallback(self.conservative(base, head))
        del self.spec["scenarios"][3]["scene"]
        self.spec["scene"] = "res://d.tscn"
        scope = self.feedback(base, head, requested=["d"])
        self.assertFalse(scope["full_recommended"])
        self.assertEqual(scope["selection_basis"]["opaque_dependencies"], {})

    def test_scene_existence_uses_head_without_losing_base_witnesses(self):
        self.spec["scenarios"][3]["scene"] = "res://new_scene.tscn"
        absent = self.commit("before new parent scene")
        self.write("new_scene.tscn", "[gd_scene]\n")
        present = self.changed("a.gd")
        scope = self.feedback(absent, present, requested=["d"])
        self.assertFalse(scope["full_recommended"])
        self.assertEqual(scope["selection_basis"]["opaque_dependencies"], {})
        self.assertEqual(scope["selection_basis"]["direct_consumers"]["d"], ["new_scene.tscn"])
        self.assertFalse(self.conservative(absent, present)["fallback_full"])
        removed = self.feedback(present, absent, requested=["d"])
        self.assertTrue(removed["full_recommended"])
        self.assertEqual(removed["selection_basis"]["direct_consumers"]["d"], ["new_scene.tscn"])
        self.assertIn("scenario root is missing: new_scene.tscn",
                      removed["selection_basis"]["opaque_dependencies"]["d"]["new_scene.tscn"])
        self.assert_full_fallback(self.conservative(present, absent))


if __name__ == "__main__":
    unittest.main()
