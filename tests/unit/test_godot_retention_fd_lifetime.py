"""Real selected-FD lifetime and all-exit cleanup controls; no engine effects."""
import errno
import json
import os
from pathlib import Path

import pytest

from tests.unit.test_godot_retention_selected_inode import _load, _home_with


def _fd_count():
    return len(os.listdir("/proc/self/fd"))


def _closed(fd):
    with pytest.raises(OSError) as exc:
        os.fstat(fd)
    assert exc.value.errno == errno.EBADF


def test_live_selected_pin_prevents_unlink_recreate_inode_reuse(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {"runs/a/report.json": '{"selected":1}'})
    original = gh._retain_walk_pattern
    pins, identities = [], []
    before = _fd_count()

    def unlink_recreate(root_fd, pattern, budget):
        matches, refused = original(root_fd, pattern, budget)
        for rel in matches:
            fd = budget["selected"][rel]
            pins.append(fd)
            selected = os.fstat(fd)
            target = home / rel
            target.unlink()
            # An unlinked but OPEN selected object cannot have its inode
            # recycled, even though unpinned unlink/recreate did so in FIRST5d.
            for n in range(16):
                target.write_text('{"replacement":%d}' % n)
                current = target.stat()
                identities.append((selected.st_ino, current.st_ino))
                assert (selected.st_dev, selected.st_ino) != (current.st_dev, current.st_ino)
                assert os.pread(fd, 100, 0) == b'{"selected":1}'
                if n != 15:
                    target.unlink()
        return matches, refused

    monkeypatch.setattr(gh, "_retain_walk_pattern", unlink_recreate)
    out = gh._retain_copy([], [home], [], {}, patterns=["runs/*/report.json"], requested=True)
    assert out["ok"] is False and out["missing"] == ["runs/a/report.json"]
    assert out["files"] == [] and out["refused"] == []
    assert pins and len(identities) == 16
    assert _fd_count() == before
    for fd in pins:
        _closed(fd)
    Path("/review/actual-selected-pin-inode-lifetime.json").write_text(json.dumps(
        {"selected_replacement_inode_pairs": identities, "pins": pins,
         "selected_original_bytes_read_from_live_fd": True,
         "replacement_not_copied": True, "result": out,
         "fd_before": before, "fd_after": _fd_count()}, indent=2) + "\n")


def test_discovery_pin_stays_live_through_real_copy_then_closes(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {"runs/a/report.json": '{"selected":1}'})
    walk, copy = gh._retain_walk_pattern, gh._safe_copy_file
    pins, consumed = [], []
    before = _fd_count()

    def observed_walk(root_fd, pattern, budget):
        out = walk(root_fd, pattern, budget)
        pins.extend(budget["selected"].values())
        return out

    def observed_copy(src, dst):
        assert pins
        identity = os.fstat(pins[0])
        actual = os.fstat(src.read_fd)
        assert (identity.st_dev, identity.st_ino) == (actual.st_dev, actual.st_ino)
        assert os.pread(pins[0], 100, 0) == b'{"selected":1}'
        result = copy(src, dst)
        assert os.fstat(pins[0]).st_ino == identity.st_ino
        consumed.append(result)
        return result

    monkeypatch.setattr(gh, "_retain_walk_pattern", observed_walk)
    monkeypatch.setattr(gh, "_safe_copy_file", observed_copy)
    out = gh._retain_copy([], [home], [], {}, patterns=["runs/*/report.json"], requested=True)
    assert out["ok"] and consumed == [len(b'{"selected":1}')]
    assert (Path(out["retained_dir"]) / out["files"][0]["path"]).read_bytes() == b'{"selected":1}'
    assert _fd_count() == before
    for fd in pins:
        _closed(fd)


@pytest.mark.parametrize("exit_path", [
    "success", "duplicate", "total-bytes", "file-count",
    "walk-error-after-transfer", "copy-oserror", "copy-unexpected", "sibling-pass"])
