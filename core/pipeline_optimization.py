"""Pure, deterministic scorer for frozen RSI pipeline-optimization matrices.

This module is a standalone mathematical library for a future trusted
coordinator.  It does exactly one thing: validate a complete matrix of
caller-attested cell receipts against a caller-supplied frozen manifest and
compute the accepted case-level score statistics and decision.

What it is NOT
--------------
* It does not launch, run, score, or call anything (no model, checker, network,
  filesystem, registry, ledger or runtime access).
* It does not prove that any run actually happened, who owned it, that inputs
  were secret, or that a holdout/budget was used only once.  Every identity,
  ``accounting`` and ``outcome`` field is a *trusted-caller attestation* that is
  only checked for exact consistency with the manifest.  A JSON-valid receipt
  is never evidence of truth.  The coordinator must establish authentication,
  freezing, privacy and one-use holdout/budget BEFORE calling this module.
* It never grants adoption or any other authority.  ``eligible`` only means the
  supplied data satisfy the numeric acceptance rule.

Public API
----------
``evaluate_matrix(manifest, receipts, *, stage)`` with ``stage`` either
``"train"`` (reports complete train statistics only, never eligibility) or
``"holdout"`` (the single fixed eight-case matrix that decides eligibility).
The two stages are never mixed: a receipt for a case of the other stage is
invalid.  Returns a plain dict whose ``status`` is one of ``"invalid"``,
``"train_complete"``, ``"eligible"`` or ``"rejected"``.  Malformed data never
raises; it yields ``invalid`` with every reason found (first reason first).

Manifest (all keys required, ids are non-empty strings)::

    {"train_case_ids": [12 ids], "holdout_case_ids": [8 ids],
     "case_families": {case_id: family_id, ...},      # all 20 cases
     "family_inventory": [family_id, ...],             # == set of families;
                                                       # each has a holdout case
     "repetitions": 3,
     "arms": {"reference": {"bundle_id", "runtime_id", "checker_id"},
              "candidate": {"bundle_id", "runtime_id", "checker_id"}},
     "input_ids": {case_id: input_id, ...}}

Receipt (one per case x arm x repetition 1..3; extra keys are preserved
privately and never copied to ``public_summary``)::

    {"case_id", "arm", "repetition", "run_receipt_id", "bundle_id",
     "runtime_id", "checker_id", "input_id",
     "accounting": "settled",
     "outcome": "scored" | "task_failure",
     "score": number 0..100 (omitted/None for task_failure)}

Any other accounting/outcome (infrastructure, scorer, unknown effect, missing
output, ...) is invalid and is never dropped or zeroed.  Only a real settled
``task_failure`` takes the predeclared zero score.  bool, NaN and Inf scores
are rejected.

Arithmetic: all scores become exact ``Fraction`` values (floats via their
shortest decimal repr, so 4.999 is exactly 4999/1000).  Decisions use the
unrounded exact values; displayed floats are informational only.

Decision (holdout): per-case mean of 3 repetitions per arm; per-case
difference candidate-minus-reference; overall = equal-weight mean of the 8 case
differences, must be >= 5 (inclusive); no family mean case-difference below 0;
one-sided paired sign-flip test over the 8 CASE differences (all 2**8 = 256
sign assignments, zeros included) with p = #(assignments whose sum >= observed
sum) / 256 <= 1/20.  The 24 repetitions are NOT independent samples.
"""
from __future__ import annotations

import copy
import itertools
import math
from decimal import Decimal
from fractions import Fraction

TRAIN_CASE_COUNT = 12
HOLDOUT_CASE_COUNT = 8
REPETITIONS = 3
ARMS = ("reference", "candidate")
IDENTITY_FIELDS = ("bundle_id", "runtime_id", "checker_id")
STAGES = ("train", "holdout")
SETTLED = "settled"
OUTCOME_SCORED = "scored"
OUTCOME_TASK_FAILURE = "task_failure"
TASK_FAILURE_SCORE = Fraction(0)
MARGIN = Fraction(5)
ALPHA = Fraction(1, 20)

