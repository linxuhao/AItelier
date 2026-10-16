"""Versioned benchmark definitions and append-only scoreboard documents.

This is a host-owned DATA provider, not an executor, evaluator, authentication
boundary or spend/adoption authority. ``BenchmarkStore(path)`` persists one
ordinary JSON business document at a caller-selected private path. Published
definitions and receipt rows cannot be changed through this interface. Readers
receive fresh public projections; private refs require the creator's label,
which is deliberately not authentication. No production default store is opened.
"""
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile

from core import pipeline_optimization as scoring

SCHEMA = "rsi.benchmark.v1"
FAMILIES = ("family-a", "family-b", "family-c", "family-d")
HEADER = ("schema", "benchmark_id", "version", "arms", "families", "cases",
          "point_total", "normalization")
ROW = ("schema", "benchmark_id", "version", "bundle_identity", "runtime", "case_id",
       "repetition", "arm", "run_receipt_id", "input_identity", "checker_identity",
       "validity", "outcome", "raw_points", "cost_usd", "duration_s")


class BenchmarkError(ValueError):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(f"{code}: {message}")


def require(condition, code="invalid_value", message=""):
    if not condition:
        raise BenchmarkError(code, message)


def text(value):
    require(isinstance(value, str) and bool(value.strip()))
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BenchmarkError("invalid_value", "text must be UTF-8") from exc
    return value


def identity(value, digest=False):
    pattern = r"sha256:[0-9a-f]{64}" if digest else r"[a-z0-9][a-z0-9._-]{0,63}"
    require(isinstance(value, str) and re.fullmatch(pattern, value) is not None)
    return value


def number(value, integer=False, minimum=0):
    require(type(value) in ((int,) if integer else (int, float)))
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    require(finite and value >= minimum)
    return value


def fields(value, names):
    require(isinstance(value, dict) and all(isinstance(k, str) for k in value))
    require(not (set(value) - set(names)), "unknown_field")
    require(set(value) == set(names))
    return value


