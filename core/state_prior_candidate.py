"""Visible account of a node's previous candidate at fresh-attempt admission.

A fresh attempt of a node builds on the base it selected (an explicit base, or
the ordinary default selection that the isolation layer resolves from the source
HEAD only when it provisions the worktree). Nothing forces that base to contain
the previous attempt's candidate commit, and a deliberate fresh rederivation is
legitimate. What was missing is that the exclusion was silent. This module only
OBSERVES: it never refuses, adopts, merges or clears anything.

The account is judged from actual Git ancestry in the bound source, through the
closed Git profile (no hooks, no repository-configured programs). It never
infers containment from node status or names. It also never GUESSES the selected
base: a base that is not yet materialized (ordinary no-base selection before the
worktree exists) or that was never declared (an external attempt without a base)
is reported as ``unknown`` with the reason, never judged against a source HEAD
read earlier or elsewhere. Whenever the identities cannot be resolved or Git
cannot answer, the state is ``unknown`` with the reason, never ``included`` or
silently absent. No previous Git candidate -> no account at all.

Nothing here may run inside a State write transaction: ``account`` shells out to
Git. Callers build the value first and only then persist it.
"""
from __future__ import annotations

import re
import subprocess

from core import closed_git

VERSION = 1
INCLUDED, EXCLUDED, UNKNOWN = "included", "excluded", "unknown"
# base_origin values: the director's exact request, the base the worktree was
# really provisioned at (read from the isolation receipt), or none known.
REQUESTED, PROVISIONED, UNRESOLVED = "requested_base", "provisioned_base", "unresolved"
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_GIT_TIMEOUT = 20


def latest_prior(conn, project_id, node_key, before_attempt_id=None):
    """The most recent EARLIER same-node attempt that retained a Git-commit
    artifact (a 40-hex ref). Non-code 64-hex output digests have no ancestry,
    and a node with no earlier candidate has nothing to account for."""
    sql = ("SELECT attempt_id,status,artifact_ref FROM state_attempts "
           "WHERE project_id=? AND node_key=? AND artifact_ref IS NOT NULL AND length(artifact_ref)=40")
    args = [project_id, node_key]
    if before_attempt_id is not None:
        sql += " AND seq < (SELECT seq FROM state_attempts WHERE attempt_id=?)"
        args.append(before_attempt_id)
    row = conn.execute(sql + " ORDER BY seq DESC LIMIT 1", tuple(args)).fetchone()
    return dict(row) if row else None


def _account(prior, base, origin, state, reason):
    return {"version": VERSION, "prior_attempt_id": prior["attempt_id"],
            "prior_attempt_status": prior["status"], "prior_artifact": prior["artifact_ref"],
            "selected_base": base, "base_origin": origin, "state": state, "reason": reason}


def unresolved(prior, reason, *, pending=False, base=None, origin=UNRESOLVED):
    """An explicit ``unknown`` account that runs no Git. ``pending`` marks one
    that a later real provision receipt may reconcile (SkillFlow only)."""
    value = _account(prior, base, origin, UNKNOWN, reason)
    if pending:
        value["pending_base"] = True
    return value


def needs_receipt(value, actual_base):
    """Whether ``value`` must be re-derived against the provisioned base."""
    return bool(value.get("pending_base")) or value.get("selected_base") != actual_base


def account(prior, source, base, origin):
    """Observed relation between ``prior``'s candidate and the selected base.

    ``base`` is the exact commit this attempt builds on. With no known base the
    answer is ``unknown``: the source HEAD is deliberately NOT substituted, as
    it may differ from what is later provisioned. Runs Git; never raises."""
    try:
        if not isinstance(prior["artifact_ref"], str) or not _COMMIT.match(prior["artifact_ref"]):
            return _account(prior, base, origin, UNKNOWN, "previous candidate artifact is not an exact Git commit")
        if base is None:
            return _account(prior, None, UNRESOLVED, UNKNOWN,
                            "no selected base is known; it is not inferred from the source HEAD")
        if not source:
            return _account(prior, base, origin, UNKNOWN, "no bound source repository to judge ancestry in")
        if not isinstance(base, str) or not _COMMIT.match(base):
            return _account(prior, base, origin, UNKNOWN, "selected base is not an exact Git commit")
        if closed_git.resolve_commit(source, prior["artifact_ref"]) != prior["artifact_ref"]:
            return _account(prior, base, origin, UNKNOWN,
                            "previous candidate commit is unavailable in the source repository")
        if closed_git.resolve_commit(source, base) != base:
            return _account(prior, base, origin, UNKNOWN, "selected base is not a commit in the source repository")
        result = closed_git.run(source, "merge-base", "--is-ancestor", prior["artifact_ref"], base,
                                timeout=_GIT_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - an unanswerable question is "unknown", never a refusal
        kind = type(exc).__name__ if isinstance(exc, (OSError, subprocess.SubprocessError,
                                                      closed_git.GitRefused)) else "unexpected " + type(exc).__name__
        return _account(prior, base, origin, UNKNOWN, f"Git could not answer: {kind}")
    if result.returncode == 0:
        return _account(prior, base, origin, INCLUDED, "previous candidate is an ancestor of the selected base")
    if result.returncode == 1:
        return _account(prior, base, origin, EXCLUDED,
                        "previous candidate is NOT an ancestor of the selected base; this attempt will not build on it")
    return _account(prior, base, origin, UNKNOWN, f"Git ancestry check failed (exit {result.returncode})")


def event_type(value):
    """Included is routine (quiet); excluded and unknown need a director's eye."""
    return "prior_candidate_included" if value["state"] == INCLUDED else "prior_candidate_unreconciled"


def emit(store, conn, project_id, node_key, attempt_id, value, reconciled=False):
    payload = {"attempt_id": attempt_id, "prior_candidate": value}
    if reconciled:
        payload["reconciled_from_provision_receipt"] = True
    store._event(conn, project_id, node_key, event_type(value), payload)

