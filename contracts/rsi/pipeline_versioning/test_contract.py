"""Shared r2 conformance harness + standalone-fake tests.

``run_conformance(make_world)`` is the ONE entrypoint every provider reuses:
    make_world() -> (api, fixture)
``api`` exposes the versioning API of README.md; ``fixture`` performs host
actions outside it (tools, registration, SDK run lifecycle, seal tampering,
effect snapshots). It returns failure strings (empty == conforms). Every
scenario runs for every provider; there are no optional capabilities.
"""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import contract as c

HERE = Path(__file__).resolve().parent
_FLOATS = {"nan": float("nan"), "inf": float("inf"), "-inf": float("-inf")}


def load_vectors() -> Dict[str, Any]:
    with open(HERE / "conformance_vectors.json", encoding="utf-8") as fh:
        return json.load(fh)


def vectors_sha256() -> str:
    return hashlib.sha256((HERE / "conformance_vectors.json").read_bytes()).hexdigest()


def literal(value: Any) -> Any:
    """Decode the non-JSON markers ($float/$tuple/$bytes) of the vectors."""
    if isinstance(value, dict):
        if set(value) == {"$float"}:
            return _FLOATS[value["$float"]]
        if set(value) == {"$tuple"}:
            return tuple(literal(v) for v in value["$tuple"])
        if set(value) == {"$bytes"}:
            return value["$bytes"].encode("utf-8")
        return {k: literal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [literal(v) for v in value]
    return value


class _Ctx:
    def __init__(self, vectors: Dict[str, Any]):
        self.v = vectors
        self.rec: Dict[str, Any] = {}
        self.pins: Dict[str, Any] = {}

    def res(self, value: Any) -> Any:
        if isinstance(value, str):
            if value.startswith("$id:"):
                return self.v["ids"][value[4:]]
            if value.startswith("$content:"):
                return literal(self.v["contents"][value[9:]])
            if value.startswith("$rec:"):
                return self.rec[value[5:]]
            if value.startswith("$pin:"):
                return self.pins[value[5:]]
            if value == "$zero":
                return self.v["zero_id"]
            return value
        if isinstance(value, list):
            return [self.res(v) for v in value]
        if isinstance(value, dict):
            return {k: self.res(v) for k, v in value.items()}
        return value


def _same(expected: Any, actual: Any) -> bool:
    """Exact equality that also keeps 1, 1.0 and True apart."""
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(
            _same(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(map(_same, expected, actual))
    return expected == actual


def _subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _subset(v, actual[k]) for k, v in expected.items())
    return _same(expected, actual)


_FIXTURE_OPS = {"install_tool", "remove_tool", "register", "start_run", "pause_run",
                "complete_run", "observe_runs", "claim_prompt", "tamper_seal"}


def _call(api: Any, fx: Any, ctx: _Ctx, step: Dict[str, Any]) -> Any:
    op, arg = step["op"], (lambda k: ctx.res(step[k]))
    v = ctx.v
    if op == "capture":
        return api.capture(arg("target"))
    if op == "list":
        out = api.list_versions()
        if isinstance(out, list):
            out.append("junk")            # results are detached
        return api.list_versions()
    if op == "read":
        return api.read(arg("version_id"))
    if op == "mutate_read":
        got = api.read(arg("version_id"))
        got["content"]["graph"]["injected"] = True
        got["content"]["tools"]["injected"] = {}
        for role in got["content"]["roles"].values():
            role["prompt"] = (role["prompt"] or "") + "!"
            role["settings"]["injected"] = 1
            role["tools"].append("injected")
        return None
    if op == "current":
        return api.current(arg("target"))
    if op == "restore":
        return api.restore(arg("target"), arg("version_id"),
                           expected_current=arg("expected_current"))
    if op == "install_tool":
        tool = v["tools"][step["tool"]]
        return fx.install_tool(tool["name"], dict(tool["files"]))
    if op == "remove_tool":
        return fx.remove_tool(step["name"])
    if op == "register":
        key = step["unit"]
        return fx.register(step["target"], literal(v["units"][key]),
                           literal(v["contents"][v["unit_contents"][key]]))
    if op == "start_run":
        ctx.pins[step["run"]] = fx.start_run(step["run"], step["target"])
        return ctx.pins[step["run"]]
    if op in ("pause_run", "complete_run", "claim_prompt"):
        return getattr(fx, op)(step["run"])
    if op == "observe_runs":
        return fx.observe_runs()
    if op == "tamper_seal":
        return fx.tamper_seal(arg("version_id"))
    raise KeyError(f"unknown vector op {op!r}")


def _generic_checks(op: str, out: Any, tag: str) -> List[str]:
    """Naming rule: a seal's physical name carries the logical id slug."""
    if op == "capture" and isinstance(out, dict):
        if not c.physical_matches(out.get("physical_name"), out.get("version_id", "")):
            return [f"{tag}: physical name {out.get('physical_name')!r} does not name its version"]
    if op == "read" and isinstance(out, dict):
        phys = (out.get("native") or {}).get("physical_name")
        if not c.physical_matches(phys, out.get("version_id", "")):
            return [f"{tag}: native physical name {phys!r} does not name its version"]
        if phys and out.get("version_id", "").endswith(phys[-16:]):
            return [f"{tag}: logical id and native identity coincide"]
    return []


def _run_scenario(make_world: Callable, vectors: Dict[str, Any], scenario: Dict[str, Any],
                  transcript: Optional[list]) -> List[str]:
    fails: List[str] = []
    name = scenario["name"]
    api, fx = make_world()
    ctx = _Ctx(vectors)
    try:
        for i, step in enumerate(scenario["steps"]):
            op, expect = step["op"], step.get("expect")
            tag = f"{name}[{i}:{op}]"
            before = fx.snapshot()
            entry = {"step": i, "op": op, "args": {k: v for k, v in step.items()
                                                   if k not in ("op", "expect")}}
            try:
                out = _call(api, fx, ctx, step)
            except Exception as exc:  # refusals compare by stable code
                after = fx.snapshot()
                code = getattr(exc, "code", None)
                entry.update(error=code or type(exc).__name__, message=str(exc)[:600],
                             unchanged=after == before)
                if transcript is not None:
                    if op not in _FIXTURE_OPS:
                        entry.update(before=before, after=after)
                    transcript.append(entry)
                want = (expect or {}).get("error")
                if op in _FIXTURE_OPS:
                    fails.append(f"{tag}: fixture action failed {type(exc).__name__}: {exc}")
                    break
                if want is None:
                    fails.append(f"{tag}: unexpected refusal {code or type(exc).__name__}: {exc}")
                elif code != want:
                    fails.append(f"{tag}: refusal code {code!r} != {want!r} ({exc})")
                elif after != before:
                    fails.append(f"{tag}: refused call changed state")
                continue
            after = fx.snapshot() if step.get("unchanged") else None
            entry.update(result=out, unchanged=None if after is None else after == before)
            if transcript is not None:
                transcript.append(entry)
            for field_name, key in (step.get("record") or {}).items():
                ctx.rec[field_name] = out[key]
            fails.extend(_generic_checks(op, out, tag))
            if after is not None and after != before:
                fails.append(f"{tag}: call that must change nothing changed state")
            if expect is None:
                continue
            if "error" in expect:
                fails.append(f"{tag}: expected refusal {expect['error']} but call succeeded")
            elif "result" in expect and not _same(ctx.res(expect["result"]), out):
                fails.append(f"{tag}: result {out!r} != {ctx.res(expect['result'])!r}")
            elif "result_subset" in expect and not _subset(ctx.res(expect["result_subset"]), out):
                fails.append(f"{tag}: result {out!r} lacks {ctx.res(expect['result_subset'])!r}")
    finally:
        fx.close()
    return fails


def _codec_checks(api: Any, vectors: Dict[str, Any]) -> List[str]:
    fails = []
    for key, want in vectors["canonical"].items():
        got = api.canonical_bytes(literal(vectors["contents"][key]))
        if got != want.encode("utf-8"):
            fails.append(f"canonical[{key}]: {got!r} != {want!r}")
        if vectors["ids"][key] != "sha256:" + hashlib.sha256(want.encode("utf-8")).hexdigest():
            fails.append(f"canonical[{key}]: pinned id is not sha256 of pinned bytes")
    for key, want in vectors["ids"].items():
        got = api.version_id_of(literal(vectors["contents"][key]))
        if got != want:
            fails.append(f"id[{key}]: {got} != {want}")
    for group in vectors["same_id"]:
        if len({api.version_id_of(literal(vectors["contents"][k])) for k in group}) != 1:
            fails.append(f"same_id{group}: ids differ")
    for group in vectors["distinct_ids"]:
        if len({api.version_id_of(literal(vectors["contents"][k])) for k in group}) != len(group):
            fails.append(f"distinct_ids{group}: ids collide")
    for case in vectors["domain_refusals"]:
        for fn in ("canonical_bytes", "version_id_of"):
            try:
                getattr(api, fn)(literal(case["content"]))
            except Exception as exc:
                if getattr(exc, "code", None) != case["error"]:
                    fails.append(f"domain[{case['name']}].{fn}: {getattr(exc, 'code', exc)!r}")
            else:
                fails.append(f"domain[{case['name']}].{fn}: accepted")
    return fails


def run_conformance(make_world: Callable, report: Optional[dict] = None) -> List[str]:
    """Codec vectors, then every scenario on a fresh world. Optional *report*
    receives per-scenario failures and the full step transcript."""
    vectors = load_vectors()
    api, fx = make_world()
    try:
        fails = _codec_checks(api, vectors)
    finally:
        fx.close()
    if report is not None:
        report.update(vectors_sha256=vectors_sha256(), codec_failures=list(fails),
                      scenarios={}, transcripts={})
    for scenario in vectors["scenarios"]:
        transcript = [] if report is not None else None
        got = _run_scenario(make_world, vectors, scenario, transcript)
        if report is not None:
            report["scenarios"][scenario["name"]] = got
            report["transcripts"][scenario["name"]] = transcript
        fails.extend(got)
    return fails


class FakeConformanceTest(unittest.TestCase):
    def setUp(self):
        import fake
        self.fake = fake

    def test_shared_vectors_pass_on_fake(self):
        report: dict = {}
        self.assertEqual(run_conformance(self.fake.make_fake_world, report), [])
        self.assertEqual(len(report["scenarios"]), len(load_vectors()["scenarios"]))

    def test_every_vector_error_code_is_declared(self):
        for code in c.ERROR_CODES:
            cls = getattr(c, "".join(w.title() for w in code.split("_")))
            self.assertEqual(cls.code, code)
        for scenario in load_vectors()["scenarios"]:
            for step in scenario["steps"]:
                err = (step.get("expect") or {}).get("error")
                if err:
                    self.assertIn(err, c.ERROR_CODES)

    def test_api_has_no_run_pin_or_finish_operation(self):
        for cls in (self.fake.FakeVersions,):
            public = {n for n in dir(cls) if not n.startswith("_")}
            self.assertEqual(public, {"canonical_bytes", "version_id_of", "capture",
                                      "list_versions", "read", "current", "restore"})

    def _mutant_fails(self, patch) -> List[str]:
        fake = self.fake

        def make():
            api, fx = fake.make_fake_world()
            patch(api, fx)
            return api, fx
        return run_conformance(make)

    def test_vectors_detect_resealing_unchanged_content(self):
        def patch(api, fx):
            orig = api.capture

            def capture(target):
                out = orig(target)
                if not out["created"]:      # a second physical seal for old content
                    seal = api._host.seals[out["version_id"]]
                    api._host.seals["sha256:" + "f" * 64] = copy.deepcopy(seal)
                return out
            api.capture = capture
        self.assertTrue(any("capture_is_content_addressed" in f for f in self._mutant_fails(patch)))

    def test_vectors_detect_repinning_existing_runs(self):
        def patch(api, fx):
            orig = api.restore

            def restore(target, vid, **kw):
                out = orig(target, vid, **kw)
                for r in api._host.runs.values():
                    if r["target"] == target:
                        r["content"] = copy.deepcopy(api._host.targets[target]["content"])
                return out
            api.restore = restore
        self.assertTrue(any("preserves_existing_runs" in f for f in self._mutant_fails(patch)))

    def test_vectors_detect_aliased_reads(self):
        def patch(api, fx):
            def read(vid):
                seal = api._seal(vid)
                return {"version_id": vid, "content": seal["content"],
                        "native": {"physical_name": seal["physical_name"]}}
            api.read = read
        self.assertTrue(any(":read]" in f for f in self._mutant_fails(patch)))

    def test_vectors_detect_float_coercion(self):
        def patch(api, fx):
            def coerce(x):
                if isinstance(x, float) and x.is_integer():
                    return int(x)
                if isinstance(x, dict):
                    return {k: coerce(v) for k, v in x.items()}
                if isinstance(x, list):
                    return [coerce(v) for v in x]
                return x
            api.version_id_of = lambda content: c.version_id_of(coerce(content))
            api.canonical_bytes = lambda content: c.canonical_bytes(coerce(content))
        self.assertTrue(any("C_float" in f for f in self._mutant_fails(patch)))

    def test_vectors_detect_missing_cas_and_provider_checks(self):
        def patch(api, fx):
            def restore(target, vid, *, expected_current):
                return self.fake.FakeVersions.restore(
                    api, target, vid, expected_current=api.current(target)["unit_identity"])
            api.restore = restore
        fails = self._mutant_fails(patch)
        self.assertTrue(any("CONFLICT" in f for f in fails))

        def patch2(api, fx):
            api._host.tool_identity = lambda name: {"native": False, "files": {}}
        fails = self._mutant_fails(patch2)
        self.assertTrue(any("PROVIDER_UNAVAILABLE" in f for f in fails))


if __name__ == "__main__":
    unittest.main()
