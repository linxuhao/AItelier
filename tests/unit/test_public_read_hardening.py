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


# ── 4. Second pass: everything else the audit turned up ───────────────────────

class TestCredentialsNeverRideOnAnOpenRead:
    """`create_github_pr` documents `https://x:<PAT>@github.com/...` as an
    anticipated remote, and the remote URL is served by `repo_status`, by
    `/api/repos`, and (before the `.git` fix above) by `.git/config`. One
    writer setting such a remote would have turned a PAT into an anonymous GET.
    """

    def test_userinfo_is_stripped(self):
        from core.git_ops import redact_url_credentials as r
        assert r("https://x:ghp_SECRET@github.com/o/r.git") == "https://github.com/o/r.git"
        assert "ghp_SECRET" not in r("https://x:ghp_SECRET@github.com/o/r.git")

    def test_ordinary_urls_are_untouched(self):
        """Redaction that mangles the common case gets reverted, not fixed."""
        from core.git_ops import redact_url_credentials as r
        for u in ("https://github.com/o/r.git", "git@github.com:o/r.git", "", None):
            assert r(u) == u

    def test_both_open_readers_apply_it(self):
        """Two independent paths serve a remote URL — the git-derived one and
        the DB-derived one. Fixing either alone leaves the leak."""
        import core.workspace_manager as wm
        import api.repo_routers as rr
        # The CALL, not the name: both files also import it, and a mutation
        # that deleted only the call left the import behind — this assertion
        # passed on code that had stopped redacting anything.
        assert "redact_url_credentials(remote_url)" in inspect.getsource(
            wm.WorkspaceManager.repo_status)
        assert "redact_url_credentials(ru)" in inspect.getsource(
            rr._build_repo_groups)


class TestGitReadsCannotFightTheRunningPipeline:
    def test_optional_locks_are_off(self):
        """`repo_status` is an open read that shells out to `git status`, which
        takes .git/index.lock. The pipeline commits into the same repo, so a
        flood of reads could fail `repo_apply` — a read-only endpoint breaking
        a paid run."""
        from core.workspace_manager import _GIT_ENV
        assert _GIT_ENV["GIT_OPTIONAL_LOCKS"] == "0"

    def test_the_overrides_actually_win(self, monkeypatch):
        """`{"LC_ALL": "C", **os.environ}` put the override BEFORE the spread,
        so the environment won and the "force English locale" line did nothing
        in exactly the case it was written for. Rebuilt with the same ordering,
        this fails."""
        import importlib
        import os as _os
        monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
        monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
        import core.workspace_manager as wm
        env = importlib.reload(wm)._GIT_ENV
        assert env["LC_ALL"] == "C"
        assert env["GIT_OPTIONAL_LOCKS"] == "0"


def test_the_archive_requires_a_writer():
    """It builds the whole working tree into an in-memory zip before streaming
    a byte — 12.6MB measured, edge-uncacheable, and a slow reader pins the
    buffer. Confidentiality was never the argument against it; cost is."""
    import api.project_routers as pr
    from api.authz import require_writer
    route = next(r for r in pr.router.routes if r.path.endswith("/repo/archive"))
    calls = [d.call for d in route.dependant.dependencies]
    assert require_writer in calls


def test_the_workspace_read_cap_is_actually_small():
    """Pins the VALUE, not just the mechanism.

    The behavioural test below monkeypatches the constant, so it stays green
    with the cap set to 10**18 — a mutation sweep caught it passing on exactly
    that. A ceiling nobody can reach is not a ceiling.
    """
    from api.project_routers import _WORKSPACE_FILE_MAX_BYTES
    assert 0 < _WORKSPACE_FILE_MAX_BYTES <= 64 * 1024 * 1024


