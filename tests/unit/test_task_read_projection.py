# Source-only unit test (UNRUN — no test execution in this phase).
#
# The private-body boundary on the REST task read surfaces: an identity that
# may not read private records (anonymous via the tunnel, a non-writer) must
# never receive Task.prompt, Task.last_error or owner_email from
#   GET /api/tasks, GET /api/tasks/{id}, GET /api/projects/{id}/tasks
# while public status/progress fields stay available, and a trusted
# writer/admin (may_read_private -> True) keeps the complete row.

from api import authz
from api.authz import execution_progress


FULL_ROW = {
    "id": 7,
    "project_id": "demo",
    "prompt": "SECRET EXECUTION PROMPT BODY",
    "last_error": "SECRET TRACE ERROR",
    "owner_email": "someone@example.com",
    "status": "running",
    "name": "demo",
    "created_at": "2026-10-07T00:00:00Z",
    "current_step": "1_2",
}


def test_unprivileged_identity_gets_public_fields_only():
    """Denied identity: prompt / last_error / owner_email are stripped,
    public status and progress fields survive."""
    class _Req:
        app = type("A", (), {"state": type("S", (), {"_test_mode": False})()})()
        headers = {}  # no Cf-Ray, no Access JWT, no admin token

    authz.cf_access.is_configured = lambda: True  # gate active: real boundary
    out = execution_progress(_Req(), dict(FULL_ROW))
    assert "prompt" not in out
    assert "last_error" not in out
    assert "owner_email" not in out
    assert out["id"] == 7
    assert out["status"] == "running"


def test_trusted_identity_keeps_complete_body():
    """Test mode (trusted local writer/admin) returns the row unchanged —
    the boundary narrows reads, never the trusted writer's complete body."""
    class _Req:
        app = type("A", (), {"state": type("S", (), {"_test_mode": True})()})()

    out = execution_progress(_Req(), dict(FULL_ROW))
    assert out == FULL_ROW
