"""Conservative, static scope plans. A plan is never publication authority.

The supported graph is literal res:// references plus GDScript class_name
references, at BOTH diff endpoints. Unsupported loads, missing references,
unmapped changes and incomplete clock measurements select the entire contract.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from aitelier.gate_coverage import validate_coverage, publication_scope_refusal, spec_digest

_TEXT = {".gd", ".tscn", ".tres", ".godot"}
_RESOURCE = re.compile(r"res://[^\s\"'\)\],;]+")
_TOKEN = re.compile(r"\b[A-Za-z_]\w*\b")
_CLASS = re.compile(r"^\s*class_name\s+(\w+)", re.M)
_DECL = re.compile(r"^\s*(?:static\s+)?(?:func|var|const|signal|class_name)\s+(\w+)", re.M)
_LOAD = re.compile(r"\b(?:load|preload|load_threaded_request|set_script|change_scene_to_file)\s*\(")
_CELLS = ("fixed_delta_load0", "fixed_delta_loaded",
          "real_time_cap_load0", "real_time_cap_loaded")


def _git(repo, *args) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True).stdout


def _snapshot(repo, revision):
    files, texts = set(), {}
    for entry in _git(repo, "ls-tree", "-rz", revision).split(b"\0"):
        if not entry:
            continue
        meta, raw = entry.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        path = raw.decode("utf-8")
        if kind != "blob" or mode == "120000":
            raise ValueError(f"unsupported Git entry: {path}")
        files.add(path)
        if Path(path).suffix in _TEXT:
            texts[path] = _git(repo, "cat-file", "blob", oid).decode("utf-8")
    return files, texts


def _graph(files, texts):
    classes, uncertain = {}, {}
    def unknown(path, reason):
        uncertain.setdefault(path, []).append(reason)
    # A nested project and an ignored directory do not contribute global
    # classes to the selected parent project. Keep the complete Git snapshot
    # for change accounting, and keep excluded targets explicitly opaque.
    # The selected root stays included. Snapshot entries are verified blobs,
    # so a directory named like a marker cannot hide its parent's scripts.
    boundaries = {Path(path).parent for path in files
                  if Path(path).name in {"project.godot", ".gdignore"}
                  and Path(path).parent != Path(".")}
    excluded = {path for path in files
                if boundaries.intersection(Path(path).parents)}
    excluded_classes = {name for path in excluded
                        for name in _CLASS.findall(texts.get(path, ""))}
    for path in sorted(excluded):
        unknown(path, "resource is outside the parent project namespace")
    texts = {path: text for path, text in texts.items() if path not in excluded}
    for path, text in texts.items():
        if re.search(r"\b(?:FileAccess|DirAccess)\b", text):
            unknown(path, "file/directory API dependencies are not resolved")
        if "uid://" in text:
            unknown(path, "UID resource reference is not resolved")
        if re.search(r"^\s*extends\s+[\"'](?!res://)", text, re.M):
            unknown(path, "relative script inheritance is not resolved")
        for name in _CLASS.findall(text):
            if name in classes and classes[name] != path:
                raise ValueError(f"ambiguous class_name: {name}")
            classes[name] = path
    edges = {}
    for path, text in texts.items():
        refs = {r[6:] for r in _RESOURCE.findall(text)}
        missing = refs - files
        if missing:
            unknown(path, f"unresolved resource: {sorted(missing)}")
        # Literal loads are understood; variables, formatting and composition
        # are not. Reject these globally because they can hide a reverse edge.
        for call in _LOAD.finditer(text):
            argument = text[call.end():].lstrip()
            literal = re.match(r"[\"']res://[^\"']+[\"']\s*\)", argument)
            if not literal:
                unknown(path, "dynamic or unsupported resource call")
        refs.update(classes[n] for n in _TOKEN.findall(text) if n in classes)
        outside = set(_TOKEN.findall(text)) & (excluded_classes - classes.keys())
        if outside:
            unknown(path, f"class names outside the parent project namespace: {sorted(outside)}")
        edges[path] = refs
    autoload = set()
    section = ""
    for line in texts.get("project.godot", "").splitlines():
        if line.strip().startswith("["):
            section = line.strip()
        elif section == "[autoload]":
            autoload.update(r[6:] for r in _RESOURCE.findall(line))
    return edges, autoload, uncertain


def _closure(edges, roots):
    visited, pending = set(), list(roots)
    while pending:
        node = pending.pop()
        if node not in visited:
            visited.add(node)
            pending.extend(edges.get(node, ()))
    return visited


def _measured_context(measurement, head, spec, names):
    """Read the existing detector's native rows, rejecting untouched negatives.

    The envelope binds the native result to the exact current tree and contract.
    A previous revision's seven-scenario probe cannot clear a current full suite.
    Native raw assertion rows, rather than the deliverer's flagged list, decide
    which scenarios are always included.
    """
    # Detector and normalizer live in the source checkout/sidecar, not the
    # installed AItelier package. Coverage transport must not import them.
    from docker.godot.clock_sensitivity import compare, flagged_scenarios
    from docker.godot.godot_harness import _normalize_asserts
    if not isinstance(measurement, dict):
        raise ValueError("full current clock-sensitivity measurement is missing")
    if measurement.get("head_sha") != head or measurement.get("spec_sha256") != spec_digest(spec):
        raise ValueError("clock-sensitivity measurement has different tree/contract identity")
    doc = measurement.get("detector_result")
    if not isinstance(doc, dict):
        raise ValueError("native clock-sensitivity result is missing")
    rows = {}
    expected = set()
    for scenario in spec["scenarios"]:
        for entry in scenario.get("timeline", []):
            for assertion in _normalize_asserts(entry.get("assert")):
                node = assertion.get("node", "")
                name = assertion.get("name", assertion.get("expr", ""))
                expr = assertion.get("expr", "")
                if assertion.get("mode"):
                    expr = assertion.get("attr", "") + " " + assertion["mode"] + " since frame 0"
                expected.add((scenario["name"], name, node, expr, entry.get("at")))
    if {k[0] for k in expected} != names:
        raise ValueError("every current scenario must declare a readable assertion")
    for cell in _CELLS:
        rows[cell] = {}
        native = (doc.get("assertion_rows") or {}).get(cell)
        if not isinstance(native, list):
            raise ValueError(f"native assertion rows missing: {cell}")
        for row in native:
            if (not isinstance(row, list) or len(row) != 7
                    or not isinstance(row[5], bool) or row[0] not in names):
                raise ValueError(f"malformed native assertion row: {cell}")
            key = tuple(row[:5])
            if key in rows[cell]:
                raise ValueError(f"duplicate native assertion identity: {cell}")
            rows[cell][key] = tuple(row[5:])
        if {k[0] for k in rows[cell]} != names:
            raise ValueError(f"measurement does not cover every current scenario: {cell}")
        if not expected.issubset(rows[cell]):
            raise ValueError(f"measurement omits current assertion identities: {cell}")
    clock = compare(rows[_CELLS[0]], rows[_CELLS[2]])
    load = compare(rows[_CELLS[2]], rows[_CELLS[3]])
    immunity = compare(rows[_CELLS[0]], rows[_CELLS[1]])
    flagged = set(flagged_scenarios(clock + load + immunity))
    bite = doc.get("load_bite")
    if not isinstance(bite, dict) or set(bite) != names:
        raise ValueError("load observations do not cover every current scenario")
    for name in names - flagged:
        reading = bite[name]
        if not isinstance(reading, dict) or reading.get("bit") is not True:
            raise ValueError(f"injected load did not reach unflagged scenario: {name}")
        if (not isinstance(reading.get("delta_sec"), (int, float))
                or abs(reading["delta_sec"]) <= 0.05
                or not reading.get("frames_load0")
                or reading.get("frames_load0") != reading.get("frames_loaded")):
            raise ValueError(f"unusable load observation: {name}")
    historical = measurement.get("batch_context_dependent_scenarios")
    if not isinstance(historical, list) or any(n not in names for n in historical):
        raise ValueError("batch context dependent scenario inventory missing or ambiguous")
    return flagged | set(historical)


def select_scope(repo: Path, base: str, head: str, spec: dict,
                 measurement: dict | None = None) -> dict:
    """A git diff -> explicit partition, with every failure recorded as FULL.

    Changes are not narrowed to symbol names: all reverse file references count.
    Symbols are recorded from both revisions for inspection, never used to
    excuse an edge. No directory or filename prefix selects a scenario.
    """
    started = time.monotonic()
    scenarios = spec.get("scenarios") if isinstance(spec, dict) else None
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("no complete authored scenario inventory; cannot produce even a full plan")
    names = [s.get("name") for s in scenarios if isinstance(s, dict)]
    if (len(names) != len(scenarios) or any(not isinstance(n, str) or not n for n in names)
            or len(set(names)) != len(names)):
        raise ValueError("authored scenario inventory has missing/duplicate identities")
    ordered_names, names = names, set(names)
    result = {"coverage": "full", "all_scenarios": ordered_names,
              "selected_scenarios": ordered_names.copy(), "unselected_scenarios": [],
              "fallback_full": False, "selection_basis": {"base_sha": base,
                  "head_sha": head, "spec_sha256": spec_digest(spec)},
              "always_included_scenarios": []}
    basis = result["selection_basis"]
    try:
        base = _git(repo, "rev-parse", "--verify", base + "^{commit}").decode().strip()
        head = _git(repo, "rev-parse", "--verify", head + "^{commit}").decode().strip()
        basis.update(base_sha=base, head_sha=head)
        try:
            always = _measured_context(measurement, head, spec, names)
            # Existing gate replays copy the entire scenario except its name.
            # Include identical opted-in repetitions without guessing a suffix.
            replay_shapes = {spec_digest({k: v for k, v in s.items() if k != "name"})
                             for s in scenarios if s["name"] in always
                             and s.get("repeatability") is True}
            always.update(s["name"] for s in scenarios if s.get("repeatability") is True
                          and spec_digest({k: v for k, v in s.items() if k != "name"}) in replay_shapes)
            result["always_included_scenarios"] = sorted(always)
        except (ValueError, TypeError, KeyError) as exc:
            always = set()
            basis["measurement_error"] = f"{type(exc).__name__}: {exc}"
        changes = _git(repo, "diff", "--name-only", "--no-renames", "-z", base, head)
        changed = {p.decode("utf-8") for p in changes.split(b"\0") if p}
        basis["changed_files"] = sorted(changed)
        snapshots = [_snapshot(repo, rev) for rev in (base, head)]
        edges, roots, symbols, uncertain = {}, set(), {}, {}
        for _, texts in snapshots:
            for path in changed:
                symbols.setdefault(path, set()).update(_DECL.findall(texts.get(path, "")))
        basis["changed_symbols"] = {p: sorted(v) for p, v in symbols.items()}
        for files, texts in snapshots:
            graph, autoload, unknown = _graph(files, texts)
            for path, reasons in unknown.items():
                uncertain.setdefault(path, set()).update(reasons)
            roots.update(autoload)
            for path, refs in graph.items():
                edges.setdefault(path, set()).update(refs)
        impacted, dependencies = set(), {}
        for scenario in scenarios:
            scene = scenario.get("scene", spec.get("scene"))
            if not isinstance(scene, str) or not scene.startswith("res://"):
                raise ValueError(f"scenario has no literal scene root: {scenario['name']}")
            scene = scene[6:]
            if scene not in snapshots[1][0]:
                raise ValueError(f"scenario root is missing: {scene}")
            deps = _closure(edges, roots | {scene})
            opaque = sorted(p for p in deps if Path(p).suffix in
                            {".cs", ".dll", ".gdextension", ".scn", ".res", ".glb", ".gltf"})
            if opaque:
                raise ValueError(f"opaque resource dependencies are not resolved: {opaque}")
            ambiguous = {p: sorted(uncertain[p]) for p in deps if p in uncertain}
            if ambiguous:
                basis["ambiguous_resources"] = ambiguous
                raise ValueError(f"resource graph is incomplete for scenario: {scenario['name']}")
            dependencies[scenario["name"]] = sorted(deps & changed)
            if deps & changed:
                impacted.add(scenario["name"])
        # A test/gate/config/spec change or an unreferenced resource may alter
        # harness semantics or introduce an indirect link. It cannot mean empty.
        accounted = set().union(*(set(v) for v in dependencies.values()))
        if changed - accounted:
            raise ValueError(f"diff contains changes outside the proven resource graph: {sorted(changed - accounted)}")
        basis["resource_intersections"] = dependencies
        basis["graph_selected_scenarios"] = [n for n in ordered_names if n in impacted]
        if basis.get("measurement_error"):
            raise ValueError(basis["measurement_error"])
        selected = impacted | always
        if not selected:
            raise ValueError("selection would be empty; no gate may pass on zero scenarios")
        result.update(coverage="full" if selected == names else "subset",
                      selected_scenarios=[n for n in ordered_names if n in selected],
                      unselected_scenarios=[n for n in ordered_names if n not in selected])
    except Exception as exc:
        # Selector/parser/import failures are a FULL plan, never a silent empty
        # list. KeyboardInterrupt and SystemExit still propagate.
        result["fallback_full"] = True
        basis["fallback_reason"] = f"{type(exc).__name__}: {exc}"
    result["selection_wall_sec"] = time.monotonic() - started
    return result
