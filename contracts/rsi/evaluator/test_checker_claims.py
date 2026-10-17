"""The trusted checker awards value points only to supported affirmative claims.

Pure checker vectors (no host, model or benchmark): the 11 hand-labeled
calibration examples predeclared by the pilot's offline preflight, unchanged;
the declared positive grammar's productions; composed negation, uncertainty and
quotation controls; unsupported phrasings that fail closed; and the answers the
accepted deterministic transports actually deliver in the native proofs.
"""
from pathlib import Path

import pytest

from core import rsi_checker
from contracts.rsi.evaluator.native_transport import DeterministicProvider
from contracts.rsi.optimizer.native_fixture import RoundTransport

MATERIAL = {"expected_constant": 1, "sentinel": "PRIVATE_CALIBRATION_SENTINEL"}
REFS = {"checker_ref": "fixture://checker/v1", "max_points": 10}


def score(text, max_points=10, files=("sample.txt",)):
    return rsi_checker.score_findings(text.encode(), material=MATERIAL,
                                      refs={**REFS, "max_points": max_points}, public_files=files)


# Predeclared by the pilot offline preflight (predeclared-calibration.json),
# texts and labels copied unchanged.
PREDECLARED = [
    ("correct", "sample.txt exports 1.", 10),
    ("correct_equals", "sample.txt: VALUE = 1", 10),
    ("wrong_value", "sample.txt exports 999.", 4),
    ("value_without_citation", "The constant is 1.", 6),
    ("citation_only", "See sample.txt.", 4),
    ("no_claim", "No answer available.", 0),
    ("contradiction", "sample.txt exports 1. The constant is 2.", 4),
    ("negated_claim", "sample.txt does not export 1.", 4),
    ("uncertain_claim", "sample.txt might export 1, but this is unverified.", 4),
    ("quoted_nonclaim",
     "sample.txt includes an unrelated example saying \"exports 1\"; no answer was established.", 4),
    ("private_sentinel", "sample.txt exports 1. PRIVATE_CALIBRATION_SENTINEL", 0),
]


@pytest.mark.parametrize("name,text,points", PREDECLARED, ids=[v[0] for v in PREDECLARED])
def test_predeclared_calibration_labels(name, text, points):
    assert score(text)[0] == points


