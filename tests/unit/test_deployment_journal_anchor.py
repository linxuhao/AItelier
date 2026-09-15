"""Storage-fault controls, including independently rehashed stale-history attacks."""
import base64
import copy
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from core import deployment_quiescence as dq


def quiet():
    value = {"schema_version": 1, "observed_at": "2026-09-14T10:00:00+00:00",
             "projects": [], "runs": [], "sidecar_owners": [],
             "godot_render_owners": [], "external_owners": [],
             "registered_external_owners": [], "blockers": {}, "errors": [],
             "quiescent": True}
    value["digest"] = dq._observation_digest(value)
    return value


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True).encode()).hexdigest()


def rehash(value):
    previous = None
    value["chain"] = []
    for position, event in enumerate(value["events"]):
        previous = canonical_hash({"version": 2, "journal_id": value["journal_id"],
                                   "position": position, "previous_hash": previous,
                                   "event": event})
        value["chain"].append(previous)
    value["latest"] = copy.deepcopy(value["events"][-1])


def write(path, value):
    path.write_text(json.dumps(value, sort_keys=True))


def history(path):
    first = dq.authorize("restart", quiet(), journal=path)
    dq.finalize(first, success=True, journal=path)
    stale = dq.authorize("restart", quiet(), journal=path)
    dq.finalize(stale, success=False, journal=path)
    return stale


def invoke(operation, path, clearance):
    if operation == "load":
        return dq._load_journal(path)
    if operation == "authorize":
        return dq.authorize("restart", quiet(), journal=path)
    if operation == "reconcile":
        return dq.reconcile(observation=quiet(), journal=path)
    if operation == "finalize":
        return dq.finalize(clearance, success=True, journal=path)
    from cli import server
    return server._finish_deployment(clearance, success=True)


@pytest.mark.parametrize("operation", ["load", "authorize", "reconcile", "finalize", "cli"])
@pytest.mark.parametrize("damage", ["stale-middle", "rehashed-middle", "prefix", "suffix", "v1-clone"])
def test_truncation_and_reroot_refused_by_every_path(tmp_path, monkeypatch, operation, damage):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path))
    path = dq.evidence_path()
    stale = history(path)
    value = json.loads(path.read_bytes())
    if damage in {"stale-middle", "rehashed-middle", "v1-clone"}:
        survivor = copy.deepcopy(stale["event"])
        survivor.pop("prior_event_id")
        value["events"] = [survivor]
        value["latest"] = copy.deepcopy(survivor)
        if damage == "v1-clone":
            value = {"version": 1, "events": [survivor], "latest": survivor}
            dq._anchor_path(path).unlink()  # even a clone to a new location is not auto-migrated
        elif damage == "rehashed-middle":
            rehash(value)
    elif damage == "prefix":
        value["events"] = value["events"][2:]
        value["events"][0].pop("prior_event_id")
        rehash(value)
    else:
        value["events"].pop()
        rehash(value)
    write(path, value)
    before = {p: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
    fence = dq.acquire_cutover_fence() if operation == "cli" else None
    stale["_cutover_fence"] = fence
    expected = ("anchor mismatch" if damage in {"rehashed-middle", "prefix", "suffix"}
                else "explicit hash-pinned migration" if damage == "v1-clone"
                else "content-hash chain")
    with pytest.raises(dq.DeploymentBlocked, match=expected):
        invoke(operation, path, stale)
    if fence:
        assert fence.closed
    for p, data in before.items():
        assert p.read_bytes() == data


@pytest.mark.parametrize("damage", ["missing", "different-journal", "count", "boolean-count", "head", "partial"])
def test_anchor_loss_or_mismatch_never_regenerates(tmp_path, damage):
    path = tmp_path / "journal.json"
    clearance = dq.authorize("restart", quiet(), journal=path)
    anchor_path = dq._anchor_path(path)
    anchor = json.loads(anchor_path.read_bytes())
    if damage == "missing":
        anchor_path.unlink()
    elif damage == "partial":
        anchor_path.write_bytes(b'{"version":2,')
    else:
        field, replacement = {
            "different-journal": ("journal_id", "f" * 32),
            "count": ("event_count", 0), "boolean-count": ("event_count", True),
            "head": ("head_hash", "0" * 64),
        }[damage]
        anchor[field] = replacement
        write(anchor_path, anchor)
    before = path.read_bytes()
    anchor_before = anchor_path.read_bytes() if anchor_path.exists() else None
    with pytest.raises(dq.DeploymentBlocked):
        dq.finalize(clearance, success=True, journal=path)
    assert path.read_bytes() == before
    assert (anchor_path.read_bytes() if anchor_path.exists() else None) == anchor_before


@pytest.mark.parametrize("damage", ["partial", "missing", "content", "duplicate-json-key", "nan"])
def test_journal_damage_is_preserved(tmp_path, damage):
    path = tmp_path / "journal.json"
    clearance = dq.authorize("restart", quiet(), journal=path)
    if damage == "missing":
        path.unlink()
    elif damage == "partial":
        path.write_bytes(b'{"version": 2,')
    elif damage == "duplicate-json-key":
        path.write_bytes(path.read_bytes().replace(b'"version": 2', b'"version": 1, "version": 2'))
    elif damage == "nan":
        path.write_bytes(path.read_bytes().replace(b'"version": 2', b'"extra": NaN, "version": 2'))
    else:
        value = json.loads(path.read_bytes())
        value["events"][0]["reason"] = "changed while retaining event ID"
        value["latest"] = copy.deepcopy(value["events"][0])
        write(path, value)
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(dq.DeploymentBlocked):
        dq.finalize(clearance, success=True, journal=path)
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("step", ["before-anchor", "after-anchor", "before-journal", "after-journal"])
def test_write_ahead_crash_boundaries(tmp_path, monkeypatch, initial, step):
    path = tmp_path / "journal.json"
    clearance = None if initial else dq.authorize("restart", quiet(), journal=path)
    old = path.read_bytes() if path.exists() else None
    real = dq._atomic_write

    def interrupted(target, value):
        which = "anchor" if target == dq._anchor_path(path) else "journal"
        if step == "before-" + which:
            raise OSError("injected power loss before durable rename")
        real(target, value)
        if step == "after-" + which:
            raise OSError("injected power loss after durable rename")

    monkeypatch.setattr(dq, "_atomic_write", interrupted)
    with pytest.raises(OSError, match="injected power loss"):
        if initial:
            dq.authorize("restart", quiet(), journal=path)
        else:
            dq.finalize(clearance, success=True, journal=path)
    monkeypatch.setattr(dq, "_atomic_write", real)
    if step in {"after-anchor", "before-journal"}:
        with pytest.raises(dq.DeploymentBlocked):
            dq._load_journal(path)
        assert (path.read_bytes() if path.exists() else None) == old
    elif step == "before-anchor":
        loaded = dq._load_journal(path)
        assert (loaded.get("latest") or {}).get("usable") is not True
    else:
        loaded = dq._load_journal(path)
        # A crash AFTER both fsynced writes is an already committed transaction.
        assert loaded["latest"]["usable"] is (not initial)


@pytest.mark.parametrize("fail_call", [1, 2])
def test_atomic_replace_failure_keeps_original_or_blocks(tmp_path, monkeypatch, fail_call):
    path = tmp_path / "journal.json"
    clearance = dq.authorize("restart", quiet(), journal=path)
    before = path.read_bytes()
    real = dq.os.replace
    calls = 0

    def fail(source, target):
        nonlocal calls
        calls += 1
        if calls == fail_call:
            raise OSError("rename failed")
        return real(source, target)

    monkeypatch.setattr(dq.os, "replace", fail)
    with pytest.raises(OSError, match="rename failed"):
        dq.finalize(clearance, success=True, journal=path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".journal.json.*"))
    if fail_call == 1:
        assert dq._load_journal(path)["latest"]["pending"] is True
    else:
        with pytest.raises(dq.DeploymentBlocked, match="anchor mismatch"):
            dq._load_journal(path)


