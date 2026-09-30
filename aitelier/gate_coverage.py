"""Structured gate coverage and its additional publication conjunct."""
from __future__ import annotations
import hashlib
import json
import re


def spec_digest(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def full_coverage(head: str, spec: dict) -> dict:
    """The default producer payload for the actual expanded full contract."""
    names = [scenario["name"] for scenario in spec["scenarios"]]
    scope = {"coverage": "full", "all_scenarios": names,
             "selected_scenarios": names.copy(), "unselected_scenarios": [],
             "fallback_full": False, "selection_basis": {
                 "head_sha": head, "spec_sha256": spec_digest(spec),
                 "method": "full_default"}}
    error = validate_coverage(scope)
    if error:
        raise ValueError(error)
    return scope


def source_scope_refusal(scope, head: str, full_spec: dict) -> str | None:
    """Additional publication conjunct bound to an independently read source."""
    error = publication_scope_refusal(scope)
    if error:
        return error
    if scope["selection_basis"]["head_sha"] != head:
        return "gate coverage head does not match the source commit"
    if scope["selection_basis"]["spec_sha256"] != spec_digest(full_spec):
        return "gate coverage contract digest does not match the source contract"
    names = [scenario["name"] for scenario in full_spec["scenarios"]]
    if scope["all_scenarios"] != names:
        return "gate coverage inventory/order does not match the source contract"
    return None


def validate_coverage(scope) -> str | None:
    """Validate the partition itself, not a filename or scenario count guess."""
    if not isinstance(scope, dict) or scope.get("coverage") not in ("full", "subset"):
        return "coverage marker is missing or invalid"
    parts = []
    for key in ("all_scenarios", "selected_scenarios", "unselected_scenarios"):
        names = scope.get(key)
        if (not isinstance(names, list) or any(not isinstance(n, str) or not n for n in names)
                or len(names) != len(set(names))):
            return f"{key} is missing, ambiguous or malformed"
        parts.append(set(names))
    all_names, selected, unselected = parts
    if not all_names or not selected or selected & unselected or selected | unselected != all_names:
        return "coverage is not a nonempty disjoint complete partition"
    for key, partition in (("selected_scenarios", selected), ("unselected_scenarios", unselected)):
        if scope[key] != [n for n in scope["all_scenarios"] if n in partition]:
            return f"{key} changes the authored execution order"
    if (scope["coverage"] == "full") != (not unselected):
        return "coverage marker disagrees with the explicit partition"
    if not isinstance(scope.get("fallback_full"), bool):
        return "fallback_full is missing or malformed"
    if scope["fallback_full"] and scope["coverage"] != "full":
        return "a fallback may not select a subset"
    if not isinstance(scope.get("selection_basis"), dict):
        return "selection_basis is missing or malformed"
    basis = scope["selection_basis"]
    for field, length in (("head_sha", 40), ("spec_sha256", 64)):
        if not isinstance(basis.get(field), str) or not re.fullmatch("[0-9a-f]{%d}" % length, basis[field]):
            return f"selection_basis {field} is missing or malformed"
    if scope["fallback_full"] and not basis.get("fallback_reason"):
        return "full fallback has no explicit reason"
    return None


def publication_scope_refusal(scope) -> str | None:
    """Additional conjunct for a release path; does not authorize by itself."""
    if isinstance(scope, dict) and scope.get("purpose") == "provisional_round_feedback":
        return "provisional feedback cannot authorize publication"
    error = validate_coverage(scope)
    if error:
        return error
    if scope["coverage"] == "subset":
        return "subset gate cannot authorize publication"
    return None
