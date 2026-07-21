# tests/unit/test_readme_delivery_regression.py
#
# Non-regression guard for the 2026-07-11 incident where a game-pipeline run
# (flappy-verify) overwrote AItelier's OWN README.md and committed it to main.
#
# Root cause: the Final Verifier (step "5") authored the project README with the
# custom `readme_*` tools, which resolved an agent-supplied `project_root` ("."
# — the agents always guessed it) against the process CWD. In the container the
# CWD is the AItelier source repo, bind-mounted at /app, so the tool wrote and
# `git commit`ed straight into AItelier's own repo instead of the delivered
# project repo.
#
# Fix: README.md became a DECLARED content-mode output of step "5" (written via
# the engine-generated `create_readme` write tool, whose path is engine-bound to
# the step staging dir — the agent never chooses a path) and is delivered into
# the RESOLVED project repo by the step's `on_deliver: repo_apply`. The custom
# `readme_*` tools were deleted.
#
# These tests fail if any leg of that regression is reintroduced: the custom
# tools coming back, an agent being wired to them, or step 5 losing either the
# README content output or its repo_apply delivery.

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG = ROOT / "configs" / "dpe_default.yaml"
AGENT_CONFIG = ROOT / "agent_configs" / "dpe_default.yaml"
TOOLS_DIR = ROOT / "aitelier" / "tools"

_README_TOOLS = ("readme_create", "readme_edit", "readme_read", "readme_search")


def _step(graph: dict, step_id: str) -> dict:
    node = next((s for s in graph["steps"] if s["id"] == step_id), None)
    assert node is not None, f"step {step_id!r} missing from dpe_default.yaml"
    return node


def test_custom_readme_tools_are_gone():
    """The CWD-clobbering readme_* tools must not exist on disk."""
    survivors = [t for t in _README_TOOLS if (TOOLS_DIR / t).exists()]
    assert not survivors, (
        f"custom readme tool(s) reintroduced: {survivors}. They resolve an "
        "agent-supplied project_root against CWD (= AItelier's own repo in the "
        "container) and can clobber it. README is delivered via content-mode "
        "output + repo_apply instead."
    )


def test_no_agent_config_wires_a_readme_tool():
    """No role may list a readme_* tool (nothing should be able to call them)."""
    agents = yaml.safe_load(AGENT_CONFIG.read_text(encoding="utf-8"))
    offenders = {
        name: cfg["tools"]
        for name, cfg in agents.items()
        if isinstance(cfg, dict)
        for t in (cfg.get("tools") or [])
        if t in _README_TOOLS
    }
    assert not offenders, f"agent config still wires readme_* tools: {offenders}"


def test_step5_declares_readme_as_content_output():
    """Step 5 must write README.md as a declared content-mode output, so the
    write path is engine-bound and the agent cannot redirect it to CWD."""
    graph = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    step5 = _step(graph, "5")

    output = step5.get("output", {})
    assert output.get("mode") == "content", "step 5 must stay content-mode"

    fixed = output.get("fixed", {})
    readme = fixed.get("readme")
    assert readme is not None, "step 5 no longer declares a `readme` output"
    # accept both the string shorthand and the {file: ...} object form
    target = readme if isinstance(readme, str) else readme.get("file")
    assert target == "README.md", f"readme output must target README.md, got {target!r}"


def test_step5_delivers_readme_via_repo_apply():
    """Step 5 must ship its README into the RESOLVED project repo via repo_apply
    (which targets the real code path) — never a CWD-relative direct write."""
    graph = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    step5 = _step(graph, "5")

    on_deliver = step5.get("lifecycle", {}).get("on_deliver")
    assert on_deliver, "step 5 lost its on_deliver lifecycle — README won't reach the repo"

    hooks = on_deliver if isinstance(on_deliver, list) else [on_deliver]
    tools = [h.get("tool") for h in hooks]
    assert "repo_apply" in tools, (
        f"step 5 on_deliver must include repo_apply to deliver README.md; got {tools}"
    )

    # The verdict must NOT ship into the delivered repo — only README.md.
    apply_hook = next(h for h in hooks if h.get("tool") == "repo_apply")
    ignore = apply_hook.get("params", {}).get("ignore") or []
    assert any("verify_report" in pat for pat in ignore), (
        "repo_apply should ignore the verdict file so only README.md is delivered; "
        f"ignore={ignore}"
    )


# ── Defense-in-depth: the other commit-writers must reject a CWD-relative path ──
#
# These four tools also resolve project_root/workspace_root and then git-commit.
# They are host-driven graph tool-steps (the host injects an absolute path), so
# unlike readme_* they are not agent-reachable — but a MISSING injection used to
# silently fall back to "." → the process CWD (= AItelier's repo at /app). They
# now hard-fail on a relative/empty path instead of committing into /app.

import pytest


def test_scaffold_bible_rejects_relative_root():
    from aitelier.tools.scaffold_bible.impl import scaffold_bible
    with pytest.raises(ValueError, match="absolute path"):
        scaffold_bible(project_root=".", workspace_root="")
    with pytest.raises(ValueError, match="absolute path"):
        scaffold_bible(project_root="", workspace_root="")


def test_apply_state_rejects_relative_root():
    from aitelier.tools.apply_state.impl import apply_state
    with pytest.raises(ValueError, match="absolute path"):
        apply_state(project_root=".", workspace_root="")
    with pytest.raises(ValueError, match="absolute path"):
        apply_state(project_root="", workspace_root="")


def test_emit_project_artifacts_rejects_relative_root():
    from aitelier.tools.emit_project_artifacts.impl import emit_project_artifacts
    with pytest.raises(ValueError, match="absolute path"):
        emit_project_artifacts(workspace_root=".")
    with pytest.raises(ValueError, match="absolute path"):
        emit_project_artifacts(workspace_root="")


def test_repo_delete_rejects_relative_root_before_git(tmp_path):
    """repo_delete only reaches project_root after finding a non-empty manifest;
    with a queued deletion + a relative project_root it must error, not git-rm CWD."""
    import json
    from aitelier.tools.repo_delete.impl import repo_delete
    step = tmp_path / "step"
    step.mkdir()
    (step / "_deletions.json").write_text(json.dumps(["some/file.py"]), encoding="utf-8")
    r = repo_delete(source_dir=str(step), project_root=".")
    assert r["committed"] is False
    assert "absolute path" in (r.get("error") or ""), r
