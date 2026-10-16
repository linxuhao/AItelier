"""Standalone in-memory fake of the RSI pipeline-benchmark data interface.

SOURCE CORRECTION CANDIDATE. README.md is normative. Pure stdlib, no I/O, no
providers, no import of any production scorer. It validates data shape only; it
does not authenticate callers, prove privacy/truth/accounting, run any model,
evaluator or checker, or grant eligibility, adoption or spend.
"""
import copy
import hashlib
import json
import math
import re

SCHEMA = "rsi.benchmark.v1"
TRAIN_COUNT = 12
HOLDOUT_COUNT = 8
REPETITIONS = 3
FAMILIES = ("family-a", "family-b", "family-c", "family-d")
NORMALIZATION = {"min": 0, "max": 100}
VALIDITIES = ("VALID", "INVALID", "INFRASTRUCTURE", "UNKNOWN_ACCOUNTING")
OUTCOMES = ("scored", "task_failure", "none")
HEADER_FIELDS = ("schema", "benchmark_id", "version", "arms", "families",
                 "cases", "point_total", "normalization")
ROW_FIELDS = ("schema", "benchmark_id", "version", "bundle_identity", "runtime",
              "case_id", "repetition", "arm", "run_receipt_id", "input_identity",
              "checker_identity", "validity", "outcome", "raw_points",
              "cost_usd", "duration_s")
RUNTIME_FIELDS = ("route", "endpoints", "effort")
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SHA = re.compile(r"sha256:[0-9a-f]{64}")


class BenchmarkError(Exception):
    """Loud refusal. `code` is the stable, machine-checked part."""

    def __init__(self, code, message=""):
        super().__init__("%s: %s" % (code, message))
        self.code = code


def _refuse(code, message=""):
    raise BenchmarkError(code, message)


def _exact(obj, fields, what):
    if not isinstance(obj, dict):
        _refuse("invalid_value", what + " must be an object")
    if not all(isinstance(key, str) for key in obj):
        _refuse("invalid_value", what + " must have string keys")
    extra = sorted(set(obj) - set(fields))
    if extra:
        _refuse("unknown_field", "%s has unknown fields %s" % (what, extra))
    missing = [f for f in fields if f not in obj]
    if missing:
        _refuse("invalid_value", "%s lacks fields %s" % (what, missing))


def _id(value, what):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _refuse("invalid_value", what + " must match " + _ID.pattern)
    return value


