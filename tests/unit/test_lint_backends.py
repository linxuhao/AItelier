"""AItelier custom lint backends (aitelier/lint_backends.py).

The eslint backend is registered into skillflow's lint registry at
startup; a DPE linter_manifest.json can then map extensions to it.
"""

import subprocess
from pathlib import Path

import pytest

from aitelier import lint_backends
from skillflow import lint_backends as sf_registry


@pytest.fixture
def clean_registry():
    yield
    sf_registry._backends.pop("eslint", None)


def test_register_all_registers_eslint(clean_registry):
    lint_backends.register_all()
    assert sf_registry.get_backend("eslint") is lint_backends.eslint_backend


def test_eslint_skips_without_node_toolchain(monkeypatch, tmp_path):
    monkeypatch.setattr(lint_backends, "_eslint_cmd", lambda: None)
    fp = tmp_path / "app.js"
    fp.write_text("console.log('hi');")
    res = lint_backends.eslint_backend(fp)
    assert res["passed"] is True
    assert "unavailable" in res["error_message"]


def test_eslint_pass_and_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(lint_backends, "_eslint_cmd", lambda: ["eslint"])
    fp = tmp_path / "app.js"
    fp.write_text("console.log('hi');")

    def fake_run(cmd, **kw):
        assert cmd == ["eslint", "--no-config-lookup", "app.js"]
        assert Path(kw["cwd"]) == tmp_path  # eslint ignores files outside cwd
        return subprocess.CompletedProcess(cmd, returncode=fake_run.rc,
                                           stdout=fake_run.out, stderr="")

    monkeypatch.setattr(lint_backends.subprocess, "run", fake_run)

    fake_run.rc, fake_run.out = 0, ""
    assert lint_backends.eslint_backend(fp)["passed"] is True

    fake_run.rc, fake_run.out = 1, "1:20 error Parsing error"
    res = lint_backends.eslint_backend(fp)
    assert res["passed"] is False
    assert "Parsing error" in res["error_message"]


def test_lint_tool_dispatches_manifest_to_eslint(
        monkeypatch, tmp_path, clean_registry):
    """End-to-end: manifest {'.js': 'eslint'} routes through skillflow's
    lint tool into the registered AItelier backend.

    Loads the tool via ToolLoader (spec_from_file_location, like
    production) to prove the registry is shared across that boundary."""
    import skillflow
    from skillflow.tool_loader import ToolLoader

    loader = ToolLoader(Path(skillflow.__file__).parent / "tools")
    lint_tool = loader.load_fn("lint")

    lint_backends.register_all()
    seen = []
    monkeypatch.setattr(
        lint_backends, "_eslint_cmd", lambda: ["eslint"])
    monkeypatch.setattr(
        lint_backends.subprocess, "run",
        lambda cmd, **kw: (seen.append(cmd),
                           subprocess.CompletedProcess(cmd, 0, "", ""))[1])

    (tmp_path / "linter_manifest.json").write_text('{".js": "eslint"}')
    (tmp_path / "app.js").write_text("console.log('hello');")
    r = lint_tool(["*.js"], workspace_root=str(tmp_path),
                  manifest_path="linter_manifest.json")
    assert r["all_passed"] is True
    assert seen == [["eslint", "--no-config-lookup", "app.js"]]
