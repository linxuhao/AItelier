"""Conformance harness for the standalone benchmark fake. UNRUN statement
candidate. Stdlib unittest only; loads only the sibling literal vectors file.
Never imports production code and never runs a model, evaluator or checker."""
import copy
import importlib.util
import json
import math
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_fake():
    spec = importlib.util.spec_from_file_location(
        "rsi_benchmark_fake", os.path.join(_HERE, "fake.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fake = _load_fake()
with open(os.path.join(_HERE, "conformance_vectors.json"), encoding="utf-8") as _f:
    DOC = json.load(_f)
FIX = DOC["fixture"]
BID = FIX["header"]["benchmark_id"]
VER = FIX["header"]["version"]
ARMS = FIX["header"]["arms"]
CASES = list(FIX["header"]["cases"])


def _set_path(obj, path, value):
    parts = path.split(".")
    for p in parts[:-1]:
        obj = obj[p]
    obj[parts[-1]] = value


def _drop_path(obj, path):
    parts = path.split(".")
    for p in parts[:-1]:
        obj = obj[p]
    del obj[parts[-1]]


def _get_path(obj, path):
    for p in path.split("."):
        obj = obj[p]
    return obj


def _resolve(value, world):
    if isinstance(value, dict):
        if set(value) == {"$float"}:
            return {"nan": math.nan, "inf": math.inf}[value["$float"]]
        if set(value) == {"$input_identity"}:
            return world.identity(value["$input_identity"], "input_identity")
        if set(value) == {"$checker_identity"}:
            return world.identity(value["$checker_identity"], "checker_identity")
        return {k: _resolve(v, world) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, world) for v in value]
    return value


class World:
    def __init__(self):
        self.f = fake.BenchmarkFake()
        self.saved = {}
        self.callers = {"$owner": DOC["owner"], "$other": DOC["other_caller"]}

    def caller(self, name):
        return self.callers.get(name, name)

    def header(self, step):
        h = copy.deepcopy(FIX["header"])
        for k, v in step.get("set", {}).items():
            _set_path(h, k, _resolve(v, self))
        for k in step.get("drop", []):
            _drop_path(h, k)
        return h

    def fill_inputs(self, caller, skip_private=()):
        for cid in CASES:
            self.f.set_public_input(caller, BID, VER, cid, {
                "prompt": FIX["public_prompt_template"].format(case=cid)})
            if cid in skip_private:
                continue
            self.f.set_private_refs(caller, BID, VER, cid, {
                "rubric_ref": FIX["private_rubric_template"].format(case=cid),
                "checker_ref": FIX["private_checker_template"].format(case=cid),
                "max_points": FIX["private_max_points"]})

    def published(self):
        owner = DOC["owner"]
        self.f.create_draft(owner, self.header({}))
        self.fill_inputs(owner)
        self.f.publish(owner, BID, VER)

    def identity(self, case, key):
        return self.f.get_definition(BID, VER)["cases"][case][key]

    def row(self, spec):
        arm, case, rep = spec["arm"], spec["case_id"], spec["repetition"]
        row = {"schema": "rsi.benchmark.v1", "benchmark_id": BID, "version": VER,
               "bundle_identity": DOC["arm_bundles"].get(arm, DOC["arm_bundles"]["baseline"]),
               "runtime": copy.deepcopy(DOC["runtime"]),
               "case_id": case, "repetition": rep, "arm": arm,
               "run_receipt_id": "run-%s-%s-%s" % (arm, case, rep)}
        if case in CASES:
            row["input_identity"] = self.identity(case, "input_identity")
            row["checker_identity"] = self.identity(case, "checker_identity")
        else:
            row["input_identity"] = row["checker_identity"] = "sha256:" + "0" * 64
        row.update(copy.deepcopy(DOC["row_defaults"]))
        for k, v in spec.get("set", {}).items():
            _set_path(row, k, _resolve(v, self))
        for k in spec.get("drop", []):
            _drop_path(row, k)
        return row

    def fill_matrix(self, step):
        skip = set(step.get("skip", []))
        over = step.get("overrides", {})
        for arm in ARMS:
            for cid in CASES:
                for rep in (1, 2, 3):
                    key = "%s/%s/%d" % (arm, cid, rep)
                    if key in skip:
                        continue
                    spec = {"arm": arm, "case_id": cid, "repetition": rep,
                            "set": over.get(key, {})}
                    self.f.append_receipt("recorder-1", self.row(spec))

    def expected(self, step):
        exp = {a: {"bundle_identity": DOC["arm_bundles"][a],
                   "runtime": copy.deepcopy(DOC["runtime"])} for a in ARMS}
        for arm, patch in step.get("expected_override", {}).items():
            if patch is None:
                exp[arm] = None
            elif arm not in exp:
                exp[arm] = patch
            else:
                for k, v in patch.items():
                    _set_path(exp[arm], k, v)
        return exp


def _contains_private(obj):
    text = json.dumps(obj)
    return [s for s in DOC["forbidden_public_substrings"] if s in text]


class ConformanceTest(unittest.TestCase):
    def run_step(self, w, step):
        op = step["op"]
        caller = w.caller(step.get("caller", "$owner"))
        f = w.f
        if op == "draft":
            return f.create_draft(caller, w.header(step))
        if op == "fill_inputs":
            return w.fill_inputs(caller, step.get("skip_private", ()))
        if op == "set_public":
            return f.set_public_input(caller, BID, VER, step["case_id"], step["public_input"])
        if op == "set_private":
            return f.set_private_refs(caller, BID, VER, step["case_id"], step["refs"])
        if op == "publish":
            return f.publish(caller, BID, VER)
        if op == "get_definition":
            return f.get_definition(BID, VER)
        if op == "get_definition_of":
            return f.get_definition(step["benchmark_id"], step["version"])
        if op == "get_private":
            return f.get_private_refs(caller, BID, VER)
        if op == "list_benchmarks":
            return f.list_benchmarks()
        if op == "append":
            return f.append_receipt("recorder-1", w.row(step["row"]))
        if op == "fill_matrix":
            return w.fill_matrix(step)
        if op in ("snapshot", "assert_prefix"):
            return f.scoreboard_snapshot(BID, VER)
        if op == "aggregate":
            return f.aggregate(BID, VER, w.expected(step))
        raise AssertionError("unknown vector op " + op)

    def check(self, w, step, result, ctx):
        if "expect" in step:
            self.assertEqual(result, step["expect"], ctx)
        for path, want in step.get("expect_paths", {}).items():
            got = _get_path(result, path)
            if isinstance(want, float):
                self.assertAlmostEqual(got, want, places=6, msg=ctx + path)
            else:
                self.assertEqual(got, want, ctx + path)
        for key, n in step.get("expect_len", {}).items():
            self.assertEqual(len(result[key]), n, ctx + key)
        if "expect_case_count" in step:
            self.assertEqual(len(result["cases"]), step["expect_case_count"], ctx)
        if step.get("expect_no_private"):
            self.assertEqual(_contains_private(result), [], ctx)
        if "expect_private_cases" in step:
            self.assertEqual(len(result), step["expect_private_cases"], ctx)
        if "expect_row_count" in step:
            self.assertEqual(result["row_count"], step["expect_row_count"], ctx)
        if "expect_seq" in step:
            self.assertEqual(result, step["expect_seq"], ctx)
        if step["op"] == "snapshot" and "save_as" in step:
            w.saved[step["save_as"]] = result["rows"]
        if step["op"] == "assert_prefix":
            old = w.saved[step["saved"]]
            self.assertEqual(result["rows"][:len(old)], old, ctx + " rows were rewritten")

    def test_vectors(self):
        self.assertTrue(DOC["vectors"])
        for vec in DOC["vectors"]:
            with self.subTest(vector=vec["id"]):
                w = World()
                if vec["setup"] == "published":
                    w.published()
                for i, step in enumerate(vec["steps"]):
                    ctx = "%s[%d]%s " % (vec["id"], i, step["op"])
                    if "expect_error" in step:
                        with self.assertRaises(fake.BenchmarkError, msg=ctx) as cm:
                            self.run_step(w, step)
                        self.assertEqual(cm.exception.code, step["expect_error"], ctx)
                    else:
                        self.check(w, step, self.run_step(w, step), ctx)

    def test_fixture_is_fixed_shape(self):
        cases = FIX["header"]["cases"]
        self.assertEqual(sum(c["partition"] == "train" for c in cases.values()), 12)
        self.assertEqual(sum(c["partition"] == "holdout" for c in cases.values()), 8)
        self.assertEqual(len(cases), 20)

    def test_instances_are_isolated(self):
        a, b = World(), World()
        a.published()
        self.assertEqual(b.f.list_benchmarks(), [])

    def test_returned_views_are_copies(self):
        w = World()
        w.published()
        view = w.f.get_definition(BID, VER)
        view["cases"]["train-01"]["public_input"]["prompt"] = "mutated"
        again = w.f.get_definition(BID, VER)
        self.assertNotEqual(again["cases"]["train-01"]["public_input"]["prompt"], "mutated")


if __name__ == "__main__":
    unittest.main()
