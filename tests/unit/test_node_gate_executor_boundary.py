# tests/unit/test_node_gate_executor_boundary.py
# The normal node gate runs npm only through the bounded CPU transport, on a
# snapshot under its own `node-` ticket. These tests replace the transport at
# its entry (`repository_executor.execute`) with a recorder that also plays the
# container's part on the ticket, and put a trap `npm` on PATH: a local npm
# would mean the backend executed repository code again.
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import aitelier.tools.run_tests.impl as rt
from core import datadir, repository_executor as executor


@pytest.fixture
def trap_npm(tmp_path, monkeypatch):
    bin_dir = tmp_path / "trap-bin"
    bin_dir.mkdir()
    marker = tmp_path / "LOCAL-NPM-RAN"
    (bin_dir / "npm").write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    (bin_dir / "npm").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    yield marker
    assert not marker.exists(), "npm ran in the backend process"


class Transport:
    """Records each launch; `effects[label]` plays the container on the ticket."""

    def __init__(self, monkeypatch, rc=None, effects=None, raises=None):
        self.calls, self.rc, self.effects, self.raises = [], rc or {}, effects or {}, raises or {}
        monkeypatch.setattr(executor, "execute", self)

    def __call__(self, repo, args, timeout, **kw):
        label = kw.get("profile", "") + ":" + " ".join(args)
        workdir = Path(kw["workdir"])
        ticket = Path(kw["writable_dirs"][0])
        self.calls.append({"repo": Path(repo), "args": list(args), "timeout": timeout,
                           "label": label, **kw,
                           "npmrc_bytes": (workdir / ".npmrc").read_bytes()
                           if (workdir / ".npmrc").exists() else None,
                           "lock_present": (workdir / "package-lock.json").exists(),
                           "userconfig": (ticket / "npmrc").read_text()
                           if (ticket / "npmrc").exists() else None,
                           "src_files": sorted(str(p.relative_to(ticket / "src"))
                                               for p in (ticket / "src").rglob("*") if p.is_file()
                                               and ".git" not in p.parts)})
        for key, exc in self.raises.items():
            if label.startswith(key):
                raise exc
        for key, effect in self.effects.items():
            if label.startswith(key):
                effect(workdir, Path(kw["writable_dirs"][0]))
        rc = next((v for k, v in self.rc.items() if label.startswith(k)), 0)
        return subprocess.CompletedProcess(args, rc, f"out {label}", f"err {label}")


def _repo(tmp_path, *, deps=True, scripts=None, lock=True, npmrc=None, sub="web"):
    repo = tmp_path / "repo"
    pkg = repo / sub if sub else repo
    pkg.mkdir(parents=True)
    manifest = {"name": "x", "version": "1.0.0",
                "scripts": {"build": "b", "test": "t"} if scripts is None else scripts}
    if deps:
        manifest["dependencies"] = {"left-pad": "^1.3.0"}
    (pkg / "package.json").write_text(json.dumps(manifest))
    if lock:
        (pkg / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {
            "": {"name": "x"},
            "node_modules/left-pad": {"version": "1.3.0",
                                      "resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
                                      "integrity": "sha512-x"}}}))
    if npmrc is not None:
        (pkg / ".npmrc").write_text(npmrc)
    (repo / "shared").mkdir()
    (repo / "shared" / "util.js").write_text("module.exports = 1\n")
    return repo


def _tree(root):
    return sorted((str(p.relative_to(root)), p.is_symlink() or p.is_file() and p.read_bytes())
                  for p in root.rglob("*"))


