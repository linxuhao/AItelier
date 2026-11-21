"""Step 23's press witness, backend half (design-steward 7.7.16 end, platform director 418
item 4): `X-AItelier-Read-As: public` must produce an ANONYMOUS answer, measured on the real
FastAPI app with the write gate ON, not argued.

1. The steward's own token WITH the header asks for a private project's overview, and for a
   project that does not exist: both answers are byte for byte what a caller with no
   credential at all gets for the same request (the PUBLIC_READS refusal, 403 "This State DAG
   record is not available." - no existence oracle).
2. The same request for the public project succeeds (200, with content).
3. The very next request of the same token without the header is the writer again (200 for
   the private project): the header is honoured per request and only narrows.
4. A write with the header is refused public_view_read_only; the family memory pair with the
   header is refused unless the recorded turn audience is a bound group.

The plugin half (a group turn's tools send the header; the family memory marker never reaches
the owner's next turn or a compaction; no snapshot in a group turn) is the node suite's
integrations/dsh-director/test/group-turns.test.mjs, run by the gate's node rows."""
from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from api import authz
from api.dependencies import get_db_manager
from api.main import app
from core import cf_access, drivers
from core.state_service import StateService
from tests.unit.test_steward_channels import OWNER, world  # noqa: F401

HEADER = {"X-AItelier-Read-As": "public"}


@pytest.fixture
def http(world, monkeypatch):
    import api.main as main
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "test-aud")
    monkeypatch.setattr(authz, "WRITERS", {OWNER})
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers",
                        lambda headers, cookies=None: headers.get("X-Test-Email"))
    monkeypatch.setattr(authz, "driver_registry", lambda: world.registry)
    monkeypatch.setattr(main, "_ALLOW_EXTERNAL", True)
    monkeypatch.setattr(drivers, "read_pepper", lambda: "steward-channel-pepper-" * 3)
    writer = StateService(world.db, actor="fixture", project_read_trusted=True)
    writer.store.create_project("wuxia-myth", "武虾传奇")
    writer.store.add_nodes("wuxia-myth", [{"key": "a", "goal": "public goal", "acceptance": [
        {"id": "c", "kind": "test", "description": "criterion"}]}])
    writer.open_project("wuxia-myth")
    writer.store.create_project("family-ledger", "家庭账本")
    app.dependency_overrides[get_db_manager] = lambda: world.db
    try:
        with TestClient(app) as client:
            monkeypatch.setattr(app.state, "_test_mode", False, raising=False)
            monkeypatch.setattr(app.state, "dsh_drivers", world.runtime, raising=False)
            assert authz.gate_enabled()
            yield client
    finally:
        app.dependency_overrides.pop(get_db_manager, None)


def _record(name, response):
    print(f"WITNESS {name} status={response.status_code} sha256={hashlib.sha256(response.content).hexdigest()} "
          f"bytes={len(response.content)}")
    return response


@pytest.mark.parametrize("project_id", ["family-ledger", "no-such-project"])
def test_the_header_makes_the_stewards_answer_byte_identical_to_an_unauthenticated_readers(world, http, project_id):
    steward = {"X-AItelier-Driver-Token": world.tokens["steward"], **HEADER}
    body = {"project_id": project_id}
    grouped = _record(f"steward+header {project_id}", http.post("/api/state/query/project_overview", json=body,
                                                                 headers=steward))
    nobody = _record(f"no credential {project_id}", http.post("/api/state/query/project_overview", json=body))
    assert grouped.status_code == nobody.status_code == 403
    assert grouped.content == nobody.content == b'{"detail":"This State DAG record is not available."}'
    # The same over the GET route the dashboard uses.
    get_grouped = http.get(f"/api/state/projects/{project_id}/overview", headers=steward)
    get_nobody = http.get(f"/api/state/projects/{project_id}/overview")
    assert get_grouped.status_code == 403 and get_grouped.content == get_nobody.content


def test_the_public_project_succeeds_and_the_next_request_without_the_header_is_the_writer(world, http):
    token = {"X-AItelier-Driver-Token": world.tokens["steward"]}
    public = _record("steward+header wuxia-myth", http.post("/api/state/query/project_overview",
                                                            json={"project_id": "wuxia-myth"}, headers={**token, **HEADER}))
    assert public.status_code == 200 and public.json()["project"]["project_id"] == "wuxia-myth"
    catalog = http.post("/api/state/query/project_catalog", json={}, headers={**token, **HEADER})
    assert catalog.status_code == 200 and "family-ledger" not in catalog.text and "wuxia-myth" in catalog.text
    private = _record("steward family-ledger", http.post("/api/state/query/project_overview",
                                                         json={"project_id": "family-ledger"}, headers=token))
    assert private.status_code == 200, private.text


def test_with_the_header_writes_are_refused_and_the_family_memory_needs_a_group_turn(world, http):
    token = {"X-AItelier-Driver-Token": world.tokens["steward"]}
    write = http.post("/api/state/commands/send_user_message", headers={**token, **HEADER},
                      json={"project_id": "aitelier-home", "subject": "s", "body": "b"})
    assert write.status_code == 422 and write.json()["detail"].startswith("public_view_read_only"), write.text
    family = http.post("/api/state/commands/note_family_memory", headers={**token, **HEADER},
                       json={"request_key": "k", "text": "x"})
    assert family.status_code == 422 and "family_memory_group_turn_only" in family.text, family.text
    plain = http.post("/api/state/commands/note_family_memory", headers=token, json={"request_key": "k", "text": "x"})
    assert plain.status_code == 422 and "family_memory_group_turn_only" in plain.text, plain.text
    questions = http.post("/api/state/query/list_user_questions", headers={**token, **HEADER},
                          json={"project_id": "aitelier-home"})
    assert questions.status_code == 403, questions.text
