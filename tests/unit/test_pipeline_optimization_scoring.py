"""Unit tests for core.pipeline_optimization (pure matrix scorer).

Literal hand-computed vectors; these tests validate caller-attested data only.
"""
from __future__ import annotations

import copy
import math
from fractions import Fraction

from core import pipeline_optimization as po

TRAIN = [f"t{i:02d}" for i in range(12)]
HOLD = [f"h{i}" for i in range(8)]


def manifest(hold_families=None):
    fams = {c: ("alpha" if i % 2 == 0 else "beta") for i, c in enumerate(TRAIN)}
    fams.update(hold_families or {
        c: ("alpha" if i < 4 else "beta") for i, c in enumerate(HOLD)})
    return {
        "train_case_ids": list(TRAIN), "holdout_case_ids": list(HOLD),
        "case_families": fams, "family_inventory": ["alpha", "beta"],
        "repetitions": 3,
        "arms": {a: {"bundle_id": f"{a}-bundle", "runtime_id": f"{a}-rt",
                     "checker_id": "checker-1"}
                 for a in ("reference", "candidate")},
        "input_ids": {c: f"in-{c}" for c in TRAIN + HOLD},
    }


def receipts(cases, score_fn):
    out = []
    for c in cases:
        for arm in ("reference", "candidate"):
            for rep in (1, 2, 3):
                out.append({
                    "case_id": c, "arm": arm, "repetition": rep,
                    "run_receipt_id": f"run-{c}-{arm}-{rep}",
                    "bundle_id": f"{arm}-bundle", "runtime_id": f"{arm}-rt",
                    "checker_id": "checker-1", "input_id": f"in-{c}",
                    "accounting": "settled", "outcome": "scored",
                    "score": score_fn(c, arm, rep)})
    return out


def diff_vector(diffs, base=50):
    """Candidate = base + diff for each holdout case; reference = base."""
    d = dict(zip(HOLD, diffs))
    return receipts(HOLD, lambda c, arm, rep:
                    base if arm == "reference" else base + d[c])


def holdout(recs, m=None):
    return po.evaluate_matrix(m or manifest(), recs, stage="holdout")


def codes(result):
    return [r["code"] for r in result["reasons"]]


# --- literal score vectors -------------------------------------------------

def test_all_zero_p_is_one_and_rejected():
    res = holdout(receipts(HOLD, lambda *_: 0))
    assert res["status"] == "rejected"
    assert res["public_summary"]["sign_flip_p"]["exact"] == "1"
    assert codes(res) == ["margin_below_threshold", "sign_flip_p_above_alpha"]
    assert res["grants_authority"] is False


def test_eight_equal_plus_five_eligible_p_one_256th():
    res = holdout(receipts(HOLD, lambda c, arm, rep:
                           40 if arm == "reference" else 45))
    s = res["public_summary"]
    assert res["status"] == "eligible" and res["reasons"] == []
    assert s["overall_difference"]["exact"] == "5"  # inclusive margin
    assert s["sign_flip_p"]["exact"] == "1/256"
    assert s["sign_assignments"] == 256


def test_below_margin_4_999_rejected():
    res = holdout(receipts(HOLD, lambda c, arm, rep:
                           0 if arm == "reference" else 4.999))
    assert res["status"] == "rejected"
    assert res["public_summary"]["overall_difference"]["exact"] == "4999/1000"
    assert codes(res) == ["margin_below_threshold"]


def test_family_mean_regression_rejected_alone():
    fams = {c: ("alpha" if c != "h7" else "beta") for c in HOLD}
    res = holdout(diff_vector([10] * 7 + [-1]), manifest(fams))
    s = res["public_summary"]
    assert s["overall_difference"]["exact"] == "69/8"
    assert s["sign_flip_p"]["exact"] == "1/128"
    assert s["family_mean_differences"]["beta"]["exact"] == "-1"
    assert res["status"] == "rejected"
    assert codes(res) == ["family_regression"]


def test_huge_outlier_has_mean_5_375_but_p_half():
    res = holdout(diff_vector([50] + [-1] * 7))
    s = res["public_summary"]
    assert s["overall_difference"]["exact"] == "43/8"
    assert s["overall_difference"]["display"] == 5.375
    assert s["sign_flip_p"]["exact"] == "1/2"
    assert res["status"] == "rejected"
    assert "sign_flip_p_above_alpha" in codes(res)
    assert "margin_below_threshold" not in codes(res)


def test_sign_flip_p_function_literals():
    assert po.sign_flip_p_value([0] * 8) == 1
    assert po.sign_flip_p_value([5] * 8) == Fraction(1, 256)
    assert po.sign_flip_p_value([50] + [-1] * 7) == Fraction(1, 2)


def test_case_means_equal_weight_over_three_repetitions():
    ref, cand = {1: 10, 2: 20, 3: 30}, {1: 40, 2: 50, 3: 60}
    res = holdout(receipts(HOLD, lambda c, arm, rep:
                           (ref if arm == "reference" else cand)[rep]))
    s = res["public_summary"]
    assert s["case_means"]["reference"]["h0"]["exact"] == "20"
    assert s["case_means"]["candidate"]["h0"]["exact"] == "50"
    assert s["overall_difference"]["exact"] == "30"
    assert res["status"] == "eligible"