def test_every_npm_command_goes_through_the_bounded_profiles(tmp_path, monkeypatch, trap_npm):
    repo = _repo(tmp_path, npmrc="legacy-peer-deps=true\n")
    t = Transport(monkeypatch)
    node = rt._run_node_checks(repo, run_id="run-1")
    assert node["passed"] is True and node["dir"] == "web"
    assert [c["profile"] for c in t.calls] == ["node_fetch", "node_run", "node_run", "node_run"]
    assert [c["args"] for c in t.calls] == [
        ["npm", "ci"], ["npm", "ci", "--offline"],
        ["npm", "run", "build"], ["npm", "test"]]
    root = datadir.aitelier_home() / "gate-reports" / hashlib.sha256(b"run-1").hexdigest()
    tickets = {Path(c["writable_dirs"][0]) for c in t.calls}
    assert len(tickets) == 1
    ticket = tickets.pop()
    assert ticket.parent == root and ticket.name.startswith("node-")
    for c in t.calls:
        assert c["repo"] == repo                          # binds the run; never mounted
        assert c["npm_cache"] == str(ticket / "cache")
        assert c["run_id"] == "run-1"
    assert all(c["workdir"] == str(ticket / "src" / "web") for c in t.calls)
    assert [c["timeout"] for c in t.calls] == [600, 600, 300, 300]
    # The fetch works in the FULL snapshot (sources included), with the project's
    # own .npmrc present — npm 12 and the gate's command line decide, not a view.
    assert "shared/util.js" in t.calls[0]["src_files"]
    assert t.calls[0]["npmrc_bytes"] == t.calls[1]["npmrc_bytes"] == b"legacy-peer-deps=true\n"
    assert all(c["userconfig"] == "install-links=true\n" for c in t.calls)
    assert not ticket.exists()                            # owned ticket removed
    assert node["snapshot"]["source"] == str(repo)
    assert node["isolation"] == {"fetch": "operator_network", "run_network": "none"}


def test_the_candidate_is_never_written_and_the_ticket_is_removed_safely(tmp_path, monkeypatch, trap_npm):
    repo = _repo(tmp_path, lock=False)
    before = _tree(repo)
    outside = tmp_path / "FAKE-outside" / "keep.txt"
    outside.parent.mkdir()
    outside.write_text("FAKE-only")

    def fetch(workdir, ticket):
        (workdir / "node_modules" / "left-pad").mkdir(parents=True)
        (workdir / "package-lock.json").write_text('{"generated": true}')

    def build(workdir, ticket):
        (workdir / "dist").mkdir()
        (workdir / "dist" / "out.js").write_text("x")
        locked = workdir / "locked"
        (locked / "inner").mkdir(parents=True)
        locked.chmod(0)                                   # run left an unwritable dir
        (workdir / "to-outside").symlink_to(outside)      # and a link out of the ticket

    t = Transport(monkeypatch, effects={"node_fetch": fetch, "node_run:npm run build": build})
    node = rt._run_node_checks(repo, run_id="run-2")
    assert node["passed"] is True
    assert t.calls[0]["args"] == ["npm", "install"] and t.calls[1]["args"] == ["npm", "install", "--offline"]
    assert node["generated_lock"]["by"] == "node_fetch"
    assert t.calls[0]["lock_present"] is False and t.calls[1]["lock_present"] is True
    assert node["generated_lock"]["sha256"] == hashlib.sha256(b'{"generated": true}').hexdigest()
    assert _tree(repo) == before                          # no node_modules, no lock, no dist
    assert outside.read_text() == "FAKE-only"             # cleanup never followed the link
    assert not Path(t.calls[0]["writable_dirs"][0]).exists()


def test_snapshot_is_the_working_tree_without_git_pointers_or_node_modules(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".git").write_text("gitdir: /somewhere/else\n")       # a worktree pointer
    (repo / "vendor" / ".git").mkdir(parents=True)                # a nested repository
    (repo / "vendor" / ".git" / "config").write_text("[remote]\n")
    (repo / "web" / "node_modules" / "junk").mkdir(parents=True)
    (repo / "web" / "lib").symlink_to("../shared")                # inside: kept as a link
    os.mkfifo(repo / "pipe")                                      # must not hang the copy
    dest = tmp_path / "snap"
    identity = rt._snapshot_tree(repo, dest)
    assert not (dest / ".git").exists() and not (dest / "vendor" / ".git").exists()
    assert not (dest / "web" / "node_modules").exists()
    assert (dest / "web" / "lib").is_symlink() and os.readlink(dest / "web" / "lib") == "../shared"
    assert (dest / "web" / "lib" / "util.js").read_text() == "module.exports = 1\n"
    assert identity["skipped_special"] == 1
    again = rt._snapshot_tree(repo, tmp_path / "snap2")
    assert again["tree_sha256"] == identity["tree_sha256"]