def test_a_workspace_read_is_size_capped(repo, monkeypatch):
    """Paging was by line but the read was whole-file, so page 1 of a huge file
    still allocated the whole thing."""
    import api.project_routers as pr
    monkeypatch.setattr(pr, "_WORKSPACE_FILE_MAX_BYTES", 8)
    big = repo / "big.txt"
    big.write_text("x" * 4096)
    with pytest.raises(HTTPException) as e:
        pr.workspace_file("p1", "big.txt", "code", None, None, None, _Db(), _Ws(repo))
    assert e.value.status_code == 413


class TestStepOutputIsBounded:
    @pytest.mark.parametrize("bad", [".", "..", "./x", "%2e%2e", "a/b", ""])
    def test_a_step_id_that_is_a_path_is_refused(self, bad):
        """`step_id` was interpolated into a filesystem path unvalidated: "."
        walked out of the step directory into the workspace root, and ".." is
        reachable percent-encoded because uvicorn decodes before routing."""
        from api.routers import _STEP_ID_RE
        assert not _STEP_ID_RE.fullmatch(bad)

    @pytest.mark.parametrize("ok", ["1", "t_impl", "1_5", "5_review", "t_plan_review"])
    def test_real_step_ids_still_match(self, ok):
        """A guard that rejects the legitimate ids is an outage, not a fix."""
        from api.routers import _STEP_ID_RE
        assert _STEP_ID_RE.fullmatch(ok)

    def test_the_reader_has_ceilings_and_reports_what_it_dropped(self):
        """Silent truncation reads as "that is all there was" — the same class
        this codebase already fixed for gate skips."""
        import api.routers as r
        src = inspect.getsource(r.get_step_output)
        assert "_STEP_FILE_MAX_BYTES" in src and "_STEP_TOTAL_MAX_BYTES" in src
        assert "skipped" in src


def test_no_event_is_pushed_to_a_channel_nobody_reads():
    """Every skillflow event was published twice, and the second channel ("0")
    has no subscriber — so push_log took its no-consumer branch every time and
    appended to a buffer nothing drains: ~60MB/day per active run, for the life
    of the process."""
    import api.main as m
    assert 'push_log("0"' not in inspect.getsource(m)


def test_sse_queues_are_bounded():
    """Unbounded, `put_nowait` could never raise, so the slow-consumer eviction
    in push_log was dead code and a connected-but-not-reading client
    accumulated without limit."""
    import api.sse_manager as sm
    assert sm._QUEUE_MAX > 0 and sm._BUFFER_MAX > 0
    assert "maxsize=_QUEUE_MAX" in inspect.getsource(sm.StreamManager.event_generator)


class TestTheReadCache:
    def test_one_computation_serves_every_caller(self):
        """The point is not that a request gets faster — it is that the Nth
        concurrent request costs nothing. At ~1.13s per dashboard tick on a
        single-core process, that is the difference between saturating at nine
        visitors and at hundreds."""
        from api import _read_cache
        _read_cache.clear()
        calls = []
        for _ in range(5):
            _read_cache.cached(("k", None), lambda: calls.append(1) or "v")
        assert len(calls) == 1

    def test_a_different_identity_gets_its_own_entry(self):
        """`/api/runs` is owner-filtered. A key that dropped the owner would
        serve one identity's runs to another — a performance fix turning into
        a disclosure."""
        from api import _read_cache
        _read_cache.clear()
        a = _read_cache.cached(("runs", None, None, "a@x"), lambda: "A")
        b = _read_cache.cached(("runs", None, None, "b@x"), lambda: "B")
        assert (a, b) == ("A", "B")

    def test_the_owner_is_in_the_key(self):
        """Pinned at the call site too: the tuple above is easy to shorten by
        accident while the cache itself stays correct."""
        import api.run_routers as rr
        src = inspect.getsource(rr.list_all_runs)
        assert '("runs", config_name, status, owner)' in src

    def test_it_expires(self):
        from api import _read_cache
        _read_cache.clear()
        calls = []
        _read_cache.cached(("k2",), lambda: calls.append(1) or "v", ttl=-1)
        _read_cache.cached(("k2",), lambda: calls.append(1) or "v", ttl=-1)
        assert len(calls) == 2