def _text(value, what):
    if not isinstance(value, str) or not value.strip():
        _refuse("invalid_value", what + " must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _refuse("invalid_value", what + " must be UTF-8 text")
    return value


def _int(value, what, low):
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        _refuse("invalid_value", "%s must be an integer >= %d" % (what, low))
    _num(value, what)
    return value


def _num(value, what):
    """Finite non-negative real; bool/NaN/Inf/str refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse("invalid_value", what + " must be a number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < 0:
        _refuse("invalid_value", what + " must be finite and >= 0")
    return value


def _sha(value, what):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        _refuse("invalid_value", what + " must be sha256:<64 hex>")
    return value


def _digest(obj):
    try:
        blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
    except (ValueError, OverflowError, UnicodeEncodeError):
        _refuse("invalid_value", "data cannot be encoded as canonical UTF-8 JSON")


class BenchmarkFake:
    """One instance is one isolated in-memory world; nothing is shared."""

    def __init__(self):
        self._defs = {}
        self._rows = []
        self._run_ids = set()
        self._cells = set()

    # ---- definition lifecycle ----
    def _get(self, benchmark_id, version):
        _id(benchmark_id, "benchmark_id")
        _int(version, "version", 1)
        found = self._defs.get((benchmark_id, version))
        if found is None:
            _refuse("unknown_benchmark", "%s v%s" % (benchmark_id, version))
        return found

    def _own_draft(self, caller, benchmark_id, version):
        _text(caller, "caller")
        d = self._get(benchmark_id, version)
        if d["status"] != "draft":
            _refuse("immutable", "published definitions never change")
        if d["owner"] != caller:
            _refuse("ownership", "caller is not the draft creator (not authentication)")
        return d

    def create_draft(self, caller, header):
        _text(caller, "caller")
        _exact(header, HEADER_FIELDS, "header")
        if header["schema"] != SCHEMA:
            _refuse("unknown_schema", str(header["schema"]))
        bid = _id(header["benchmark_id"], "benchmark_id")
        ver = _int(header["version"], "version", 1)
        if (bid, ver) in self._defs:
            _refuse("duplicate", "benchmark id/version already exists")
        arms = header["arms"]
        if not isinstance(arms, list) or not 1 <= len(arms) <= 8:
            _refuse("invalid_value", "arms must be a list of 1..8 ids")
        for a in arms:
            _id(a, "arm")
        if len(set(arms)) != len(arms):
            _refuse("invalid_value", "arms must be unique")
        if header["families"] != list(FAMILIES):
            _refuse("invalid_value", "families must equal the fixed inventory")
        cases = header["cases"]
        if not isinstance(cases, dict):
            _refuse("invalid_value", "cases must be an object")
        counts = {"train": 0, "holdout": 0}
        for cid, c in cases.items():
            _id(cid, "case_id")
            _exact(c, ("partition", "family"), "case " + cid)
            if not isinstance(c["partition"], str) or c["partition"] not in counts:
                _refuse("invalid_value", "partition must be train or holdout")
            if c["family"] not in FAMILIES:
                _refuse("invalid_value", "family outside fixed inventory")
            counts[c["partition"]] += 1
        if counts != {"train": TRAIN_COUNT, "holdout": HOLDOUT_COUNT}:
            _refuse("invalid_value", "need exactly 12 train and 8 holdout cases")
        _int(header["point_total"], "point_total", 1)
        norm = header["normalization"]
        _exact(norm, ("min", "max"), "normalization")
        if (any(type(norm[k]) is not int for k in ("min", "max"))
                or norm != NORMALIZATION):
            _refuse("invalid_value", "normalization must be {min:0,max:100}")
        self._defs[(bid, ver)] = {
            "owner": caller, "status": "draft", "header": copy.deepcopy(header),
            "public": {}, "private": {}, "identities": None, "digest": None}
        return self.get_definition(bid, ver)

    def set_public_input(self, caller, benchmark_id, version, case_id, public_input):
        d = self._own_draft(caller, benchmark_id, version)
        _id(case_id, "case_id")
        if case_id not in d["header"]["cases"]:
            _refuse("unknown_case", str(case_id))
        _exact(public_input, ("prompt",), "public_input")
        _text(public_input["prompt"], "prompt")
        d["public"][case_id] = copy.deepcopy(public_input)

    def set_private_refs(self, caller, benchmark_id, version, case_id, refs):
        d = self._own_draft(caller, benchmark_id, version)
        _id(case_id, "case_id")
        if case_id not in d["header"]["cases"]:
            _refuse("unknown_case", str(case_id))
        _exact(refs, ("rubric_ref", "checker_ref", "max_points"), "refs")
        _text(refs["rubric_ref"], "rubric_ref")
        _text(refs["checker_ref"], "checker_ref")
        _int(refs["max_points"], "max_points", 1)
        d["private"][case_id] = copy.deepcopy(refs)

    def publish(self, caller, benchmark_id, version):
        d = self._own_draft(caller, benchmark_id, version)
        cases = d["header"]["cases"]
        for cid in cases:
            if cid not in d["public"] or cid not in d["private"]:
                _refuse("incomplete", "case %s lacks public input or private refs" % cid)
        for part in ("train", "holdout"):
            fams = {c["family"] for c in cases.values() if c["partition"] == part}
            if fams != set(FAMILIES):
                _refuse("incomplete", "every family must appear in " + part)
        total = sum(r["max_points"] for r in d["private"].values())
        if total != d["header"]["point_total"]:
            _refuse("incomplete", "per-case max_points must sum to point_total")
        identities = {cid: {"input_identity": _digest(d["public"][cid]),
                                 "checker_identity": _digest(d["private"][cid])}
                           for cid in cases}
        digest = _digest({"header": d["header"], "identities": identities})
        d["identities"], d["digest"] = identities, digest
        d["status"] = "published"
        return self.get_definition(benchmark_id, version)

    def get_definition(self, benchmark_id, version):
        """Public view only: never contains private rubric/checker material."""
        d = self._get(benchmark_id, version)
        h = d["header"]
        cases = {}
        for cid, c in h["cases"].items():
            ident = (d["identities"] or {}).get(cid, {})
            cases[cid] = {"partition": c["partition"], "family": c["family"],
                          "public_input": copy.deepcopy(d["public"].get(cid)),
                          "input_identity": ident.get("input_identity"),
                          "checker_identity": ident.get("checker_identity")}
        return {"schema": SCHEMA, "benchmark_id": benchmark_id, "version": version,
                "status": d["status"], "arms": list(h["arms"]),
                "families": list(h["families"]), "repetitions": REPETITIONS,
                "point_total": h["point_total"],
                "normalization": dict(h["normalization"]),
                "partition_counts": {"train": TRAIN_COUNT, "holdout": HOLDOUT_COUNT},
                "cases": cases, "definition_digest": d["digest"]}

    def get_private_refs(self, caller, benchmark_id, version):
        _text(caller, "caller")
        d = self._get(benchmark_id, version)
        if d["owner"] != caller:
            _refuse("ownership", "private refs are creator-only (not authentication)")
        return copy.deepcopy(d["private"])

    def list_benchmarks(self):
        return [{"benchmark_id": b, "version": v, "status": d["status"]}
                for (b, v), d in sorted(self._defs.items())]

    # ---- append-only receipts ----
    def _check_runtime(self, runtime):
        _exact(runtime, RUNTIME_FIELDS, "runtime")
        _text(runtime["route"], "route")
        _text(runtime["effort"], "effort")
        eps = runtime["endpoints"]
        if not isinstance(eps, list) or not eps:
            _refuse("invalid_value", "endpoints must be a non-empty unique list")
        for e in eps:
            _text(e, "endpoint")
        if len(set(eps)) != len(eps):
            _refuse("invalid_value", "endpoints must be a non-empty unique list")

    def append_receipt(self, caller, row):
        """Append one row. Never replaces, edits or deletes. Records the row
        even when it is invalid/infrastructure/unknown-accounting."""
        _text(caller, "caller")
        _exact(row, ROW_FIELDS, "row")
        if row["schema"] != SCHEMA:
            _refuse("unknown_schema", str(row["schema"]))
        d = self._get(row["benchmark_id"], row["version"])
        if d["status"] != "published":
            _refuse("not_published", "receipts need a published benchmark")
        _sha(row["bundle_identity"], "bundle_identity")
        self._check_runtime(row["runtime"])
        cid = _id(row["case_id"], "case_id")
        if cid not in d["header"]["cases"]:
            _refuse("unknown_case", str(cid))
        rep = row["repetition"]
        if isinstance(rep, bool) or not isinstance(rep, int):
            _refuse("invalid_value", "repetition must be an integer")
        if not 1 <= rep <= REPETITIONS:
            _refuse("unknown_repetition", str(rep))
        _id(row["arm"], "arm")
        if row["arm"] not in d["header"]["arms"]:
            _refuse("unknown_arm", str(row["arm"]))
        _id(row["run_receipt_id"], "run_receipt_id")
        _sha(row["input_identity"], "input_identity")
        _sha(row["checker_identity"], "checker_identity")
        ident = d["identities"][cid]
        if (row["input_identity"] != ident["input_identity"]
                or row["checker_identity"] != ident["checker_identity"]):
            _refuse("identity_mismatch", "input/checker identity differs from published")
        if row["validity"] not in VALIDITIES:
            _refuse("invalid_value", "unknown validity")
        if row["outcome"] not in OUTCOMES:
            _refuse("invalid_value", "unknown outcome")
        for f in ("cost_usd", "duration_s"):
            if row[f] is not None:
                _num(row[f], f)
        if row["validity"] == "VALID":
            if row["outcome"] == "none":
                _refuse("invalid_value", "VALID row needs scored or task_failure")
            if row["cost_usd"] is None or row["duration_s"] is None:
                _refuse("invalid_value", "VALID row needs known cost and duration")
            if row["outcome"] == "scored":
                _num(row["raw_points"], "raw_points")
                if row["raw_points"] > d["private"][cid]["max_points"]:
                    _refuse("invalid_value", "raw_points exceeds case max_points")
            elif row["raw_points"] is not None:
                _refuse("invalid_value", "task_failure carries no raw_points")
        else:
            if row["outcome"] != "none" or row["raw_points"] is not None:
                _refuse("invalid_value", "non-VALID row has outcome none and no points")
        if row["run_receipt_id"] in self._run_ids:
            _refuse("duplicate_run_receipt", row["run_receipt_id"])
        cell = (row["benchmark_id"], row["version"], row["bundle_identity"],
                _digest(row["runtime"]), row["arm"], cid, rep)
        if cell in self._cells:
            _refuse("duplicate_cell", "cell already has a retained row")
        stored = copy.deepcopy(row)
        stored["seq"] = len(self._rows) + 1
        stored["recorded_by"] = caller
        self._rows.append(stored)
        self._run_ids.add(row["run_receipt_id"])
        self._cells.add(cell)
        return stored["seq"]

    def list_receipts(self, benchmark_id, version):
        self._get(benchmark_id, version)
        return [copy.deepcopy(r) for r in self._rows
                if r["benchmark_id"] == benchmark_id and r["version"] == version]

    def scoreboard_snapshot(self, benchmark_id, version):
        rows = self.list_receipts(benchmark_id, version)
        return {"benchmark_id": benchmark_id, "version": version,
                "row_count": len(rows), "rows": rows, "snapshot_digest": _digest(rows)}

    # ---- aggregate ----
    def aggregate(self, benchmark_id, version, expected):
        """expected: {arm: {"bundle_identity": sha, "runtime": {...}}} for
        EVERY arm. Stats only for a complete matrix; otherwise INVALID with
        the retained reasons and no statistics (never zero, never a subset)."""
        d = self._get(benchmark_id, version)
        if d["status"] != "published":
            _refuse("not_published", "aggregate needs a published benchmark")
        arms = d["header"]["arms"]
        if not isinstance(expected, dict):
            _refuse("invalid_value", "expected must be an object")
        for a in expected:
            _id(a, "arm")
            if a not in arms:
                _refuse("unknown_arm", str(a))
        for a in arms:
            if a not in expected:
                _refuse("invalid_value", "expected lacks arm " + a)
            _exact(expected[a], ("bundle_identity", "runtime"), "expected " + a)
            _sha(expected[a]["bundle_identity"], "bundle_identity")
            self._check_runtime(expected[a]["runtime"])
        by_cell = {}
        for r in self._rows:
            if r["benchmark_id"] != benchmark_id or r["version"] != version:
                continue
            e = expected.get(r["arm"])
            if (e and r["bundle_identity"] == e["bundle_identity"]
                    and r["runtime"] == e["runtime"]):
                by_cell[(r["arm"], r["case_id"], r["repetition"])] = r
        missing, invalid = [], []
        for a in arms:
            for cid in d["header"]["cases"]:
                for rep in range(1, REPETITIONS + 1):
                    r = by_cell.get((a, cid, rep))
                    key = "%s/%s/%d" % (a, cid, rep)
                    if r is None:
                        missing.append(key)
                    elif r["validity"] != "VALID":
                        invalid.append({"cell": key, "validity": r["validity"],
                                        "run_receipt_id": r["run_receipt_id"]})
        out = {"benchmark_id": benchmark_id, "version": version,
               "expected_cells": len(arms) * len(d["header"]["cases"]) * REPETITIONS,
               "missing_cells": missing, "invalid_cells": invalid, "arms": None}
        if missing or invalid:
            out["status"] = "INVALID"
            return out
        out["status"] = "COMPLETE"
        stats = {}
        for a in arms:
            parts = {}
            cost = dur = 0.0
            for part in ("train", "holdout"):
                case_means = []
                for cid, c in d["header"]["cases"].items():
                    if c["partition"] != part:
                        continue
                    mx = d["private"][cid]["max_points"]
                    vals = []
                    for rep in range(1, REPETITIONS + 1):
                        r = by_cell[(a, cid, rep)]
                        cost = _num(cost + r["cost_usd"], "total_cost_usd")
                        dur = _num(dur + r["duration_s"], "total_duration_s")
                        # Known settled task failure is the ONLY fixed zero.
                        pts = 0.0 if r["outcome"] == "task_failure" else r["raw_points"]
                        vals.append((pts / mx) * 100.0)
                    case_means.append(sum(vals) / len(vals))
                parts[part] = {"cases": len(case_means),
                               "mean_normalized": sum(case_means) / len(case_means)}
            stats[a] = {"partitions": parts, "total_cost_usd": cost,
                        "total_duration_s": dur}
        out["arms"] = stats
        return out

