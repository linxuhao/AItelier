"""Criterion 2 (rev 3): NO write action can open a project unnoticed.

The rejected r1 delivery walked 6 hand-picked "representative" writes, and
mutations on `refresh_project` / `start_external_attempt` left the whole suite
green. The criterion demands a property about the TEST COLLECTION, not about six
paths: there must exist no write action that, mutated to open the project, goes
unnoticed. So this reader DERIVES the full set from `WRITE_REQUESTS` (the
authoritative table — an action added there is covered automatically), and for
each action:

1. requires a real ORGANIC test witness (a test file outside this module and the
   always-included privacy modules that names or calls the action) and refuses
   to pass without one;
2. runs them under the `mutation_gate` plugin, which makes that one action open
   the project every time it runs;
3. requires the run to go RED *and* the mutation to have FIRED > 0 times — a
   green with fired == 0 is meaningless and is treated as a failure.

The run happens IN THIS PROCESS: the row installs the plugin's own mutation
(`mutation_gate.pytest_configure`, undone at teardown) and calls the sentinel
`test_performing_a_write_action_leaves_the_project_private[action]` directly.
That is what each nested `pytest -x -p mutation_gate <sentinel> <files>` run used
to amount to: `-x` stopped it at the sentinel's red, so the exercising files
never ran. Only when the sentinel stays green under the mutation does the row
still start the nested pytest over the exercising files, as `-x` would have
gone on to them.

`open_project` is the only exemption, named with its reason; the meta-test
proves that dropping the exemption would turn the gate red naming it.
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import core.state_commands as state_commands
import core.state_database as state_database
import core.state_service as state_service
import mutation_gate
from core.state_commands import WRITE_REQUESTS
from tests.support import state_template

REPO = Path(__file__).resolve().parents[2]

# The ONLY legitimate opening path. Named, with the reason the criterion asks
# for. The meta-test below proves that removing this entry turns the gate red
# naming `open_project` itself.
EXEMPT = {
    "open_project": ("the door itself: its entire contract is to open the project, so a "
                     "mutation 'open_project opens the project' is the correct behavior "
                     "and cannot be expected to go red"),
}

# Write actions whose request names NO project at all: the steward's global connection
# registry (step 24.1a, design-steward 7.8.4) and its host's fallback-marker write (the
# turn audience is step 23's set_steward_turn_audience, which names the steward's project). The mutation "this action opens the project it touched" has
# no project to open for them (mutation_gate finds none and fires 0 times), so they
# are outside the gate; `test_a_projectless_action_names_no_project` pins that their
# request models carry no project-naming field, so a field added later puts the action
# back in the gate.
PROJECTLESS = {action: "the request names no project: the steward's connection registry and its host's "
                       "marker write are global, so no project can be opened by the mutation"
               for action in ("steward_connection_upsert", "steward_connection_delete",
                              "steward_connection_tools_set", "steward_connection_secret_set",
                              "steward_connection_probe", "steward_connection_refresh",
                              "steward_tools_resync_due",
                              # Step 24.1b: the host's confirmation request and read.
                              "create_steward_confirmation", "get_steward_confirmation",
                              "cancel_steward_confirmation")}
_PROJECT_FIELDS = {"project_id", "sender_project_id", "target_project_id", "attempt_id", "delivery_id",
                   "node_key", "driver_id"}

MAX_FILES_PER_ACTION = 4   # smallest exercising files first, for tractability
TIMEOUT_SECONDS = 240


def _covered_actions():
    """DERIVED, not written: every write action minus the named exemptions."""
    return sorted(set(WRITE_REQUESTS) - set(EXEMPT) - set(PROJECTLESS))


_SCOPE_MODULE_NAMES = ("test_state_project_privacy.py",
                       "test_project_privacy_rev3.py",
                       "test_state_read_visibility.py")


@functools.cache
def _candidate_sources():
    """(path, text) of every test file a witness may come from, read once per
    process: the tree does not change under the scan.
    This module and the three always-included privacy modules never count (they
    would make the witness requirement always true)."""
    own = Path(__file__).resolve()
    sources = []
    for path in (REPO / "tests").rglob("test_*.py"):
        if path.resolve() == own or path.name in _SCOPE_MODULE_NAMES:
            continue
        try:
            sources.append((path, path.read_text(encoding="utf-8")))
        except UnicodeDecodeError:
            continue
    return tuple(sources)


_NAME = "[A-Za-z_][A-Za-z0-9_]*"
# A quoted name "x" / 'x' (the closing quote is only looked at, so it can open the
# next one), or a direct call x( not preceded by an identifier character.
_QUOTED_OR_CALLED = re.compile(rf"[\"']({_NAME})(?=[\"'])|(?<![A-Za-z0-9_])({_NAME})\(")


@functools.cache
def _source_names():
    """(path, names) per candidate file: every name it quotes or calls, found in one
    pass per file instead of one regex search per action and file."""
    return tuple((path, frozenset(quoted or called
                                  for quoted, called in _QUOTED_OR_CALLED.findall(text)))
                 for path, text in _candidate_sources())


def _organic_witnesses(action):
    """Sorted test files under tests/ that really exercise `action`: the action
    appears as a quoted string or a direct call `name(`."""
    assert re.fullmatch(_NAME, action), f"write action '{action}' is not an identifier"
    return sorted(path for path, names in _source_names() if action in names)


def _exercising_files(action):
    """Files run under the mutation: the always-included privacy modules first,
    then the smallest ORGANIC witnesses of the action. That a witness exists is
    asserted separately (the privacy modules never count as one).

    The privacy modules are ALWAYS in scope: they hold the canonical
    'this project stays private' invariant for EVERY action (a project created
    now is private; only set_project_access writes the visibility table; the
    three poles). They are not a handwritten list of ACTIONS — the action set
    itself is derived from WRITE_REQUESTS; these modules hold the one
    invariant all mutations must break."""
    scope = [REPO / "tests" / "unit" / name for name in _SCOPE_MODULE_NAMES]
    hits = _organic_witnesses(action)

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
        # The sentinel is not in this run: the caller ran it in-process under the
        # same mutation and it stayed green, so this is the rest of what the old
        # `pytest -x <sentinel> <files>` run went on to: the exercising files.
        cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider",
               "-p", "mutation_gate", *[str(f) for f in files]]
        cmd_line = " ".join(cmd)
        try:
            proc = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True,
                                  text=True, timeout=TIMEOUT_SECONDS)
            rc, tail = proc.returncode, (proc.stdout or "")[-1500:]
            stderr_tail = (proc.stderr or "")[-1500:]
        except subprocess.TimeoutExpired as exc:
            def _partial(data):
                if data is None:
                    return ""
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                return data[-1500:]
            return 124, {"fired": 0, "error": f"timed out after {TIMEOUT_SECONDS}s",
                         "tail": _partial(exc.stdout),
                         "stderr_tail": _partial(exc.stderr),
                         "cmd": cmd_line}
        report = json.loads(fire.read_text()) if fire.exists() else {"fired": 0}
        report["rc"] = rc
        report["tail"] = tail
        report["stderr_tail"] = stderr_tail
        report["cmd"] = cmd_line
        return rc, report


@pytest.fixture(scope="module")
def _write_handlers(_state_templates, tmp_path_factory):
    """The `_handlers` table of one trusted probe service: what the plugin infects
    from. Built once per module on a template copy; the plugin's own probe builds a
    fresh schema, which costs seconds per action."""
    path = _state_templates.copy(state_template.state,
                                 tmp_path_factory.mktemp("write-handlers") / "probe.sqlite")
    return state_commands._handlers(StateService(StateDatabase(path), project_read_trusted=True))


@pytest.fixture
def mutate(_write_handlers, monkeypatch):
    """`mutate(action)` installs in THIS process the mutation `-p mutation_gate`
    installs in a nested run, by calling the plugin's own `pytest_configure`, so
    the infected callable and the opening are the plugin's, not a copy. Teardown
    restores the original callable. Returns the plugin's live report dict."""
    def install(action):
        handler = _write_handlers[action]
        owner, name = type(handler.__self__), handler.__name__
        monkeypatch.setattr(owner, name, getattr(owner, name))    # teardown restores it
        monkeypatch.setattr(mutation_gate, "_STATE",
                            {"action": None, "target": None, "fired": 0, "error": None})
        monkeypatch.setenv("STATE_MUTATION_ACTION", action)
        monkeypatch.delenv("STATE_MUTATION_FIRE", raising=False)
        with monkeypatch.context() as m:
            # Hand the plugin the table derived once above instead of its probe.
            m.setattr(state_database, "StateDatabase", lambda path: None)
            m.setattr(state_service, "StateService", lambda db, **kwargs: None)
            m.setattr(state_commands, "_handlers", lambda probe: _write_handlers)
            mutation_gate.pytest_configure(None)
        assert mutation_gate._STATE["target"] == f"{owner.__module__}.{owner.__qualname__}.{name}"
        return mutation_gate._STATE
    return install


