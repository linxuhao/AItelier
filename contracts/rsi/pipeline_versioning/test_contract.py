"""Conformance harness + tests for the pipeline-versioning contract.

`run_conformance(factory)` is the entrypoint a real provider re-uses:
    factory(available_tools: dict) -> provider object exposing the API in README.md
It returns a list of failure strings (empty == conforms). Refusals must be
exceptions carrying a stable string attribute `code` (see README.md).
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List

HERE = Path(__file__).resolve().parent


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_vectors() -> Dict[str, Any]:
    with open(HERE / "conformance_vectors.json", encoding="utf-8") as fh:
        return json.load(fh)


def _resolve(value: Any, ids: Dict[str, str], bundles: Dict[str, Any], zero: str) -> Any:
    if isinstance(value, str):
        if value.startswith("$id:"):
            return ids[value[4:]]
        if value.startswith("$bundle:"):
            return copy.deepcopy(bundles[value[8:]])
        if value == "$zero":
            return zero
        if value == "$null":
            return None
        return value
    if isinstance(value, list):
        return [_resolve(v, ids, bundles, zero) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, ids, bundles, zero) for k, v in value.items()}
    return value


def _subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _subset(v, actual[k]) for k, v in expected.items())
    return expected == actual


def _snapshot(p: Any) -> Any:
    return (p.list_versions(), p.current_version(), p.list_pins())


def _run_scenario(factory: Callable, vectors: Dict[str, Any], scenario: Dict[str, Any]) -> List[str]:
    fails: List[str] = []
    name = scenario["name"]
    bundles = vectors["bundles"]
    p = factory(dict(vectors["default_tools"]))
    ids = {}
    for key, bundle in bundles.items():
        try:
            ids[key] = p.version_id_of(copy.deepcopy(bundle))
        except Exception:
            pass  # M_* bundles are deliberately malformed
    for i, step in enumerate(scenario["steps"]):
        tag = f"{name}[{i}:{step['op']}]"
        res = lambda v: _resolve(v, ids, bundles, vectors["zero_id"])  # noqa: E731
        op, expect = step["op"], step.get("expect")
        before = _snapshot(p)
        try:
            if op == "capture":
                kwargs = {}
                if "expected_current" in step:
                    kwargs["expected_current"] = res(step["expected_current"])
                out = p.capture(copy.deepcopy(bundles[step["bundle"]]), **kwargs)
            elif op == "capture_then_mutate_input":
                mine = copy.deepcopy(bundles[step["bundle"]])
                out = p.capture(mine)
                mine["graph"]["injected"] = True
                for role in mine["roles"].values():
                    role["prompt"] += "!"
                    role["settings"]["injected"] = 1
            elif op == "mutate_read":
                got = p.read(res(step["version_id"]))
                got["bundle"]["graph"]["injected"] = True
                for role in got["bundle"]["roles"].values():
                    role["prompt"] += "!"
                out = None
            elif op == "list":
                out = p.list_versions()
                out.append({"junk": 1})
                out = p.list_versions()
            elif op == "read":
                out = p.read(res(step["version_id"]))
            elif op == "current":
                out = p.current_version()
            elif op == "restore":
                kwargs = {}
                if "expected_current" in step:
                    kwargs["expected_current"] = res(step["expected_current"])
                out = p.restore(res(step["version_id"]), **kwargs)
            elif op == "set_tools":
                p.set_available_tools(step["tools"])
                out = None
            elif op == "tamper":
                p._tamper_for_test(res(step["version_id"]), copy.deepcopy(bundles[step["bundle"]]))
                out = None
            elif op in ("pin_run", "finish_run", "run_pin"):
                out = getattr(p, op)(step["run_id"])
            elif op == "list_pins":
                out = p.list_pins()
            else:
                fails.append(f"{tag}: unknown vector op")
                continue
        except Exception as exc:  # refusals are compared by stable code
            code = getattr(exc, "code", None)
            want = (expect or {}).get("error")
            if want is None:
                fails.append(f"{tag}: unexpected refusal {code or type(exc).__name__}: {exc}")
            elif code != want:
                fails.append(f"{tag}: refusal code {code!r} != {want!r} ({exc})")
            elif _snapshot(p) != before:
                fails.append(f"{tag}: refused call changed state")
            continue
        if expect is None:
            continue
        if "error" in expect:
            fails.append(f"{tag}: expected refusal {expect['error']} but call succeeded")
        elif "result" in expect and out != res(expect["result"]):
            fails.append(f"{tag}: result {out!r} != {res(expect['result'])!r}")
        elif "result_subset" in expect and not _subset(res(expect["result_subset"]), out):
            fails.append(f"{tag}: result {out!r} lacks {res(expect['result_subset'])!r}")
    return fails


def run_conformance(factory: Callable) -> List[str]:
    """Shared entrypoint: canonical-byte vectors, then every scenario."""
    vectors = load_vectors()
    fails: List[str] = []
    probe = factory(dict(vectors["default_tools"]))
    for vec in vectors["canonical_vectors"]:
        got = probe.canonical_form(copy.deepcopy(vectors["bundles"][vec["bundle"]]))
        want = vec["canonical"].encode("utf-8")
        if got != want:
            fails.append(f"canonical[{vec['bundle']}]: {got!r} != {want!r}")
        expected_id = "sha256:" + hashlib.sha256(want).hexdigest()
        actual_id = probe.version_id_of(copy.deepcopy(vectors["bundles"][vec["bundle"]]))
        if actual_id != expected_id:
            fails.append(f"id[{vec['bundle']}]: {actual_id} != {expected_id}")
    for key in ("A", "A_reordered"):
        if key == "A_reordered":
            same = probe.version_id_of(vectors["bundles"]["A"]) == probe.version_id_of(vectors["bundles"][key])
            if not same:
                fails.append("A and A_reordered must share one id")
    distinct = {probe.version_id_of(vectors["bundles"][k]) for k in
                ("A", "A_prompt", "A_setting", "A_graph", "A_template", "A_tool_identity")}
    if len(distinct) != 6:
        fails.append("prompt/setting/graph/template/tool-identity changes must each change the id")
    caps = set(getattr(probe, "capabilities", ()))
    for scenario in vectors["scenarios"]:
        if set(scenario.get("requires", [])) - caps:
            continue  # optional capability absent: scenario skipped, not passed
        fails.extend(_run_scenario(factory, vectors, scenario))
    return fails


class FakeConformanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = _load_module("rsi_pipeline_versioning_fake", "fake.py")

    def test_shared_vectors_pass_on_fake(self):
        failures = run_conformance(lambda tools: self.fake.FakePipelineVersionStore(tools))
        self.assertEqual(failures, [])

    def test_every_vector_error_code_is_declared(self):
        text = json.dumps(load_vectors())
        for code in self.fake.ERROR_CODES:
            self.assertEqual(getattr(self.fake, "".join(
                w.title() for w in code.split("_"))).code, code)
        for scenario in load_vectors()["scenarios"]:
            for step in scenario["steps"]:
                err = (step.get("expect") or {}).get("error")
                if err:
                    self.assertIn(err, self.fake.ERROR_CODES)
        self.assertTrue(text)

    def test_fake_refuses_unknown_input_loudly(self):
        store = self.fake.FakePipelineVersionStore({})
        with self.assertRaises(self.fake.PipelineVersionError):
            store.read("sha256:" + "1" * 64)
        with self.assertRaises(self.fake.PipelineVersionError):
            store.capture({"graph": {}})
        with self.assertRaises(self.fake.PipelineVersionError):
            store.restore("nope")

    def test_a_broken_provider_is_detected(self):
        """Guards the harness: a store that overwrites history must fail vectors."""
        fake = self.fake

        class Leaky(fake.FakePipelineVersionStore):
            def read(self, version_id):
                entry = self._load(version_id)
                return {"version_id": version_id, "bundle": entry["bundle"]}  # no copy

        failures = run_conformance(lambda tools: Leaky(tools))
        self.assertTrue(any("read_and_inputs_cannot_mutate_history" in f for f in failures))


    def test_vectors_detect_untyped_run_lookup(self):
        fake = self.fake

        class UntypedRun(fake.FakePipelineVersionStore):
            def run_pin(self, run_id):
                if run_id not in self._pins:
                    raise fake.UnknownRun("unknown run")
                return self._pins[run_id]["version_id"]

        failures = run_conformance(lambda tools: UntypedRun(tools))
        self.assertTrue(any("malformed_run_ids" in f for f in failures))

    def test_vectors_detect_silent_version_id_normalization(self):
        fake = self.fake

        class NormalizeId(fake.FakePipelineVersionStore):
            def read(self, version_id):
                if isinstance(version_id, str):
                    version_id = version_id.rstrip("\n")
                return super().read(version_id)

        failures = run_conformance(lambda tools: NormalizeId(tools))
        self.assertTrue(any("whole_string_version_ids" in f for f in failures))


if __name__ == "__main__":
    unittest.main()
