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
    return _resolve_workspace_target("p1", path, root, None, _Db(), _Ws(repo), None)


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
        pr.workspace_file("p1", None, "big.txt", "code", None, None, None, _Db(), _Ws(repo))
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

    def test_the_key_carries_the_owner_and_nothing_a_caller_picks(self):
        """Two properties, and the second one was learned the hard way.

        The owner must be IN the key or one identity's runs reach another. And
        nothing else may be: the key used to carry `config_name` and `status`,
        which are free-form query params, so `?status=<random>` was a fresh key
        every time — never a hit, full ~0.5s uncached kernel, i.e. the exact
        amplifier the cache was added to remove, one parameter away. After the
        cache grew a key cap it was worse: ~512 such requests evicted the real
        entries and pushed every dashboard tab back onto the slow path.
        """
        import api.run_routers as rr
        src = inspect.getsource(rr.list_all_runs)
        assert '("runs", owner)' in src
        key_line = next(l for l in src.splitlines() if '("runs", owner)' in l)
        for caller_supplied in ("config_name", "status"):
            assert caller_supplied not in key_line, (
                f"{caller_supplied} is caller-controlled; keying on it makes the "
                f"cache bypassable and, with a key cap, flushable")

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
        return pr.workspace_tree("p1", None, subdir, "code", None, _Db(), _Ws(repo))

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
        out = pr.workspace_file("p1", None, "many.txt", "code", 10, 12,
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


class TestARepoOutsideTheDataDirIsWriterOnly:
    """The predicate is CONTAINMENT, not `repo_type`.

    A repo AItelier created under its own data dir is AItelier's to publish, and
    browsing it is the point of a public dashboard. A repo the operator pointed
    at is not — `repo_path` is an arbitrary absolute path and there is no
    gitignore filter anywhere in the reader. One project created with
    `repo_path=/app` (which passes the existing `.git`-presence check, and is
    the natural answer when you ask the butler to fix something in AItelier
    itself) makes `?root=code&path=.env` serve AITELIER_ADMIN_TOKEN to the
    internet: public read to full write, in one request.
    """

    def _project(self, repo_path):
        return {"project_id": "p1", "owner_email": "cli@local", "repo_path": repo_path}

    def test_inside_the_data_dir_stays_public(self):
        from api.project_routers import _repo_is_inside_the_data_dir
        from core import datadir
        inside = str(datadir.aitelier_home() / "projects" / "game")
        assert _repo_is_inside_the_data_dir(self._project(inside))

    @pytest.mark.parametrize("outside", ["/app", "/home/someone/secrets", "/etc"])
    def test_outside_is_not(self, outside):
        from api.project_routers import _repo_is_inside_the_data_dir
        assert not _repo_is_inside_the_data_dir(self._project(outside))

    def test_a_repoless_project_is_not_flagged(self):
        """`repo_type: none` runs have nothing to expose; treating them as
        external would break the authoring pipelines' own dashboards."""
        from api.project_routers import _repo_is_inside_the_data_dir
        assert _repo_is_inside_the_data_dir({"project_id": "p", "repo_path": None})

    def test_an_anonymous_read_of_an_external_repo_is_refused(self):
        from api.project_routers import _require_writer_for_external_repo
        with pytest.raises(HTTPException) as e:
            _require_writer_for_external_repo(self._project("/app"), None)
        assert e.value.status_code == 403

    def test_all_three_readers_apply_it(self):
        """file, raw and tree — the whole point of this round is that a rule
        applied to some of the readers is a rule that is not applied."""
        import api.project_routers as pr
        assert "_require_writer_for_external_repo" in inspect.getsource(
            pr._resolve_workspace_target)
        assert "_require_writer_for_external_repo" in inspect.getsource(
            pr.workspace_tree)


class TestProjectIdIsConstrained:
    """It is interpolated into a filesystem path AND into a `{@html}` block in
    the SPA's delete confirmation, and its value can come straight from the
    model — `args.get("project_id") or self._slugify(...)`, where the slugify is
    only the FALLBACK. "filesystem-safe slug" was a description with nothing
    enforcing it."""

    @pytest.mark.parametrize("bad", [
        '<img src=x onerror=alert(1)>',
        "../escape", "a/b", "", "x" * 65, ".hidden", "-dash",
    ])
    def test_dangerous_ids_are_refused(self, bad):
        from pydantic import ValidationError
        from models.schemas import ProjectCreate
        with pytest.raises(ValidationError):
            ProjectCreate(project_id=bad)

    @pytest.mark.parametrize("ok", ["jinyong-hud", "gen_dsh_code_review",
                                    "a", "proj.v2", "P1"])
    def test_real_ids_still_pass(self, ok):
        from models.schemas import ProjectCreate
        assert ProjectCreate(project_id=ok).project_id == ok


def test_every_subprocess_in_run_tests_scrubs_the_environment():
    """Third pass over the same module. `bash` was hardened first, `npm ci` and
    pytest in the second round, and `pip install -e` — which EXECUTES the
    target's build backend, i.e. LLM-authored setup.py — was still inheriting
    everything. Counting the spawns is the only way this stops being a
    one-at-a-time discovery."""
    import aitelier.tools.run_tests.impl as impl
    src = inspect.getsource(impl)
    spawns = src.count("subprocess.run(") + src.count("subprocess.Popen(")
    scrubbed = src.count("env_scrub.scrubbed_env()") + src.count(
        "env_scrub.scrubbed_env(PYTHONPATH=")
    assert scrubbed >= 4, f"{spawns} spawn sites, only {scrubbed} scrubbed"


# ── 6. Round 3: the critical one was mine ─────────────────────────────────────

class TestAnUnknownKidCannotLockOutTheRealWriter:
    """The negative cache was an anonymous denial-of-WRITE.

    `_remember_bad_kid` sat in the `except` of the whole try, which covered
    `jwt.decode` — so a token whose key resolved fine and whose SIGNATURE was
    wrong marked that `kid` bad. The kid is public: it is in the JWKS and in the
    header of every Access token. One unauthenticated request carrying the REAL
    kid with a garbage signature locked every legitimate writer out of
    write_gate, require_writer and /api/me for the full TTL, at zero cost,
    repeatably. An expired cookie in a stale tab did it by accident.

    The cache means "Cloudflare does not have this key" — a property of the KID.
    A signature or claim failure is a property of the TOKEN and must never be
    attributed to the key it names. This is the round-2 lesson recurring: a
    cache added to a previously-dead path, whose new behaviour was not examined.
    """

    @pytest.fixture
    def signed(self, monkeypatch):
        import types
        import jwt as _jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from core import cf_access
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "t.example")
        monkeypatch.setattr(cf_access, "_AUD", "aud")
        monkeypatch.setattr(cf_access, "_ISSUER", "https://t.example")
        cf_access._bad_kids.clear()
        claims = {"email": "w@x.y", "aud": "aud", "iss": "https://t.example"}
        good = _jwt.encode(claims, key, algorithm="RS256", headers={"kid": "real"})
        forged = _jwt.encode(claims, other, algorithm="RS256", headers={"kid": "real"})

        class _Client:
            def get_signing_key_from_jwt(self, tok):
                return types.SimpleNamespace(key=key.public_key())

        monkeypatch.setattr(cf_access, "_client", lambda: _Client())
        return cf_access, good, forged

    def test_a_forged_signature_does_not_poison_the_real_key(self, signed):
        cf_access, good, forged = signed
        assert cf_access.verify(forged) is None
        assert not cf_access._bad_kids, "a signature failure blamed the key"
        assert (cf_access.verify(good) or {}).get("email") == "w@x.y"

    def test_a_kid_the_jwks_does_not_have_is_still_cached(self, signed, monkeypatch):
        """The protection this cache exists for must survive the fix."""
        cf_access, good, _ = signed

        def _missing():
            raise KeyError("no such kid")

        monkeypatch.setattr(cf_access, "_client", _missing)
        assert cf_access.verify(good) is None
        assert "real" in cf_access._bad_kids