def _gate_run(action, mutate, tmp_path, state_db_copy):
    """(red, report) for `action` under its mutation. The sentinel runs here; if it
    stays green, the exercising files run in a nested pytest under the same mutation."""
    state = mutate(action)
    try:
        test_performing_a_write_action_leaves_the_project_private(action, tmp_path, state_db_copy)
    except Exception as exc:    # any non-pass is what made the nested run exit non-zero
        return True, {**state, "sentinel": f"{type(exc).__name__}: {exc}"[:1500]}
    files = _exercising_files(action)
    rc, report = _mutation_run(action, files)
    report["fired"] = report.get("fired", 0) + state["fired"]
    report["sentinel"] = f"green, fired {state['fired']}; ran {[f.name for f in files]}"
    return rc != 0, report


@pytest.mark.parametrize("action", _covered_actions())
def test_a_write_action_that_opens_the_project_goes_red(action, mutate, tmp_path, state_db_copy):
    assert _organic_witnesses(action), (
        f"write action '{action}' has no organic test witness: the always-included "
        f"privacy modules do not count as a witness, so a mutation that makes "
        f"'{action}' open the project could go entirely unnoticed")
    red, report = _gate_run(action, mutate, tmp_path, state_db_copy)
    assert report.get("fired", 0) > 0, (
        f"mutation for '{action}' (infected {report.get('target')}) FIRED 0 TIMES — "
        f"this green would prove nothing; widen or fix the scope. {report}")
    assert red, (
        f"mutation making '{action}' open the project ran GREEN: no test "
        f"notices '{action}' opening a project. {report}")