class TestTheEnvScrubCoversTheUnattendedPath:
    def test_the_rule_has_one_definition(self):
        """It was a private attribute on MetaAgent and applied to the one
        subprocess a human watches. The pipeline's test runner — which runs
        LLM-authored pytest and `npm ci` postinstall scripts unattended —
        passed os.environ through untouched."""
        from core.env_scrub import ENV_SECRET_RE
        from core.meta_agent import MetaAgent
        assert MetaAgent._ENV_SECRET_RE is ENV_SECRET_RE

    def test_secrets_are_dropped_and_ordinary_vars_kept(self, monkeypatch):
        from core.env_scrub import scrubbed_env
        monkeypatch.setenv("SOMETHING_API_KEY", "x")
        monkeypatch.setenv("SOMETHING_TOKEN", "x")
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        env = scrubbed_env()
        assert "SOMETHING_API_KEY" not in env
        assert "SOMETHING_TOKEN" not in env
        assert env["GIT_CONFIG_COUNT"] == "1"

    def test_an_override_survives_the_filter(self):
        """PYTHONPATH is set deliberately by the caller; the filter must not be
        able to remove what the caller just asked for."""
        from core.env_scrub import scrubbed_env
        assert scrubbed_env(PYTHONPATH="/p")["PYTHONPATH"] == "/p"

    def test_both_subprocess_paths_in_run_tests_use_it(self):
        """pytest and `npm ci` are two separate spawns; the node one passed no
        env= at all, so it inherited everything."""
        import aitelier.tools.run_tests.impl as impl
        assert "env_scrub.scrubbed_env" in inspect.getsource(impl._run_node_cmd)
        assert inspect.getsource(impl).count("env_scrub.scrubbed_env") >= 2


def test_the_code_path_jail_uses_component_containment():
    """`str(target).startswith(str(base))` admitted a SIBLING sharing the
    prefix — a project at .../projects/foo could read .../projects/foo-backup."""
    import core.meta_agent as ma
    src = inspect.getsource(ma.MetaAgent._resolve_code_target)
    # Comments stripped: the fix's own note explains what `startswith` did
    # wrong, and a naive substring check matched that explanation instead of
    # the code — the guard would have passed on a revert that kept the comment.
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    assert "is_relative_to" in code
    assert "startswith" not in code


# ── 5. Third pass: what the second round of reviewers found ───────────────────

class TestTheTreeListingIsJailedAndBounded:
    """`workspace_tree` was the endpoint the FIRST round used as the reference
    implementation — `workspace_file` was faulted for missing the `.git` filter
    that this one had. It turned out to have no path jail at all. Confirmed on
    the public host: `?subdir=../../../../../run/secrets` returned the mounted
    secret FILENAMES."""

    def _tree(self, repo, subdir=None):
        import api.project_routers as pr
        return pr.workspace_tree("p1", subdir, "code", None, _Db(), _Ws(repo))

    @pytest.mark.parametrize("subdir", ["../..", "../../../../../run/secrets", "/etc"])
    def test_escaping_the_workspace_is_refused(self, repo, subdir):
        with pytest.raises(HTTPException) as e:
            self._tree(repo, subdir)
        assert e.value.status_code == 403

    def test_a_real_subdir_still_lists(self, repo):
        assert self._tree(repo, "src")["tree"] == ["main.gd"]

    def test_git_is_still_pruned(self, repo):
        assert not any(".git" in p for p in self._tree(repo)["tree"])

    def test_the_walk_stops_instead_of_truncating_afterwards(self, repo, monkeypatch):
        """`sorted(base.rglob("*"))` materialised and sorted the ENTIRE
        recursive listing before `[:200]` discarded almost all of it — so the
        cap bounded the response and not the work, and one anonymous GET
        pointed at a large tree was a few hundred thousand stat() calls."""
        import api.project_routers as pr
        monkeypatch.setattr(pr, "_WORKSPACE_TREE_MAX", 3)
        for i in range(20):
            (repo / f"f{i}.txt").write_text("x")
        out = self._tree(repo)
        assert len(out["tree"]) == 3
        assert out["truncated"] is True, "a cut-off listing must say so"

    def test_a_short_listing_is_not_marked_truncated(self, repo):
        assert self._tree(repo)["truncated"] is False