def test_selected_fd_cleanup_on_every_consumption_exit(monkeypatch, tmp_path, exit_path):
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {
        "runs/a/report.json": '{"a":1}', "runs/b/report.json": '{"b":2}',
        "runs/c/report.json": '{"c":3}', "prior1.json": "{}", "prior2.json": "{}"})
    walk = gh._retain_walk_pattern
    pins = []
    before = _fd_count()

    def observed_walk(root_fd, pattern, budget):
        result = walk(root_fd, pattern, budget)
        pins.extend(budget["selected"].values())
        if exit_path == "sibling-pass" and Path(os.readlink("/proc/self/fd/%d" % root_fd)) == home:
            for rel in result[0]:
                (home / rel).unlink()
        if exit_path == "walk-error-after-transfer":
            raise OSError("owned injected discovery handoff failure")
        return result

    monkeypatch.setattr(gh, "_retain_walk_pattern", observed_walk)
    declared = []
    homes = [home]
    patterns = ["runs/*/report.json"]
    if exit_path == "sibling-pass":
        homes.append(_home_with(tmp_path, {
            "runs/a/report.json": '{"sibling":1}', "runs/b/report.json": '{"sibling":2}',
            "runs/c/report.json": '{"sibling":3}'}, name="sibling-home"))
    if exit_path == "duplicate":
        declared = ["runs/a/report.json"]
        patterns += ["runs/*/report.json"]
    elif exit_path == "total-bytes":
        monkeypatch.setattr(gh, "_RETAIN_MAX_BYTES", 0)
    elif exit_path == "file-count":
        declared = ["prior1.json", "prior2.json"]
        monkeypatch.setattr(gh, "_RETAIN_MAX_FILES", 3)
    elif exit_path in ("copy-oserror", "copy-unexpected"):
        def failing_copy(src, dst):
            actual = os.fstat(src.read_fd)
            assert pins and any((os.fstat(fd).st_dev, os.fstat(fd).st_ino)
                                == (actual.st_dev, actual.st_ino) for fd in pins)
            if exit_path == "copy-oserror":
                raise OSError("owned copy failure")
            raise RuntimeError("owned unexpected copy failure")
        monkeypatch.setattr(gh, "_safe_copy_file", failing_copy)

    if exit_path == "copy-unexpected":
        with pytest.raises(RuntimeError, match="^owned unexpected copy failure$"):
            gh._retain_copy(declared, homes, [], {}, patterns=patterns, requested=True)
    else:
        out = gh._retain_copy(declared, homes, [], {}, patterns=patterns, requested=True)
        assert out["ok"] is (exit_path in ("success", "duplicate")), out
        if exit_path in ("success", "duplicate"):
            assert len(out["files"]) == 3
        elif exit_path == "total-bytes":
            assert out["limit_hit"] == "total_bytes" and out["files"] == []
        elif exit_path == "file-count":
            assert out["limit_hit"] == "file_count" and len(out["files"]) == 3
        elif exit_path == "sibling-pass":
            assert out["missing"] == ["runs/a/report.json", "runs/b/report.json", "runs/c/report.json"]
            assert len(out["files"]) == 3 and all(row["pass"] == 1 for row in out["files"])
        else:
            assert out["refused"] and out["files"] == []
    assert pins
    assert _fd_count() == before
    for fd in pins:
        _closed(fd)


def test_partial_walk_budget_exception_closes_already_selected_fd(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {
        "runs/a/report.json": "{}", "runs/b/report.json": "{}",
        "runs/c/report.json": "{}"})
    before = _fd_count()
    # HOME entry + three child names + first leaf =5; the next leaf exceeds
    # the budget AFTER one descriptor was selected.
    monkeypatch.setattr(gh, "_RETAIN_MAX_SEARCH_ENTRIES", 5)
    out = gh._retain_copy([], [home], [], {}, patterns=["runs/*/report.json"], requested=True)
    assert out["ok"] is False and out["files"] == []
    assert any("search exceeded 5" in e for e in out["refused"])
    assert _fd_count() == before