def test_task_failure_uses_zero_policy():
    recs = receipts(HOLD, lambda c, arm, rep: 90 if arm == "candidate" else 0)
    for r in recs:
        if (r["case_id"], r["arm"], r["repetition"]) == ("h0", "candidate", 1):
            r.update(outcome="task_failure", score=None)
    res = holdout(recs)
    assert res["status"] == "eligible"
    assert res["public_summary"]["case_means"]["candidate"]["h0"]["exact"] == "60"


def test_train_stage_reports_statistics_only():
    res = po.evaluate_matrix(manifest(), receipts(
        TRAIN, lambda c, arm, rep: 10 if arm == "reference" else 90),
        stage="train")
    assert res["status"] == "train_complete"
    assert res["public_summary"]["overall_difference"]["exact"] == "80"
    assert "sign_flip_p" not in res["public_summary"]


# --- invalid data ----------------------------------------------------------

def good():
    return receipts(HOLD, lambda c, arm, rep: 40 if arm == "reference" else 50)


def assert_invalid(recs, code, m=None):
    res = holdout(recs, m)
    assert res["status"] == "invalid"
    assert code in codes(res)
    assert "sign_flip_p" not in res["public_summary"]
    assert "overall_difference" not in res["public_summary"]
    return res


def test_missing_cell_invalid():
    res = assert_invalid(good()[:-1], "missing_cell")
    assert res["reasons"][0]["detail"]["case_id"] == "h7"


def test_duplicate_cell_and_duplicate_receipt_id_invalid():
    recs = good()
    recs.append(copy.deepcopy(recs[0]))
    res = assert_invalid(recs, "duplicate_cell")
    assert "duplicate_run_receipt_id" in codes(res)
    recs = good()
    recs[1]["run_receipt_id"] = recs[0]["run_receipt_id"]
    assert_invalid(recs, "duplicate_run_receipt_id")


def test_unknown_case_arm_and_repetition_invalid():
    for field, value in (("case_id", "zzz"), ("arm", "third"),
                         ("repetition", 4), ("repetition", True)):
        recs = good()
        recs[0][field] = value
        assert_invalid(recs, "unexpected_cell")


def test_train_receipts_not_mixed_into_holdout():
    assert_invalid(good() + receipts(TRAIN[:1], lambda *_: 50),
                   "wrong_stage_case")
    res = po.evaluate_matrix(
        manifest(), receipts(TRAIN, lambda *_: 1) + good(), stage="train")
    assert res["status"] == "invalid" and "wrong_stage_case" in codes(res)


def test_frozen_identity_mismatch_invalid():
    for field in ("bundle_id", "runtime_id", "checker_id", "input_id"):
        recs = good()
        recs[3][field] = "other"
        res = assert_invalid(recs, "identity_mismatch")
        assert res["reasons"][0]["detail"]["field"] == field


def test_accounting_and_outcome_not_settled_invalid():
    for accounting in ("unsettled", "unknown", "infrastructure_error", None):
        recs = good()
        recs[0]["accounting"] = accounting
        assert_invalid(recs, "accounting_not_settled")
    for outcome in ("infrastructure_error", "scorer_error", "unknown_effect",
                    "missing_output", None):
        recs = good()
        recs[0]["outcome"] = outcome
        assert_invalid(recs, "invalid_outcome")


def test_bad_scores_invalid_not_zeroed():
    for bad in (True, math.nan, math.inf, -math.inf, -0.1, 100.01, "50", None):
        recs = good()
        recs[0]["score"] = bad
        assert_invalid(recs, "invalid_score")
    recs = good()
    recs[0].update(outcome="task_failure", score=5)
    assert_invalid(recs, "invalid_score")


def test_malformed_inputs_never_raise():
    assert_invalid("nope", "receipts_invalid")
    assert_invalid([1], "malformed_receipt")
    r = good()
    del r[0]["accounting"]
    assert_invalid(r, "malformed_receipt")
    for key, value in (("holdout_case_ids", HOLD[:7]), ("repetitions", 5),
                       ("family_inventory", ["alpha"])):
        bad_manifest = manifest()
        bad_manifest[key] = value
        assert_invalid(good(), "manifest_invalid", bad_manifest)
    assert po.evaluate_matrix(None, good(), stage="holdout")["status"] == "invalid"
    assert po.evaluate_matrix(manifest(), good(), stage="x")["status"] == "invalid"


def test_invalid_preserved_and_private_text_not_in_public_summary():
    recs = good()
    recs[0]["grader_text"] = "SECRET-GOLD"
    recs[1]["accounting"] = "unknown"
    res = holdout(recs)
    assert res["status"] == "invalid"
    assert res["private_receipts"] == recs
    assert "SECRET-GOLD" not in repr(res["public_summary"])
    ok = good()
    ok[0]["grader_text"] = "SECRET-GOLD"
    res = holdout(ok)
    assert res["status"] == "eligible"
    assert res["private_receipts"][0]["grader_text"] == "SECRET-GOLD"
    assert "SECRET-GOLD" not in repr(res["public_summary"])
    assert "SECRET-GOLD" not in repr(res["reasons"])