class TestTheWorkspaceReadIsBoundedByTheWindowNotTheFile:
    def test_it_does_not_read_the_whole_file(self):
        """The cap was chosen against FILE BYTES while the cost is the decoded
        string plus the line list: 8MB of 0xff decodes to 16.8MB of str, and
        8MB of "ab\\n" becomes 2.8M list entries — measured at 153-178MB peak
        for a file the cap called acceptable, times a 40-thread pool. A smaller
        number would not have fixed the shape."""
        import api.project_routers as pr
        src = inspect.getsource(pr.workspace_file)
        assert "read_text" not in src, "still materialising the whole file"
        assert "target.open(" in src

    def test_the_window_is_what_comes_back(self, repo):
        import api.project_routers as pr
        (repo / "many.txt").write_text("\n".join(str(i) for i in range(5000)))
        out = pr.workspace_file("p1", "many.txt", "code", 10, 12,
                                None, _Db(), _Ws(repo))
        assert out["content"] == "9\n10\n11"
        assert out["total_lines"] == 5000
        assert out["truncated"] is True

    def test_the_cap_is_still_a_real_ceiling(self):
        from api.project_routers import _WORKSPACE_FILE_MAX_BYTES
        assert 0 < _WORKSPACE_FILE_MAX_BYTES <= 8 * 1024 * 1024

    @pytest.mark.parametrize("name", ["trace.db", "x.sqlite3", "y.db-wal"])
    def test_engine_bookkeeping_is_not_project_content(self, repo, name):
        """`trace.db` sits in the DPS root and holds system prompts, tool calls
        and tool results — the same content the /api/agent lock was added to
        protect, reachable through a second door as a binary decoded with
        errors="replace" (most of it comes out printable)."""
        (repo / name).write_bytes(b"SQLite format 3\x00You are a helpful agent")
        with pytest.raises(HTTPException) as e:
            _resolve(repo, name)
        assert e.value.status_code == 404


class TestAnUnknownJwtKidCostsNoNetworkCall:
    """`write_gate` verifies the JWT BEFORE returning 403, so the denial is not
    the protection it looks like. PyJWT's cache does not help: an unknown `kid`
    falls through to `get_signing_keys(refresh=True)`, which bypasses it — so a
    token with a random kid forces a live HTTPS round-trip per request, on the
    event loop, at PyJWT's 30-SECOND default timeout."""

    def test_the_fetch_timeout_is_short(self):
        from core import cf_access
        assert 0 < cf_access._FETCH_TIMEOUT <= 5

    def test_a_rejected_kid_is_remembered(self):
        import jwt as _jwt
        from core import cf_access
        cf_access._bad_kids.clear()
        tok = _jwt.encode({"email": "a@b.c"}, "k", algorithm="HS256",
                          headers={"kid": "made-up-kid"})
        assert cf_access.verify(tok) is None
        assert "made-up-kid" in cf_access._bad_kids

        # The second call must not TOUCH the client. Asserting `verify(...) is
        # None` proves nothing: `verify` swallows every exception, so a broken
        # client and a short-circuit are indistinguishable from the outside —
        # the first version of this test passed with the short-circuit deleted.
        # Record the call instead.
        touched = []

        def _spy():
            touched.append(1)
            raise AssertionError("should not be reached")

        monkeypatched = cf_access._client
        cf_access._client = _spy
        try:
            assert cf_access.verify(tok) is None
        finally:
            cf_access._client = monkeypatched
        assert touched == [], "an unknown kid still reached the JWKS client"

    def test_the_bad_kid_table_is_bounded(self):
        """The attacker picks the keys, so the table must not be theirs to grow."""
        import jwt as _jwt
        from core import cf_access
        cf_access._bad_kids.clear()
        for i in range(cf_access._BAD_KID_MAX + 50):
            cf_access._remember_bad_kid(_jwt.encode(
                {}, "k", algorithm="HS256", headers={"kid": f"k{i}"}))
        assert len(cf_access._bad_kids) <= cf_access._BAD_KID_MAX