def encoded(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (ValueError, OverflowError, UnicodeEncodeError) as exc:
        raise BenchmarkError("invalid_value", "not canonical JSON") from exc


def digest(value):
    return "sha256:" + hashlib.sha256(encoded(value)).hexdigest()


def runtime(value):
    fields(value, ("route", "endpoints", "effort"))
    text(value["route"]); text(value["effort"])
    endpoints = value["endpoints"]
    require(isinstance(endpoints, list) and bool(endpoints))
    for endpoint in endpoints:
        text(endpoint)
    require(len(set(endpoints)) == len(endpoints))


def exact(value):
    return Fraction(repr(value)) if isinstance(value, float) else Fraction(value)


def normalized_score(points, maximum):
    """Exact decimal input arithmetic, using the accepted scorer's score rule."""
    value = scoring._exact_score(exact(points) / maximum * 100)
    require(value is not None)
    return value


class BenchmarkStore:
    """Persistent definitions/scoreboard, isolated by an explicit document path.

    A file lock serializes ordinary document mutations across instances/processes;
    an atomic replacement commits a validated complete document. It is not a
    workflow operation ledger and does not claim power-loss durability or hostile
    filesystem custody. Callers choose a trusted path; there is no global store.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _document(self, write=False):
        with self.path.with_name(self.path.name + ".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            document = (json.loads(self.path.read_bytes()) if self.path.exists()
                        else {"schema": "rsi.benchmark.store.v1", "definitions": [], "rows": []})
            require(document.get("schema") == "rsi.benchmark.store.v1", "storage_invalid")
            yield document
            if write:
                data = encoded(document)  # refuse before touching the stored document
                fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".")
                try:
                    with os.fdopen(fd, "wb") as out:
                        out.write(data)
                        out.flush()
                        os.fsync(out.fileno())
                    os.replace(name, self.path)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)

    @staticmethod
    def _find(document, bid, version):
        identity(bid); number(version, integer=True, minimum=1)
        for definition in document["definitions"]:
            h = definition["header"]
            if (h["benchmark_id"], h["version"]) == (bid, version):
                return definition
        raise BenchmarkError("unknown_benchmark")

    @classmethod
    def _draft(cls, document, caller, bid, version):
        text(caller)
        definition = cls._find(document, bid, version)
        require(definition["status"] == "draft", "immutable")
        require(definition["owner"] == caller, "ownership")
        return definition

    @staticmethod
    def _public(definition):
        h = definition["header"]
        return json.loads(encoded({
            **{k: h[k] for k in ("schema", "benchmark_id", "version", "arms", "families", "point_total", "normalization")},
            "status": definition["status"], "repetitions": scoring.REPETITIONS,
            "partition_counts": {"train": scoring.TRAIN_CASE_COUNT, "holdout": scoring.HOLDOUT_CASE_COUNT},
            "cases": {cid: {**case, "public_input": definition["public"].get(cid),
                              "input_identity": definition["identities"].get(cid, {}).get("input_identity"),
                              "checker_identity": definition["identities"].get(cid, {}).get("checker_identity")}
                      for cid, case in h["cases"].items()},
            "definition_digest": definition["digest"]}))

    def create_draft(self, caller, header):
        text(caller); fields(header, HEADER)
        require(header["schema"] == SCHEMA, "unknown_schema")
        identity(header["benchmark_id"]); number(header["version"], integer=True, minimum=1)
        with self._document(write=True) as doc:
            require(not any((d["header"]["benchmark_id"], d["header"]["version"]) ==
                            (header["benchmark_id"], header["version"]) for d in doc["definitions"]), "duplicate")
            arms = header["arms"]
            require(isinstance(arms, list) and 1 <= len(arms) <= 8)
            for arm in arms: identity(arm)
            require(len(set(arms)) == len(arms) and header["families"] == list(FAMILIES))
            require(isinstance(header["cases"], dict))
            counts = {"train": 0, "holdout": 0}
            for cid, case in header["cases"].items():
                identity(cid); fields(case, ("partition", "family"))
                require(isinstance(case["partition"], str) and case["partition"] in counts)
                require(case["family"] in FAMILIES)
                counts[case["partition"]] += 1
            require(counts == {"train": scoring.TRAIN_CASE_COUNT, "holdout": scoring.HOLDOUT_CASE_COUNT})
            number(header["point_total"], integer=True, minimum=1)
            fields(header["normalization"], ("min", "max"))
            require(all(type(v) is int for v in header["normalization"].values())
                    and header["normalization"] == {"min": 0, "max": 100})
            definition = {"header": json.loads(encoded(header)), "owner": caller, "status": "draft",
                          "public": {}, "private": {}, "identities": {}, "digest": None}
            doc["definitions"].append(definition)
            return self._public(definition)

    def set_public_input(self, caller, bid, version, cid, value):
        with self._document(write=True) as doc:
            d = self._draft(doc, caller, bid, version)
            identity(cid); require(cid in d["header"]["cases"], "unknown_case")
            fields(value, ("prompt",)); text(value["prompt"])
            d["public"][cid] = json.loads(encoded(value))

    def set_private_refs(self, caller, bid, version, cid, value):
        with self._document(write=True) as doc:
            d = self._draft(doc, caller, bid, version)
            identity(cid); require(cid in d["header"]["cases"], "unknown_case")
            fields(value, ("rubric_ref", "checker_ref", "max_points"))
            text(value["rubric_ref"]); text(value["checker_ref"]); number(value["max_points"], integer=True, minimum=1)
            d["private"][cid] = json.loads(encoded(value))

    def publish(self, caller, bid, version):
        with self._document(write=True) as doc:
            d = self._draft(doc, caller, bid, version); cases = d["header"]["cases"]
            require(set(d["public"]) == set(d["private"]) == set(cases), "incomplete")
            for part in ("train", "holdout"):
                require({c["family"] for c in cases.values() if c["partition"] == part} == set(FAMILIES), "incomplete")
            require(sum(v["max_points"] for v in d["private"].values()) == d["header"]["point_total"], "incomplete")
            identities = {cid: {"input_identity": digest(d["public"][cid]),
                                "checker_identity": digest(d["private"][cid])} for cid in cases}
            definition_digest = digest({"header": d["header"], "identities": identities})
            d.update(identities=identities, digest=definition_digest, status="published")
            return self._public(d)

    def get_definition(self, bid, version):
        with self._document() as doc: return self._public(self._find(doc, bid, version))

    def get_private_refs(self, caller, bid, version):
        text(caller)
        with self._document() as doc:
            d = self._find(doc, bid, version); require(d["owner"] == caller, "ownership")
            return json.loads(encoded(d["private"]))

    def list_benchmarks(self):
        with self._document() as doc:
            return sorted(({"benchmark_id": d["header"]["benchmark_id"], "version": d["header"]["version"],
                            "status": d["status"]} for d in doc["definitions"]), key=lambda d: (d["benchmark_id"], d["version"]))

    @staticmethod
    def _cell(row):
        return tuple(row[k] for k in ("benchmark_id", "version", "bundle_identity")) + (digest(row["runtime"]),) + tuple(row[k] for k in ("arm", "case_id", "repetition"))

    def append_receipt(self, caller, row):
        text(caller); fields(row, ROW); require(row["schema"] == SCHEMA, "unknown_schema")
        with self._document(write=True) as doc:
            d = self._find(doc, row["benchmark_id"], row["version"])
            require(d["status"] == "published", "not_published")
            identity(row["bundle_identity"], True); runtime(row["runtime"])
            cid = identity(row["case_id"]); require(cid in d["header"]["cases"], "unknown_case")
            require(type(row["repetition"]) is int)
            require(1 <= row["repetition"] <= scoring.REPETITIONS, "unknown_repetition")
            identity(row["arm"]); require(row["arm"] in d["header"]["arms"], "unknown_arm")
            identity(row["run_receipt_id"])
            for field in ("input_identity", "checker_identity"):
                identity(row[field], True); require(row[field] == d["identities"][cid][field], "identity_mismatch")
            require(row["validity"] in ("VALID", "INVALID", "INFRASTRUCTURE", "UNKNOWN_ACCOUNTING"))
            require(row["outcome"] in ("scored", "task_failure", "none"))
            for field in ("cost_usd", "duration_s"):
                if row[field] is not None: number(row[field])
            if row["validity"] == "VALID":
                require(row["cost_usd"] is not None and row["duration_s"] is not None and row["outcome"] != "none")
                if row["outcome"] == "scored":
                    number(row["raw_points"]); require(row["raw_points"] <= d["private"][cid]["max_points"])
                    normalized_score(row["raw_points"], d["private"][cid]["max_points"])
                else: require(row["raw_points"] is None)
            else: require(row["outcome"] == "none" and row["raw_points"] is None)
            require(not any(r["run_receipt_id"] == row["run_receipt_id"] for r in doc["rows"]), "duplicate_run_receipt")
            cell = self._cell(row)
            require(not any(self._cell(r) == cell for r in doc["rows"]), "duplicate_cell")
            stored = json.loads(encoded(row)); stored.update(seq=len(doc["rows"]) + 1, recorded_by=caller)
            doc["rows"].append(stored)
            return stored["seq"]

    def list_receipts(self, bid, version):
        with self._document() as doc:
            self._find(doc, bid, version)
            return [r for r in doc["rows"] if (r["benchmark_id"], r["version"]) == (bid, version)]

    def scoreboard_snapshot(self, bid, version):
        rows = self.list_receipts(bid, version)
        return {"benchmark_id": bid, "version": version, "row_count": len(rows),
                "rows": rows, "snapshot_digest": digest(rows)}

    def aggregate(self, bid, version, expected):
        with self._document() as doc:
            d = self._find(doc, bid, version); require(d["status"] == "published", "not_published")
            arms = d["header"]["arms"]; require(isinstance(expected, dict))
            for arm in expected:
                identity(arm); require(arm in arms, "unknown_arm")
            for arm in arms:
                require(arm in expected); fields(expected[arm], ("bundle_identity", "runtime"))
                identity(expected[arm]["bundle_identity"], True); runtime(expected[arm]["runtime"])
            cells = {(r["arm"], r["case_id"], r["repetition"]): r for r in doc["rows"]
                     if (r["benchmark_id"], r["version"]) == (bid, version)
                     and r["bundle_identity"] == expected[r["arm"]]["bundle_identity"]
                     and r["runtime"] == expected[r["arm"]]["runtime"]}
            missing, invalid = [], []
            for arm in arms:
                for cid in d["header"]["cases"]:
                    for rep in range(1, scoring.REPETITIONS + 1):
                        row = cells.get((arm, cid, rep)); key = f"{arm}/{cid}/{rep}"
                        if row is None: missing.append(key)
                        elif row["validity"] != "VALID":
                            invalid.append({"cell": key, "validity": row["validity"], "run_receipt_id": row["run_receipt_id"]})
            result = {"benchmark_id": bid, "version": version, "expected_cells": len(arms) * 20 * scoring.REPETITIONS,
                      "missing_cells": missing, "invalid_cells": invalid, "arms": None,
                      "status": "INVALID" if missing or invalid else "COMPLETE"}
            if missing or invalid: return result
            stats = {}
            for arm in arms:
                parts = {}; cost = duration = Fraction(0)
                for part in ("train", "holdout"):
                    means = []
                    for cid, case in d["header"]["cases"].items():
                        if case["partition"] != part: continue
                        scores = []
                        for rep in range(1, scoring.REPETITIONS + 1):
                            row = cells[(arm, cid, rep)]
                            cost += exact(row["cost_usd"]); duration += exact(row["duration_s"])
                            scores.append(scoring.TASK_FAILURE_SCORE if row["outcome"] == "task_failure"
                                          else normalized_score(row["raw_points"], d["private"][cid]["max_points"]))
                        means.append(sum(scores, Fraction(0)) / scoring.REPETITIONS)
                    parts[part] = {"cases": len(means), "mean_normalized": float(sum(means, Fraction(0)) / len(means))}
                try: cost_float, duration_float = float(cost), float(duration)
                except OverflowError as exc: raise BenchmarkError("invalid_value", "aggregate accounting overflow") from exc
                number(cost_float); number(duration_float)
                stats[arm] = {"partitions": parts, "total_cost_usd": cost_float, "total_duration_s": duration_float}
            result["arms"] = stats
            return result