def test_a_projectless_action_names_no_project():
    assert set(PROJECTLESS) <= set(WRITE_REQUESTS)
    for action in PROJECTLESS:
        assert not set(WRITE_REQUESTS[action].model_fields) & _PROJECT_FIELDS, (
            f"'{action}' names a project now: take it out of PROJECTLESS so the gate covers it")


def test_the_exemption_is_named_justified_and_inside_the_table():
    assert set(EXEMPT) <= set(WRITE_REQUESTS), "exemptions must name real write actions"
    assert "open_project" in EXEMPT
    for reason in EXEMPT.values():
        assert isinstance(reason, str) and len(reason) > 40, "exemptions carry a reason"


@pytest.mark.parametrize("action", sorted(EXEMPT))
def test_dropping_an_exemption_would_turn_the_gate_red_naming_the_action(action, mutate, tmp_path,
                                                                         state_db_copy):
    """Remove `action` from EXEMPT and the parametrized gate above picks it up
    and demands red for it. This test runs the same mutation machinery for the
    exempted action and pins that the mutation genuinely fires — so the gate's
    demand would be evaluated on a live mutation, and its verdict (red or a
    named failure) names the action, never a silent pass."""
    assert _organic_witnesses(action), (
        f"exempted action '{action}' has no organic test witness: the always-included "
        f"privacy modules do not count as a witness")
    red, report = _gate_run(action, mutate, tmp_path, state_db_copy)
    assert report.get("fired", 0) > 0, f"'{action}' mutation never fired: {report}"
    assert red, (f"dropping the exemption for '{action}' would leave the gate facing a "
                     f"GREEN run — the exemption would be untestable; investigate. "
                     f"{report}")