class TestSseEvictionIsNotSilent:
    def test_an_evicted_client_is_told(self):
        """Bounding the queue made this eviction path live for the first time.
        Dropping the queue alone left `event_generator` awaiting a queue nobody
        feeds while still emitting its 15s `: ping` — the socket stays open,
        EventSource never fires onerror, and web/src/lib/sse.ts only reconnects
        from onerror. The cap would have turned "slow client" into "permanently
        dead client with no symptom"."""
        import asyncio
        import api.sse_manager as sm

        async def go():
            m = sm.StreamManager()
            q = asyncio.Queue(maxsize=2)
            m._get_queues("c").add(q)
            for _ in range(5):
                await m.push_log("c", "x")
            drained = []
            while not q.empty():
                drained.append(q.get_nowait())
            return drained

        assert "__END__" in asyncio.run(go())

    def test_the_channel_count_is_bounded(self):
        import api.sse_manager as sm
        assert sm._BUFFER_CHANNEL_MAX > 0
        assert "_BUFFER_CHANNEL_MAX" in inspect.getsource(sm.StreamManager.push_log)


class TestTheCacheTablesAreBounded:
    def test_clear_clears_the_locks_too(self):
        from api import _read_cache as rc
        rc.clear()
        rc.cached(("a",), lambda: 1)
        assert rc._locks
        rc.clear()
        assert not rc._locks, "clear() left the lock table behind"

    def test_attacker_chosen_keys_cannot_grow_it_without_bound(self):
        """`/api/runs` takes free-form `config_name` / `status`, so every
        `?config_name=zzz<n>` minted a permanent lock entry."""
        from api import _read_cache as rc
        rc.clear()
        for i in range(rc._MAX_KEYS * 3):
            rc.cached(("runs", f"cfg{i}", None, None), lambda: i)
        assert len(rc._locks) <= rc._MAX_KEYS
        assert len(rc._store) <= rc._MAX_KEYS


def test_every_git_env_puts_its_overrides_after_the_spread():
    """Two modules define `_GIT_ENV`; the first pass fixed one of them. The one
    left behind is the copy whose comment says it exists for the user-visible
    'Make PR' result messages."""
    import core.git_ops as go
    import core.workspace_manager as wm
    for mod in (go, wm):
        line = next(l for l in inspect.getsource(mod).splitlines()
                    if l.startswith("_GIT_ENV = "))
        assert line.index("**os.environ") < line.index('"LC_ALL"'), (
            f"{mod.__name__}: os.environ wins over the override — {line}")


def test_the_third_endpoint_with_the_same_core_is_cached_too():
    """`/api/projects` runs the SAME `list_projects_with_stats` +
    `enrich_project_status` core as `/api/runs`, and the first caching pass
    covered the two endpoints the SPA polls and missed this one — because not
    being on a page is what hid it, not that it was cheap. Measured at 529ms of
    CPU per anonymous call, which put the deployment's saturation point at two
    callers against anything that loops a URL.

    Same one-of-N shape as `get_repo` and as `_GIT_ENV`: the fix was applied to
    the instances someone was looking at.
    """
    import api.project_routers as pr
    src = inspect.getsource(pr.list_projects)
    assert "_read_cache.cached" in src
    assert '("projects", owner)' in src, "the key must carry the owner filter"