def deployed_legacy():
    # Exact observed five-event producer schemas; operational payloads are synthetic.
    def pending(n, action):
        return {"event_id": str(n) * 32, "at": f"2026-09-14T10:0{n}:00+00:00",
                "status": "overridden", "action": action, "pending": True,
                "usable": False, "inventory_digest": str(n) * 64,
                "blockers": {}, "errors": [],
                "audit": {"actor": "fixture", "reason": "fixture", "ticket": "fixture"}}

    def completed(n, action):
        return {"event_id": str(n) * 32, "at": f"2026-09-14T10:0{n}:00+00:00",
                "status": "completed", "action": action, "usable": True,
                "prior_event_id": str(n - 1) * 32, "replayed": False}

    events = [pending(1, "redeploy"), completed(2, "redeploy"),
              {"event_id": "3" * 32, "at": "2026-09-14T10:03:00+00:00",
               "status": "aborted", "action": "restart", "usable": False,
               "inventory_digest": "3" * 64, "blockers": {}, "errors": [], "reason": "blocked"},
              pending(4, "restart"), completed(5, "restart")]
    return {"version": 1, "events": events, "latest": copy.deepcopy(events[-1])}


def migrate(path, mode="deployed-v1", **kwargs):
    return dq.migrate_legacy_journal(
        journal=path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        actor="test", provenance="reviewed test fixture", legacy_format=mode, **kwargs)