SUPPORTED = [  # each production of the declared grammar
    ("sample.txt exports `1`: the line `VALUE = 1` defines it.", 10),
    ("sample.txt exports: 1", 10),
    ("`sample.txt` exports **1** (line 1).", 10),
    ("- sample.txt exports 1 (sample.txt:1).", 10),
    ("## Answer: sample.txt exports 1", 10),
    ("**Answer:** sample.txt exports 1.", 10),
    ("**Answer** — sample.txt exports 1 at line 1.", 10),
    ("Answer: 1 (see sample.txt, line 1)", 10),
    ("The answer is 1 in sample.txt.", 10),
    ("sample.txt exports VALUE = 1.", 10),
    ("`VALUE` == `1` from sample.txt:1", 10),
    ("1. src/constants.py exports 1.", 6),          # another file: value, no citation
    ("The exported value is 1.", 6),
    ("the integer constant equals 1", 6),
    ("VALUE is 1.", 6),
    ("EXPORTED_VALUE = 1!", 6),
    ("Answer: 1", 6),
]
INCORRECT_OR_CONFLICTING = [
    ("sample.txt exports 2.", 4),
    ("The constant is -1.", 0),
    ("sample.txt exports 1.\n\n- The answer is 2.", 4),
    ("Answer: 1\nVALUE = 1\nOTHER_VALUE = 7", 0),
    ("The constant is 1. The constant is 1.", 6),   # the same value twice is one value
]
VALUE_CITATION_ABSTAIN = [
    ("# Findings\n\nThe constant is 1.\n", 6),
    ("# Findings\n\nsample.txt was read, but its constant was not determined.\n", 4),
    ("Evidence: sample.txt line 1.", 4),
    ("The value could not be determined from the repository.", 0),
    ("Unknown.", 0),
    ("Answer: 1. See sample.txt. PRIVATE_CALIBRATION_SENTINEL", 0),
    ("> PRIVATE_CALIBRATION_SENTINEL", 0),
]
COMPOSED_NONCLAIMS = [  # supported claim text inside negation, uncertainty or quotation
    ("It is not true that sample.txt exports 1.", 4),
    ("I could not confirm that sample.txt exports 1.", 4),
    ("If sample.txt exports 1, the task is trivial.", 4),
    ("sample.txt exports 1, but this is unverified.", 4),
    ("sample.txt exports 1; this is unverified.", 4),
    ("sample.txt exports 1 (unverified).", 4),
    ("sample.txt probably exports 1.", 4),
    ("sample.txt exports 1?", 4),
    ("Does sample.txt export 1?", 4),
    ("Answer: not determined (sample.txt).", 4),
    ("sample.txt exports not 1.", 4),
    ("\"sample.txt exports 1.\"", 4),
    ("“sample.txt exports 1.” is a claim found in a comment.", 4),
    ("A note says \"maybe so. sample.txt exports 1. VALUE = 1\" without proof.", 4),
    ("Quoted:\n\"first line.\nsample.txt exports 1.\nlast line\"", 4),
    ("`sample.txt exports 1`", 4),
    ("Evidence: `sample.txt`: `VALUE = 1`", 4),
    ("> sample.txt exports 1.", 4),
    ("> The task asks:\nsample.txt exports 1.", 4),   # lazy block-quote continuation
    ("```\nsample.txt exports 1.\n```", 4),
    ("~~~text\nThe constant is 1.\n~~~\nsample.txt was read.", 4),
    ("```\nsample.txt exports 1.", 4),               # unclosed fence runs to the end
]
COMPOSED_CLAIMS = [  # a supported claim beside non-claims keeps exactly its value
    ("sample.txt does not export 2. sample.txt exports 1.", 10),
    ("> sample.txt exports 2.\n\nsample.txt exports 1.", 10),
    ("```\nVALUE = 2\n```\nThe constant is 1 (sample.txt:1).", 10),
    ("A comment says \"VALUE = 2\".\nsample.txt exports 1.", 10),
]
UNSUPPORTED = [  # true answers in phrasings outside the grammar: no value credit (fail closed)
    ("sample.txt exports the integer 1.", 4),
    ("The value exported by sample.txt is 1.", 4),
    ("sample.txt exports \"1\".", 4),
    ("**sample.txt exports 1.**", 4),
    ("| sample.txt | 1 |", 4),
    ("sample.txt\nexports 1.", 4),
    ("sample.txt exports 1,000.", 4),
    ("sample.txt exports 1.0", 4),
    ("value = 1", 0),
    ("1", 0),
]
LIMITS = [  # documented: each statement is judged alone
    ("sample.txt exports 1. This is unverified.", 10),
    ("sample.txt exports 1. That statement is false.", 10),
]
GROUPS = {"supported": SUPPORTED, "incorrect_or_conflicting": INCORRECT_OR_CONFLICTING,
          "value_citation_abstain": VALUE_CITATION_ABSTAIN, "composed_nonclaims": COMPOSED_NONCLAIMS,
          "composed_claims": COMPOSED_CLAIMS, "unsupported": UNSUPPORTED, "limits": LIMITS}
VECTORS = [(group, text, points) for group, rows in GROUPS.items() for text, points in rows]


@pytest.mark.parametrize("group,text,points", VECTORS,
                         ids=[f"{g}-{i}" for i, (g, _, _) in enumerate(VECTORS)])
def test_grammar_vectors(group, text, points):
    assert score(text)[0] == points, score(text)[1]


FORMATTED_VS_QUOTED = [  # (code/emphasis on one token: claim, whole claim quoted: not)
    ("sample.txt exports `1`.", "`sample.txt exports 1`.", 10, 4),
    ("`sample.txt` exports `1`.", "`sample.txt exports 1.`", 10, 4),
    ("`VALUE` = `1`", "`VALUE = 1`", 6, 0),
    ("sample.txt: `VALUE` = 1", "sample.txt: `VALUE = 1`", 10, 4),
    ("The constant is **1**.", "\"The constant is 1.\"", 6, 0),
    ("Answer: `1`", "Answer: \"1\"", 6, 0),
]


