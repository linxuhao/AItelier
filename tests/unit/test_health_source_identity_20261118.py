"""/health must say WHICH source bytes the answering process loaded.

The backend binds the deployed checkout into its container, so a merge changes the
files on disk while the running process keeps the modules it already imported.
Three existing fields cannot answer "did my deploy load what I merged?":

* ``gate_harness.sha256`` is read per request BY DESIGN, so it moves at a merge
  even though nothing reloaded - it follows the disk.
* ``process.started_at`` proves a restart happened at some moment, not what the
  restarted process read.
* ``status`` / ``engine`` are constants.

So the answer carries one more field, ``source_identity``: the root this process
imported from, a digest over the covered modules' files captured ONCE at import,
the whole checkout's git commit and dirty state captured alongside it, and the
moment of that capture. These tests execute the property rather than describe it:

* the field is there, and the four pre-existing fields keep their shapes
  (``test_the_answer_carries_the_identity_beside_the_unchanged_fields``);
* the digest recomputes, by the documented rule, from the tree this process
  imported - and every covered file is a module that is really in
  ``sys.modules`` (``test_the_reported_digest_recomputes_from_the_imported_tree``);
* editing a covered module's file on disk leaves the REPORTED digest untouched
  while a digest freshly computed from the changed bytes differs
  (``test_editing_the_imported_module_on_disk_does_not_move_the_reported_digest``)
  - that is the whole point, and it is why the assertion is a real byte change,
  not a mocked hash. The edit is made in a copy of the covered files under
  ``tmp_path`` whose capture the process reports, never in the checkout itself,
  so the row also runs on a read-only checkout and beside parallel rows;
* two consecutive calls report an identical identity, from the route and from a
  direct ``health_check()`` call, and the route still runs the payload on its own
  probe pool (``test_two_calls_report_one_identity``,
  ``test_the_route_still_runs_the_payload_in_its_own_probe_pool``).

The four-file digest cannot see a merge that leaves those four files unchanged.
A real scratch repository proves the added `vcs` half: two commits give two
reported ids, a later commit does not move an earlier capture, and a git
failure (no repository, no git on PATH, a non-zero exit, a timeout) is DECLARED
with commit=None and dirty=None while /health still answers ok.

The three pre-existing /health contracts are pinned next door and are run
unchanged alongside this file: ``test_health_reports_process_age.py`` (uptime
advances), ``test_health_reports_the_gate_export.py`` (the gate digest follows a
rewritten file) and
``test_health_answers_while_the_state_pool_is_saturated.py`` (the probe does not
queue behind State work).
"""
import hashlib
import json
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import api.main as main
from core import source_identity

# The digest set, fixed in api/main.py. The mutated file below must be one of
# them, and it must be a module this process imported - both are asserted.
MUTATED_FILE = "api/_read_cache.py"


def _digest_from_tree(root, files, replaced=None):
    """The digest exactly as ``api.main._source_identity``'s docstring states it.

    A reader recomputes the reported value with this: one outer SHA-256 over, for
    each repo-relative file in the declared order, ``path NUL sha256(bytes) LF``.
    ``replaced`` substitutes in-memory bytes for a path, so a caller can ask "what
    WOULD the digest be if that file held these bytes" without writing them.
    """
    replaced = replaced or {}
    outer = hashlib.sha256()
    for rel in files:
        payload = (replaced[rel] if rel in replaced
                   else (Path(root) / rel).read_bytes())
        outer.update(rel.encode() + b"\0")
        outer.update(hashlib.sha256(payload).hexdigest().encode() + b"\n")
    return outer.hexdigest()


def _as_json(value):
    """The value as it arrives over HTTP (tuples become lists)."""
    return json.loads(json.dumps(value))


def _module_name(rel):
    return rel[:-len(".py")].replace("/", ".")


