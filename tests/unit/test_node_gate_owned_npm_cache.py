# tests/unit/test_node_gate_owned_npm_cache.py
# The node gate's npm must write its cache somewhere the running uid owns.
# In the backend container HOME is a root-owned directory, so npm's default
# $HOME/.npm died with `EACCES mkdir /home/<user>/.npm` (rc 243). The gate's
# npm now runs only in the bounded CPU container, and its cache is the
# `cache/` of the gate's own `node-` ticket under this run's gate-reports/ —
# one cache for every command, outside the repository, removed afterwards.
import hashlib
import json
import os
import subprocess

import aitelier.tools.run_tests.impl as rt
from core import datadir


def _repo(tmp_path, scripts):
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / "web" / "package.json").write_text(
        json.dumps({"name": "x", "version": "0.0.0", "scripts": scripts,
                    "dependencies": {"left-pad": "1.3.0"}}))
    (repo / "web" / "package-lock.json").write_text("{}")
    return repo


def _transport(monkeypatch, body=lambda args: (0, "")):
    calls = []

    def execute(repo, args, timeout, **kw):
        cache = kw["npm_cache"]
        calls.append({"args": args, "cache": cache, "workdir": kw["workdir"],
                      "is_dir": os.path.isdir(cache), "mode": os.stat(cache).st_mode & 0o777})
        rc, out = body(args)
        return subprocess.CompletedProcess(args, rc, out, "")

    monkeypatch.setattr(rt.repository_executor, "execute", execute)
    return calls


def test_every_node_command_gets_one_owned_cache(tmp_path, monkeypatch):
    calls = _transport(monkeypatch)
    repo = _repo(tmp_path, {"build": "x", "test": "y"})

    node = rt._run_node_checks(repo, run_id="run-1")

    assert node["passed"] is True, node
    assert len(calls) == 4                                  # fetch, ci, run build, test
    caches = {c["cache"] for c in calls}
    assert len(caches) == 1
    cache = caches.pop()
    root = datadir.aitelier_home() / "gate-reports" / hashlib.sha256(b"run-1").hexdigest()
    assert os.path.dirname(os.path.dirname(cache)) == str(root)
    assert os.path.basename(cache) == "cache"
    assert all(c["is_dir"] and c["mode"] == 0o700 for c in calls)
    assert not cache.startswith(str(repo))
    assert not os.path.exists(cache)                        # cleaned up
    assert not list(repo.rglob("_logs"))


def test_failing_command_keeps_its_real_exit_and_output(tmp_path, monkeypatch):
    _transport(monkeypatch, lambda args: (7, "build exploded") if args[1:3] == ["run", "build"]
               else (0, ""))
    node = rt._run_node_checks(_repo(tmp_path, {"build": "x", "test": "y"}), run_id="run-2")

    build = node["checks"]["build"]
    assert node["passed"] is False and node["checks"]["test"]["passed"] is True
    assert build["passed"] is False and build["returncode"] == 7
    assert "build exploded" in build["output"]
    assert not list((datadir.aitelier_home() / "gate-reports").rglob("node-*"))


def test_unrelated_failure_is_not_reported_as_a_cache_problem(tmp_path, monkeypatch):
    _transport(monkeypatch, lambda args: (1, "npm ERR! 404 no such package"))
    node = rt._run_node_checks(_repo(tmp_path, {}), run_id="run-3")
    install = node["checks"]["install"]
    assert install["returncode"] == 1 and "404" in install["output"]
    assert "EACCES" not in install["output"]


def test_ticket_that_cannot_be_created_is_unmeasured_without_running_npm(tmp_path, monkeypatch):
    calls = _transport(monkeypatch)
    def refuse(*a, **k):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(rt.tempfile, "mkdtemp", refuse)

    node = rt._run_node_checks(_repo(tmp_path, {}), run_id="run-4")

    assert node["skipped"] is True and node["checks"] == {}
    assert "node ticket" in node["unmeasured_reason"] and calls == []


def test_run_tests_threads_its_run_id_into_the_node_gate(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(rt, "_run_node_checks",
                        lambda repo, **kw: seen.update(kw) or None)
    repo = tmp_path / "repo"
    repo.mkdir()
    rt.run_tests(project_root=str(repo), out_dir=str(tmp_path / "out"), run_id="run-5", repo_gate=False)
    assert seen == {"run_id": "run-5"}
