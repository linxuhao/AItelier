"""The dashboard read surface for the user's conversation:

    GET /api/dsh-drivers/{project_id}/conversation?limit=&cursor=
    GET /api/dsh-drivers/unread[?project_id=]

One conversation, both directions, and the unread badge. Both routes take the
same authenticated-reader verdict as `GET .../messages` and read persisted rows
only, so an offline (or stopped) driver's messages still read.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import authz
from api.dsh_driver_routers import router
from api.state_graph_routers import get_service
from core.driver_presence import declare
from core.drivers import DriverRegistry
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService


@pytest.fixture
def world(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "Alpha")
    seed.create_project("beta", "Beta")
    registry = DriverRegistry(db, "user-conversation-route-pepper-not-a-secret")
    registry.register("ada", "Ada", actor="test")
    registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    return {"db": db,
            "driver": StateService(db, actor="driver:ada", driver_id="ada", project_read_trusted=True),
            "user": StateService(db, actor="owner:user@example.test", project_read_trusted=True)}


def client(world, monkeypatch, *, reader=True):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_service] = lambda: world["user"]
    monkeypatch.setattr(authz, "write_denial_reason",
                        lambda _: authz.WRITE_DENIED_NOT_AUTHENTICATED)
    if reader:
        app.dependency_overrides[authz.require_reader] = lambda: None
    return TestClient(app)


def say_driver(world, **kw):
    return execute(world["driver"], "send_user_message",
                   {"request_key": "hello", "project_id": "alpha", "subject": "Progress",
                    "body": "Half the nodes are done.", **kw}, allow_write=True)


def say_user(world, **kw):
    return execute(world["user"], "send_user_conversation_message",
                   {"request_key": "u", "project_id": "alpha", "body": "node a first", **kw},
                   allow_write=True)


def test_both_reads_need_the_reader_credential(world, monkeypatch):
    say_driver(world)
    with client(world, monkeypatch, reader=False) as anonymous:
        assert anonymous.get("/api/dsh-drivers/alpha/conversation").status_code == 403
        assert anonymous.get("/api/dsh-drivers/unread").status_code == 403
    with client(world, monkeypatch) as reader:
        assert reader.get("/api/dsh-drivers/alpha/conversation").status_code == 200
        assert reader.get("/api/dsh-drivers/unread").status_code == 200


def test_the_route_serves_both_directions_with_senders_and_the_persisted_reply(world, monkeypatch):
    asked = say_driver(world, request_key="ask", subject="Question", body="which node first?")
    answer = say_user(world, reply_to_message_id=asked["message_id"])
    declare(world["db"], "ada", "offline", reason="stopped for the day", actor="driver:ada")

    with client(world, monkeypatch) as reader:
        page = reader.get("/api/dsh-drivers/alpha/conversation").json()
        assert page["project_id"] == "alpha" and page["matched_total"] == 2
        assert [m["message_id"] for m in page["messages"]] == [
            answer["message_id"], asked["message_id"]]
        newest, oldest = page["messages"]
        assert (newest["sender_kind"], newest["sender_driver_id"]) == ("user", None)
        assert (oldest["sender_kind"], oldest["sender_driver_id"]) == ("driver", "ada")
        # The reply link comes from the row, not from anything the client sent.
        assert newest["reply_to_message_id"] == asked["message_id"]
        assert oldest["reply_to_message_id"] is None
        assert newest["created_at"] and oldest["created_at"]
        # The pre-existing feed route is still there and still answers.
        assert reader.get("/api/dsh-drivers/alpha/messages").json()["matched_total"] == 1
        # An unknown project and a malformed cursor are refused, not answered.
        assert reader.get("/api/dsh-drivers/nonexistent/conversation").status_code == 404
        assert reader.get("/api/dsh-drivers/alpha/conversation?limit=201").status_code == 422
        assert reader.get("/api/dsh-drivers/alpha/conversation?cursor=zzz").status_code == 422
        assert reader.get("/api/dsh-drivers/nonexistent/unread").status_code == 404


def test_walking_every_page_over_http_yields_each_message_exactly_once(world, monkeypatch):
    created = [say_driver(world, request_key=f"s{index}", subject=f"S{index}",
                          body=f"b{index}")["message_id"] for index in range(7)]
    with client(world, monkeypatch) as reader:
        seen, cursor, pages = [], None, []
        while True:
            url = "/api/dsh-drivers/alpha/conversation?limit=2"
            if cursor:
                url += "&cursor=" + cursor
            page = reader.get(url).json()
            pages.append(page)
            seen += [m["message_id"] for m in page["messages"]]
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            # New traffic arrives between two page fetches.
            say_driver(world, request_key="late-" + str(len(pages)), subject="late")
        assert len(seen) == len(set(seen))
        assert set(created) <= set(seen)
        assert pages[-1]["end_of_history"] is True and pages[-1]["next_cursor"] is None
        assert all(p["has_more"] and not p["end_of_history"] for p in pages[:-1])
        # The newest page now shows the traffic that arrived during the walk.
        newest = reader.get("/api/dsh-drivers/alpha/conversation").json()
        assert newest["messages"][0]["subject"] == "late"


def test_the_unread_badge_clears_on_read_and_stays_cleared(world, monkeypatch):
    first = say_driver(world, request_key="n1", subject="One")
    second = say_driver(world, request_key="n2", subject="Two")
    with client(world, monkeypatch) as reader:
        assert reader.get("/api/dsh-drivers/unread").json() == {
            "projects": {"alpha": 2}, "total": 2,
            "questions": {}, "questions_total": 0}
        assert reader.get("/api/dsh-drivers/unread?project_id=alpha").json() == {
            "projects": {"alpha": 2}, "total": 2,
            "questions": {}, "questions_total": 0}
        # Reading the conversation is a read: it does NOT move the marker.
        assert reader.get("/api/dsh-drivers/alpha/conversation").json()["unread_count"] == 2

    execute(world["user"], "mark_user_conversation_read",
            {"project_id": "alpha", "message_id": second["message_id"]}, allow_write=True)

    # A DIFFERENT client (fresh app, fresh connection) sees the cleared badge.
    with client(world, monkeypatch) as later:
        assert later.get("/api/dsh-drivers/unread").json() == {
            "projects": {"alpha": 0}, "total": 0,
            "questions": {}, "questions_total": 0}
        assert later.get("/api/dsh-drivers/alpha/conversation").json()["read_upto_message_id"] == \
            second["message_id"]
    assert first["message_id"] != second["message_id"]


def _state_app(world):
    """The product's State router - the route the dashboard actually posts to."""
    from api import state_graph_routers as routes
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(router)
    app.dependency_overrides[routes.get_service] = lambda: world["user"]
    return app