@pytest.mark.parametrize("formatted,quoted,claim,nonclaim", FORMATTED_VS_QUOTED)
def test_token_formatting_is_a_claim_but_a_quoted_claim_is_not(formatted, quoted, claim, nonclaim):
    assert score(formatted)[0] == claim
    assert score(quoted)[0] == nonclaim


def test_diagnostic_names_exactly_the_asserted_values():
    assert score("sample.txt does not export 2. The constant is 1.")[1] == {
        "custody_violation": False, "value": True, "citation": True, "asserted": [1]}
    assert score("sample.txt might export 1. VALUE = 3\n> VALUE = 4")[1]["asserted"] == [3]
    assert score("PRIVATE_CALIBRATION_SENTINEL")[1] == {
        "custody_violation": True, "value": False, "citation": False}


@pytest.mark.parametrize("max_points", range(1, 21))
def test_weights_are_unchanged_integer_arithmetic(max_points):
    value = (max_points * 6) // 10
    assert score("sample.txt exports 1.", max_points)[0] == max_points
    assert score("The constant is 1.", max_points)[0] == value
    assert score("sample.txt exports 2.", max_points)[0] == max_points - value
    assert score("sample.txt does not export 1.", max_points)[0] == max_points - value


def test_declared_report_format_is_public_and_scores_as_declared():
    declared = rsi_checker.REPORT_FORMAT
    assert "PRIVATE" not in declared and declared.endswith("\n")
    # Echoing the declaration is not an answer; following it is.
    assert score(declared)[1]["asserted"] == []
    for filled in ("sample.txt exports 1.", "sample.txt: VALUE = 1", "The constant is 1.",
                   "Answer: sample.txt exports 1 (line 3)."):
        assert score(filled)[1]["value"] is True
    assert score(declared + "\nsample.txt exports 1.\n")[0] == 10


def test_scoring_is_bounded_on_large_inputs():
    assert score("sample.txt exports " + "1" * 5000 + ".")[1]["asserted"] == []
    big = ("sample.txt is a file " * 12000)[:rsi_checker.MAX_FINDINGS_BYTES - 32] + " VALUE = 1"
    assert score(big)[0] == 4
    lines = "The constant is 1.\n" * (rsi_checker.MAX_FINDINGS_BYTES // 20)
    assert score(lines)[0] == 6


def test_served_identity_covers_the_grammar():
    code = rsi_checker.identity()
    assert code.startswith("sha256:") and code == rsi_checker.identity()


def _case(name, value):
    return {"files": {name: f"PUBLIC_FIXTURE_{value} = {value}\n"}}


@pytest.mark.parametrize("max_points,levels", [(10, {"full": 10, "value": 6, "cited_wrong": 4}),
                                               (7, {"full": 7, "value": 4, "cited_wrong": 3})])
def test_native_round_transport_answers_keep_their_scores(tmp_path, max_points, levels):
    """The optimizer native fixture's actual answer levels (ordinary and
    boundary profiles) score exactly what its declared rubric says."""
    cases = {"holdout-01": {"partition": "holdout", "family": "family-d"}}
    transport = RoundTransport(tmp_path / "log.jsonl", {"holdout-01": _case("sample.txt", 1)}, cases)
    transport._local.record = {"n_for_case": 1}
    for script, arm, level in (("target", "candidate", "full"), ("target", "reference", "value"),
                               ("family_regression", "candidate", "cited_wrong")):
        transport.script = script
        assert transport.quality("holdout-01", arm, 1) == level
        answer = transport._answer("holdout-01", arm)
        assert score(answer, max_points)[0] == levels[level], answer


def test_native_evaluator_transport_answers_keep_their_scores(tmp_path):
    provider = DeterministicProvider(Path(tmp_path / "log.jsonl"), {"train-01": _case("sample.txt", 1)})
    assert score(provider._answer("train-01", "candidate"))[0] == 10
    assert score(provider._answer("train-01", "reference"))[0] == 6