@pytest.mark.parametrize("bad", ['<img src=x onerror=1>', "../escape", ".", ""])
def test_project_ids_are_validated_at_the_choke_points_not_the_callers(bad, tmp_path):
    """There are thirteen `pid = args["project_id"]` sites in core/meta_agent
    alone, plus core/run_launcher. The first attempt put a `pattern=` on the
    REST schema — which guards `POST /api/projects`, a writer-gated mutating
    method, i.e. not the door the model uses. Validating at creation is the one
    place that cannot be walked around.

    Called, not grepped — same reason as the repo-reader test above.
    """
    from core.db_manager import DBManager
    from core.workspace_manager import WorkspaceManager
    db = DBManager(str(tmp_path / "t.db"))
    with pytest.raises(ValueError):
        db.ensure_project(bad)
    with pytest.raises(ValueError):
        WorkspaceManager.setup_workspace(object(), bad)


def test_the_read_window_cannot_be_widened_by_the_caller(repo):
    """Streaming bounded memory by the PAGE — and then `?end_line=99999999`
    made the page the whole file again, measured at 76MB, within 13% of the
    pre-fix cost. A caller picking the page size is the same lever renamed.

    Asserted on the RESULT, not on the source line that produces it: a test
    that greps for `min(stop_idx, ...)` fails on a rename and passes on a
    reordering, which is the opposite of what it is for."""
    import api.project_routers as pr
    n = pr._WORKSPACE_FILE_MAX_LINES * 3
    (repo / "huge.txt").write_text("\n".join(str(i) for i in range(n)))
    out = pr.workspace_file("p1", None, "huge.txt", "code", 1, 99999999,
                            None, _Db(), _Ws(repo))
    assert out["total_lines"] == n
    assert len(out["content"].splitlines()) == pr._WORKSPACE_FILE_MAX_LINES
    assert out["truncated"] is True


