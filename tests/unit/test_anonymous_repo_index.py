"""The anonymous repo index publishes coarse status labels, never run narrative.

`execution_progress` is the one projection every anonymous project/run/repo
read goes through. Its `status` field is the run's raw operator text: the
scheduler appends a suffix (`running:<step>`, `checkpoint:<label>`,
`failed:<reason>`), and that suffix carries run ids, budget sentences, pasted
review verdicts and product-decision prose. The owner ruling: under the
anonymous branch every `status` value is a coarse label from a fixed set, with no
':' narrative suffix. The private branch returns the row unchanged.

These tests drive the REAL `execution_progress` -> `may_read_private` ->
`write_denial_reason` chain with a real Starlette request. Nothing is mocked
except the Cloudflare gate configuration, which is scoped per test with
monkeypatch.
"""

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from api import authz
from core import cf_access


ADMIN_TOKEN = "synthetic-off-tunnel-admin-token"
TEAM_DOMAIN = "team.cloudflareaccess.com"
AUD = "test-aud"

# Every status the anonymous branch may emit. Fixed on purpose: adding a label
# is a deliberate, reviewed change, not something a narrative string can do.
FIXED_LABELS = {"completed", "failed", "running", "pending", "planning", "paused",
                "checkpoint", "waiting_user_approval", "unknown"}

# The narrative a run really carries, as the scheduler writes it. None of these
# may survive into an anonymous payload.
NARRATIVE_STATUSES = [
    "running:t_impl",
    "checkpoint:Final Review — pasted verdict: APPROVE; ship the v2 pricing page",
    "failed:Budget exhausted at 40 turns; run run-7f3a9c21 stopped before migration",
    "failed:could not start run — no such config: dpe_default_v2",
]


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", TEAM_DOMAIN)
    monkeypatch.setattr(cf_access, "_AUD", AUD)
    monkeypatch.setattr(authz, "ADMIN_TOKEN", ADMIN_TOKEN)
    assert authz.gate_enabled() is True


def _request(headers=None):
    app = SimpleNamespace(state=SimpleNamespace(_test_mode=False))
    raw_headers = [(name.lower().encode("latin-1"), value.encode("latin-1"))
                   for name, value in (headers or {}).items()]
    return Request({
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/repos",
        "raw_path": b"/api/repos",
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    })


ANON = {"Cf-Ray": "anon-ray"}
PRIVATE = {"X-AItelier-Admin-Token": ADMIN_TOKEN}


def _row(status):
    return {
        "id": "run-1",
        "project_id": "p1",
        "name": "Project one",
        "status": status,
        "config_name": "dpe_default_v2",
        "owner_email": "owner@example.com",
        "repo_path": "/srv/private-repo",
        "current_project_step": "t_impl",
        "task_count": 3,
        "completed_count": 1,
    }


@pytest.mark.parametrize("status", NARRATIVE_STATUSES)
def test_anonymous_status_drops_the_narrative_suffix(gate_on, status):
    """Anonymous reader: the ':' suffix (the narrative) never reaches the payload."""
    out = authz.execution_progress(_request(ANON), _row(status))
    assert ":" not in out["status"]
    assert out["status"] in FIXED_LABELS
    for leaked in ("pasted verdict", "Budget exhausted", "run-7f3a9c21",
                   "ship the v2", "dpe_default_v2 ", "t_impl"):
        assert leaked not in out["status"]


@pytest.mark.parametrize("status,expected", [
    ("completed", "completed"),
    ("failed", "failed"),
    ("running", "running"),
    ("pending", "pending"),
    ("planning", "planning"),
    ("paused", "paused"),
    ("checkpoint", "checkpoint"),
    ("waiting_user_approval", "waiting_user_approval"),
    ("running:t_impl", "running"),
    ("checkpoint:Final Review", "checkpoint"),
    ("failed:Budget exhausted", "failed"),
])
def test_anonymous_status_is_the_coarse_label(gate_on, status, expected):
    """The label is the text before ':' when that label is in the fixed set."""
    out = authz.execution_progress(_request(ANON), _row(status))
    assert out["status"] == expected


def test_anonymous_status_outside_the_fixed_set_becomes_unknown(gate_on):
    """A value that is not a coarse label is not passed through verbatim."""
    out = authz.execution_progress(_request(ANON), _row("the operator wrote a sentence"))
    assert out["status"] == "unknown"
    assert "operator" not in out["status"]


def test_anonymous_unset_status_stays_unset(gate_on):
    """None and empty status are not invented into a label."""
    assert authz.execution_progress(_request(ANON), _row(None))["status"] is None
    assert authz.execution_progress(_request(ANON), _row(""))["status"] == ""


def test_private_reader_gets_the_row_unchanged(gate_on):
    """Private reader: the row comes back as the SAME object, narrative intact."""
    row = _row("checkpoint:Final Review — pasted verdict: APPROVE")
    before = dict(row)
    out = authz.execution_progress(_request(PRIVATE), row)
    assert out is row
    assert out == before
    assert out["status"] == "checkpoint:Final Review — pasted verdict: APPROVE"


def test_anonymous_projection_keeps_the_structural_rows(gate_on):
    """The anonymous payload still lists the same structural fields, only status is reduced."""
    row = _row("running:t_impl")
    out = authz.execution_progress(_request(ANON), row)
    assert set(out) == {"id", "project_id", "name", "status", "config_name",
                        "current_project_step", "task_count", "completed_count"}
    assert out["id"] == "run-1"
    assert out["name"] == "Project one"
    assert out["task_count"] == 3


def test_anonymous_projection_does_not_mutate_the_input_row(gate_on):
    """Reducing status must not write back into the row a caller still holds."""
    row = _row("failed:Budget exhausted at 40 turns")
    authz.execution_progress(_request(ANON), row)
    assert row["status"] == "failed:Budget exhausted at 40 turns"


def test_only_status_is_reduced_not_other_narrative_fields(gate_on):
    """Fields outside the status change are left to the existing whitelist, not rewritten."""
    row = _row("running:t_impl")
    row["latest_status"] = "running"
    out = authz.execution_progress(_request(ANON), row)
    assert out["latest_status"] == "running"
    assert out["current_project_step"] == "t_impl"


def test_public_status_labels_are_the_fixed_set():
    """The label set is exactly the one the shipped reader renders, plus the lifecycle."""
    assert authz.PUBLIC_STATUS_LABELS == {
        "completed", "failed", "running", "pending", "planning", "paused",
        "checkpoint", "waiting_user_approval"}
    assert "unknown" not in authz.PUBLIC_STATUS_LABELS