def test_the_answer_carries_the_identity_beside_the_unchanged_fields(client):
    direct = main.health_check()
    over_http = client.get("/health").json()

    for body in (direct, _as_json(over_http)):
        # The four pre-existing keys, with the shapes they always had.
        assert body["status"] == "ok"
        assert body["engine"] == "DPE SOTA v3.0"
        assert set(body["gate_harness"]) == {"path", "sha256", "reason"}
        assert set(body["process"]) == {"started_at", "uptime_seconds", "reason"}

        identity = body["source_identity"]
        # The five old keys keep their names and meanings; `vcs` is the added
        # whole-tree fact, asserted in its own tests below.
        assert set(identity) == {"root", "digest", "captured_at", "files",
                                 "reason", "vcs"}
        assert identity["reason"] is None
        assert Path(identity["root"]).is_dir()
        assert isinstance(identity["digest"], str) and len(identity["digest"]) == 64
        int(identity["digest"], 16)                     # it is hex, not prose
        captured = datetime.fromisoformat(identity["captured_at"])
        assert captured.tzinfo is not None              # an absolute moment
        assert list(identity["files"]) == list(main.SOURCE_IDENTITY_MODULES)
        assert identity["files"]
        # Captured by THIS process, after it started (both fields are read from
        # this process, at import and per request respectively).
        started = datetime.fromisoformat(body["process"]["started_at"])
        assert started.timestamp() - 5 <= captured.timestamp() <= \
            datetime.now(timezone.utc).timestamp() + 5

    # The route and a caller with no event loop answer identically.
    assert over_http["source_identity"] == _as_json(direct["source_identity"])


def test_the_reported_digest_recomputes_from_the_imported_tree():
    identity = main.health_check()["source_identity"]
    root = Path(identity["root"])

    # The root is the directory this process's own module was imported from, not
    # a guess and not a configured path.
    assert (root / "api" / "main.py").resolve() == Path(main.__file__).resolve()

    for rel in identity["files"]:
        module = _module_name(rel)
        assert module in sys.modules, f"{module} was never imported"
        assert (root / rel).is_file(), rel

    assert _digest_from_tree(root, identity["files"]) == identity["digest"]


def test_editing_the_imported_module_on_disk_does_not_move_the_reported_digest(
        client, tmp_path, monkeypatch):
    """The witness: the reported digest is the loaded bytes, not the disk's bytes.

    The checkout may be mounted read-only and other rows read it concurrently, so
    the byte change is made in a copy of the covered files, and the process is
    made to report that copy's capture - the same one-time reading
    ``api.main`` makes at import, taken from a tree this row may write.
    """
    imported = main.health_check()["source_identity"]      # captured at import
    imported_root = Path(imported["root"])
    files = imported["files"]
    assert MUTATED_FILE in files
    assert _module_name(MUTATED_FILE) in sys.modules

    tree = tmp_path / "tree"
    for rel in files:
        target = tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((imported_root / rel).read_bytes())
    # The copy holds the imported bytes: the same digest by the same rule.
    assert source_identity.compute_digest(tree, files) == imported["digest"]

    monkeypatch.setattr(main, "_SOURCE_IDENTITY", source_identity.capture(tree))
    identity = main.health_check()["source_identity"]
    assert identity["root"] == str(tree)
    assert identity["reason"] is None
    assert identity["digest"] == imported["digest"]

    path = tree / MUTATED_FILE
    original = path.read_bytes()
    # A real content change - not a different file, not a mock: the same length
    # class of edit a merge would make.
    changed = original + b"\n# a merge landed this line after the process started\n"
    assert changed != original
    path.write_bytes(changed)
    assert path.read_bytes() == changed

    # The witness: the process keeps reporting the bytes it captured, through
    # both entry points, capture moment included.
    assert main.health_check()["source_identity"] == identity
    assert client.get("/health").json()["source_identity"] == _as_json(identity)

    # A digest of the disk HAS moved, and it moved exactly because of the edited
    # file's bytes - computed independently of the capture, both by reading the
    # tree and by substituting the bytes.
    fresh = source_identity.compute_digest(tree, files)
    assert fresh == _digest_from_tree(tree, files, replaced={MUTATED_FILE: changed})
    assert fresh != identity["digest"]

    # And with the edit gone, the tree and the reported digest agree again - the
    # reported value was a digest of exactly these files all along.
    path.write_bytes(original)
    assert source_identity.compute_digest(tree, files) == identity["digest"]
    assert main.health_check()["source_identity"] == identity

    # The other direction: a capture taken while the copy held DIFFERENT bytes.
    # Now the copy and the imported checkout both hash to the imported digest,
    # so a reported digest re-read from either root - the captured one or this
    # module's own checkout - would move away from the capture.
    path.write_bytes(changed)
    edited = source_identity.capture(tree)
    path.write_bytes(original)
    assert edited["digest"] != imported["digest"]
    monkeypatch.setattr(main, "_SOURCE_IDENTITY", edited)
    assert main.health_check()["source_identity"] == {**edited, "files": list(edited["files"])}
    assert client.get("/health").json()["source_identity"] == _as_json(edited)


