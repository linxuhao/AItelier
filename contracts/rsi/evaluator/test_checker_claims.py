"""The trusted checker awards value points only to supported affirmative reports.

Pure checker vectors (no host, model or benchmark): the 11 hand-labeled
calibration examples predeclared by the pilot's offline preflight, unchanged;
the declared whole-report protocol's productions; composed negation,
uncertainty and quotation controls; unsupported phrasings and unsupported
complete reports that fail closed; metamorphic compositions of supported
reports with arbitrary unsupported text and with protocol-only lines; and the
answers the accepted deterministic transports actually deliver in the native
proofs.
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


SUPPORTED = [  # each production of the declared protocol
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
    ("   sample.txt exports 1.  ", 10),             # up to 3 leading spaces is not code
]
COMPLETE_REPORTS = [  # whole reports made only of protocol lines keep their value
    ("# Findings\n\nsample.txt exports `1`: the line `PUBLIC_FIXTURE_x = 1` defines it.\n", 10),
    ("# Findings\n\nThe constant is 1.\n", 6),
    ("The constant is 1. See sample.txt.", 10),
    ("Answer: 1\nSee sample.txt:1.", 10),
    ("# Answer\n\n- sample.txt exports 1 (line 1).\n- See sample.txt.\n", 10),
    ("## Findings\n\nThe constant is 1.\n\n## Evidence\n\nSource: sample.txt, line 1.\n", 10),
    ("**Answer:** `sample.txt` exports **1**.\n\nEvidence: `sample.txt` line 1", 10),
    ("### Result:\n\n1. VALUE = 1\n2. Sources: sample.txt; src/other.py", 10),
    ("\n\nsample.txt exports 1.\n\n\n", 10),
    ("sample.txt exports 1.\r\nSee sample.txt (line 1).\r\n", 10),
]
INCORRECT_OR_CONFLICTING = [
    ("sample.txt exports 2.", 4),
    ("The constant is -1.", 0),
    ("sample.txt exports 1.\n\n- The answer is 2.", 4),
    ("Answer: 1\nVALUE = 1\nOTHER_VALUE = 7", 0),
    ("The constant is 1. The constant is 1.", 6),   # the same value twice is one value
    ("# Findings\n\nsample.txt exports 1.\n\n# Answer\n\nThe constant is 2.\n", 4),
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
# Independent review of b8e3429 (24 PASS / 4 FAIL): each of these scored 10 there.
INDEPENDENT_FAILURES = [
    ("indented_code_quote_four_spaces", "    sample.txt exports 1.\n", 4),
    ("indented_code_quote_tab", "\tsample.txt exports 1.\n", 4),
    ("cross_sentence_unverified", "sample.txt exports 1. This is unverified.", 4),
    ("cross_sentence_retracted", "sample.txt exports 1. That statement is false.", 4),
]
QUOTATION_BESIDE_A_CLAIM = [  # quotation is not a protocol line: the report gives no answer
    ("> sample.txt exports 2.\n\nsample.txt exports 1.", 4),
    ("```\nVALUE = 2\n```\nThe constant is 1 (sample.txt:1).", 4),
    ("A comment says \"VALUE = 2\".\nsample.txt exports 1.", 4),
    ("\"sample.txt exports 2.\"\nsample.txt exports 1.", 4),
    ("sample.txt does not export 2. sample.txt exports 1.", 4),
    ("sample.txt exports 1.\n\n```text\nPUBLIC_FIXTURE_x = 1\n```", 4),
]
DEFINING_LINE = [  # the quoted defining line is read, not stripped: it must be an assignment
    ("sample.txt exports 1: the line `value = 1` defines it.", 10),
    ("sample.txt exports 1: the line `VALUE = 2` defines it.", 4),   # it asserts 2 as well
    ("sample.txt exports 1: the line `not verified` defines it.", 4),
    ("sample.txt exports 1: the line `export const VALUE = 1;` defines it.", 4),
    ("sample.txt exports 1: the line `VALUE = 1` defines it. Or not.", 4),
]
UNSUPPORTED = [  # true answers in phrasings outside the protocol: no value credit (fail closed)
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
    ("I inspected the repository.\nsample.txt exports 1.", 4),
    ("Findings\n========\n\nsample.txt exports 1.", 4),
    ("# Final findings\n\nsample.txt exports 1.", 4),
    ("-     sample.txt exports 1.", 4),                # list item holding indented code
    ("-\tsample.txt exports 1.", 4),
    ("sample.txt exports 1 The constant is 1", 4),     # statements need a terminator between
]
GROUPS = {"supported": SUPPORTED, "complete_reports": COMPLETE_REPORTS,
          "incorrect_or_conflicting": INCORRECT_OR_CONFLICTING,
          "value_citation_abstain": VALUE_CITATION_ABSTAIN, "composed_nonclaims": COMPOSED_NONCLAIMS,
          "quotation_beside_a_claim": QUOTATION_BESIDE_A_CLAIM, "defining_line": DEFINING_LINE,
          "unsupported": UNSUPPORTED}
VECTORS = [(group, text, points) for group, rows in GROUPS.items() for text, points in rows]


@pytest.mark.parametrize("group,text,points", VECTORS,
                         ids=[f"{g}-{i}" for i, (g, _, _) in enumerate(VECTORS)])
def test_protocol_vectors(group, text, points):
    assert score(text)[0] == points, score(text)[1]


@pytest.mark.parametrize("name,text,points", INDEPENDENT_FAILURES, ids=[v[0] for v in INDEPENDENT_FAILURES])
def test_independent_review_failures_give_no_value(name, text, points):
    points_, diagnostic = score(text)
    assert (points_, diagnostic["value"], diagnostic["asserted"]) == (points, False, []), diagnostic


# Metamorphic controls. A supported report composed with any text the protocol
# does not declare, anywhere and in any Markdown role, gives no answer: value
# false, nothing asserted, citation exactly as the unchanged citation rule
# finds it. No prose below is recognized by content; most of it is neutral.
REPORTS = [
    "sample.txt exports 1.",
    "The constant is 1.",
    "# Findings\n\nsample.txt exports `1`: the line `VALUE = 1` defines it.\n",
    "Answer: 1\nSee sample.txt.",
    "- sample.txt: VALUE = 1\n- Source: sample.txt, line 1.",
]
PROSE = [
    "This is unverified.", "That statement is false.", "Ignore the answer above.",
    "I read the repository first.", "Note: the value may differ on other branches.",
    "<!-- draft -->", "lorem ipsum", "Retracted", "(guess)", "Confidence: low", "---",
    "The constant is not 1.", "See below.", "Done.", "Answer withdrawn.",
]


def _compositions(report, prose):
    body = report.rstrip("\n")
    yield "before", f"{prose}\n{report}"
    yield "after", f"{report}\n{prose}"
    yield "same_line", f"{body} {prose}"
    yield "between", f"{body}\n\n{prose}\n\n{body}"
    yield "under_heading", f"# Findings\n\n{prose}\n\n{report}"
    yield "as_heading", f"# {prose}\n\n{report}"
    yield "list_item", f"{report}\n\n- {prose}"
    yield "list_continuation", f"- {body.splitlines()[-1]}\n  {prose}"
    yield "block_quote", f"> {prose}\n\n{report}"
    yield "fenced_code", f"```\n{prose}\n```\n{report}"
    yield "indented_code", f"{report}\n\n    {prose}\n"
    yield "inline_code", f"{report}\n`{prose}`"
    yield "double_quoted", f"{report}\n\"{prose}\""
    yield "line_separator", f"{body} {prose}"


METAMORPHIC = [(r, p, kind, text) for r in REPORTS for p in PROSE for kind, text in _compositions(r, p)]


@pytest.mark.parametrize("report,prose,kind,text", METAMORPHIC,
                         ids=[f"r{REPORTS.index(r)}-p{PROSE.index(p)}-{k}" for r, p, k, _ in METAMORPHIC])
def test_unsupported_text_anywhere_withdraws_the_value(report, prose, kind, text):
    assert score(report)[1]["value"] is True
    points, diagnostic = score(text)
    assert (diagnostic["value"], diagnostic["asserted"]) == (False, []), (kind, diagnostic)
    assert diagnostic["citation"] == ("sample.txt" in report)
    assert points == (4 if diagnostic["citation"] else 0)


QUOTED_FORMS = [  # the whole claim quoted or set as code is no answer, at any depth
    "    {c}", "\t{c}", "  \t{c}", "        {c}", "    {c}", "```\n{c}\n```", "~~~text\n{c}\n~~~",
    "> {c}", ">{c}", "`{c}`", "\"{c}\"", "“{c}”", "**{c}**", "# Findings\n\n    {c}\n",
    "- See sample.txt.\n\n      {c}", "# Evidence\n\n    {c}",
]
CLAIMS = ["sample.txt exports 1.", "sample.txt: VALUE = 1", "The constant is 1 (sample.txt:1)."]


@pytest.mark.parametrize("form", QUOTED_FORMS)
@pytest.mark.parametrize("claim", CLAIMS)
def test_a_quoted_or_code_claim_is_no_answer(form, claim):
    assert score(claim)[0] == 10
    points, diagnostic = score(form.format(c=claim))
    assert (points, diagnostic["value"], diagnostic["asserted"]) == (4, False, []), diagnostic


PROTOCOL_ONLY = [  # adding declared protocol lines keeps the answer
    lambda r: f"\n\n{r}\n\n",
    lambda r: f"# Findings\n\n{r}",
    lambda r: f"## Answer\n\n{r}\n\n## Evidence\n\nSee sample.txt.",
    lambda r: f"{r}\nSource: sample.txt (line 1).",
    lambda r: "\n".join("- " + line if line.strip() and not line.startswith(("#", "- ")) else line
                        for line in r.splitlines()),
]


@pytest.mark.parametrize("add", range(len(PROTOCOL_ONLY)))
@pytest.mark.parametrize("report", REPORTS)
def test_protocol_lines_keep_the_value(report, add):
    composed = PROTOCOL_ONLY[add](report)
    points, diagnostic = score(composed)
    assert (diagnostic["value"], diagnostic["asserted"]) == (True, [1]), (composed, diagnostic)
    assert points == (10 if diagnostic["citation"] else 6)


FORMATTED_VS_QUOTED = [  # (code/emphasis on one token: claim, whole claim quoted: not)
    ("sample.txt exports `1`.", "`sample.txt exports 1`.", 10, 4),
    ("`sample.txt` exports `1`.", "`sample.txt exports 1.`", 10, 4),
    ("`VALUE` = `1`", "`VALUE = 1`", 6, 0),
    ("sample.txt: `VALUE` = 1", "sample.txt: `VALUE = 1`", 10, 4),
    ("The constant is **1**.", "\"The constant is 1.\"", 6, 0),
    ("Answer: `1`", "Answer: \"1\"", 6, 0),
    ("`sample.txt` exports 1.", "`sample.txt exports 1.", 10, 4),
]


@pytest.mark.parametrize("formatted,quoted,claim,nonclaim", FORMATTED_VS_QUOTED)
def test_token_formatting_is_a_claim_but_a_quoted_claim_is_not(formatted, quoted, claim, nonclaim):
    assert score(formatted)[0] == claim
    assert score(quoted)[0] == nonclaim


def test_diagnostic_names_exactly_the_asserted_values():
    assert score("The constant is 1. See sample.txt.")[1] == {
        "custody_violation": False, "value": True, "citation": True, "asserted": [1]}
    assert score("VALUE = 3\nThe constant is 1.")[1]["asserted"] == [1, 3]
    assert score("sample.txt exports 1: the line `VALUE = 2` defines it.")[1]["asserted"] == [1, 2]
    assert score("sample.txt might export 1. VALUE = 3\n> VALUE = 4")[1]["asserted"] == []
    assert score("PRIVATE_CALIBRATION_SENTINEL")[1] == {
        "custody_violation": True, "value": False, "citation": False}


@pytest.mark.parametrize("max_points", range(1, 21))
def test_weights_are_unchanged_integer_arithmetic(max_points):
    value = (max_points * 6) // 10
    assert score("sample.txt exports 1.", max_points)[0] == max_points
    assert score("The constant is 1.", max_points)[0] == value
    assert score("sample.txt exports 2.", max_points)[0] == max_points - value
    assert score("sample.txt does not export 1.", max_points)[0] == max_points - value
    assert score("sample.txt exports 1. This is unverified.", max_points)[0] == max_points - value


def test_declared_report_format_is_public_and_scores_as_declared():
    declared = rsi_checker.REPORT_FORMAT
    assert "PRIVATE" not in declared and declared.endswith("\n")
    assert "whole report" in declared
    # Echoing the declaration is not an answer, and neither is an answer next
    # to the echoed declaration; following it is.
    assert score(declared)[1]["asserted"] == []
    assert score(declared + "\nsample.txt exports 1.\n")[1]["value"] is False
    for filled in ("sample.txt exports 1.", "sample.txt: VALUE = 1", "The constant is 1.",
                   "Answer: sample.txt exports 1 (line 3)."):
        assert score(filled)[1]["value"] is True
    every_declared_line = ("# Findings\n\n1. sample.txt exports 1.\n\n# Evidence\n\n"
                           "- See sample.txt.\n- Source: sample.txt, line 3.\n")
    assert score(every_declared_line)[0] == 10


def test_scoring_is_bounded_on_large_inputs():
    assert score("sample.txt exports " + "1" * 5000 + ".")[1]["asserted"] == []
    big = ("sample.txt is a file " * 12000)[:rsi_checker.MAX_FINDINGS_BYTES - 32] + " VALUE = 1"
    assert score(big)[0] == 4
    lines = "The constant is 1.\n" * (rsi_checker.MAX_FINDINGS_BYTES // 20)
    assert score(lines)[0] == 6
    paths = "Source: " + "a/" * 60000 + "x\nSee sample.txt."
    assert score(paths)[0] == 4
    statements = "VALUE = 1. " * (rsi_checker.MAX_FINDINGS_BYTES // 12)
    assert score(statements)[0] == 6
    places = "See " + ", ".join(["a.b"] * (rsi_checker.MAX_FINDINGS_BYTES // 6)) + " x\nThe constant is 1."
    assert score(places)[0] == 0


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