def test_the_dashboard_write_route_is_writer_only_and_lands_in_the_thread(world, monkeypatch):
    """`/api/state/commands/...` is the path the SPA uses: an anonymous visitor
    is refused there exactly as for every other write, and an accepted write is
    the same row the conversation read serves."""
    from api import authz as auth
    monkeypatch.setattr(auth, "gate_enabled", lambda: True)
    monkeypatch.setattr(auth, "WRITERS", set())
    monkeypatch.setattr(auth, "ADMIN_TOKEN", "off-tunnel-admin-token")
    monkeypatch.setattr(auth.cf_access, "email_from_request_headers", lambda *_: None)
    app = _state_app(world)
    body = {"request_key": "from-the-dashboard", "project_id": "alpha", "body": "node a first"}
    with TestClient(app) as anonymous:
        refused = anonymous.post("/api/state/commands/send_user_conversation_message", json=body)
        assert refused.status_code == 403, refused.text
        assert anonymous.post("/api/state/commands/mark_user_conversation_read",
                              json={"project_id": "alpha", "message_id": "x"}).status_code == 403
    assert execute(world["user"], "list_user_conversation",
                   {"project_id": "alpha"})["messages"] == []

    app.state._test_mode = True
    with TestClient(app) as writer:
        sent = writer.post("/api/state/commands/send_user_conversation_message", json=body)
        assert sent.status_code == 200, sent.text
        message_id = sent.json()["message_id"]
        page = writer.get("/api/dsh-drivers/alpha/conversation")
        assert page.status_code == 200
        # The write landed in the thread the read serves.
        assert [m["message_id"] for m in page.json()["messages"]] == [message_id]
        marked = writer.post("/api/state/commands/mark_user_conversation_read",
                             json={"project_id": "alpha", "message_id": message_id})
        assert marked.status_code == 200, marked.text
        assert marked.json()["advanced"] is True