STATUS_INVALID = "invalid"
STATUS_TRAIN_COMPLETE = "train_complete"
STATUS_ELIGIBLE = "eligible"
STATUS_REJECTED = "rejected"

_REQUIRED_RECEIPT_FIELDS = (
    "case_id", "arm", "repetition", "run_receipt_id", *IDENTITY_FIELDS,
    "input_id", "accounting", "outcome",
)


def _reason(code: str, **detail) -> dict:
    return {"code": code, "detail": detail}


def _is_id(value) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _id_list(value, count: int, name: str, reasons: list):
    if (not isinstance(value, list) or len(value) != count
            or not all(_is_id(v) for v in value) or len(set(value)) != count):
        reasons.append(_reason("manifest_invalid", field=name,
                               expected=f"{count} unique id strings"))
        return None
    return list(value)


def _id_map(value, keys: set, name: str, reasons: list):
    if (not isinstance(value, dict) or set(value) != keys
            or not all(_is_id(v) for v in value.values())):
        reasons.append(_reason("manifest_invalid", field=name,
                               expected="id strings for exactly the frozen cases"))
        return None
    return dict(value)


def _check_manifest(manifest, reasons: list):
    """Return normalized frozen spec, or None after appending reasons."""
    if not isinstance(manifest, dict):
        reasons.append(_reason("manifest_invalid", field="manifest",
                               expected="dict"))
        return None
    start = len(reasons)
    train = _id_list(manifest.get("train_case_ids"), TRAIN_CASE_COUNT,
                     "train_case_ids", reasons)
    holdout = _id_list(manifest.get("holdout_case_ids"), HOLDOUT_CASE_COUNT,
                       "holdout_case_ids", reasons)
    if train and holdout and set(train) & set(holdout):
        reasons.append(_reason("manifest_invalid", field="case_ids",
                               expected="disjoint train/holdout"))
    if not _is_int(manifest.get("repetitions")) \
            or manifest.get("repetitions") != REPETITIONS:
        reasons.append(_reason("manifest_invalid", field="repetitions",
                               expected=REPETITIONS))
    arms = manifest.get("arms")
    arm_ids = {}
    if (not isinstance(arms, dict) or set(arms) != set(ARMS)
            or not all(isinstance(arms[a], dict)
                       and set(arms[a]) == set(IDENTITY_FIELDS)
                       and all(_is_id(v) for v in arms[a].values())
                       for a in ARMS)):
        reasons.append(_reason("manifest_invalid", field="arms",
                               expected="reference/candidate identity fields"))
    else:
        arm_ids = {a: dict(arms[a]) for a in ARMS}
    families = input_ids = None
    if train and holdout:
        cases = set(train) | set(holdout)
        families = _id_map(manifest.get("case_families"), cases,
                           "case_families", reasons)
        input_ids = _id_map(manifest.get("input_ids"), cases, "input_ids",
                            reasons)
        inventory = manifest.get("family_inventory")
        if (not isinstance(inventory, list)
                or not all(_is_id(f) for f in inventory)
                or len(set(inventory)) != len(inventory)):
            reasons.append(_reason("manifest_invalid", field="family_inventory",
                                   expected="unique family id list"))
        elif families is not None:
            if set(inventory) != set(families.values()):
                reasons.append(_reason(
                    "manifest_invalid", field="family_inventory",
                    expected="exactly the families of case_families"))
            elif {families[c] for c in holdout} != set(inventory):
                reasons.append(_reason(
                    "manifest_invalid", field="family_inventory",
                    expected="every family has a holdout case"))
    if len(reasons) > start or families is None or input_ids is None:
        return None
    return {"train": train, "holdout": holdout, "arms": arm_ids,
            "families": families, "input_ids": input_ids,
            "inventory": list(manifest["family_inventory"])}