@pytest.mark.parametrize("target", ["/etc/passwd", "../../outside-secret", "a/../../.."])
def test_a_link_out_of_the_repository_is_refused_without_reading_it(tmp_path, monkeypatch, trap_npm, target):
    repo = _repo(tmp_path)
    secret = tmp_path / "outside-secret"
    secret.write_text("FAKE-SECRET-CONTENT")
    (repo / "web" / "leak").symlink_to(target)
    t = Transport(monkeypatch)
    node = rt._run_node_checks(repo, run_id="run-3")
    assert node["skipped"] is True and node["passed"] is True and t.calls == []
    assert "web/leak" in node["unmeasured_reason"] and "FAKE-SECRET" not in json.dumps(node)


def test_hard_linked_input_is_refused(tmp_path, monkeypatch, trap_npm):
    repo = _repo(tmp_path)
    os.link(repo / "shared" / "util.js", repo / "web" / "alias.js")
    t = Transport(monkeypatch)
    node = rt._run_node_checks(repo, run_id="run-4")
    assert node["skipped"] is True and "hard-linked" in node["unmeasured_reason"] and t.calls == []


def test_snapshot_caps_make_the_gate_unmeasured(tmp_path, monkeypatch, trap_npm):
    monkeypatch.setattr(rt, "SNAPSHOT_MAX_ENTRIES", 3)
    t = Transport(monkeypatch)
    node = rt._run_node_checks(_repo(tmp_path), run_id="run-5")
    assert node["skipped"] is True and "entries" in node["unmeasured_reason"] and t.calls == []


def test_a_project_npmrc_is_left_to_npm_and_never_echoed(tmp_path, monkeypatch, trap_npm):
    rc = "fund=false\n//registry.npmjs.org/:_authToken=FAKE-TOKEN-123\n"
    t = Transport(monkeypatch)
    node = rt._run_node_checks(_repo(tmp_path, npmrc=rc), run_id="run-6")
    assert node["passed"] is True and len(t.calls) == 4     # no homemade .npmrc policy
    assert "FAKE-TOKEN" not in json.dumps(node)


def test_snapshot_streams_a_large_file_in_bounded_memory(tmp_path):
    import tracemalloc
    repo = tmp_path / "repo"
    repo.mkdir()
    big = repo / "asset.bin"
    with big.open("wb") as out:
        for i in range(64):
            out.write(bytes([i]) * (1 << 20))                  # 64 MiB, benign
    tracemalloc.start()
    identity = rt._snapshot_tree(repo, tmp_path / "snap")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 8 << 20, peak                               # a whole-file read peaks >= 64 MiB
    assert identity["bytes"] == 64 << 20
    assert (tmp_path / "snap" / "asset.bin").read_bytes() == big.read_bytes()