def test_the_route_returns_segment_ids_and_the_answers_a_reply_cites(world, monkeypatch):
    """The rail's data: a question block's id, and the reply that closes it."""
    asked = say_driver(world, request_key="ask-q", subject="Question", body="ok?",
                       segments=[{"id": "q-restart", "kind": "question", "text": "may I restart?"},
                                 {"kind": "info", "text": "coordinated"}])
    answer = say_user(world, request_key="answer-q", answers=[
        {"message_id": asked["message_id"], "segment_id": "q-restart"}])

    with client(world, monkeypatch) as reader:
        page = reader.get("/api/dsh-drivers/alpha/conversation").json()
        by_id = {m["message_id"]: m for m in page["messages"]}
        driver_row = by_id[asked["message_id"]]
        assert driver_row["segments"][0] == {"id": "q-restart", "kind": "question", "text": "may I restart?"}
        # The info block carries a server-assigned id too: a reply can name any block.
        assert isinstance(driver_row["segments"][1]["id"], str) and driver_row["segments"][1]["id"]
        assert "answers" not in driver_row
        user_row = by_id[answer["message_id"]]
        assert user_row["answers"] == [{"message_id": asked["message_id"], "segment_id": "q-restart"}]


def test_a_refused_citation_is_a_422_and_stores_nothing_over_http(world, monkeypatch):
    """Through the real command route the SPA posts to: a bad citation is refused
    with a 422 and writes nothing, so no half-answered reply reaches the thread."""
    from api import authz as auth
    monkeypatch.setattr(auth, "gate_enabled", lambda: True)
    monkeypatch.setattr(auth, "WRITERS", set())
    monkeypatch.setattr(auth, "ADMIN_TOKEN", "off-tunnel-admin-token")
    monkeypatch.setattr(auth.cf_access, "email_from_request_headers", lambda *_: None)
    asked = say_driver(world, request_key="ask-r", subject="Question", body="ok?",
                       segments=[{"id": "i1", "kind": "info", "text": "context"}])
    app = _state_app(world)
    app.state._test_mode = True
    body = {"request_key": "bad-cite", "project_id": "alpha", "body": "x",
            "answers": [{"message_id": asked["message_id"], "segment_id": "i1"}]}
    with TestClient(app) as writer:
        refused = writer.post("/api/state/commands/send_user_conversation_message", json=body)
        assert refused.status_code == 422, refused.text
    page = execute(world["user"], "list_user_conversation", {"project_id": "alpha", "limit": 50})
    assert [m["message_id"] for m in page["messages"]] == [asked["message_id"]]
    assert all("answers" not in m for m in page["messages"])


def test_the_unread_badge_carries_the_open_question_count_it_computes(world, monkeypatch):
    """A project with an OPEN question shows that count OUTSIDE the conversation.

    The empty shape (`questions: {}`) is pinned above; this pins the NON-zero one
    - the case the project list renders as its own badge - and pins it to the
    number the service computes, over the HTTP surface the SPA reads. Without
    this, the route could stop forwarding the two keys for a project that has
    open questions and every delivered test would stay green.
    """
    asked = execute(world["driver"], "send_user_message",
                    {"request_key": "ask", "project_id": "alpha", "subject": "Question",
                     "body": "may I restart the build?",
                     "segments": [
                         {"id": "q1", "kind": "question", "text": "may I restart the build?"},
                         {"id": "q2", "kind": "question", "text": "may I deploy afterwards?"}]},
                    allow_write=True)
    # The reply cites q1 by its exact pair, so exactly one question stays open.
    execute(world["user"], "send_user_conversation_message",
            {"request_key": "close-q1", "project_id": "alpha", "body": "yes, restart it",
             "answers": [{"message_id": asked["message_id"], "segment_id": "q1"}]},
            allow_write=True)
    computed = execute(world["user"], "user_conversation_unread", {})["questions"]
    assert computed == {"alpha": 1}

    with client(world, monkeypatch) as reader:
        body = reader.get("/api/dsh-drivers/unread").json()
        # The route forwards the service's own map, not a second count of the rows.
        assert body["questions"] == computed, body
        assert body["questions_total"] == 1
        # The message badge is a different number over the same rows: one
        # driver-to-user message, and the user's own reply does not add to it.
        assert (body["projects"], body["total"]) == ({"alpha": 1}, 1)
        # Naming the project gives the same map, and a project with no open
        # question stays absent from it - the empty shape, still reachable.
        assert reader.get("/api/dsh-drivers/unread?project_id=alpha").json()["questions"] == \
            computed
        beta = reader.get("/api/dsh-drivers/unread?project_id=beta").json()
        assert (beta["questions"], beta["questions_total"]) == ({}, 0)

    # The two badges move independently: reading the thread clears the message
    # badge and closes no question.
    execute(world["user"], "mark_user_conversation_read",
            {"project_id": "alpha", "message_id": asked["message_id"]}, allow_write=True)
    with client(world, monkeypatch) as later:
        after = later.get("/api/dsh-drivers/unread").json()
        assert after["total"] == 0
        assert after["questions"] == computed and after["questions_total"] == 1