def _exact_score(value):
    """Exact Fraction in [0, 100] for a finite real number, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        exact = Fraction(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        exact = Fraction(repr(value))
    elif isinstance(value, Decimal):
        if not value.is_finite():
            return None
        exact = Fraction(value)
    elif isinstance(value, Fraction):
        exact = value
    else:
        return None
    return exact if 0 <= exact <= 100 else None


def _check_receipts(spec: dict, stage: str, receipts, reasons: list) -> dict:
    """Validate receipts; return {(case, arm, rep): Fraction} of valid cells."""
    stage_cases = spec[stage]
    other_cases = set(spec["holdout" if stage == "train" else "train"])
    if not isinstance(receipts, (list, tuple)):
        reasons.append(_reason("receipts_invalid", expected="list of dicts"))
        return {}
    cells, seen_cells, seen_ids = {}, set(), set()
    for index, rec in enumerate(receipts):
        if not isinstance(rec, dict):
            reasons.append(_reason("malformed_receipt", index=index))
            continue
        absent = [f for f in _REQUIRED_RECEIPT_FIELDS if f not in rec]
        if absent:
            reasons.append(_reason("malformed_receipt", index=index,
                                   missing=absent))
            continue
        case, arm, rep = rec["case_id"], rec["arm"], rec["repetition"]
        if _is_id(case) and case in other_cases:
            reasons.append(_reason("wrong_stage_case", index=index))
            continue
        if (not _is_id(case) or case not in stage_cases or not _is_id(arm)
                or arm not in ARMS or not _is_int(rep)
                or not 1 <= rep <= REPETITIONS):
            reasons.append(_reason("unexpected_cell", index=index))
            continue
        key = (case, arm, rep)
        where = {"index": index, "case_id": case, "arm": arm, "repetition": rep}
        ok = True
        if key in seen_cells:
            reasons.append(_reason("duplicate_cell", **where))
            ok = False
        seen_cells.add(key)
        rid = rec["run_receipt_id"]
        if not _is_id(rid):
            reasons.append(_reason("invalid_run_receipt_id", **where))
            ok = False
        elif rid in seen_ids:
            reasons.append(_reason("duplicate_run_receipt_id", **where))
            ok = False
        else:
            seen_ids.add(rid)
        expected = dict(spec["arms"][arm], input_id=spec["input_ids"][case])
        for field, want in expected.items():
            if rec[field] != want:
                reasons.append(_reason("identity_mismatch", field=field, **where))
                ok = False
        if rec["accounting"] != SETTLED:
            reasons.append(_reason("accounting_not_settled", **where))
            ok = False
        outcome, score = rec["outcome"], rec.get("score")
        value = None
        if outcome == OUTCOME_SCORED:
            value = _exact_score(score)
            if value is None:
                reasons.append(_reason("invalid_score", **where))
                ok = False
        elif outcome == OUTCOME_TASK_FAILURE:
            if score is not None and _exact_score(score) != TASK_FAILURE_SCORE:
                reasons.append(_reason("invalid_score", **where))
                ok = False
            value = TASK_FAILURE_SCORE
        else:
            reasons.append(_reason("invalid_outcome", **where))
            ok = False
        if ok and value is not None:
            cells[key] = value
    for case in stage_cases:
        for arm in ARMS:
            for rep in range(1, REPETITIONS + 1):
                if (case, arm, rep) not in seen_cells:
                    reasons.append(_reason("missing_cell", case_id=case,
                                           arm=arm, repetition=rep))
    return cells


def _num(value: Fraction) -> dict:
    text = str(value.numerator) if value.denominator == 1 else \
        f"{value.numerator}/{value.denominator}"
    return {"exact": text, "display": float(value)}


def sign_flip_p_value(differences) -> Fraction:
    """Exact one-sided paired sign-flip p over all 2**n sign assignments.

    p = #(assignments whose signed sum >= observed sum) / 2**n, zeros included.
    """
    diffs = [Fraction(d) for d in differences]
    observed = sum(diffs, Fraction(0))
    hits = 0
    for signs in itertools.product((1, -1), repeat=len(diffs)):
        if sum((s * d for s, d in zip(signs, diffs)), Fraction(0)) >= observed:
            hits += 1
    return Fraction(hits, 2 ** len(diffs))


def _statistics(spec: dict, stage: str, cells: dict) -> dict:
    cases = spec[stage]
    means = {arm: {c: sum((cells[(c, arm, r)]
                           for r in range(1, REPETITIONS + 1)), Fraction(0))
                   / REPETITIONS for c in cases} for arm in ARMS}
    diffs = {c: means["candidate"][c] - means["reference"][c] for c in cases}
    overall = sum(diffs.values(), Fraction(0)) / len(cases)
    family = {}
    for fam in spec["inventory"]:
        members = [c for c in cases if spec["families"][c] == fam]
        if members:
            family[fam] = sum((diffs[c] for c in members), Fraction(0)) \
                / len(members)
    return {"means": means, "diffs": diffs, "overall": overall,
            "family": family}


def _public_stats(stats: dict) -> dict:
    return {
        "case_means": {arm: {c: _num(v) for c, v in m.items()}
                       for arm, m in stats["means"].items()},
        "case_differences": {c: _num(v) for c, v in stats["diffs"].items()},
        "overall_difference": _num(stats["overall"]),
        "family_mean_differences": {f: _num(v)
                                    for f, v in stats["family"].items()},
    }


def evaluate_matrix(manifest, receipts, *, stage: str) -> dict:
    """Validate a frozen matrix of attested receipts and score it.

    Pure validation of caller-attested data; see the module docstring.  Returns
    a dict with ``status``, ``stage``, ``reasons`` (list of ``{"code",
    "detail"}``, validation first-errors first), ``public_summary`` (numbers
    and ids only, no receipt free text), ``private_receipts`` (deep copy of the
    supplied receipts for caller-chosen private storage) and
    ``grants_authority`` (always False).
    """
    reasons: list = []
    try:
        private = copy.deepcopy(receipts)
    except Exception:  # noqa: BLE001 - preserve data best-effort, never raise
        private = None
    result = {"stage": stage, "status": STATUS_INVALID, "reasons": reasons,
              "public_summary": {"stage": stage, "status": STATUS_INVALID},
              "private_receipts": private, "grants_authority": False}
    if stage not in STAGES:
        reasons.append(_reason("invalid_stage", expected=list(STAGES)))
        return result
    spec = _check_manifest(manifest, reasons)
    cells = _check_receipts(spec, stage, receipts, reasons) if spec else {}
    if reasons or spec is None:
        result["public_summary"]["reason_codes"] = [r["code"] for r in reasons]
        return result
    stats = _statistics(spec, stage, cells)
    summary = {"stage": stage, **_public_stats(stats)}
    if stage == "train":
        status = STATUS_TRAIN_COMPLETE
    else:
        p_value = sign_flip_p_value(stats["diffs"][c] for c in spec["holdout"])
        checks = {
            "margin_below_threshold": stats["overall"] < MARGIN,
            "family_regression": any(v < 0 for v in stats["family"].values()),
            "sign_flip_p_above_alpha": p_value > ALPHA,
        }
        failed = [name for name, bad in checks.items() if bad]
        status = STATUS_REJECTED if failed else STATUS_ELIGIBLE
        summary.update({
            "margin_required": _num(MARGIN), "alpha": _num(ALPHA),
            "sign_assignments": 2 ** HOLDOUT_CASE_COUNT,
            "sign_flip_p": _num(p_value), "rejection_reasons": failed})
        result["reasons"] = [_reason(name) for name in failed]
    summary["status"] = status
    result["status"] = status
    result["public_summary"] = summary
    return result