def test_snapshot_charges_a_file_that_grows_while_it_is_copied(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    grow = repo / "log.txt"
    grow.write_bytes(b"x" * 1024)                              # small when it is stat()ed
    monkeypatch.setattr(rt, "SNAPSHOT_MAX_BYTES", 3 << 20)
    real_read = rt.os.read

    def growing(fd, n):
        chunk = real_read(fd, n)
        if chunk:
            with grow.open("ab") as more:                      # the file keeps growing
                more.write(b"y" * (1 << 20))
        return chunk

    monkeypatch.setattr(rt.os, "read", growing)
    with pytest.raises(rt.SnapshotRefused, match="more than"):
        rt._snapshot_tree(repo, tmp_path / "snap")


def test_snapshot_byte_cap_refuses_before_copying(tmp_path, monkeypatch, trap_npm):
    monkeypatch.setattr(rt, "SNAPSHOT_MAX_BYTES", 100)
    t = Transport(monkeypatch)
    repo = _repo(tmp_path)
    (repo / "web" / "big.bin").write_bytes(b"z" * 1000)
    node = rt._run_node_checks(repo, run_id="run-cap")
    assert node["skipped"] is True and "more than 100 bytes" in node["unmeasured_reason"]
    assert t.calls == []


def test_unavailable_transport_is_unmeasured_never_a_pass_or_a_red(tmp_path, monkeypatch, trap_npm):
    Transport(monkeypatch, raises={"node_fetch": executor.NodeRefused("operator has not enabled it")})
    out = tmp_path / "out"
    res = rt.run_tests(project_root=str(_repo(tmp_path)), out_dir=str(out), run_id="run-7",
                       repo_gate=False)
    report = json.loads((out / "test_report.json").read_text())
    assert report["node"]["skipped"] is True
    assert "operator has not enabled it" in report["node"]["unmeasured_reason"]
    assert report["node"]["isolation"]["fetch"] == "refused"
    assert report["infrastructure_unavailable"] is True and res["passed"] is False
    assert any(f.startswith("node gate unavailable") for f in report["failures"])
    assert not any(f.startswith("node:") for f in report["failures"])


def test_failing_fetch_is_the_measured_install_failure_and_nothing_else_runs(tmp_path, monkeypatch, trap_npm):
    t = Transport(monkeypatch, rc={"node_fetch": 1})
    node = rt._run_node_checks(_repo(tmp_path), run_id="run-8")
    install = node["checks"]["install"]
    assert node["passed"] is False and install["returncode"] == 1 and install["phase"] == "fetch"
    assert "out node_fetch:npm ci" in install["output"] and len(t.calls) == 1


def test_real_exit_codes_and_timeouts_stay_what_they_were(tmp_path, monkeypatch, trap_npm):
    Transport(monkeypatch, rc={"node_run:npm test": 3},
              raises={"node_run:npm run build": subprocess.TimeoutExpired(["npm"], 300)})
    node = rt._run_node_checks(_repo(tmp_path), run_id="run-9")
    assert node["passed"] is False
    assert node["checks"]["build"]["timed_out"] is True
    assert node["checks"]["test"]["returncode"] == 3
    assert "err node_run:npm test" in node["checks"]["test"]["output"]


def test_no_dependencies_means_no_network_fetch(tmp_path, monkeypatch, trap_npm):
    t = Transport(monkeypatch)
    node = rt._run_node_checks(_repo(tmp_path, deps=False, lock=False, sub=""), run_id="run-10")
    assert node["passed"] is True and node["dir"] == "."
    assert [c["profile"] for c in t.calls] == ["node_run"] * 3
    assert node["isolation"]["fetch"] == "skipped"


def test_a_command_without_a_profile_is_refused_not_run_locally(tmp_path, trap_npm):
    result = rt._run_node_cmd(tmp_path, ["npm", "install"], 5)
    assert result["isolation_unavailable"] is True and result["passed"] is False
    assert rt._repo_gate_outcome(result) == rt.REPO_GATE_UNMEASURED


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   check=True, capture_output=True)


@pytest.mark.parametrize("worktree", [False, True])
def test_snapshot_git_is_self_contained_shallow_head_metadata(tmp_path, worktree):
    main = _repo(tmp_path)
    _git(main, "init", "-q")
    _git(main, "config", "remote.origin.url", "https://FAKE-TOKEN@example.invalid/x.git")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "one")
    _git(main, "commit", "-q", "--allow-empty", "-m", "two")
    repo = main
    if worktree:
        repo = tmp_path / "wt"
        _git(main, "worktree", "add", "-q", str(repo))
    for hook in ("post-checkout", "reference-transaction", "post-index-change", "pre-auto-gc"):
        path = main / ".git" / "hooks" / hook
        path.write_text(f"#!/bin/sh\ntouch {tmp_path}/HOOK-RAN-{hook}\n")
        path.chmod(0o755)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()
    (repo / "shared" / "util.js").write_text("changed\n")
    dest = tmp_path / "snap"
    rt._snapshot_tree(repo, dest)
    git = rt._snapshot_git(repo, dest)
    assert git["provided"] is True and git["head"] == head and git["dirty"] is True
    meta = dest / ".git"
    assert meta.is_dir() and not (meta / "hooks").exists() and not (meta / "FETCH_HEAD").exists()
    config = (meta / "config").read_text()
    assert "remote" not in config and "FAKE-TOKEN" not in config and str(main) not in config
    assert (meta / "shallow").read_text().strip() == head
    log = subprocess.run(["git", "log", "--format=%H"], cwd=dest, capture_output=True, text=True)
    assert log.stdout.split() == [head]                   # one commit, no history
    assert not list(tmp_path.rglob("HOOK-RAN*"))         # no source hook ran


def test_snapshot_git_absent_source_history_is_stated(tmp_path):
    repo = _repo(tmp_path)
    dest = tmp_path / "snap"
    rt._snapshot_tree(repo, dest)
    git = rt._snapshot_git(repo, dest)
    assert git["provided"] is False and git["reason"] and not (dest / ".git").exists()
