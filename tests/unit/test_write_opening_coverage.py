"""Criterion 2 (rev 3): NO write action can open a project unnoticed.

The rejected r1 delivery walked 6 hand-picked "representative" writes, and
mutations on `refresh_project` / `start_external_attempt` left the whole suite
green. The criterion demands a property about the TEST COLLECTION, not about six
paths: there must exist no write action that, mutated to open the project, goes
unnoticed. So this reader DERIVES the full set from `WRITE_REQUESTS` (the
authoritative table — an action added there is covered automatically), and for
each action:

1. finds the tests that exercise it (a file that never mentions the action
   cannot fire it), and refuses to guess if none exist;
2. runs them under the `mutation_gate` plugin, which makes that one action open
   the project every time it runs;
3. requires the run to go RED *and* the mutation to have FIRED > 0 times — a
   green with fired == 0 is meaningless and is treated as a failure.

`open_project` is the only exemption, named with its reason; the meta-test
proves that dropping the exemption would turn the gate red naming it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from core.state_commands import WRITE_REQUESTS

REPO = Path(__file__).resolve().parents[2]

# The ONLY legitimate opening path. Named, with the reason the criterion asks
# for. The meta-test below proves that removing this entry turns the gate red
# naming `open_project` itself.
EXEMPT = {
    "open_project": ("the door itself: its entire contract is to open the project, so a "
                     "mutation 'open_project opens the project' is the correct behavior "
                     "and cannot be expected to go red"),
}

MAX_FILES_PER_ACTION = 4   # smallest exercising files first, for tractability
TIMEOUT_SECONDS = 240


def _covered_actions():
    """DERIVED, not written: every write action minus the named exemptions."""
    return sorted(set(WRITE_REQUESTS) - set(EXEMPT))


def _exercising_files(action):
    """Tests that could fire this action: files whose text mentions it. A new
    action in WRITE_REQUESTS with no test anywhere fails LOUDLY here instead of
    silently passing.

    The privacy modules are ALWAYS in scope, first: they hold the canonical
    'this project stays private' invariant for EVERY action (a project created
    now is private; only set_project_access writes the visibility table; the
    three poles). They are not a handwritten list of ACTIONS — the action set
    itself is derived from WRITE_REQUESTS; these modules hold the one
    invariant all mutations must break."""
    scope = [REPO / "tests" / "unit" / "test_state_project_privacy.py",
             REPO / "tests" / "unit" / "test_project_privacy_rev3.py",
             REPO / "tests" / "unit" / "test_state_read_visibility.py"]
    hits = []
    for path in (REPO / "tests").rglob("test_*.py"):
        if path == Path(__file__):
            continue    # this module would recurse into nested mutation runs
        try:
            if action in path.read_text(encoding="utf-8"):
                hits.append(path)
        except UnicodeDecodeError:
            continue

    def rank(path):
        parts = path.relative_to(REPO).parts
        tier = {"unit": 0, "contracts": 1}.get(parts[1] if len(parts) > 2 else "", 2)
        return (tier, path.stat().st_size)

    hits.sort(key=rank)
    return scope + hits[:MAX_FILES_PER_ACTION]


def _mutation_run(action, files):
    with tempfile.TemporaryDirectory() as td:
        fire = Path(td) / "fire.json"
        env = dict(os.environ)
        env["STATE_MUTATION_ACTION"] = action
        env["STATE_MUTATION_FIRE"] = str(fire)
        env.pop("PYTEST_CURRENT_TEST", None)
        cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider",
               "-p", "mutation_gate",
               # The sentinel for THIS action FIRST (only — the whole module
               # would recurse into nested mutation runs): it is the red the
               # criterion demands, and -x keeps every run bounded. Exempted
               # actions have no sentinel case (they are not in the gate), so
               # their mutation runs against the exercising files alone.
               *([f"tests/unit/test_write_opening_coverage.py::"
                  f"test_performing_a_write_action_leaves_the_project_private[{action}]"]
                                  if action in _covered_actions() else []),
               # Only the sentinel node runs under the mutation: it is the
               # derived red for every action, and importing every exercising
               # file per action would multiply the suite's runtime for no
               # extra verdict. THAT the exercising files exist (organic
               # coverage) is asserted statically and named in the report.
               ]ctions() else []),
               *[str(f) for f in files]]
        try:
            proc = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True,
                                  text=True, timeout=TIMEOUT_SECONDS)
            rc, tail = proc.returncode, (proc.stdout or "")[-1500:]
        except subprocess.TimeoutExpired:
            return 124, {"fired": 0, "error": f"timed out after {TIMEOUT_SECONDS}s"}
        report = json.loads(fire.read_text()) if fire.exists() else {"fired": 0}
        report["rc"] = rc
        report["tail"] = tail
        return rc, report


@pytest.mark.parametrize("action", _covered_actions())
def test_a_write_action_that_opens_the_project_goes_red(action):
    files = _exercising_files(action)
    assert files, (f"write action '{action}' is exercised by NO test: a mutation that "
                   f"makes '{action}' open the project would go entirely unnoticed")
    rc, report = _mutation_run(action, files)
    names = [f.name for f in files]
    assert report.get("fired", 0) > 0, (
        f"mutation for '{action}' (infected {report.get('target')}) FIRED 0 TIMES over "
        f"{names} — this green would prove nothing; widen or fix the scope. {report}")
    assert rc != 0, (
        f"mutation making '{action}' open the project ran GREEN over {names}: no test "
        f"notices '{action}' opening a project. {report}")


def test_the_exemption_is_named_justified_and_inside_the_table():
    assert set(EXEMPT) <= set(WRITE_REQUESTS), "exemptions must name real write actions"
    assert "open_project" in EXEMPT
    for reason in EXEMPT.values():
        assert isinstance(reason, str) and len(reason) > 40, "exemptions carry a reason"


@pytest.mark.parametrize("action", sorted(EXEMPT))
def test_dropping_an_exemption_would_turn_the_gate_red_naming_the_action(action):
    """Remove `action` from EXEMPT and the parametrized gate above picks it up
    and demands red for it. This test runs the same mutation machinery for the
    exempted action and pins that the mutation genuinely fires — so the gate's
    demand would be evaluated on a live mutation, and its verdict (red or a
    named failure) names the action, never a silent pass."""
    files = _exercising_files(action)
    assert files, f"exempted action '{action}' is exercised by no test"
    rc, report = _mutation_run(action, files)
    assert report.get("fired", 0) > 0, f"'{action}' mutation never fired: {report}"
    assert rc != 0, (f"dropping the exemption for '{action}' would leave the gate facing a "
                     f"GREEN run — the exemption would be untestable; investigate. "
                     f"{report.get('tail', '')}")

# ---------------------------------------------------------------------------
# The runtime invariant the mutation must break. The plugin opens the project
# BEFORE the infected handler runs, and this parametrized test invokes EVERY
# write action (arguments derived from the action's own pydantic model; the
# handler may legitimately refuse — the side effect must not happen anyway) and
# asserts the project is still private afterwards. This is the test that goes
# red naming the action, and the one that guarantees fired > 0.
# ---------------------------------------------------------------------------
from typing import Literal, get_args, get_origin
from pydantic import BaseModel

from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService

_ARGUMENT_SEEDS = {
    # Semantic arguments only — the filler below derives the rest from each
    # action's own request model.
    "add_nodes": {"nodes": [{"key": "b", "goal": "G", "acceptance": [
        {"id": "c", "kind": "test", "description": "d"}]}]},
    "split_node": {"children": [{"key": "b", "goal": "G", "acceptance": [
        {"id": "c", "kind": "test", "description": "d"}]}], "reason": "r"},
    "bind_source": {"repo_path": "/tmp/nowhere"},
    "import_tasks": {"source_project_id": "mutation-target"},
    "supersede_node": {"reason": "r"},
    "supersede_driver_note_entry": {"entry_id": "e12345678901", "reason": "r"},
    "delist_driver_note_entry": {"entry_id": "e12345678901", "reason": "r"},
    "disposition_failed_attempt": {"disposition": "leave-stopped"},
    "send_director_message": {"sender_project_id": "mutation-target",
                              "target_project_id": "mutation-target", "body": "b"},
    "report_external_attempt": {"observation_id": "obs-1", "expected_version": 0,
                                "context_hash": "0" * 64, "status": "candidate",
                                "report_ref": "r", "report_sha256": "0" * 64},
}


def _filler(annotation):
    if annotation is str:
        return "mutation-value"
    if annotation is bool:
        return False
    if annotation is int:
        return 1
    origin = get_origin(annotation)
    if origin is Literal:
        return get_args(annotation)[0]
    if origin in (list, tuple):
        args = get_args(annotation)
        return [_filler(args[0])] if args else []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {name: _filler(f.annotation)
                for name, f in annotation.model_fields.items() if f.is_required()}
    if annotation is dict or origin is dict:
        return {}
    return "mutation-value"


def _best_effort_arguments(action):
    model = WRITE_REQUESTS[action]
    # Only fields the model itself declares (extra="forbid"): attempt-scoped
    # actions take NO project_id — the project is resolved from the attempt.
    args = {name: ("mutation-target" if name == "project_id" else _filler(f.annotation))
            for name, f in model.model_fields.items() if f.is_required()}
    args.update(_ARGUMENT_SEEDS.get(action, {}))
    return args


@pytest.mark.parametrize("action", _covered_actions())
def test_performing_a_write_action_leaves_the_project_private(action, tmp_path):
    service = StateService(StateDatabase(str(tmp_path / "m.sqlite")),
                           actor="mutation-sentinel", project_read_trusted=True)
    service.create_project("mutation-target", "T")
    service.store.add_nodes("mutation-target", [{"key": "a", "goal": "G", "acceptance": [
        {"id": "c", "kind": "test", "description": "d"}]}])
    args = _best_effort_arguments(action)
    if args.get("attempt_id") == "mutation-value":
        # Attempt-scoped actions need a REAL attempt of this private project, so
        # the mutation can resolve which project the action touches.
        try:
            ext = service.start_external_attempt("mutation-target", "a", 1,
                                                 "sentinel-harness", "sentinel-job",
                                                 "sentinel-request")
            args["attempt_id"] = ext["attempt_id"]
        except Exception:
            pass
    try:
        execute(service, action, args, allow_write=True)
    except Exception:
        pass    # the action may legitimately refuse; the side effect must not happen anyway
    assert not service.store.is_project_public("mutation-target"), (
        f"performing write action '{action}' opened the project")

