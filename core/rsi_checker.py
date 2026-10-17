"""Trusted programmatic checker of the RSI evaluator pilot.

Runs only inside the trusted coordinator. It receives the COMPLETE immutable
delivered ``findings.md`` bytes and the frozen private material of one case,
treats the delivered text strictly as data (no import, eval or execution of
anything it contains) and returns integer points plus a private diagnostic
that never leaves the evaluator's private artifacts.

Frozen rubric (synthetic offline pilot: "which constant does <file> export?"):

* ``value``: 6/10 of max_points when the findings assert the expected constant
  and assert no other value for it;
* ``citation``: the remaining points when the findings name the public fixture
  file that holds the constant;
* a findings text that contains the case's private sentinel scores 0 and is
  flagged as a custody failure (the tested pipeline never receives it).

Its identity is the served code of this module (``identity()``), recorded as
the benchmark's ``checker_code``; ``package_identity`` binds that code to the
complete per-case scorer manifest (arm-wide ``checker_package``).
"""
from __future__ import annotations

import re

CHECKER_REF = "fixture://checker/v1"
MAX_FINDINGS_BYTES = 256 * 1024
_ASSERTED = re.compile(r"(?:\bis\b|=|\bequals\b|\bexports?\b)\s*[:=]?\s*`?(-?\d+)`?")


class CheckerInputError(ValueError):
    """Delivered bytes are not a scorable findings document."""


def decode_findings(data: bytes) -> str:
    if not isinstance(data, (bytes, bytearray)):
        raise CheckerInputError("findings must be bytes")
    if len(data) > MAX_FINDINGS_BYTES:
        raise CheckerInputError("findings exceed the scorable size")
    try:
        text = bytes(data).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CheckerInputError(f"findings are not UTF-8: {exc}") from exc
    if not text.strip():
        raise CheckerInputError("findings are empty")
    return text


def score_findings(data: bytes, *, material: dict, refs: dict, public_files: tuple) -> tuple[int, dict]:
    """(raw_points, private diagnostic) for one complete findings document."""
    if refs.get("checker_ref") != CHECKER_REF:
        raise CheckerInputError(f"case names checker {refs.get('checker_ref')!r}, not {CHECKER_REF}")
    maximum = refs["max_points"]
    expected = material["expected_constant"]
    if not isinstance(maximum, int) or maximum < 1 or isinstance(expected, bool) \
            or not isinstance(expected, int):
        raise CheckerInputError("frozen material is malformed")
    text = decode_findings(data)
    if material.get("sentinel") and material["sentinel"] in text:
        return 0, {"custody_violation": True, "value": False, "citation": False}
    asserted = {int(m) for m in _ASSERTED.findall(text)}
    value = asserted == {expected}
    cited = any(re.search(r"(?<![\w./-])" + re.escape(name) + r"(?![\w-])", text)
                for name in public_files)
    value_points = (maximum * 6) // 10
    points = (value_points if value else 0) + ((maximum - value_points) if cited else 0)
    return points, {"custody_violation": False, "value": value, "citation": cited,
                    "asserted": sorted(asserted)}


def identity() -> str:
    """Served-code identity of this checker (no source file is consulted)."""
    from core import pipeline_frozen_bundle as fb
    return "sha256:" + fb._served_identity(score_findings, "rsi checker")["sha256"]


def package_identity(checker_code: str, cases) -> str:
    """Arm-wide checker package: code + complete per-case scorer manifest."""
    from contracts.rsi.evaluator.contract import digest
    return digest({"checker_code": checker_code,
                   "cases": {c.case_id: {"scorer_ref_identity": c.scorer_ref_identity,
                                         "resolved_private_material": c.resolved_private_material,
                                         "max_points": c.max_points} for c in cases}})