def test_two_calls_report_one_identity(client):
    first = client.get("/health").json()["source_identity"]
    second = client.get("/health").json()["source_identity"]
    direct = main.health_check()["source_identity"]

    assert first == second == _as_json(direct)
    assert direct == _as_json(main._SOURCE_IDENTITY)


def test_the_answer_cannot_edit_the_captured_identity():
    """Reported "as-is" means a copy: a caller cannot rewrite the witness."""
    handed_out = main.health_check()["source_identity"]
    handed_out["digest"] = "tampered"
    handed_out["root"] = "/elsewhere"
    handed_out["files"].append("core/nothing_was_ever_imported.py")

    again = main.health_check()["source_identity"]
    assert again["digest"] == main._SOURCE_IDENTITY["digest"]
    assert again["root"] == main._SOURCE_IDENTITY["root"]
    assert again["files"] == list(main.SOURCE_IDENTITY_MODULES)


def test_the_route_still_runs_the_payload_in_its_own_probe_pool(client, monkeypatch):
    """Placement is part of the contract: the payload is not run on the loop."""
    seen = {}
    payload = main.health_check

    def spy():
        seen["thread"] = threading.current_thread()
        return payload()

    monkeypatch.setattr(main, "health_check", spy)

    client.get("/health")

    thread = seen["thread"]
    assert thread is not threading.main_thread()
    assert thread.name.startswith("health-probe")
    # The sizing is the frozen part: two probe threads, whatever the payload does.
    assert main._HEALTH_PROBE_THREADS == 2
    assert main._health_probe_pool()._max_workers == 2


_COMMIT_ID = re.compile(r"[0-9a-f]{40}\Z")


def _git(repo, *args):
    """A real git command against a scratch repository the test created."""
    done = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True, timeout=30)
    return done.stdout.strip()