def test_deployed_migration_preserves_bytes_provenance_and_requires_fresh_gate(tmp_path):
    path = tmp_path / "journal.json"
    original = deployed_legacy()
    write(path, original)
    raw = path.read_bytes()
    with pytest.raises(dq.DeploymentBlocked):
        dq.authorize("restart", quiet(), journal=path)
    migrated = migrate(path)
    assert path.with_name(path.name + ".legacy-v1.backup").read_bytes() == raw
    assert migrated["migration"]["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert migrated["migration"]["source_bytes"] == len(raw)
    assert migrated["migration"]["actor"] == "test"
    assert migrated["migration"]["provenance"] == "reviewed test fixture"
    assert migrated["latest"]["status"] == "aborted"
    assert migrated["latest"]["usable"] is False
    assert len(migrated["events"]) == 6
    for old, new in zip(original["events"], migrated["events"]):
        assert all(new[key] == value for key, value in old.items())
    with pytest.raises(dq.DeploymentBlocked):
        dq.finalize({"event": original["events"][3]}, success=True, journal=path)
    pair = (path.read_bytes(), dq._anchor_path(path).read_bytes())
    for _ in range(3):
        assert dq._load_journal(path) == migrated
        assert dq.migrate_legacy_journal(
            journal=path, expected_sha256=hashlib.sha256(raw).hexdigest(), actor="retry",
            provenance="retry", legacy_format="deployed-v1") == migrated
        assert pair == (path.read_bytes(), dq._anchor_path(path).read_bytes())
    clearance = dq.authorize("restart", quiet(), journal=path)
    assert dq.finalize(clearance, success=True, journal=path)["event"]["usable"] is True


@pytest.mark.parametrize("damage", ["missing-terminal", "wrong-link", "wrong-action", "wrong-usable",
                                    "unexpected-field", "bad-id", "backwards-time", "latest",
                                    "contradictory-pending", "wrong-digest", "unknown-root-link"])
def test_ambiguous_or_corrupt_deployed_legacy_is_never_migrated(tmp_path, damage):
    path = tmp_path / "journal.json"
    value = deployed_legacy()
    if damage == "missing-terminal":
        value["events"].pop()
    elif damage == "wrong-link":
        value["events"][4]["prior_event_id"] = "1" * 32
    elif damage == "wrong-action":
        value["events"][4]["action"] = "redeploy"
    elif damage == "wrong-usable":
        value["events"][4]["usable"] = False
    elif damage == "unexpected-field":
        value["events"][4]["mystery"] = True
    elif damage == "bad-id":
        value["events"][0]["event_id"] = "not-an-id"
    elif damage == "backwards-time":
        value["events"][4]["at"] = "2025-01-01T00:00:00+00:00"
    elif damage == "contradictory-pending":
        value["events"][4]["pending"] = True
    elif damage == "wrong-digest":
        value["events"][0]["inventory_digest"] = "bad"
    elif damage == "unknown-root-link":
        value["events"][3]["prior_event_id"] = "f" * 32
    value["latest"] = copy.deepcopy(value["events"][-1])
    if damage == "latest":
        value["latest"]["at"] = "different"
    write(path, value)
    before = path.read_bytes()
    with pytest.raises(dq.DeploymentBlocked):
        migrate(path)
    assert path.read_bytes() == before
    assert not dq._anchor_path(path).exists()
    assert not path.with_name(path.name + ".legacy-v1.backup").exists()


def test_strict_linked_migration_refuses_missing_links_and_pending_roots(tmp_path):
    path = tmp_path / "journal.json"
    history(path)
    value = json.loads(path.read_bytes())
    legacy = {"version": 1, "events": value["events"], "latest": value["latest"]}
    dq._anchor_path(path).unlink()
    write(path, legacy)
    migrated = migrate(path, "linked-v1")
    assert migrated["latest"]["usable"] is False
    for events in [[copy.deepcopy(legacy["events"][2])], copy.deepcopy(legacy["events"])]:
        events[0].pop("prior_event_id", None)
        if len(events) > 1:
            events[2].pop("prior_event_id")
        other = tmp_path / f"legacy-{len(events)}.json"
        write(other, {"version": 1, "events": events, "latest": events[-1]})
        with pytest.raises(dq.DeploymentBlocked):
            migrate(other, "linked-v1")


@pytest.mark.parametrize("step", [
    "before-backup", "after-backup", "before-source", "after-source",
    "before-transaction", "after-transaction", "before-anchor", "after-anchor",
    "before-journal", "after-journal",
])
def test_migration_crash_never_loses_legacy_or_manufactures_completion(tmp_path, monkeypatch, step):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    backup = path.with_name(path.name + ".legacy-v1.backup")
    real = dq._atomic_write_bytes
    real_install = dq._install_backup_bytes

    def interrupted(target, payload):
        which = "anchor" if target == dq._anchor_path(path) else "journal"
        if step == "before-" + which:
            raise OSError("migration crash")
        receipt = real(target, payload)
        if step == "after-" + which:
            raise OSError("migration crash")
        return receipt

    source = dq._migration_source_path(path)
    transaction = dq._migration_transaction_path(path)

    def interrupted_backup(target, payload):
        which = ("backup" if target == backup else "source" if target == source
                 else "transaction")
        if step == "before-" + which:
            raise OSError("migration crash")
        real_install(target, payload)
        if step == "after-" + which:
            raise OSError("migration crash")

    monkeypatch.setattr(dq, "_atomic_write_bytes", interrupted)
    monkeypatch.setattr(dq, "_install_backup_bytes", interrupted_backup)
    with pytest.raises(OSError, match="migration crash"):
        migrate(path)
    monkeypatch.setattr(dq, "_atomic_write_bytes", real)
    monkeypatch.setattr(dq, "_install_backup_bytes", real_install)
    if step not in {"before-backup"}:
        assert backup.read_bytes() == raw
    if step not in {"before-backup", "after-backup", "before-source"}:
        assert source.read_bytes() == raw
    if step in {"after-journal"}:
        assert dq._load_journal(path)["latest"]["usable"] is False
    else:
        assert path.read_bytes() == raw
        with pytest.raises(dq.DeploymentBlocked):
            dq.authorize("restart", quiet(), journal=path)
        assert migrate(path)["latest"]["usable"] is False


@pytest.mark.parametrize("boundary", ["backup", "source", "transaction", "anchor", "journal"])
def test_backup_replacement_at_every_migration_publication_boundary_is_fail_closed(
        tmp_path, monkeypatch, boundary):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    backup = path.with_name(path.name + ".legacy-v1.backup")
    source = dq._migration_source_path(path)
    transaction = dq._migration_transaction_path(path)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement-corruption")
    real_install = dq._install_backup_bytes
    real_atomic = dq._atomic_write_bytes
    replaced = False

    def replace_after_install(target, payload):
        nonlocal replaced
        real_install(target, payload)
        which = ("backup" if target == backup else "source" if target == source
                 else "transaction")
        if which == boundary and not replaced:
            replacement.replace(backup)
            replaced = True

    def replace_after_atomic(target, payload):
        nonlocal replaced
        real_atomic(target, payload)
        which = "anchor" if target == dq._anchor_path(path) else "journal"
        if which == boundary and not replaced:
            replacement.replace(backup)
            replaced = True

    monkeypatch.setattr(dq, "_install_backup_bytes", replace_after_install)
    monkeypatch.setattr(dq, "_atomic_write_bytes", replace_after_atomic)
    with pytest.raises(dq.DeploymentBlocked):
        migrate(path)

    assert replaced
    assert backup.read_bytes() == b"replacement-corruption"
    if source.exists():
        assert source.is_file() and not source.is_symlink()
        assert source.read_bytes() == raw
    else:
        assert path.read_bytes() == raw
    if json.loads(path.read_bytes()).get("version") == 2:
        with pytest.raises(dq.DeploymentBlocked, match="migration evidence"):
            dq._load_journal(path)


def test_interrupted_migration_never_resumes_from_a_conflicting_anchor(tmp_path, monkeypatch):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    real = dq._atomic_write_bytes

    def crash_after_anchor(target, payload):
        real(target, payload)
        if target == dq._anchor_path(path):
            raise OSError("migration crash")

    monkeypatch.setattr(dq, "_atomic_write_bytes", crash_after_anchor)
    with pytest.raises(OSError, match="migration crash"):
        migrate(path)
    monkeypatch.setattr(dq, "_atomic_write_bytes", real)
    anchor = json.loads(dq._anchor_path(path).read_bytes())
    anchor["journal_hash"] = "0" * 64
    write(dq._anchor_path(path), anchor)

    with pytest.raises(dq.DeploymentBlocked, match="does not match durable anchor"):
        migrate(path)
    assert path.read_bytes() == raw
    assert path.with_name(path.name + ".legacy-v1.backup").read_bytes() == raw
    assert dq._migration_source_path(path).read_bytes() == raw


def test_migration_pin_and_backup_collisions_refuse_without_changes(tmp_path):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    before = path.read_bytes()
    with pytest.raises(dq.DeploymentBlocked, match="SHA-256"):
        dq.migrate_legacy_journal(journal=path, expected_sha256="0" * 64, actor="test",
                                  provenance="test", legacy_format="deployed-v1")
    backup = path.with_name(path.name + ".legacy-v1.backup")
    backup.write_bytes(b"older evidence")
    with pytest.raises(dq.DeploymentBlocked, match="different bytes"):
        migrate(path)
    assert path.read_bytes() == before
    assert backup.read_bytes() == b"older evidence"
    assert not dq._anchor_path(path).exists()


@pytest.mark.parametrize("target", ["journal", "matching-file"])
def test_migration_refuses_symlink_backup_without_changes(tmp_path, target):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    backup = path.with_name(path.name + ".legacy-v1.backup")
    matching = tmp_path / "matching-v1.json"
    matching.write_bytes(raw)
    backup.symlink_to(path.name if target == "journal" else matching.name)
    link_target = backup.readlink()

    with pytest.raises(dq.DeploymentBlocked, match="regular file"):
        migrate(path)

    assert path.read_bytes() == raw
    assert backup.is_symlink()
    assert backup.readlink() == link_target
    assert matching.read_bytes() == raw
    assert not dq._anchor_path(path).exists()


def test_existing_backup_open_uses_no_follow(tmp_path, monkeypatch):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    backup = path.with_name(path.name + ".legacy-v1.backup")
    backup.write_bytes(path.read_bytes())
    real_open = dq.os.open
    backup_flags = []

    def capture_flags(target, flags, *args, **kwargs):
        if Path(target) == backup:
            backup_flags.append(flags)
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(dq.os, "open", capture_flags)
    migrate(path)
    assert backup_flags
    assert all(flags & dq.os.O_NOFOLLOW for flags in backup_flags)


def test_migration_refuses_backup_replaced_during_descriptor_open(tmp_path, monkeypatch):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    backup = path.with_name(path.name + ".legacy-v1.backup")
    backup.write_bytes(raw)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(raw)
    original_identity = backup.stat().st_ino
    real_open = dq.os.open
    replaced = False

    def replace_after_open(target, flags, *args, **kwargs):
        nonlocal replaced
        fd = real_open(target, flags, *args, **kwargs)
        if Path(target) == backup and not replaced:
            replacement.replace(backup)
            replaced = True
        return fd

    monkeypatch.setattr(dq.os, "open", replace_after_open)
    with pytest.raises(dq.DeploymentBlocked, match="changed identity"):
        migrate(path)

    assert path.read_bytes() == raw
    assert backup.is_file() and not backup.is_symlink()
    assert backup.stat().st_ino != original_identity
    assert backup.read_bytes() == raw
    assert not dq._anchor_path(path).exists()


def test_migration_refuses_backup_replaced_after_validation(tmp_path, monkeypatch):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    backup = path.with_name(path.name + ".legacy-v1.backup")
    backup.write_bytes(raw)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(raw)
    real_read = dq._read_open_backup
    reads = 0

    def replace_before_precommit_read(target, fd, signature):
        nonlocal reads
        reads += 1
        if reads == 2:
            replacement.replace(backup)
        return real_read(target, fd, signature)

    monkeypatch.setattr(dq, "_read_open_backup", replace_before_precommit_read)
    with pytest.raises(dq.DeploymentBlocked, match="changed identity"):
        migrate(path)

    assert path.read_bytes() == raw
    assert backup.is_file() and not backup.is_symlink()
    assert backup.read_bytes() == raw
    assert not dq._anchor_path(path).exists()


def test_migration_never_replaces_backup_appearing_during_creation(tmp_path, monkeypatch):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    backup = path.with_name(path.name + ".legacy-v1.backup")
    raced_bytes = b"concurrent backup evidence"
    real_link = dq.os.link

    def create_before_link(source, destination, **kwargs):
        backup.write_bytes(raced_bytes)
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(dq.os, "link", create_before_link)
    with pytest.raises(dq.DeploymentBlocked, match="appeared during migration"):
        migrate(path)

    assert path.read_bytes() == raw
    assert backup.read_bytes() == raced_bytes
    assert not dq._anchor_path(path).exists()


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_migration_refuses_non_regular_backup_without_changes(tmp_path, kind):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    backup = path.with_name(path.name + ".legacy-v1.backup")
    if kind == "directory":
        backup.mkdir()
    else:
        dq.os.mkfifo(backup)

    with pytest.raises(dq.DeploymentBlocked, match="regular file"):
        migrate(path)

    assert path.read_bytes() == raw
    assert backup.is_dir() if kind == "directory" else stat.S_ISFIFO(backup.stat().st_mode)
    assert not dq._anchor_path(path).exists()


def test_matching_existing_regular_backup_is_stable_and_idempotent(tmp_path):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    source_sha256 = hashlib.sha256(raw).hexdigest()
    backup = path.with_name(path.name + ".legacy-v1.backup")
    backup.write_bytes(raw)
    identity = (backup.stat().st_dev, backup.stat().st_ino)

    migrated = migrate(path)
    pair = (path.read_bytes(), dq._anchor_path(path).read_bytes())
    assert backup.read_bytes() == raw
    assert (backup.stat().st_dev, backup.stat().st_ino) == identity

    assert dq.migrate_legacy_journal(
        journal=path, expected_sha256=source_sha256, actor="retry",
        provenance="retry", legacy_format="deployed-v1") == migrated
    assert (path.read_bytes(), dq._anchor_path(path).read_bytes()) == pair
    assert backup.read_bytes() == raw
    assert (backup.stat().st_dev, backup.stat().st_ino) == identity


@pytest.mark.parametrize("left,right", [
    ("journal", "backup"),
    ("journal", "source"),
    ("backup", "source"),
])
def test_migration_refuses_preexisting_hard_link_aliases_before_publication(
        tmp_path, left, right):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    entries = {
        "journal": path,
        "backup": path.with_name(path.name + ".legacy-v1.backup"),
        "source": dq._migration_source_path(path),
    }
    if left != "journal":
        entries[left].write_bytes(raw)
    os.link(entries[left], entries[right])

    with pytest.raises(dq.DeploymentBlocked, match="independent|link count"):
        migrate(path)

    assert path.read_bytes() == raw
    assert json.loads(path.read_bytes())["version"] == 1
    assert not dq._anchor_path(path).exists()
    assert entries[left].stat().st_ino == entries[right].stat().st_ino


def test_migration_refuses_unnamed_hard_link_before_publication(tmp_path, monkeypatch):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    source = dq._migration_source_path(path)
    transaction = dq._migration_transaction_path(path)
    external = tmp_path / "unreviewed-alias"
    real_install = dq._install_backup_bytes

    def link_after_transaction(target, payload):
        real_install(target, payload)
        if target == transaction:
            os.link(source, external)

    monkeypatch.setattr(dq, "_install_backup_bytes", link_after_transaction)
    with pytest.raises(dq.DeploymentBlocked, match="link count"):
        migrate(path)

    assert path.read_bytes() == raw
    assert not dq._anchor_path(path).exists()
    assert source.stat().st_nlink == 2


@pytest.mark.parametrize("target_name", ["journal", "backup", "source", "transaction"])
def test_completed_migration_rejects_unnamed_hard_links(tmp_path, target_name):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    migrate(path)
    target = {
        "journal": path,
        "backup": path.with_name(path.name + ".legacy-v1.backup"),
        "source": dq._migration_source_path(path),
        "transaction": dq._migration_transaction_path(path),
    }[target_name]
    os.link(target, tmp_path / f"{target_name}-external-alias")

    with pytest.raises(dq.DeploymentBlocked, match="link count"):
        dq._load_journal(path)


def test_completed_migration_rejects_backup_source_identity_alias(tmp_path):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    migrate(path)
    backup = path.with_name(path.name + ".legacy-v1.backup")
    source = dq._migration_source_path(path)
    source.unlink()
    os.link(backup, source)

    with pytest.raises(dq.DeploymentBlocked, match="independent|link count"):
        dq._load_journal(path)


@pytest.mark.parametrize("left,right", [
    ("journal", "backup"),
    ("journal", "source"),
    ("backup", "source"),
])
def test_distinct_identity_check_is_independent_of_link_count_observation(
        tmp_path, monkeypatch, left, right):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    entries = {
        "journal": path,
        "backup": path.with_name(path.name + ".legacy-v1.backup"),
        "source": dq._migration_source_path(path),
    }
    if left != "journal":
        entries[left].write_bytes(raw)
    os.link(entries[left], entries[right])
    real_signature = dq._backup_signature

    def signature_with_hidden_link_count(value):
        signature = list(real_signature(value))
        signature[3] = 1
        return tuple(signature)

    monkeypatch.setattr(dq, "_backup_signature", signature_with_hidden_link_count)
    with pytest.raises(dq.DeploymentBlocked, match="storage-independent"):
        migrate(path)

    assert path.read_bytes() == raw
    assert not dq._anchor_path(path).exists()


@pytest.mark.parametrize("target_name", ["journal", "backup", "source", "transaction"])
def test_identity_replacement_after_anchor_stops_before_v2_journal(
        tmp_path, monkeypatch, target_name):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    target = {
        "journal": path,
        "backup": path.with_name(path.name + ".legacy-v1.backup"),
        "source": dq._migration_source_path(path),
        "transaction": dq._migration_transaction_path(path),
    }[target_name]
    real_atomic = dq._atomic_write
    replaced = False

    def replace_after_anchor(destination, value):
        nonlocal replaced
        real_atomic(destination, value)
        if destination == dq._anchor_path(path) and not replaced:
            replacement = tmp_path / f"{target_name}-replacement"
            replacement.write_bytes(target.read_bytes())
            replacement.replace(target)
            replaced = True

    monkeypatch.setattr(dq, "_atomic_write", replace_after_anchor)
    with pytest.raises(dq.DeploymentBlocked, match="identity"):
        migrate(path)

    assert replaced
    assert path.read_bytes() == raw
    assert json.loads(path.read_bytes())["version"] == 1


@pytest.mark.parametrize("remove_alias", [False, True])
def test_link_count_change_after_anchor_stops_before_v2_journal(
        tmp_path, monkeypatch, remove_alias):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    source = dq._migration_source_path(path)
    external = tmp_path / "late-source-alias"
    real_atomic = dq._atomic_write

    def link_after_anchor(destination, value):
        real_atomic(destination, value)
        if destination == dq._anchor_path(path):
            os.link(source, external)
            if remove_alias:
                external.unlink()

    monkeypatch.setattr(dq, "_atomic_write", link_after_anchor)
    with pytest.raises(dq.DeploymentBlocked, match="identity|link count"):
        migrate(path)

    assert path.read_bytes() == raw
    assert json.loads(path.read_bytes())["version"] == 1
    assert source.stat().st_nlink == (1 if remove_alias else 2)


def test_migration_creates_four_independent_single_link_files_and_is_idempotent(tmp_path):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    migrated = migrate(path)
    paths = [
        path,
        path.with_name(path.name + ".legacy-v1.backup"),
        dq._migration_source_path(path),
        dq._migration_transaction_path(path),
    ]
    identities = {(entry.stat().st_dev, entry.stat().st_ino) for entry in paths}
    assert len(identities) == len(paths)
    assert all(entry.stat().st_nlink == 1 for entry in paths)
    pair = (path.read_bytes(), dq._anchor_path(path).read_bytes())

    assert dq.migrate_legacy_journal(
        journal=path, expected_sha256=hashlib.sha256(raw).hexdigest(), actor="retry",
        provenance="retry", legacy_format="deployed-v1") == migrated
    assert pair == (path.read_bytes(), dq._anchor_path(path).read_bytes())
    assert {(entry.stat().st_dev, entry.stat().st_ino) for entry in paths} == identities
    assert all(entry.stat().st_nlink == 1 for entry in paths)


@pytest.mark.parametrize("target_name", ["backup", "source", "transaction", "anchor"])
def test_final_journal_publication_refuses_same_byte_path_replacement(
        tmp_path, monkeypatch, target_name):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    target = {
        "backup": path.with_name(path.name + ".legacy-v1.backup"),
        "source": dq._migration_source_path(path),
        "transaction": dq._migration_transaction_path(path),
        "anchor": dq._anchor_path(path),
    }[target_name]
    original_identity = None
    real_atomic = dq._atomic_write

    def replace_at_final_publication(destination, value):
        nonlocal original_identity
        if destination == path and original_identity is None:
            original_identity = (target.stat().st_dev, target.stat().st_ino)
            replacement = tmp_path / f"same-byte-{target_name}-replacement"
            replacement.write_bytes(target.read_bytes())
            replacement.replace(target)
        return real_atomic(destination, value)

    monkeypatch.setattr(dq, "_atomic_write", replace_at_final_publication)
    with pytest.raises(dq.DeploymentBlocked):
        migrate(path)

    assert original_identity is not None
    assert (target.stat().st_dev, target.stat().st_ino) != original_identity
    published = json.loads(path.read_bytes())
    if published["version"] == 2:
        assert published["latest"]["usable"] is False


@pytest.mark.parametrize("target_name", ["backup", "source", "transaction", "anchor"])
def test_final_journal_publication_refuses_new_proof_hardlink(
        tmp_path, monkeypatch, target_name):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    target = {
        "backup": path.with_name(path.name + ".legacy-v1.backup"),
        "source": dq._migration_source_path(path),
        "transaction": dq._migration_transaction_path(path),
        "anchor": dq._anchor_path(path),
    }[target_name]
    real_atomic = dq._atomic_write
    injected = False

    def link_at_final_publication(destination, value):
        nonlocal injected
        if destination == path and not injected:
            os.link(target, tmp_path / f"late-{target_name}-alias")
            injected = True
        return real_atomic(destination, value)

    monkeypatch.setattr(dq, "_atomic_write", link_at_final_publication)
    with pytest.raises(dq.DeploymentBlocked):
        migrate(path)

    assert injected
    assert target.stat().st_nlink == 2


@pytest.mark.parametrize("replacement", [
    "changed-content", "same-bytes-new-inode", "hardlink",
])
def test_new_anchor_identity_is_bound_before_v2_commit(
        tmp_path, monkeypatch, replacement):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    anchor = dq._anchor_path(path)
    real_atomic = dq._atomic_write

    def replace_after_anchor(destination, value):
        real_atomic(destination, value)
        if destination == anchor:
            if replacement == "hardlink":
                os.link(anchor, tmp_path / "anchor-alias")
            else:
                substitute = tmp_path / "anchor-substitute"
                substitute.write_bytes(
                    b"{}\n" if replacement == "changed-content" else anchor.read_bytes())
                substitute.replace(anchor)

    monkeypatch.setattr(dq, "_atomic_write", replace_after_anchor)
    with pytest.raises(dq.DeploymentBlocked):
        migrate(path)

    assert path.read_bytes() == raw


@pytest.mark.parametrize("replacement", ["changed-content", "same-bytes-new-inode"])
def test_existing_anchor_is_held_through_final_proof_validation(
        tmp_path, monkeypatch, replacement):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    migrate(path)
    path.write_bytes(raw)
    anchor = dq._anchor_path(path)
    real_validate = dq._validate_open_evidence
    calls = 0

    def replace_before_final(entries):
        nonlocal calls
        calls += 1
        if calls == 2:
            substitute = tmp_path / "anchor-substitute"
            substitute.write_bytes(
                b"{}\n" if replacement == "changed-content" else anchor.read_bytes())
            substitute.replace(anchor)
        return real_validate(entries)

    monkeypatch.setattr(dq, "_validate_open_evidence", replace_before_final)
    with pytest.raises(dq.DeploymentBlocked):
        migrate(path)

    assert calls >= 2
    assert path.read_bytes() == raw


def test_same_byte_source_replacement_after_return_keeps_exact_recovery(tmp_path):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    migrated = migrate(path)
    source = dq._migration_source_path(path)
    original_identity = (source.stat().st_dev, source.stat().st_ino)
    replacement = tmp_path / "post-return-source-replacement"
    replacement.write_bytes(raw)
    replacement.replace(source)

    assert (source.stat().st_dev, source.stat().st_ino) != original_identity
    assert base64.b64decode(
        migrated["migration"]["source_base64"], validate=True) == raw
    assert dq._load_journal(path) == migrated


@pytest.mark.parametrize("damage", ["remove-source", "add-source-link"])
def test_final_journal_publication_race_cannot_make_v2_loadable_without_valid_evidence(
        tmp_path, monkeypatch, damage):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    source = dq._migration_source_path(path)
    real_atomic = dq._atomic_write
    injected = False

    def damage_at_final_publication(destination, value):
        nonlocal injected
        if destination == path and not injected:
            injected = True
            if damage == "remove-source":
                source.unlink()
            else:
                os.link(source, tmp_path / "late-source-alias")
        real_atomic(destination, value)

    monkeypatch.setattr(dq, "_atomic_write", damage_at_final_publication)
    with pytest.raises(dq.DeploymentBlocked):
        migrate(path)

    assert injected
    published = json.loads(path.read_bytes())
    assert published["version"] == 2
    assert base64.b64decode(
        published["migration"]["source_base64"], validate=True) == raw
    with pytest.raises(dq.DeploymentBlocked):
        dq._load_journal(path)


def test_migration_rejects_noncanonical_or_mismatched_embedded_recovery(tmp_path):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    migrate(path)
    source_bytes = path.with_name(path.name + ".legacy-v1.backup").read_bytes()

    for source_base64 in [
        "not base64!", base64.b64encode(b"x" * len(source_bytes)).decode("ascii")
    ]:
        value = json.loads(path.read_bytes())
        value["migration"]["source_base64"] = source_base64
        value["chain"] = dq._chain(value)
        write(path, value)
        write(dq._anchor_path(path), dq._checkpoint(value))
        transaction_path = dq._migration_transaction_path(path)
        transaction = json.loads(transaction_path.read_bytes())
        transaction["journal"] = copy.deepcopy(value)
        write(transaction_path, transaction)
        with pytest.raises(dq.DeploymentBlocked, match="recovery"):
            dq._load_journal(path)


@pytest.mark.parametrize("evidence", ["backup", "source", "transaction"])
def test_completed_migration_rejects_evidence_identity_drift(tmp_path, evidence):
    path = tmp_path / "journal.json"
    write(path, deployed_legacy())
    raw = path.read_bytes()
    migrate(path)
    evidence_path = {
        "backup": path.with_name(path.name + ".legacy-v1.backup"),
        "source": dq._migration_source_path(path),
        "transaction": dq._migration_transaction_path(path),
    }[evidence]
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement-corruption")
    replacement.replace(evidence_path)

    with pytest.raises(dq.DeploymentBlocked, match="migration evidence|unreadable"):
        dq._load_journal(path)
    retained = [candidate.read_bytes() for candidate in (
        path.with_name(path.name + ".legacy-v1.backup"),
        dq._migration_source_path(path),
    )]
    assert raw in retained


def test_content_chain_guard_is_independent_of_checkpoint(tmp_path):
    path = tmp_path / "journal.json"
    dq.authorize("restart", quiet(), journal=path)
    value = json.loads(path.read_bytes())
    value["events"][0]["reason"] = "changed content retaining stale content chain"
    value["latest"] = copy.deepcopy(value["events"][0])
    write(path, value)
    # A matching envelope checksum is insufficient: the event chain must also verify.
    anchor = json.loads(dq._anchor_path(path).read_bytes())
    anchor["journal_hash"] = canonical_hash(value)
    write(dq._anchor_path(path), anchor)
    with pytest.raises(dq.DeploymentBlocked, match="content-hash chain"):
        dq._load_journal(path)


@pytest.mark.parametrize("fail_call", [1, 2, 3, 4])
def test_file_and_directory_fsync_failures_are_fail_closed(tmp_path, monkeypatch, fail_call):
    path = tmp_path / "journal.json"
    clearance = dq.authorize("restart", quiet(), journal=path)
    real = dq.os.fsync
    calls = 0

    def fail(fd):
        nonlocal calls
        calls += 1
        if calls == fail_call:
            raise OSError("fsync failed")
        return real(fd)

    monkeypatch.setattr(dq.os, "fsync", fail)
    with pytest.raises(OSError, match="fsync failed"):
        dq.finalize(clearance, success=True, journal=path)
    if fail_call == 1:
        assert dq._load_journal(path)["latest"]["pending"] is True
    elif fail_call in {2, 3}:
        with pytest.raises(dq.DeploymentBlocked, match="anchor mismatch"):
            dq._load_journal(path)
    else:
        # Both renames are visible, although a real power loss could lose the last
        # rename whose directory fsync failed. Either old journal then mismatches
        # the durable new anchor, or the committed new pair is readable.
        assert dq._load_journal(path)["latest"]["status"] == "completed"
