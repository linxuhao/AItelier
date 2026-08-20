"""t_impl's static gate: the code must be IMPORTABLE before anything runs it.

NL2Repo sweep 2026-08-18. Five tasks delivered substantially complete repos that
scored zero, ~3200 tests, because one import-time defect made the package
unimportable. Clearest case, `fastapi-users`: a missing TypeVar import — `AP`
used in a class-body `Generic[...]` and never imported — cost all 556 tests.
AItelier saw it: 5_test reported the traceback and 5_review wrote a blocking
verdict naming the file, the line and the one-line fix. It then goal-looped back
to the PM twice, fixed three OTHER things, and ran out of budget with the bug
shipped, because pytest surfaces one collection error at a time and
`5_review → 3` is capped at `max_loop: 2`.

Running the code is the wrong instrument for this: Python fails fast, so an
import reports the first exception in a module and nothing after it. A static
pass has no such limit — `NameError: name 'AP' is not defined` at class-body
scope IS pyflakes/ruff F821, findable across the whole tree without executing
anything. The gate for it was already declared at t_impl and had never run once:
`workspace_root: "$STEP_DIR"` is not resolved by StepValidator, so `lint` globbed
a directory named `$STEP_DIR`, matched nothing, and returned all_passed on an
empty result set.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import skillflow
from skillflow.lint_backends import register_backend
from skillflow.step_validation import StepValidator
from skillflow.tool_loader import ToolLoader
from skillflow.tools.lint.impl import lint

from api.dependencies import _ruff_check_only

DPE_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "dpe_default.yaml"

# The delivered file, reduced to the shape that matters: every import resolves,
# the syntax is valid, and one name in the class header is undefined.
FASTAPI_USERS_DEFECT = '''\
import secrets
from typing import Any, Generic, Optional


class DatabaseStrategy(
    Strategy[Any, Any], Generic[Any, Any, AP]
):
    def __init__(self, database: Any, lifetime_seconds: Optional[int] = None):
        self.database = database
        self.lifetime_seconds = lifetime_seconds

    def _create_access_token_dict(self, user: Any) -> dict[str, Any]:
        return {"token": secrets.token_urlsafe(), "user_id": user.id}
'''


@pytest.fixture(autouse=True)
def _host_backend():
    """The host override that get_skillflow() installs at boot."""
    register_backend("ruff", _ruff_check_only)


def _t_impl() -> dict:
    steps = yaml.safe_load(DPE_CONFIG.read_text(encoding="utf-8"))["steps"]
    return next(s for s in steps if s["id"] == "t_impl")


def _validate(staging: Path) -> dict:
    """Run the SHIPPED t_impl validation specs the way skillflow runs them.

    StepValidator's root is the step's staging dir — the tree the implementer
    just wrote — supplied by the engine, never by the config.
    """
    loader = ToolLoader(Path(skillflow.__file__).resolve().parent / "tools")
    # The gate also runs gdscript_check, an AItelier tool — mirror the loader
    # api/dependencies.py builds at boot, or every spec fails "Tool not found".
    loader.add_tools_dir(Path(__file__).resolve().parents[2] / "aitelier" / "tools")
    return StepValidator(loader, staging).validate(_t_impl()["validation"])


# ── the defect that zeroed 556 tests ─────────────────────────────────────────

def test_undefined_name_at_class_body_scope_fails_the_gate(tmp_path):
    """The fastapi-users regression, through the real config and real validator."""
    (tmp_path / "strategy.py").write_text(FASTAPI_USERS_DEFECT)

    result = _validate(tmp_path)

    assert result["passed"] is False, (
        "an undefined name in a class header passed the gate: the package is "
        "unimportable and every test in it will count as failed"
    )
    reported = " ".join(str(e) for e in result["errors"])
    assert "AP" in reported and "strategy.py" in reported, reported


def test_the_defect_is_found_among_files_that_are_fine(tmp_path):
    """One pass over the tree, not a stop at the first file."""
    pkg = tmp_path / "pkg" / "sub"
    pkg.mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "ok.py").write_text("import json\n\n\ndef dump(x):\n    return json.dumps(x)\n")
    (pkg / "strategy.py").write_text(FASTAPI_USERS_DEFECT)

    result = _validate(tmp_path)

    assert result["passed"] is False
    assert any("strategy.py" in str(e) for e in result["errors"])


def test_every_broken_file_is_reported_in_one_pass(tmp_path):
    """A compiler reports everything it found; so must this.

    Importing cannot: Python aborts a module at its first exception, so a probe
    that imports each module yields one defect per module and one traceback per
    run. Reporting them together is the whole reason to check statically.
    """
    for i in range(3):
        (tmp_path / f"mod_{i}.py").write_text(f"value = MISSING_{i}\n")

    result = _validate(tmp_path)

    assert result["passed"] is False
    reported = " ".join(str(e) for e in result["errors"])
    for i in range(3):
        assert f"MISSING_{i}" in reported, reported


# ── why the config must not name a workspace_root ────────────────────────────

def test_a_literal_step_dir_makes_the_gate_a_vacuous_pass(tmp_path):
    """The actual 2026-08-18 bug, pinned so it cannot come back.

    `lint` returns `all_passed` over an EMPTY result set, so a workspace_root
    that resolves to nothing is indistinguishable from clean code. 36 lint calls
    in the fastapi-users run, every one of them this.
    """
    (tmp_path / "strategy.py").write_text(FASTAPI_USERS_DEFECT)

    assert lint(["*.py"], workspace_root="$STEP_DIR") == {
        "all_passed": True, "results": []}
    assert lint(["*.py"], workspace_root=str(tmp_path))["all_passed"] is False


@pytest.mark.parametrize("spec", [
    *_t_impl()["validation"],
    *[s for s in _t_impl()["lifecycle"]["after_deliver"] if "*.py" in str(s.get("files"))],
])
def test_python_lint_specs_let_skillflow_supply_the_root(spec):
    """StepValidator resolves no variables; only the on_deliver tool-hook path does.

    So any workspace_root written into a check spec is passed through verbatim,
    and every value a config could write is either wrong or already the default.
    """
    assert "workspace_root" not in spec, (
        f"{spec} sets workspace_root: StepValidator passes it through unresolved, "
        f"which is how this gate spent the whole sweep matching zero files"
    )


def test_python_lint_specs_do_not_delegate_the_checker_to_the_manifest():
    """`lint` returns passed=True for a backend name it does not recognise.

    linter_manifest.json is written by the architect agent, so one invented
    linter name ("flake8", "pylint") switches the importability gate off with no
    error anywhere. Which checker guards importability is not the agent's choice.
    """
    specs = [_t_impl()["validation"][0]] + [
        s for s in _t_impl()["lifecycle"]["after_deliver"]
        if "*.py" in str(s.get("files"))]
    for spec in specs:
        assert "manifest_path" not in spec, spec

    # the built-in default is what those specs fall back to
    from skillflow.tools.lint.impl import _DEFAULT_MANIFEST
    assert _DEFAULT_MANIFEST[".py"] == "ruff"


def test_an_unknown_backend_would_pass_anything(tmp_path):
    """The mechanism the test above guards against, stated once."""
    (tmp_path / "strategy.py").write_text(FASTAPI_USERS_DEFECT)
    (tmp_path / "m.json").write_text('{".py": "flake8"}')
    assert lint(["*.py"], workspace_root=str(tmp_path),
                manifest_path="m.json")["all_passed"] is True


# ── scope: importability only, never style ───────────────────────────────────
# Step "3" carries the record of what over-strict validation costs — its
# json_schema was removed because a check the agent could not satisfy retried
# forever. So the selection is E9 (syntax), F63/F7 (real bugs) and F82
# (undefined name), and nothing that a working program can be guilty of.

@pytest.mark.parametrize("name,source", [
    ("unused_import", "import os\nimport json\n\n\nx = json.dumps({})\n"),
    ("unsorted_imports", "import sys\nimport json\nimport os\n\n\nx = (sys, json, os)\n"),
    ("long_line", "x = 1  # " + "y" * 300 + "\n"),
    ("bad_quotes", "x = 'single'\ny = \"double\"\n"),
    ("no_docstring", "def f():\n    return 1\n"),
    ("bare_except", "try:\n    x = 1\nexcept Exception:\n    x = 2\n"),
    ("mutable_default", "def f(xs=[]):\n    return xs\n"),
    ("star_import", "from os.path import *\n\n\nx = join('a', 'b')\n"),
    ("typing_only_import", (
        "from typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:\n    from decimal import Decimal\n\n\n"
        "def f(x: 'Decimal') -> 'Decimal':\n    return x\n")),
    ("forward_ref_to_own_class", (
        "class Node:\n    def clone(self) -> 'Node':\n        return Node()\n")),
])
def test_style_and_valid_idioms_are_not_gated(tmp_path, name, source):
    (tmp_path / f"{name}.py").write_text(source)
    result = _validate(tmp_path)
    assert result["passed"] is True, f"{name} was rejected: {result['errors']}"


def test_syntax_error_fails(tmp_path):
    (tmp_path / "broken.py").write_text("def f(:\n    return 1\n")
    assert _validate(tmp_path)["passed"] is False


def test_an_empty_step_is_not_a_failure(tmp_path):
    """A step that writes no .py file (docs, config) must still pass."""
    (tmp_path / "README.md").write_text("# notes\n")
    assert _validate(tmp_path)["passed"] is True


# ── the gate checks; it must not edit ────────────────────────────────────────

def test_the_gate_never_rewrites_the_code_it_checks(tmp_path):
    """skillflow's built-in ruff backend runs `ruff check --fix` FIRST.

    Replayed over the 12 delivered repos of the sweep, that pass deleted imports
    the tests depend on — this file is freezegun's `another_module.py`, whose
    aliased imports exist precisely so the tests can watch them get patched, and
    which a per-file linter can only read as unused. The t_impl lint had been a
    no-op since it was written, so un-breaking it is also the first time that
    auto-fix would ever have touched delivered code.
    """
    module = ("from datetime import date as date_alias\n"
              "from datetime import datetime as dt_alias\n"
              "from time import time as time_alias\n")
    (tmp_path / "another_module.py").write_text(module)

    assert _validate(tmp_path)["passed"] is True
    assert (tmp_path / "another_module.py").read_text() == module, (
        "the gate rewrote the file it was asked to check"
    )


def test_the_host_backend_is_the_one_lint_dispatches_to(tmp_path):
    """The override is only real if `lint` actually calls it."""
    seen: list[Path] = []

    def spy(fp: Path) -> dict:
        seen.append(fp)
        return {"file": str(fp), "passed": True, "error_message": ""}

    register_backend("ruff", spy)
    try:
        (tmp_path / "a.py").write_text("x = 1\n")
        lint(["*.py"], workspace_root=str(tmp_path))
    finally:
        register_backend("ruff", _ruff_check_only)
    assert [p.name for p in seen] == ["a.py"]


def test_a_ruff_that_will_not_start_does_not_invent_failures(tmp_path, monkeypatch):
    """A missing checker must not masquerade as broken code — the run_tests rule.

    The syntax check still stands, so the file is not waved through blind.
    """
    def boom(*a, **k):
        raise FileNotFoundError("no ruff here")
    monkeypatch.setattr(subprocess, "run", boom)

    good = tmp_path / "good.py"
    good.write_text("x = 1\n")
    assert _ruff_check_only(good)["passed"] is True

    bad = tmp_path / "bad.py"
    bad.write_text("def f(:\n")
    assert _ruff_check_only(bad)["passed"] is False


def test_ruff_is_actually_installed():
    """The gate is only as real as its checker.

    ruff is a hard dependency of skillflow-py (`ruff>=0.4`), so the container
    gets it from `pip install -e .` — but the fallback above passes files when it
    cannot run, so an install that lost ruff would silently return this gate to
    the no-op it just came out of. Fail here instead, where it is readable.
    """
    assert shutil.which("ruff") or subprocess.run(
        [sys.executable, "-m", "ruff", "--version"],
        capture_output=True).returncode == 0, (
        "ruff is not runnable: t_impl's importability gate degrades to a syntax "
        "check and undefined names ship again"
    )
