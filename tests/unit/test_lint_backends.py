"""The registered JavaScript backend requires explicit owned execution."""
import hashlib
from pathlib import Path
import subprocess

import pytest
from aitelier import lint_backends as lint
from core import repository_executor as ex
from skillflow import lint_backends as registry


@pytest.fixture(autouse=True)
def registry_snapshot():
    before = dict(registry._backends)
    yield
    registry._backends.clear()
    registry._backends.update(before)


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    fp = repo / "file.js"
    fp.write_text("const café = 42;\n")
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(ex.HOST_LAUNCHER_ENV, "/own/test-fixture.sock")
    return repo, fp


def test_register_all_registers_owned_backend():
    lint.register_all()
    assert registry.get_backend("eslint") is lint.eslint_backend


def test_missing_identity_is_not_a_skipped_pass(candidate, monkeypatch):
    repo, fp = candidate
    monkeypatch.setattr(ex, "execute", lambda *a, **k: pytest.fail("unbound execution"))
    res = lint.eslint_backend(fp)
    assert res["passed"] is False and res["unavailable"]
    assert "explicit" in res["error_message"]


@pytest.mark.parametrize("rc,out,err", [(0, "", ""), (1, "Parsing error: Unexpected token", ""),
                                      (2, "", "tool usage failure")])
def test_input_bytes_owned_ticket_and_bare_result(candidate, monkeypatch, rc, out, err):
    repo, fp = candidate
    seen = []
    def execute(root, args, timeout, **kw):
        ticket = kw["writable_dirs"][0]
        assert root == repo and args == ["file.js"] and timeout == 120
        assert kw["run_id"] == "own" and kw["profile"] == ex.LINT_PROFILE
        assert (ticket / "input.js").read_bytes() == fp.read_bytes()
        seen.append(ticket)
        return subprocess.CompletedProcess(args, rc, out, err)
    monkeypatch.setattr(ex, "execute", execute)
    res = lint.eslint_backend(fp, run_id="own", step_id="validate", project_root=str(repo))
    assert res["passed"] is (rc == 0)
    assert (res["returncode"], res["stdout"], res["stderr"]) == (rc, out, err)
    assert res["input_sha256"] == hashlib.sha256(fp.read_bytes()).hexdigest()
    receipt = res["details"]["aitelier_javascript_lint"]
    assert receipt["measured"] is (rc in (0, 1))
    assert receipt["returncode"] == rc and receipt["input_sha256"] == res["input_sha256"]
    assert receipt["run_id"] == "own" and receipt["step_id"] == "validate"
    assert receipt["project_root"] == str(repo)
    assert not seen[0].exists()


@pytest.mark.parametrize("failure", [ex.IsolationUnavailable("owned facility absent"),
                                     subprocess.TimeoutExpired(["lint"], 120, "partial", "timeout")])
def test_unavailable_timeout_never_pass(candidate, monkeypatch, failure):
    repo, fp = candidate
    def execute(*a, **kw):
        raise failure
    monkeypatch.setattr(ex, "execute", execute)
    result = lint.eslint_backend(fp, run_id="own", step_id="s", project_root=str(repo))
    assert result["passed"] is False and result["returncode"] == -1
    assert result["details"]["aitelier_javascript_lint"]["measured"] is False
    assert not list((Path(lint.datadir.aitelier_home()) / "gate-reports").rglob("lint-*"))


@pytest.mark.parametrize("kind", ["outside", "symlink", "parent-symlink", "hardlink", "fifo", "directory", "dash", "extension"])
def test_target_refusal_precedes_transport(candidate, monkeypatch, tmp_path, kind):
    repo, fp = candidate
    outside = tmp_path / "outside.js"
    outside.write_text("const outside = 1;")
    target = fp
    if kind == "outside": target = outside
    elif kind == "symlink":
        fp.unlink();fp.symlink_to(outside)
    elif kind == "parent-symlink":
        (repo / "alias").symlink_to(tmp_path, target_is_directory=True);target = repo / "alias/outside.js"
    elif kind == "hardlink":
        fp.unlink();fp.hardlink_to(outside)
    elif kind == "fifo":
        import os
        fp.unlink();os.mkfifo(fp)
    elif kind == "directory": fp.unlink();fp.mkdir()
    elif kind == "dash": target = repo / "-file.js";target.write_text("let ok = 1")
    else: target = repo / "file.ts";target.write_text("let ok = 1")
    monkeypatch.setattr(ex, "execute", lambda *a, **kw: pytest.fail("refused input executed"))
    result = lint.eslint_backend(target, run_id="own", step_id="s", project_root=str(repo))
    assert result["passed"] is False and result["unavailable"]


def test_no_direct_adapter_lint_fallback(candidate, monkeypatch):
    repo, fp = candidate
    monkeypatch.delenv(ex.HOST_LAUNCHER_ENV)
    original_direct = ex._execute
    monkeypatch.setattr(ex, "_execute", lambda *a, **kw: pytest.fail("local fallback"))
    result = lint.eslint_backend(fp, run_id="own", step_id="s", project_root=str(repo))
    assert result["passed"] is False
    with pytest.raises(ex.IsolationUnavailable, match="owned host launcher"):
        original_direct(repo, ["file.js"], 10, run_id="own", profile=ex.LINT_PROFILE)