def test_a_nested_run_that_cannot_start_keeps_its_stderr_command_and_exit(tmp_path):
    """A nested pytest that fails to START (usage error, conftest import error)
    explains itself on STDERR only; stdout holds just warnings. The report must
    therefore keep the stderr tail, the nested command and the exit code, or the
    failure shows as a bare 'fired 0' with the cause lost."""
    missing = tmp_path / "no_such_file_zz.py"
    rc, report = _mutation_run("heartbeat", [missing])
    assert rc == 4, report
    assert report["rc"] == 4, report
    assert report["fired"] == 0, report
    assert "file or directory not found" in report["stderr_tail"], report
    assert str(missing) in report["cmd"], report
    assert "-p mutation_gate" in report["cmd"], report


@pytest.mark.parametrize("action", sorted(WRITE_REQUESTS))
def test_every_write_action_has_an_organic_witness(action):
    """ALL write actions (exempt ones too) need a real test witness outside the
    always-included privacy modules; the action is named on failure."""
    witnesses = _organic_witnesses(action)
    assert witnesses, (f"write action '{action}' has no organic test witness: no test "
                       f"outside the always-included privacy modules names or calls it")
    assert not {w.name for w in witnesses} & set(_SCOPE_MODULE_NAMES), (
        f"always-included privacy modules leaked into the witnesses of '{action}'")


def test_an_action_with_no_organic_witness_is_reported(monkeypatch):
    monkeypatch.setitem(WRITE_REQUESTS, "zz_unwitnessed_action", WRITE_REQUESTS["heartbeat"])
    assert _organic_witnesses("zz_unwitnessed_action") == []
    assert _organic_witnesses("heartbeat")


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
    # Every heartbeat list is optional, but a batch needs at least one item.
    "heartbeat": {"claims": [{"claim_id": "claim-mutation", "fence": 1}]},
    "report_external_attempt": {"observation_id": "obs-1", "expected_version": 0,
                                "context_hash": "0" * 64, "status": "candidate",
                                "report_ref": "r", "report_sha256": "0" * 64},
    # P3 (multi-driver): a label must match its pattern and a handoff names ONE
    # subject, or pydantic refuses before the handler - and the mutation - can run.
    "register_subagent": {"label": "w", "workspace": "h:/w#b"},
    "offer_handoff": {"claim_id": "claim-mutation", "package": {"next_step": "x"}},
    # Presence v2: a takeover reason is 20..300 characters.
    "take_over_claim": {"reason": "mutation sentinel takeover reason"},
    # A payload hash is 64 hex characters, or pydantic refuses before the handler runs.
    "retire_context_run_protection": {"expected_payload_hash": "0" * 64},
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
    if "project_id" in model.model_fields and "driver_id" in model.model_fields:
        args["project_id"] = "mutation-target"
    if action == "send_driver_message":
        args["project_id"] = "mutation-target"
    args.update(_ARGUMENT_SEEDS.get(action, {}))
    return args


@pytest.mark.parametrize("action", _covered_actions())
def test_performing_a_write_action_leaves_the_project_private(action, tmp_path, state_db_copy):
    service = StateService(StateDatabase(state_db_copy(state_template.state, tmp_path / "m.sqlite")),
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
    if action in ("acknowledge_driver_message","resolve_driver_message"):
        from core.drivers import DriverRegistry
        registry = DriverRegistry(service.db,"synthetic-mutation-pepper-not-a-secret")
        registry.register("mutation-driver","Mutation",actor="fixture")
        service = StateService(service.db,actor="driver:mutation-driver",driver_id="mutation-driver",project_read_trusted=True)
        sent = service.driver_inbox.send_driver_message("fixture","fixture","",
                    target_driver_id="mutation-driver",project_id="mutation-target")
        args["delivery_id"] = sent["deliveries"][0]["delivery_id"]
        if action == "resolve_driver_message":
            service.driver_inbox.acknowledge_driver_message(args["delivery_id"],1,"prepare")
            args["expected_version"] = 2
    try:
        execute(service, action, args, allow_write=True)
    except Exception:
        pass    # the action may legitimately refuse; the side effect must not happen anyway
    assert not service.store.is_project_public("mutation-target"), (
        f"performing write action '{action}' opened the project")