def _scratch_repo(path):
    """A clean checkout with the covered files present, made in tmp_path."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "health@example.invalid")
    _git(path, "config", "user.name", "health test")
    for rel in source_identity.SOURCE_IDENTITY_MODULES:
        covered = path / rel
        covered.parent.mkdir(parents=True, exist_ok=True)
        covered.write_text(f"# {rel}\nfirst\n", encoding="utf-8")
    _git(path, "add", "--", *source_identity.SOURCE_IDENTITY_MODULES)
    _git(path, "commit", "-q", "-m", "first commit")
    return path


def _commit_file(repo, rel, text, message):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _assert_degraded(record, reason_fragment):
    """Both git facts are absent and DECLARED; the digest half is untouched."""
    vcs = record["vcs"]
    assert set(vcs) == {"commit", "dirty", "reason"}
    assert vcs["commit"] is None
    assert vcs["dirty"] is None
    assert isinstance(vcs["reason"], str) and vcs["reason"]
    assert reason_fragment in vcs["reason"], vcs["reason"]
    assert isinstance(record["digest"], str) and len(record["digest"]) == 64
    assert list(record["files"]) == list(source_identity.SOURCE_IDENTITY_MODULES)
    assert record["reason"] is None
    # The failure is declared, never raised out of capture or out of /health.
    assert main.health_check()["status"] == "ok"


def test_two_distinct_commits_give_two_reported_vcs_commits(tmp_path):
    repo = _scratch_repo(tmp_path / "two-commits")
    first_commit = _git(repo, "rev-parse", "HEAD")
    assert _COMMIT_ID.fullmatch(first_commit)

    first = source_identity.capture(repo)
    assert first["vcs"]["commit"] == first_commit
    assert first["vcs"]["dirty"] is False
    assert first["vcs"]["reason"] is None

    second_commit = _commit_file(repo, "core/state_channels.py",
                                 "a merge outside the digest\n", "merge elsewhere")
    assert _COMMIT_ID.fullmatch(second_commit)
    assert second_commit != first_commit
    assert _git(repo, "rev-parse", "HEAD") == second_commit

    second = source_identity.capture(repo)
    assert second["vcs"]["commit"] == second_commit
    assert second["vcs"]["dirty"] is False
    assert first["vcs"]["commit"] == first_commit
    # Exactly why the added field exists: the four covered files never changed.
    assert first["digest"] == second["digest"]


def test_a_capture_never_follows_the_repo_or_a_second_answer(tmp_path):
    repo = _scratch_repo(tmp_path / "snapshot")
    before = _git(repo, "rev-parse", "HEAD")
    captured = source_identity.capture(repo)
    assert captured["vcs"]["commit"] == before

    after = _commit_file(repo, "core/state_channels.py",
                         "merged after import\n", "merge after capture")
    assert after != before
    assert _git(repo, "rev-parse", "HEAD") == after
    assert captured["vcs"]["commit"] == before
    assert captured["vcs"]["dirty"] is False
    assert source_identity.capture(repo)["vcs"]["commit"] == after

    first = main.health_check()["source_identity"]
    second = main.health_check()["source_identity"]
    assert first["captured_at"] == second["captured_at"] == \
        main._SOURCE_IDENTITY["captured_at"]
    assert first["vcs"]["commit"] == second["vcs"]["commit"] == \
        main._SOURCE_IDENTITY["vcs"]["commit"]
    # The app was imported from this checkout, whose HEAD names a real commit.
    assert _COMMIT_ID.fullmatch(first["vcs"]["commit"])


def test_not_a_git_repository_is_declared(tmp_path):
    plain = tmp_path / "plain-tree"                 # a real directory, no .git
    plain.mkdir()
    for rel in source_identity.SOURCE_IDENTITY_MODULES:
        target = plain / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("readable\n", encoding="utf-8")

    _assert_degraded(source_identity.capture(plain), "not a git repository")


def test_git_absent_from_path_is_declared(tmp_path, monkeypatch):
    repo = _scratch_repo(tmp_path / "without-git")
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    _assert_degraded(source_identity.capture(repo), "git not found on PATH")


def test_a_nonzero_git_exit_is_declared(tmp_path, monkeypatch):
    repo = _scratch_repo(tmp_path / "failing-git")

    def refuse(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 128, stdout="",
                                           stderr="fatal: deliberate git failure")

    monkeypatch.setattr(source_identity.subprocess, "run", refuse)
    record = source_identity.capture(repo)
    _assert_degraded(record, "exit 128")
    assert "deliberate git failure" in record["vcs"]["reason"]


def test_a_git_timeout_is_declared(tmp_path, monkeypatch):
    repo = _scratch_repo(tmp_path / "hanging-git")

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 5.0))

    monkeypatch.setattr(source_identity.subprocess, "run", hang)
    _assert_degraded(source_identity.capture(repo), "timed out")


def test_an_unreadable_covered_file_keeps_its_own_reason(tmp_path):
    repo = _scratch_repo(tmp_path / "missing-covered-file")
    (repo / MUTATED_FILE).unlink()
    record = source_identity.capture(repo)

    assert record["digest"] is None
    assert MUTATED_FILE in record["reason"]
    assert record["vcs"]["commit"] == _git(repo, "rev-parse", "HEAD")
    assert record["vcs"]["reason"] is None