def test_all_four_repo_readers_are_gated(repo):
    """`repo_status` was the fourth. The batch that added the gate said "three
    readers together" and there were four — it publishes the absolute host path,
    the remote URL, branch state and 20 commit subjects with author identities.

    CALLED, not grepped. The first version of this test asserted that the string
    `_require_writer_for_external_repo` appeared in each function's source, and
    it passed while `repo_status` was raising `NameError: name 'request' is not
    defined` on the FIRST line of the gate — a hard 500 on the public host, for
    a whole day, with a green suite. A source-string assertion is a comment that
    fails the build; it cannot tell "the gate runs" from "the gate crashes
    before it runs".
    """
    import api.project_routers as pr

    class _Ws2(_Ws):
        def repo_status(self, project_id):
            return {"is_git": False, "path": str(self._base)}

    ws = _Ws2(repo)
    # Each reader, invoked. A project inside the data dir stays public, so the
    # gate must let these through rather than raise anything.
    assert pr.workspace_tree("p1", None, None, "code", None, _Db(), ws)["tree"]
    assert pr._resolve_workspace_target(
        "p1", "src/main.gd", "code", None, _Db(), ws, None).name == "main.gd"
    assert pr.repo_status("p1", None, None, _Db(), ws)["is_git"] is False

    route = next(r for r in pr.router.routes if r.path.endswith("/repo/archive"))
    from api.authz import require_writer
    assert require_writer in [d.call for d in route.dependant.dependencies]


def test_an_external_repo_is_refused_by_all_of_them(repo):
    """The other direction: the gate must actually REFUSE, not just not-crash."""
    import api.project_routers as pr

    class _ExternalDb:
        def get_project(self, pid):
            return {"project_id": pid, "owner_email": "cli@local",
                    "repo_path": "/app"}

    db = _ExternalDb()
    ws = _Ws(repo)
    for call in (
        lambda: pr.workspace_tree("p1", None, None, "code", None, db, ws),
        lambda: pr._resolve_workspace_target(
            "p1", "src/main.gd", "code", None, db, ws, None),
        lambda: pr.repo_status("p1", None, None, db, ws),
    ):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 403


def test_the_data_dir_itself_is_not_a_legitimate_repo_root():
    """`is_relative_to(aitelier_home())` also admitted the data dir and
    `projects/` as a whole, so one project whose repo_path was an ANCESTOR
    published every other project's tree through a single reader — and bypassed
    each of their own owner checks getting there."""
    from api.project_routers import _repo_is_inside_the_data_dir as ok
    from core import datadir
    assert not ok({"repo_path": str(datadir.aitelier_home())})
    assert not ok({"repo_path": str(datadir.projects_dir())})
    assert ok({"repo_path": str(datadir.projects_dir() / "a-real-project")})


def test_the_response_carries_a_content_security_policy():
    """The SPA renders content authored by agents that read the open web, to
    anonymous strangers. The CSP is the compensating control for every
    `{@html}` site — the ones that exist and the one somebody adds next."""
    import api.main as m
    csp = m._SEC_HEADERS["Content-Security-Policy"]
    for directive in ("default-src 'self'", "script-src 'self'",
                      "object-src 'none'", "frame-ancestors 'none'",
                      "base-uri 'none'", "form-action 'none'"):
        assert directive in csp
    # Verified against the real build: no inline <script>, no eval. If either
    # ever appears, this must fail rather than be loosened quietly.
    assert "'unsafe-eval'" not in csp
    assert "'unsafe-inline'" not in csp.split("style-src")[0]
    src = inspect.getsource(m.security_headers)
    assert "setdefault" in src, (
        "assignment would LOOSEN the raw-image endpoint's stricter own policy")


def test_the_cache_ttl_matches_the_polling_interval_it_serves():
    """The fixed cost of this cache is `keys / TTL * rebuild`, and it does not
    depend on how many people are watching — two visitors and five hundred pay
    the same. So the TTL is not a comfort setting, it is the throughput knob.

    At TTL 5 against the SPA's 10s poll it rebuilt twice per poll: ~550ms x3
    endpoints x2 = about a third of the single core, spent recomputing an answer
    nobody had asked to change. Pinned against the SPA rather than asserted as a
    number, because the two drifting apart is the failure — the constant on its
    own looks arbitrary and would be "tuned" by someone who could not see why.
    """
    import re
    from pathlib import Path as _P
    from api._read_cache import DEFAULT_TTL

    spa = (_P(__file__).resolve().parents[2] / "web" / "src" / "views"
           / "UnifiedDashboard.svelte").read_text(encoding="utf-8")
    poll_ms = max(int(m) for m in re.findall(r"\}, (\d{4,})\);", spa))
    # Equality, not a range. The first version of this test allowed anything
    # from half the poll upward, and 5.0 — the exact value being fixed — sat on
    # the boundary and passed. A bound that admits the bug is not a bound.
    assert DEFAULT_TTL * 1000 == poll_ms, (
        f"TTL {DEFAULT_TTL}s and the {poll_ms/1000}s poll have drifted apart. "
        f"Below the poll, every poll is a miss and the fixed cost is paid twice "
        f"or more per poll; above it, staleness (TTL + poll) grows for nothing.")
