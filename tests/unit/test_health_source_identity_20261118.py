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
and the moment of that capture. These tests execute the property rather than
describe it:

* the field is there, and the four pre-existing fields keep their shapes
  (``test_the_answer_carries_the_identity_beside_the_unchanged_fields``);
* the digest recomputes, by the documented rule, from the tree this process
  imported - and every covered file is a module that is really in
  ``sys.modules`` (``test_the_reported_digest_recomputes_from_the_imported_tree``);
* editing a covered module's file on disk leaves the REPORTED digest untouched
  while a digest freshly computed from the changed bytes differs
  (``test_editing_the_imported_module_on_disk_does_not_move_the_reported_digest``)
  - that is the whole point, and it is why the assertion is a real byte change
  restored in a ``finally``, not a mocked hash;
* two consecutive calls report an identical identity, from the route and from a
  direct ``health_check()`` call, and the route still runs the payload on its own
  probe pool (``test_two_calls_report_one_identity``,
  ``test_the_route_still_runs_the_payload_in_its_own_probe_pool``).

The three pre-existing /health contracts are pinned next door and are run
unchanged alongside this file: ``test_health_reports_process_age.py`` (uptime
advances), ``test_health_reports_the_gate_export.py`` (the gate digest follows a
rewritten file) and
``test_health_answers_while_the_state_pool_is_saturated.py`` (the probe does not
queue behind State work).
"""
import hashlib
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import api.main as main

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
        assert set(identity) == {"root", "digest", "captured_at", "files", "reason"}
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


def test_editing_the_imported_module_on_disk_does_not_move_the_reported_digest(client):
    """The witness: the reported digest is the loaded bytes, not the disk's bytes."""
    identity = main.health_check()["source_identity"]       # captured at import
    root = Path(identity["root"])
    assert MUTATED_FILE in identity["files"]
    path = root / MUTATED_FILE
    original = path.read_bytes()
    # A real content change - not a different file, not a mock: the same length
    # class of edit a merge would make.
    changed = original + b"\n# a merge landed this line after the process started\n"
    assert changed != original

    try:
        path.write_bytes(changed)
        assert path.read_bytes() == changed

        reported_while_changed = main.health_check()["source_identity"]
        over_http_while_changed = client.get("/health").json()["source_identity"]
        # What the bytes on disk hash to NOW, computed independently of the
        # capture - both by reading the tree and by substituting the bytes.
        fresh = _digest_from_tree(root, identity["files"])
        expected = _digest_from_tree(root, identity["files"],
                                     replaced={MUTATED_FILE: changed})
    finally:
        path.write_bytes(original)

    # Nothing left behind.
    assert path.read_bytes() == original

    # The used-to-be-wrong answer is gone: a digest of the disk HAS moved, and it
    # moved exactly because of the edited file's bytes.
    assert fresh == expected
    assert fresh != identity["digest"]
    assert hashlib.sha256(changed).hexdigest() != hashlib.sha256(original).hexdigest()

    # The witness: the process keeps reporting the bytes it loaded, through both
    # entry points, capture moment included.
    assert reported_while_changed == identity
    assert over_http_while_changed == _as_json(identity)

    # And with the edit gone, the tree and the reported digest agree again - the
    # reported value was a digest of exactly these files all along.
    assert _digest_from_tree(root, identity["files"]) == identity["digest"]


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
