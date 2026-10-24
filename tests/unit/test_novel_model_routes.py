"""Ownership and portability checks for the novel pipeline's model aliases."""

import json
import os
from pathlib import Path

import pytest
import yaml

from core.model_routes import ModelRoutes

ROOT = Path(__file__).resolve().parents[2]
ROLE_FILES = (
    ROOT / "agent_configs" / "novel_chapter.yaml",
    ROOT / "agent_configs" / "novel_init.yaml",
)
EXPECTED_ROLE_ROUTES = {
    "novel_outliner": "novel",
    "novel_writer": "novel",
    "novel_humanizer": "novel_alt",
    "novel_chapter_reviewer": "novel_alt",
    "novel_brainstormer": "novel",
    "novel_designer": "novel",
    "novel_design_reviewer": "novel_alt",
}
SPECIALIZED_ALIASES = {"novel", "novel_alt"}


def _roles() -> dict:
    roles = {}
    for path in ROLE_FILES:
        roles.update(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return roles


def _private_routes_path() -> Path:
    configured = os.environ.get("AITELIER_PRIVATE_MODEL_ROUTES")
    return Path(configured) if configured else ROOT / "model_routes.json"


def _assert_specialized_alias_parity(path: Path) -> None:
    private = ModelRoutes(path)
    actual = {name for name in private.names() if name.startswith("novel")}
    assert actual == SPECIALIZED_ALIASES, (
        f"{path}: specialized novel aliases drifted: expected "
        f"{sorted(SPECIALIZED_ALIASES)}, got {sorted(actual)}"
    )
    for alias in SPECIALIZED_ALIASES:
        assert private.resolve(alias)


def test_novel_roles_own_explicit_specialized_aliases():
    selected = {name: _roles()[name]["model"] for name in EXPECTED_ROLE_ROUTES}
    assert selected == EXPECTED_ROLE_ROUTES
    assert set(selected.values()) == SPECIALIZED_ALIASES
    assert "smart" not in selected.values()


def test_clean_checkout_resolves_every_novel_role_from_tracked_example():
    routes = ModelRoutes(ROOT / "model_routes.example.json")
    for role, alias in EXPECTED_ROLE_ROUTES.items():
        assert routes.resolve(alias), f"{role} -> {alias} is undeclared"


def test_example_novel_routes_add_no_private_or_smart_endpoint():
    """Novel defaults may reuse public examples, never disclose a new endpoint."""
    raw = json.loads((ROOT / "model_routes.example.json").read_text(encoding="utf-8"))
    routes = ModelRoutes(ROOT / "model_routes.example.json")
    public_existing = set(routes.resolve("flash")) | set(routes.resolve("glm"))
    smart = set(routes.resolve("smart"))
    for alias in SPECIALIZED_ALIASES:
        candidates = set(routes.resolve(alias))
        assert candidates <= public_existing
        assert candidates.isdisjoint(smart)
        assert alias in raw


def test_private_production_alias_set_has_no_novel_drift():
    """Opt-in parity check; endpoint bytes remain deployment-private."""
    configured = os.environ.get("AITELIER_PRIVATE_MODEL_ROUTES")
    path = _private_routes_path()
    if not path.is_file():
        if configured:
            # An operator explicitly asked for this measurement: never skip it.
            raise AssertionError(
                f"AITELIER_PRIVATE_MODEL_ROUTES={configured!r} is not a readable "
                f"file ({path}); private route parity was requested but not measured"
            )
        pytest.skip(
            "set AITELIER_PRIVATE_MODEL_ROUTES to run the private route parity check"
        )
    _assert_specialized_alias_parity(path)


def _run_real_private_entry_explicit() -> None:
    """Run the real entry; an explicit-input skip is converted to a failure."""
    try:
        test_private_production_alias_set_has_no_novel_drift()
    except pytest.skip.Exception as exc:
        pytest.fail(f"explicit private route input was skipped, not measured: {exc}")


def test_explicit_missing_private_input_is_actionable_failure(monkeypatch, tmp_path):
    missing = tmp_path / "absent_routes.json"
    monkeypatch.setenv("AITELIER_PRIVATE_MODEL_ROUTES", str(missing))
    with pytest.raises(AssertionError, match="AITELIER_PRIVATE_MODEL_ROUTES") as info:
        _run_real_private_entry_explicit()
    assert str(missing) in str(info.value)


def test_explicit_directory_private_input_is_actionable_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("AITELIER_PRIVATE_MODEL_ROUTES", str(tmp_path))
    with pytest.raises(AssertionError, match="AITELIER_PRIVATE_MODEL_ROUTES") as info:
        _run_real_private_entry_explicit()
    assert str(tmp_path) in str(info.value)


def test_explicit_valid_private_input_runs_real_parity(monkeypatch, tmp_path):
    path = tmp_path / "routes.json"
    path.write_text(
        json.dumps({"novel": ["public/prose"], "novel_alt": ["public/reviewer"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AITELIER_PRIVATE_MODEL_ROUTES", str(path))
    _run_real_private_entry_explicit()


def test_explicit_drifted_private_input_fails_via_entry(monkeypatch, tmp_path):
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"novel": ["public/prose"]}), encoding="utf-8")
    monkeypatch.setenv("AITELIER_PRIVATE_MODEL_ROUTES", str(path))
    with pytest.raises(AssertionError, match="specialized novel aliases drifted"):
        _run_real_private_entry_explicit()


def test_optional_unconfigured_missing_default_still_skips(monkeypatch, tmp_path):
    monkeypatch.delenv("AITELIER_PRIVATE_MODEL_ROUTES", raising=False)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)  # no model_routes.json here
    with pytest.raises(pytest.skip.Exception, match="AITELIER_PRIVATE_MODEL_ROUTES"):
        test_private_production_alias_set_has_no_novel_drift()



@pytest.mark.parametrize(
    "routes",
    [
        {"novel": ["public/prose"]},
        {
            "novel": ["public/prose"],
            "novel_alt": ["public/reviewer"],
            "novel_extra": ["public/other"],
        },
    ],
)
def test_private_alias_drift_is_actionable(tmp_path, routes):
    path = tmp_path / "model_routes.json"
    path.write_text(json.dumps(routes), encoding="utf-8")
    with pytest.raises(AssertionError, match="specialized novel aliases drifted"):
        _assert_specialized_alias_parity(path)
