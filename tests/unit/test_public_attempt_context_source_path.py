"""The anonymous attempt reads do not name a host path.

An attempt pins a host launch descriptor before dispatch
(`StateAttempts.pin_host_contract`). Its `source_repo` is an absolute path on THIS
host (`/home/<user>/.AItelier/projects/<dir>`), and it lives in the attempt's
`context.host_contract`. The attempt-bearing public reads return that context, and
`get_node` and `list_attempts` embed attempts at unknown depth, so the anonymous
answer blanks `source_repo` wherever a `host_contract` appears. Every other field
of the answer is unchanged.

The seeded attempt is a real `reserved` attempt, and its descriptor is pinned
through the product's own `pin_host_contract` against the bound source, so the
recorded value is exactly what production writes.

Real store, real `execute` handler; the caller's trust is the only thing that
changes between the two arms, as in `test_public_catalog_source_path.py`.
"""
from __future__ import annotations

import re

import pytest

from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService

PID = "demo-project"
NODE = "a"
REPO_PATH = "/home/somebody/.AItelier/projects/demo-repo"
COMMON_DIR = "/home/somebody/.AItelier/projects/demo-repo/.git"
DESCRIPTOR = {"source_repo": REPO_PATH, "seed_file": "plan.md", "output_step": "work",
              "scheduler_owned": True, "repo_mode": "code"}

# The same host-path shape the anonymous catalog must never carry to a caller.
HOST_PATH = re.compile(r"(^|\s)/(home|Users|srv|opt|app|var)/")


def _strings(value):
    """Every string value in a JSON-like answer, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _host_contracts(value):
    """Every `host_contract` mapping in a JSON-like answer, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "host_contract" and isinstance(item, dict):
                yield item
            yield from _host_contracts(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _host_contracts(item)


def _null_known_leaves(value):
    """Set, in place, the two leaves the anonymous projection withholds, found by
    an explicit walk over the keys this fixture carries: a `host_contract`'s
    `source_repo` and any `context_hash`. It is deliberately NOT the product's
    walker, so the expected side of the equality is independent of it."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "host_contract" and isinstance(item, dict):
                item["source_repo"] = None
            if key == "context_hash":
                value[key] = None
            _null_known_leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _null_known_leaves(item)


def _leaves(value, path=()):
    """(path, leaf) for every scalar leaf of a JSON-like answer."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, path + (key,))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _leaves(item, path + (index,))
    else:
        yield path, value


def _differing_leaf_names(a, b):
    """The final key names of every leaf whose value differs between two answers
    of the same shape. Lists are compared by position, so an added or dropped
    row shows up as a difference too."""
    left, right = dict(_leaves(a)), dict(_leaves(b))
    assert left.keys() == right.keys(), "answers must have the same shape"
    return {path[-1] for path in left if left[path] != right[path] and isinstance(path[-1], str)}


def _args(action, aid):
    return {
        "get_attempt": {"attempt_id": aid},
        "attempt_detail": {"attempt_id": aid},
        "list_attempts": {"project_id": PID, "node_key": NODE, "limit": 100},
        "get_node": {"project_id": PID, "node_key": NODE},
    }[action]


def _seeded(tmp_path):
    """A public project with one reserved attempt whose host contract is pinned."""
    svc = StateService(StateDatabase(str(tmp_path / "state.sqlite")),
                       actor="attempt-source-path", project_read_trusted=True)
    svc.create_project(PID, "Demo project")
    svc.store.add_nodes(PID, [{"key": NODE, "goal": "A", "acceptance": [
        {"id": "c", "kind": "test", "description": "d"}]}])
    svc.portfolio.bind_source(PID, REPO_PATH, COMMON_DIR, 0)
    svc.open_project(PID)
    attempt = svc.attempts.reserve(PID, NODE, 1, "feature", "request-1")
    pinned = svc.attempts.pin_host_contract(attempt["attempt_id"], dict(DESCRIPTOR))
    assert pinned["context"]["host_contract"] == DESCRIPTOR, "fixture must really pin the descriptor"
    return svc, attempt["attempt_id"]


def _answer(service, trusted, action, aid):
    service.project_read_trusted = trusted
    return execute(service, action, _args(action, aid))


@pytest.mark.parametrize("action", ["get_attempt", "attempt_detail", "list_attempts", "get_node"])
def test_anonymous_attempt_read_names_no_host_path(tmp_path, action):
    service, aid = _seeded(tmp_path)
    answer = _answer(service, False, action, aid)

    # (a) No string anywhere in the anonymous answer is a host path.
    leaked = [s for s in _strings(answer) if HOST_PATH.search(s)]
    assert leaked == [], f"anonymous {action} names a host path: {leaked}"

    # (b) The answer is not empty: the attempt is still there, reserved.
    assert aid in set(_strings(answer)), f"anonymous {action} lost the attempt id"
    assert "reserved" in set(_strings(answer)), f"anonymous {action} lost the attempt status"

    # (c) The contract is kept with every field, and only source_repo is blanked.
    contracts = list(_host_contracts(answer))
    assert len(contracts) == 1, f"anonymous {action} carries {len(contracts)} host contracts"
    contract = contracts[0]
    assert set(contract) == set(DESCRIPTOR), f"anonymous {action} changed the contract fields"
    assert "source_repo" in contract and contract["source_repo"] is None
    assert {k: v for k, v in contract.items() if k != "source_repo"} == \
        {k: v for k, v in DESCRIPTOR.items() if k != "source_repo"}


@pytest.mark.parametrize("action", ["get_attempt", "attempt_detail", "list_attempts", "get_node"])
def test_anonymous_answer_differs_from_trusted_only_in_source_repo(tmp_path, action):
    service, aid = _seeded(tmp_path)
    trusted = _answer(service, True, action, aid)
    anonymous = _answer(service, False, action, aid)

    # The anonymous answer equals the trusted one with source_repo blanked AND
    # the attempt's context digest nulled, and nothing else moved. The expected
    # side is built here by an explicit closure over the digest/path keys this
    # fixture is known to carry, NOT by the product's walker: the witness must
    # not degenerate into "whatever the walker does".
    expected = _answer(service, True, action, aid)
    _null_known_leaves(expected)
    assert anonymous == expected
    assert anonymous != trusted, "the fixture must carry a path for the anonymous read to remove"
    # And the only leaves that differ between the trusted and anonymous answers
    # are exactly the two named ones: the host path and the attempt digest.
    assert _differing_leaf_names(trusted, anonymous) == {"source_repo", "context_hash"}


@pytest.mark.parametrize("action", ["get_attempt", "attempt_detail", "list_attempts", "get_node"])
def test_trusted_attempt_read_keeps_the_recorded_source_repo(tmp_path, action):
    service, aid = _seeded(tmp_path)
    answer = _answer(service, True, action, aid)

    # The control: a trusted writer sees the recorded descriptor verbatim.
    contracts = list(_host_contracts(answer))
    assert len(contracts) == 1, f"trusted {action} carries {len(contracts)} host contracts"
    assert contracts[0] == DESCRIPTOR
    assert any(HOST_PATH.search(s) for s in _strings(answer)), \
        f"trusted {action} must still carry the path (the control)"


def test_host_path_matcher_has_a_positive_control():
    # The scan must be able to find the leak it guards against.
    assert HOST_PATH.search(REPO_PATH)
    assert HOST_PATH.search(f"x {COMMON_DIR}")
    assert not HOST_PATH.search("demo-repo")
