"""Multi-driver P4: advisory review-independence marking (design/multi-driver-coop.md §7.2).

Project setting ``review_independence`` is ``off`` (default) or ``advisory``;
there is no ``required`` mode (owner decision D2). Under ``advisory``
``verify_node`` succeeds exactly as before, and the acceptance receipt's
``provenance_json`` additionally says whether any ``kind=review`` criterion's
LATEST evidence was recorded by the attempt's current owner or by any driver
that owned the attempt earlier (handoff or take-over). This is visibility, not
a refusal: the product's own acceptance text ("independent review by ...")
stays the criterion's and the evidence writer's responsibility.

Under ``off`` nothing here runs and the receipt is byte-identical to a pre-P4
receipt (the test suite proves that).
"""
from __future__ import annotations

import json

from core.state_metadata import review_independence

DRIVER_ACTOR_PREFIX = "driver:"


def evidence_drivers(evidence: dict) -> set[str]:
    """Drivers an evidence row is attributable to.

    ``reviewer`` is the AUTHENTICATED actor (``driver:<id>`` for a driver);
    ``director_identity`` is the self-declared ``<driver>`` or
    ``<driver>/<subagent label>``. A subagent of the owner reviewing the
    owner's work is still a self-review, so both are counted.
    """
    found = set()
    reviewer = evidence.get("reviewer") or ""
    if reviewer.startswith(DRIVER_ACTOR_PREFIX) and len(reviewer) > len(DRIVER_ACTOR_PREFIX):
        found.add(reviewer[len(DRIVER_ACTOR_PREFIX):])
    identity = evidence.get("director_identity") or ""
    if identity:
        found.add(identity.split("/", 1)[0])
    return found


def attempt_owners(conn, attempt: dict) -> set[str]:
    """Current owner plus every former owner recorded by a transfer event."""
    owners = set()
    if attempt.get("owner_driver_id"):
        owners.add(attempt["owner_driver_id"])
    for row in conn.execute(
            "SELECT payload_json FROM state_events WHERE project_id=? AND event_type='attempt_ownership_transferred' "
            "AND json_extract(payload_json,'$.attempt_id')=?", (attempt["project_id"], attempt["attempt_id"])):
        payload = json.loads(row[0])
        for key in ("from_driver_id", "to_driver_id"):
            if payload.get(key):
                owners.add(payload[key])
    return owners


def review_marking(conn, attempt: dict, latest: dict, required: list) -> dict | None:
    """Provenance fields for a receipt, or None when the project is ``off``.

    ``latest`` maps criterion id -> its latest evidence row (what verify
    accepts); ``required`` is the node's pinned contract.
    """
    if review_independence(conn, attempt["project_id"]) != "advisory":
        return None
    owners = attempt_owners(conn, attempt)
    flagged = sorted(c["id"] for c in required
                     if c.get("kind") == "review" and c["id"] in latest
                     and evidence_drivers(latest[c["id"]]) & owners)
    return {"review_independence": "advisory", "self_reviewed": bool(flagged),
            "self_reviewed_criteria": flagged}


def self_reviewed_count(conn, project_id) -> int:
    row = conn.execute("SELECT COUNT(*) FROM state_acceptances WHERE project_id=? "
                       "AND json_extract(provenance_json,'$.self_reviewed')=1", (project_id,)).fetchone()
    return int(row[0]) if row else 0
