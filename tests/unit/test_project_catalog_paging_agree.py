"""Anonymous and trusted-writer `project_catalog` paging follow the SAME rule.

ONE SQL store holds several opened (public) and several unopened projects. Each
caller's COMPLETE walk (loop on `next_after` until None) must equal an
independent oracle over the rows THAT caller may see: ids sorted, chunked by
limit, a page's `next_after` = its last id iff more ids follow, else None. The
writer keeps seeing the unopened projects (visibility is unchanged); the
anonymous caller never gets an unopened id, title or cursor.

Real store, real `execute` handler; the caller's trust is the only thing that
changes between the two walks.
"""
from __future__ import annotations

import json

import pytest

from core.state_commands import _filter_catalog, execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService

OPENED = ["b-open-1", "d-open-2", "e-open-3", "g-open-4"]
# The LAST id overall is unopened: the writer's last page and the anonymous
# last page then end at different projects.
UNOPENED = ["a-hid-1", "c-hid-2", "f-hid-3", "h-hid-4"]
ALL = sorted(OPENED + UNOPENED)


@pytest.fixture
def service(tmp_path):
    svc = StateService(StateDatabase(str(tmp_path / "state.sqlite")),
                       actor="paging-agree", project_read_trusted=True)
    for pid in ALL:
        svc.create_project(pid, f"Title {pid} {'OPEN' if pid in OPENED else 'SECRETTITLE'}")
    for pid in OPENED:
        svc.open_project(pid)
    return svc


def oracle(ids, limit):
    """Expected pages as (ids, next_after) for a caller who may see `ids`."""
    ids = sorted(ids)
    pages = []
    for i in range(0, len(ids), limit):
        chunk = ids[i:i + limit]
        more = i + limit < len(ids)
        pages.append((chunk, chunk[-1] if more else None))
    return pages or [([], None)]


def walk(service, limit, trusted):
    """The complete finite page sequence of one caller, as (ids, next_after)."""
    service.project_read_trusted = trusted
    pages, bodies, after = [], [], ""
    while True:
        body = execute(service, "project_catalog", {"after": after, "limit": limit})
        bodies.append(body)
        pages.append(([p["project_id"] for p in body["projects"]], body["next_after"]))
        if body["next_after"] is None:
            return pages, bodies
        after = body["next_after"]
        assert len(pages) <= len(ALL) + 2, f"catalog paging does not terminate: {pages}"


def _first_divergence(got, want):
    for n, (g, w) in enumerate(zip(got, want)):
        if g != w:
            return f"page {n}: got {g}, want {w}"
    return f"page counts differ: got {len(got)}, want {len(want)}"


@pytest.mark.parametrize("limit", [1, 2, len(ALL) + 5])
class TestWalksAgreeWithOracle:
    def test_anonymous_walk_equals_oracle_over_public_ids(self, service, limit):
        pages, bodies = walk(service, limit, trusted=False)
        want = oracle(OPENED, limit)
        assert pages == want, _first_divergence(pages, want)
        assert pages[-1][1] is None
        # Never an unopened cursor, id or title anywhere in an anonymous page.
        blob = json.dumps(bodies, default=str)
        for _, cursor in pages:
            assert cursor not in UNOPENED
        for pid in UNOPENED:
            assert pid not in blob
        assert "SECRETTITLE" not in blob

    def test_trusted_writer_walk_on_same_store_equals_oracle_over_all_ids(self, service, limit):
        pages, _ = walk(service, limit, trusted=True)
        want = oracle(ALL, limit)
        assert pages == want, _first_divergence(pages, want)
        seen = [pid for ids, _ in pages for pid in ids]
        assert seen == ALL  # the writer keeps seeing the unopened projects

    def test_both_walks_on_one_store_in_sequence(self, service, limit):
        anon, _ = walk(service, limit, trusted=False)
        writer, _ = walk(service, limit, trusted=True)
        assert anon == oracle(OPENED, limit)
        assert writer == oracle(ALL, limit)


class TestFilterCatalogCursor:
    def test_dropped_row_never_leaks_as_cursor(self, service):
        service.project_read_trusted = False
        page = {"projects": [{"project_id": "b-open-1"}, {"project_id": "c-hid-2"}],
                "next_after": "c-hid-2"}
        out = _filter_catalog(service, "project_catalog", page)
        assert [p["project_id"] for p in out["projects"]] == ["b-open-1"]
        assert out["next_after"] not in UNOPENED
        assert out["next_after"] == "b-open-1"

    def test_page_emptied_by_filter_has_no_unopened_cursor(self, service):
        service.project_read_trusted = False
        page = {"projects": [{"project_id": "c-hid-2"}], "next_after": "c-hid-2"}
        out = _filter_catalog(service, "project_catalog", page)
        assert out["projects"] == []
        assert out["next_after"] is None

    def test_none_cursor_stays_none(self, service):
        service.project_read_trusted = False
        page = {"projects": [{"project_id": "b-open-1"}, {"project_id": "c-hid-2"}],
                "next_after": None}
        assert _filter_catalog(service, "project_catalog", page)["next_after"] is None
        whole = {"projects": [{"project_id": "b-open-1"}], "next_after": None}
        assert _filter_catalog(service, "project_catalog", whole)["next_after"] is None

    def test_untouched_page_keeps_its_own_cursor(self, service):
        service.project_read_trusted = False
        page = {"projects": [{"project_id": "b-open-1"}, {"project_id": "d-open-2"}],
                "next_after": "d-open-2"}
        assert _filter_catalog(service, "project_catalog", page)["next_after"] == "d-open-2"


class TestRefusalsAndListPreserved:
    @pytest.mark.parametrize("trusted", [False, True])
    def test_bad_cursor_and_over_limit_refused(self, service, trusted):
        service.project_read_trusted = trusted
        for args in ({"after": "not a valid cursor!"}, {"limit": 0},
                     {"limit": 201}, {"limit": -1}):
            with pytest.raises(StateGraphError):
                execute(service, "project_catalog", args)

    def test_list_projects_still_filters_to_public_ids(self, service):
        service.project_read_trusted = False
        anon = execute(service, "list_projects", {})
        assert sorted(p["project_id"] for p in anon) == sorted(OPENED)
        service.project_read_trusted = True
        writer = execute(service, "list_projects", {})
        assert sorted(p["project_id"] for p in writer) == ALL
