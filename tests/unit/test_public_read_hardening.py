"""Three holes that only mattered once the hostname stopped asking who you are.

This backend spent its life behind Cloudflare Access, where "open read" meant
open to the handful of people who could sign in. On 2026-08-26 the Access policy
came off and the same endpoints started answering strangers. Nothing about the
code changed; what changed is that the read surface became the ATTACK surface,
and three things that were merely untidy became live defects:

  1. `workspace_file` had no `.git` filter, while `workspace_tree` and
     `repo_archive` — its own neighbours — both had one.
  2. `/api/agent/*` had no authorization at all, because `write_gate` gates
     METHODS and those two routes are GETs.
  3. `get_repo` was `async def` around a fully synchronous call, eighteen lines
     below the sibling whose docstring describes the outage that caused.

Each test below fails if its fix is reverted. They are deliberately not written
against "the current behaviour" but against the property that was missing.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api import authz
from api.main import app
from core import cf_access

WRITER = "writer@example.com"


# ── 1. `.git` is not readable through the workspace reader ────────────────────

class _Ws:
    """Only the two accessors `_resolve_workspace_target` reaches for."""

    def __init__(self, base: Path):
        self._base = base

    def get_code_path(self, project_id):
        return self._base

    def _get_secure_path(self, project_id):
        return self._base


class _Db:
    def get_project(self, project_id):
        return {"project_id": project_id, "owner_email": "cli@local"}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A workspace with a real file and a `.git` beside it."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.gd").write_text("extends Node\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/o/r.git\n')
    (tmp_path / ".git" / "logs").mkdir()
    (tmp_path / ".git" / "logs" / "HEAD").write_text(
        "0 1 Someone <someone@example.com> 1787761179 +0000\tcommit: x\n")
    return tmp_path


def _resolve(repo: Path, path: str, root: str = "code"):
    from api.project_routers import _resolve_workspace_target
    return _resolve_workspace_target("p1", path, root, None, _Db(), _Ws(repo))


@pytest.mark.parametrize("path", [
    ".git/config",
    ".git/logs/HEAD",
    ".git",
])
def test_git_internals_are_not_readable(repo, path):
    """`.git/config` carries the remote URL — and the credential, when the
    remote is a token-in-URL HTTPS remote. `.git/logs/HEAD` carries every
    committer identity that ever touched the repo: on the live deployment it
    published the operator's real email in 51 reflog lines, while the DB
    projection everyone was looking at reported `owner_email: cli@local`."""
    with pytest.raises(HTTPException) as e:
        _resolve(repo, path)
    assert e.value.status_code == 404


def test_a_normal_file_is_still_readable(repo):
    """The guard must be about `.git`, not about refusing everything."""
    assert _resolve(repo, "src/main.gd").name == "main.gd"


def test_the_reader_is_no_more_open_than_the_archive(repo):
    """`repo_archive`'s docstring justifies being open by saying the file reads
    "already expose every file". That was an argument FROM the file reader, so
    the file reader must not be the wider of the two. Both skip `.git`; this
    pins them together rather than trusting the sentence."""
    import api.project_routers as pr
    archive_src = inspect.getsource(pr.repo_archive)
    resolve_src = inspect.getsource(pr._resolve_workspace_target)
    assert '".git"' in archive_src
    assert '".git"' in resolve_src


def test_traversal_is_still_refused(repo):
    """The `.git` check must not have displaced the containment check."""
    with pytest.raises(HTTPException) as e:
        _resolve(repo, "../../etc/passwd")
    assert e.value.status_code == 403


# ── 2. Every /api/agent route requires a writer ───────────────────────────────

def _agent_routes():
    """Read the ROUTER, not `app.routes`.

    `app.routes` on this app is not flat — included routers appear as opaque
    `_IncludedRouter` entries — so a filter over it silently matched nothing and
    the coverage test below passed on an empty list. The router object is also
    the more precise subject: it is the thing carrying the dependency.
    """
    from api.agent_routers import router
    return list(router.routes)


def test_there_are_agent_routes_to_check():
    """Guards the test below from passing vacuously if the router is renamed or
    unmounted — an empty loop is not a proof. It has already earned its keep:
    the first version of `_agent_routes` returned [] and the coverage test went
    green anyway."""
    assert len(_agent_routes()) >= 4


def test_every_agent_route_carries_require_writer():
    """Declared on the ROUTER, so this holds for routes nobody has written yet.

    The defect was not that someone made a wrong call about `/chat/history` —
    it is that the route was added and no one noticed a check was missing,
    because the method-based gate makes a GET look already-handled. A per-route
    decorator would have to be remembered every time; this cannot be forgotten.
    """
    for route in _agent_routes():
        calls = [d.call for d in route.dependant.dependencies]
        assert authz.require_writer in calls, f"{route.path} is unguarded"


class TestAgentReadsAreDenied:
    """End to end, with the gate actually armed — the structural test above
    says the dependency is attached; this says attaching it does something."""

    @pytest.fixture
    def gated_client(self, monkeypatch):
        import api.main as main
        monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
        monkeypatch.setattr(cf_access, "_AUD", "test-aud")
        monkeypatch.setattr(authz, "WRITERS", {WRITER})
        monkeypatch.setattr(authz, "ADMIN_TOKEN", "s3cret-admin-token")
        monkeypatch.setattr(
            cf_access, "verify",
            lambda tok: {"email": tok[4:]} if tok.startswith("jwt:") else None,
        )
        monkeypatch.setattr(main, "_ALLOW_EXTERNAL", True)
        with TestClient(app) as c:
            monkeypatch.setattr(app.state, "_test_mode", False, raising=False)
            yield c

    @pytest.mark.parametrize("path", [
        "/api/agent/sessions",
        "/api/agent/chat/history?session_id=whatever",
    ])
    def test_anonymous_reader_is_refused(self, gated_client, path):
        """`message_json` is the raw transcript, tool results included — bash
        stdout, file contents, diffs. Reading it after the fact is the same
        permission as running the agent, not a weaker one."""
        assert gated_client.get(path).status_code == 403

    def test_a_writer_still_gets_in(self, gated_client):
        """A gate that refuses everyone is not a gate, it is an outage."""
        r = gated_client.get(
            "/api/agent/sessions",
            headers={"Cf-Ray": "abc", "Cf-Access-Jwt-Assertion": f"jwt:{WRITER}"},
        )
        assert r.status_code == 200

    def test_other_reads_stay_open(self, gated_client):
        """The point of the deployment is that reads are public. Locking one
        router must not have locked the surface."""
        assert gated_client.get("/health").status_code == 200


# ── 3. Nothing that calls _build_repo_groups runs on the event loop ───────────

def test_no_repo_handler_is_async():
    """`_build_repo_groups` walks every repo, every project under it, and runs
    several synchronous skillflow queries per project. On the event loop that
    is not slow, it is fatal — `list_repos` carries the incident note.

    Written as "no handler that calls it", not "get_repo specifically",
    because the bug WAS a specific fix: one of the two callers was made sync
    and the other was left alone. A test naming one function reproduces the
    same blind spot it is meant to close.
    """
    import api.repo_routers as rr
    callers = [
        (name, fn) for name, fn in vars(rr).items()
        if inspect.isfunction(fn) or inspect.iscoroutinefunction(fn)
        if getattr(fn, "__module__", None) == rr.__name__
        if fn is not rr._build_repo_groups
        if "_build_repo_groups(" in inspect.getsource(fn)
    ]
    assert len(callers) >= 2, "expected both repo routes to call it"
    for name, fn in callers:
        assert not inspect.iscoroutinefunction(fn), (
            f"{name} is async around a fully synchronous call — it blocks the "
            f"event loop, and with it the scheduler, SSE and /health")
