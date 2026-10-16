"""Standalone loud fake of the r2 pipeline-versioning contract.

Pure stdlib, no YAML, filesystem, registry, SDK or model service. It states
the contract in executable form for downstream tests; it does NOT establish
real host behaviour and is not a substitute for the native binding.

Two objects share one in-memory ``FakeHost``:

* ``FakeVersions`` is the versioning API (capture/list/read/current/restore).
  It has no run, pin or finish method.
* ``FakeFixture`` models what the host does OUTSIDE the versioning API: tool
  files, ordinary target registration, and the SDK run lifecycle (a run pins
  the graph version current at its start and keeps that version's prompts).
  The native conformance fixture performs the same actions through the real
  ToolLoader, registry and SDK instead.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, List, Optional

import contract as c

TOOL_FILES = ("tool.yaml", "impl.py", "README.md")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeHost:
    """The modelled host: installed tools, generated targets, runs, seals."""

    def __init__(self):
        self.tools: Dict[str, Dict[str, str]] = {}
        self.targets: Dict[str, Dict[str, Any]] = {}
        self.runs: Dict[str, Dict[str, Any]] = {}
        self.seals: Dict[str, Dict[str, Any]] = {}
        self.writes = 0

    def tool_identity(self, name: str) -> Dict[str, Any]:
        files = self.tools.get(name)
        if not files or "tool.yaml" not in files or "impl.py" not in files:
            raise c.ProviderUnavailable(f"unresolved tool {name!r}: no provider serves it")
        return {"native": False,
                "files": {f: _sha(files[f]) for f in TOOL_FILES if f in files}}

    def resolve(self, content: Dict[str, Any]) -> Dict[str, Any]:
        """Content as captured now: tool identities re-read from installed tools."""
        tools = {n: self.tool_identity(n) for n in sorted(content["tools"])}
        return {**copy.deepcopy(content), "tools": tools}

    def new_unit(self, target: str) -> str:
        self.writes += 1
        return hashlib.sha256(f"{target}\0{self.writes}".encode()).hexdigest()

    def publish(self, target: str, content: Dict[str, Any]) -> None:
        t = self.targets[target]
        t["content"] = copy.deepcopy(content)
        t["unit"] = self.new_unit(target)
        if repr(t["graphs"][-1]) != repr(content):
            t["graphs"].append(copy.deepcopy(content))


class FakeVersions:
    """In-memory provider of the r2 API. Every refusal precedes every change."""

    def __init__(self, host: FakeHost):
        self._host = host

    canonical_bytes = staticmethod(c.canonical_bytes)
    version_id_of = staticmethod(c.version_id_of)

    def _target(self, target: Any) -> Dict[str, Any]:
        c.check_target(target)
        found = self._host.targets.get(target)
        if found is None:
            raise c.UnknownTarget(f"{target!r} is not a registered generated target")
        return found

    def _seal(self, version_id: Any) -> Dict[str, Any]:
        c.check_version_id(version_id)
        seal = self._host.seals.get(version_id)
        if seal is None:
            raise c.UnknownVersion(f"no seal holds logical version {version_id}")
        if seal["tampered"]:
            raise c.TamperedVersion(f"seal {seal['physical_name']} no longer verifies")
        return seal

    def _live_id(self, t: Dict[str, Any]) -> Optional[str]:
        try:
            return c.version_id_of(self._host.resolve(t["content"]))
        except c.VersioningError:
            return None

    def capture(self, target: Any) -> Dict[str, Any]:
        """Order: target form, known target, providers, content domain, catalog."""
        t = self._target(target)
        content = self._host.resolve(t["content"])
        vid = c.version_id_of(content)
        if vid in self._host.seals:
            seal = self._seal(vid)
            return {"target": target, "version_id": vid, "created": False,
                    "physical_name": seal["physical_name"]}
        native = hashlib.sha256(f"{target}\0{t['unit']}\0{vid}".encode()).hexdigest()
        phys = f"{c.SEAL_PREFIX}{c.seal_slug(vid)}_{native[:16]}"
        self._host.seals[vid] = {"content": copy.deepcopy(content), "physical_name": phys,
                                 "bundle_identity": native, "tampered": False}
        return {"target": target, "version_id": vid, "created": True, "physical_name": phys}

    def list_versions(self) -> List[str]:
        for vid in self._host.seals:
            self._seal(vid)
        return sorted(self._host.seals)

    def read(self, version_id: Any) -> Dict[str, Any]:
        seal = self._seal(version_id)
        return {"version_id": version_id, "content": copy.deepcopy(seal["content"]),
                "native": {"physical_name": seal["physical_name"],
                           "bundle_identity": seal["bundle_identity"]}}

    def current(self, target: Any) -> Dict[str, Any]:
        t = self._target(target)
        return {"target": target, "unit_identity": t["unit"],
                "graph_version": len(t["graphs"])}

    def restore(self, target: Any, version_id: Any, *, expected_current: Any) -> Dict[str, Any]:
        """Order: version form, target form, CAS form, known target, known
        version, tamper, CAS, no-op, providers; then one whole-unit change."""
        c.check_version_id(version_id)
        c.check_target(target)
        c.check_unit_identity(expected_current)
        t = self._target(target)
        seal = self._seal(version_id)
        if t["unit"] != expected_current:
            raise c.Conflict(f"target {target!r} is {t['unit'][:12]}, "
                             f"not expected {expected_current[:12]}")
        previous = t["unit"]
        if self._live_id(t) == version_id:
            return {"target": target, "version_id": version_id, "changed": False,
                    "previous_unit_identity": previous, "unit_identity": previous}
        for name, want in seal["content"]["tools"].items():
            if self._host.tool_identity(name) != want:
                raise c.ProviderChanged(f"tool {name!r} changed since capture")
        self._host.publish(target, seal["content"])
        t["recorded"] = True
        return {"target": target, "version_id": version_id, "changed": True,
                "previous_unit_identity": previous, "unit_identity": t["unit"]}


class FakeFixture:
    """Host actions outside the versioning API (tools, registration, runs)."""

    def __init__(self, host: FakeHost):
        self._host = host

    def install_tool(self, name: str, files: Dict[str, str]) -> None:
        self._host.tools[name] = dict(files)

    def remove_tool(self, name: str) -> None:
        self._host.tools.pop(name)

    def register(self, target: str, unit: Dict[str, Any], content: Dict[str, Any]) -> None:
        """Ordinary generated-config registration. ``content`` is the vector's
        statement of what registering ``unit`` resolves to; the native fixture
        registers ``unit`` itself and conformance compares the capture."""
        c.check_target(target)
        t = self._host.targets.get(target)
        if t is not None and t.get("recorded"):
            raise AssertionError(f"{target} is a recorded whole bundle; not edited in place")
        if t is None:
            self._host.targets[target] = {
                "graphs": [copy.deepcopy(content)], "recorded": False,
                "content": copy.deepcopy(content), "unit": self._host.new_unit(target)}
        else:
            self._host.publish(target, content)

    def start_run(self, run: str, target: str) -> int:
        t = self._host.targets[target]
        self._host.runs[run] = {"target": target, "status": "running",
                                "graph_version": len(t["graphs"]),
                                "content": copy.deepcopy(t["content"])}
        return len(t["graphs"])

    def pause_run(self, run: str) -> None:
        self._host.runs[run]["status"] = "paused"

    def complete_run(self, run: str) -> None:
        self._host.runs[run]["status"] = "completed"

    def observe_runs(self) -> Dict[str, Any]:
        out = {}
        for key, r in self._host.runs.items():
            roles = r["content"]["roles"]
            out[key] = {"status": r["status"], "graph_version": r["graph_version"],
                        "prompts": {n: roles[n]["prompt"]
                                    for n in sorted(set(c.agent_refs(r["content"]["graph"])))}}
        return out

    def claim_prompt(self, run: str) -> Dict[str, Any]:
        r = self._host.runs[run]
        if r["status"] not in ("running", "paused"):
            raise AssertionError(f"run {run} is {r['status']}")
        r["status"] = "running"
        graph = r["content"]["graph"]
        step = next(s for s in graph["steps"] if s["id"] == graph["begin"])
        return {"step": step["id"],
                "prompt": r["content"]["roles"][step["agent_config"]]["prompt"]}

    def tamper_seal(self, version_id: str) -> None:
        self._host.seals[version_id]["tampered"] = True

    def _state(self) -> Dict[str, Any]:
        return {
            "tools": self._host.tools,
            "targets": {k: {"unit": t["unit"], "graphs": len(t["graphs"]),
                            "content": repr(t["content"])}
                        for k, t in self._host.targets.items()},
            "runs": {k: [r["status"], r["graph_version"], repr(r["content"])]
                     for k, r in self._host.runs.items()},
            "seals": {k: [s["physical_name"], s["tampered"], repr(s["content"])]
                      for k, s in self._host.seals.items()}}

    def snapshot(self) -> str:
        return json.dumps(self._state(), sort_keys=True)

    def snapshot_outside(self, target: str, since: Optional[str] = None) -> str:
        state = self._state()
        state["targets"].pop(target, None)
        return json.dumps(state, sort_keys=True)

    def close(self) -> None:
        pass


def make_fake_world():
    host = FakeHost()
    return FakeVersions(host), FakeFixture(host)
