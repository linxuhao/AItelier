# tests/unit/test_node_gate_owned_npm_cache.py
# The node gate's npm must write its cache somewhere the running uid owns.
# In the backend container HOME is a root-owned directory, so npm's default
# $HOME/.npm died with `EACCES mkdir /home/<user>/.npm` (rc 243) before it read
# package.json. The gate hands npm a cache of its own under this run's
# gate-reports/ ticket area instead, through the scrubbed env, and removes it.
import hashlib
import json
import os
import stat

import pytest

import aitelier.tools.run_tests.impl as rt
from core import datadir


def _repo(tmp_path, scripts):
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / "web" / "package.json").write_text(
        json.dumps({"name": "x", "version": "0.0.0", "scripts": scripts}))
    (repo / "web" / "package-lock.json").write_text("{}")
    return repo


def _npm(tmp_path, monkeypatch, body):
    """A fake `npm` whose HOME is unwritable, like the container's."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    home = tmp_path / "rohome"
    home.mkdir()
    home.chmod(stat.S_IRUSR | stat.S_IXUSR)
    npm = bin_dir / "npm"
    npm.write_text("#!/bin/bash\n" + body)
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(home))
    return home


REAL_LIKE = r'''
# like npm: needs a writable cache dir, defaulting to $HOME/.npm
cache="${npm_config_cache:-$HOME/.npm}"
mkdir -p "$cache/_logs" || { echo "npm ERR! EACCES mkdir $cache"; exit 243; }
echo "$PWD|$cache|${DEEPSEEK_API_KEY:-}" >> "$NPM_LOG"
%s
'''


def test_every_node_command_gets_one_owned_scrubbed_cache(tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    monkeypatch.setenv("NPM_LOG", str(log))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-not-leak")
    home = _npm(tmp_path, monkeypatch, REAL_LIKE % "exit 0")
    repo = _repo(tmp_path, {"build": "x", "test": "y"})

    node = rt._run_node_checks(repo, run_id="run-1")

    assert node["passed"] is True, node
    rows = [line.split("|") for line in log.read_text().splitlines()]
    assert len(rows) == 3                                   # ci, run build, test
    caches = {r[1] for r in rows}
    assert len(caches) == 1
    cache = caches.pop()
    root = datadir.aitelier_home() / "gate-reports" / hashlib.sha256(b"run-1").hexdigest()
    assert os.path.dirname(cache) == str(root)
    assert not cache.startswith((str(repo), str(home)))
    assert all(r[2] == "" for r in rows)                    # secret env still scrubbed
    assert all(r[0] == str(repo / "web") for r in rows)     # cwd unchanged
    assert not os.path.exists(cache)                        # cleaned up
    assert not list(repo.rglob("_logs")) and not any(home.iterdir())


def test_failing_command_keeps_its_real_exit_and_output(tmp_path, monkeypatch):
    monkeypatch.setenv("NPM_LOG", str(tmp_path / "calls.log"))
    body = REAL_LIKE % ('[[ "$*" == "run build"* ]] && { echo "build exploded"; exit 7; }\nexit 0')
    _npm(tmp_path, monkeypatch, body)
    repo = _repo(tmp_path, {"build": "x", "test": "y"})

    node = rt._run_node_checks(repo, run_id="run-2")

    build = node["checks"]["build"]
    assert node["passed"] is False and node["checks"]["test"]["passed"] is True
    assert build["passed"] is False and build["returncode"] == 7
    assert "build exploded" in build["output"]
    assert not list((datadir.aitelier_home() / "gate-reports").rglob("npm-cache-*"))


def test_unrelated_failure_is_not_reported_as_a_cache_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("NPM_LOG", str(tmp_path / "calls.log"))
    _npm(tmp_path, monkeypatch, REAL_LIKE % 'echo "npm ERR! 404 no such package"; exit 1')
    node = rt._run_node_checks(_repo(tmp_path, {}), run_id="run-3")
    install = node["checks"]["install"]
    assert install["returncode"] == 1 and "404" in install["output"]
    assert "EACCES" not in install["output"]


def test_cache_that_cannot_be_created_fails_honestly_without_running_npm(tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    monkeypatch.setenv("NPM_LOG", str(log))
    _npm(tmp_path, monkeypatch, REAL_LIKE % "exit 0")
    def refuse(*a, **k):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(rt.tempfile, "mkdtemp", refuse)

    node = rt._run_node_checks(_repo(tmp_path, {}), run_id="run-4")

    install = node["checks"]["install"]
    assert node["passed"] is False
    assert install["runner_error"] is True and install["returncode"] == -1
    assert "npm cache" in install["output"] and not log.exists()


def test_run_tests_threads_its_run_id_into_the_node_gate(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(rt, "_run_node_checks",
                        lambda repo, **kw: seen.update(kw) or None)
    repo = tmp_path / "repo"
    repo.mkdir()
    rt.run_tests(project_root=str(repo), out_dir=str(tmp_path / "out"), run_id="run-5", repo_gate=False)
    assert seen == {"run_id": "run-5"}
