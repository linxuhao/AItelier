"""The todo CLI, driven through its real entry point against real git repositories.

Every test spawns `python -m core.todo_cli`, so each call is a fresh process: that
is how the DSH tools will call it, and it is what makes "the ledger episode
survives" a statement about the plan FILE rather than about live objects.

No case here needs a file to be absent or wrong before the step runs: a step that
modifies a file is judged by the delta against what the file held when the step
began.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from core.gate_deferral import ABSENCE_TERMINAL, absent_terminal_names_no_failure
from tests.unit.test_todo_runtime import process_is_stopped

REPO_ROOT = Path(__file__).resolve().parents[2]
WAIT, CEILING = 300, 1000          # ledger constants for these tests (seconds)
T0 = 1_000_000.0
_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
#: Absent without a missing binary: the process exists but outlives its timeout.
SLOW = [sys.executable, "-c", "import time; time.sleep(30)"]
PASS_ARGV = [sys.executable, "-c", "pass"]


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env={**os.environ, **_GIT_ENV})
    assert done.returncode == 0, f"git {args}: {done.stderr}"
    return done.stdout.strip()


def commit_all(repo: Path, message: str = "work") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "ws"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "app.py").write_text("x = 1\n")
    (path / "other.py").write_text("o = 1\n")
    commit_all(path, "base")
    return path


@pytest.fixture
def base(repo) -> str:
    return git(repo, "rev-parse", "HEAD")


class Cli:
    def __init__(self, store: Path, tmp_path: Path):
        self.store, self.tmp_path, self.n = store, tmp_path, 0

    def run(self, *argv: str, now: float | None = T0, store: bool = True):
        extra = ["--store", str(self.store)] if store else []
        if now is not None:
            extra += ["--now", str(now)]
        done = subprocess.run([sys.executable, "-m", "core.todo_cli", *argv, *extra], cwd=REPO_ROOT,
                              capture_output=True, text=True, env=os.environ.copy(), timeout=120)
        assert done.stdout.strip(), f"no stdout (rc={done.returncode}): {done.stderr}"
        return done.returncode, json.loads(done.stdout)

    def ok(self, *argv: str, **kw) -> dict:
        code, out = self.run(*argv, **kw)
        assert code == 0, out
        return out

    def refused(self, *argv: str, **kw) -> dict:
        code, out = self.run(*argv, **kw)
        assert code == 2 and "refused" in out, (code, out)
        return out

    def create(self, plan: dict, **kw):
        self.n += 1
        path = self.tmp_path / f"plan-{self.n}.json"
        path.write_text(json.dumps(plan))
        return self.run("create", "--plan-json", str(path), **kw)


@pytest.fixture
def cli(tmp_path, monkeypatch) -> Cli:
    monkeypatch.setenv("AITELIER_GATE_DEFERRAL_WAIT_SECONDS", str(WAIT))
    monkeypatch.setenv("AITELIER_GATE_DEFERRAL_EPISODE_MAX_SECONDS", str(CEILING))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("AITELIER_TODO_CLOCK_OVERRIDE", "1")
    return Cli(tmp_path / "store", tmp_path)


EDIT = {"id": "edit", "title": "edit app.py",
        "predicates": [{"predicate": "file_changed", "args": {"path": "app.py", "contains": "y = 2"}}]}


def plan(repo: Path, base: str, items=None, **over) -> dict:
    return {"attempt_id": "att-1", "node_key": "execution.demo", "context_hash": "c" * 64, "base_sha": base,
            "workspace": str(repo), "scope": ["app.py"], "goal": "make app.py define y",
            "items": [EDIT] if items is None else items, **over}


def item(item_id: str, predicates, needs=()) -> dict:
    return {"id": item_id, "title": item_id, "needs": list(needs), "predicates": predicates}


def begin_done(cli: Cli, item_id: str, **kw) -> dict:
    cli.ok("begin", "--attempt", "att-1", "--item", item_id, **kw)
    return cli.ok("done", "--attempt", "att-1", "--item", item_id, **kw)


# -- create -------------------------------------------------------------------

def test_create_refuses_an_unknown_predicate_name_and_writes_nothing(cli, repo, base):
    bad = item("edit", [{"predicate": "worker_says_done", "args": {}}])
    code, out = cli.create(plan(repo, base, [bad]))
    assert code == 2 and out["refused"] == "unknown_predicate", out
    assert "worker_says_done" in out["reason"]
    assert not list(cli.store.glob("*.json")), "a refused plan must leave no file behind"


def plan_directory(directory: Path) -> list[str]:
    """Every entry in the plan directory, by name.

    `*.json` cannot tell "the audit refused before anything was created" from "the lock
    was taken and THEN the audit refused": `PlanStore.lock` opens a sidecar `.lock` next
    to the plan and never unlinks it (it closes the fd, which releases the flock and
    leaves the file), so an empty `<attempt>.lock` is the footprint of the lock having
    been taken. A temp file from `PlanStore.write` (`.att-1.json.<pid>.tmp`) and the plan
    file itself land here too, which is why this lists EVERY name and not a glob.
    """
    return sorted(p.name for p in directory.iterdir())


def refused_by_the_path_audit(repo: Path, base: str, attempt_id: str) -> dict:
    """A plan `check_declared_paths` refuses: one file declared twice, in two spellings.

    This is the audit's own refusal (`impossible_path`), not the plan's: `["./app.py",
    "app.py"]` validates fine and normalises to one path, which is exactly the defect
    the audit exists to catch. It is decided by reading the PLAN, so it refuses whether
    or not the workspace exists - and it is reached through `cmd_create` with
    `--plan-json`, the same call the DSH todo_plan tool makes.
    """
    twice = item("edit", [{"predicate": "files_changed", "args": {"paths": ["app.py", "./app.py"]}}])
    return plan(repo, base, [twice], attempt_id=attempt_id)


def test_a_refused_create_does_not_even_bring_the_plan_directory_into_being(cli, repo, base):
    """The audit refuses BEFORE the lock, so not even the directory the lock would create exists.

    `PlanStore.lock` mkdirs the store root before it opens the sidecar, so a store root
    that exists after a refusal is the lock's doing even when it holds nothing. The
    shipped code raises from `_plan_from_args` (core/todo_cli.py:179, before
    `with store.lock(...)` at :180).
    """
    assert not cli.store.exists(), "the fixture must start without a plan directory, or this proves nothing"
    code, out = cli.create(refused_by_the_path_audit(repo, base, "att-audit"))
    assert code == 2 and out["refused"] == "impossible_path", out
    assert out["path"] == "app.py" and out["predicate"] == "files_changed", out
    assert not cli.store.exists(), \
        f"the refusal happened inside the lock: it left {plan_directory(cli.store)} behind"


def test_a_refused_create_adds_nothing_to_a_live_plan_directory(cli, repo, base):
    """A directory this CLI HAS written through must gain nothing - no `.lock`, no temp, no plan.

    The directory is live on purpose: it already holds a frozen plan and its sidecar
    (see `test_a_successful_create_leaves_the_plan_file_and_its_lock_sidecar_and_nothing_else`),
    so "the exact same contents afterwards" cannot be satisfied by a directory that is
    simply never written. A refusal for a DIFFERENT attempt must add no
    `att-refused.lock`, no `att-refused.json` and no temp file.
    """
    code, accepted = cli.create(plan(repo, base, attempt_id="att-accepted"))
    assert code == 0 and accepted["created"] is True, accepted
    before = plan_directory(cli.store)
    assert before == ["att-accepted.json", "att-accepted.lock"], before

    code, out = cli.create(refused_by_the_path_audit(repo, base, "att-refused"))
    assert code == 2 and out["refused"] == "impossible_path", out
    assert plan_directory(cli.store) == before, (
        "a refused create touched the plan directory: the audit ran after the lock was taken "
        f"({plan_directory(cli.store)} instead of {before})")
    assert not (cli.store / "att-refused.lock").exists(), "the audit refused with the plan lock already taken"
    assert not (cli.store / "att-refused.json").exists(), "a refused plan must leave no plan file"
    assert not [n for n in plan_directory(cli.store) if n.endswith(".tmp")], "a refused plan must leave no temp file"

    # The refused attempt is still free: the refusal wrote nothing that would make a
    # corrected plan look like a DIFFERENT plan for an attempt that already exists.
    corrected = item("edit", [{"predicate": "files_changed", "args": {"paths": ["app.py"]}}])
    code, fixed = cli.create(plan(repo, base, [corrected]))
    assert code == 0 and fixed["created"] is True and fixed["attempt_id"] == "att-1", fixed
    assert plan_directory(cli.store) == sorted(before + ["att-1.json", "att-1.lock"]), plan_directory(cli.store)


def test_a_successful_create_leaves_the_plan_file_and_its_lock_sidecar_and_nothing_else(cli, repo, base):
    """The control for the two refusals above: this directory IS written, and this is what that looks like.

    Exactly two names are left - `att-1.json` and the EMPTY `att-1.lock` sidecar
    `PlanStore.lock` opens next to it - and no temp file. The sidecar is the important
    half: it is what the lock leaves whenever it is taken, so the absence of a `.lock`
    after a refusal is a decision about ORDER, not a name that never appears here.
    """
    code, out = cli.create(plan(repo, base))
    assert code == 0 and out["created"] is True, out
    assert plan_directory(cli.store) == ["att-1.json", "att-1.lock"], plan_directory(cli.store)
    assert (cli.store / "att-1.lock").stat().st_size == 0, "the sidecar is a lock, not a record: it stays empty"
    assert json.loads((cli.store / "att-1.json").read_text())["plan_hash"] == out["plan_hash"]
    assert cli.ok("status", "--attempt", "att-1")["next"] == "edit", "the directory really holds a readable plan"


def test_create_refuses_more_than_twelve_items(cli, repo, base):
    def many(n):
        return [item(f"i{k}", [{"predicate": "files_present", "args": {"pattern": "*.py"}}]) for k in range(n)]
    code, out = cli.create(plan(repo, base, many(13)))
    assert code == 2 and out["refused"] == "too_many_items", out
    code, out = cli.create(plan(repo, base, many(12)))
    assert code == 0, "twelve items is the limit, not over it"
    assert out["items"][-1] == "deliver" and len(out["items"]) == 13


def test_create_refuses_more_than_four_predicates_per_item(cli, repo, base):
    def preds(n):
        return [{"predicate": "files_present", "args": {"pattern": f"{k}*.py"}} for k in range(n)]
    code, out = cli.create(plan(repo, base, [item("edit", preds(5))]))
    assert code == 2 and out["refused"] == "too_many_predicates", out
    assert cli.create(plan(repo, base, [item("edit", preds(4))]))[0] == 0


def test_create_is_idempotent_for_the_same_plan_and_refuses_a_different_one(cli, repo, base):
    code, first = cli.create(plan(repo, base))
    assert code == 0 and first["created"] is True
    reordered = dict(reversed(list(plan(repo, base).items())))      # same plan, different key order
    code, again = cli.create(reordered)
    assert code == 0 and again["created"] is False and again["plan_hash"] == first["plan_hash"]

    code, out = cli.create(plan(repo, base, goal="a different goal"))
    assert code == 2 and out["refused"] == "plan_differs"
    assert out["plan_hash"] == first["plan_hash"]
    assert cli.ok("status", "--attempt", "att-1")["goal"] == "make app.py define y", "the frozen plan must not change"


def test_create_appends_a_deliver_item_that_needs_everything(cli, repo, base):
    second = item("second", [{"predicate": "files_present", "args": {"pattern": "*.py"}}], needs=["edit"])
    code, out = cli.create(plan(repo, base, [EDIT, second]))
    assert code == 0 and out["items"] == ["edit", "second", "deliver"]
    status = cli.ok("status", "--attempt", "att-1")
    deliver = status["items"][-1]
    assert deliver["id"] == "deliver" and deliver["needs"] == ["edit", "second"]
    document = json.loads((cli.store / "att-1.json").read_text())
    stored = document["plan"]["items"][-1]["predicates"]
    assert stored == [{"predicate": "git_delivered", "args": {"base": base}},
                      {"predicate": "changes_within", "args": {"scope": ["app.py"]}}]
    assert document["plan_hash"] == out["plan_hash"] and len(out["plan_hash"]) == 64


def test_create_refuses_a_plan_that_tries_to_define_deliver_or_escape_the_workspace(cli, repo, base):
    sneaky = item("deliver", [{"predicate": "files_present", "args": {"pattern": "*"}}])
    assert cli.create(plan(repo, base, [sneaky]))[1]["refused"] == "invalid_plan"
    escape = item("edit", [{"predicate": "file_changed", "args": {"path": "../../etc/passwd"}}])
    assert cli.create(plan(repo, base, [escape]))[1]["refused"] == "invalid_plan"
    wrong_args = item("edit", [{"predicate": "file_changed", "args": {"file": "app.py"}}])
    assert cli.create(plan(repo, base, [wrong_args]))[1]["refused"] == "invalid_plan"


# -- begin / done ---------------------------------------------------------------

def test_begin_refuses_unmet_needs_and_accepts_them_once_met(cli, repo, base):
    second = item("second", [{"predicate": "files_present", "args": {"pattern": "*.py"}}], needs=["edit"])
    cli.create(plan(repo, base, [EDIT, second]))
    out = cli.refused("begin", "--attempt", "att-1", "--item", "second")
    assert out["refused"] == "unmet_needs" and out["unmet"] == ["edit"]
    cli.ok("begin", "--attempt", "att-1", "--item", "edit")
    (repo / "app.py").write_text("x = 1\ny = 2\n")
    assert cli.ok("done", "--attempt", "att-1", "--item", "edit")["outcome"] == "pass"
    assert cli.ok("begin", "--attempt", "att-1", "--item", "second")["begun"] is True


def test_begin_records_the_session_and_keeps_the_first_pre_state(cli, repo, base):
    cli.create(plan(repo, base))
    first = cli.ok("begin", "--attempt", "att-1", "--item", "edit", "--session", "child-A")
    assert first["first_begin"] is True and first["child_session"] == "child-A"
    (repo / "app.py").write_text("x = 1\ny = 2\n")                     # work done by the first child
    resumed = cli.ok("begin", "--attempt", "att-1", "--item", "edit", "--session", "child-B", "--rebind")
    assert resumed["first_begin"] is False and resumed["pre_state"] == first["pre_state"]
    assert cli.ok("status", "--attempt", "att-1")["child_session"] == "child-B"
    assert cli.ok("done", "--attempt", "att-1", "--item", "edit")["outcome"] == "pass", \
        "a resumed child is judged against the state at the ORIGINAL begin"


def test_a_plan_bound_to_one_session_refuses_another_until_it_is_rebound(cli, repo, base):
    cli.create(plan(repo, base))
    cli.ok("begin", "--attempt", "att-1", "--item", "edit", "--session", "child-A")
    out = cli.refused("begin", "--attempt", "att-1", "--item", "edit", "--session", "child-B")
    assert out["refused"] == "bound_to_other_session" and out["bound_session"] == "child-A"
    assert "child-A" in out["reason"]
    status = cli.ok("status", "--attempt", "att-1")
    assert status["child_session"] == "child-A" and status["rebound_from"] is None, "a refusal changes nothing"

    same = cli.ok("begin", "--attempt", "att-1", "--item", "edit", "--session", "child-A")
    assert same["child_session"] == "child-A" and same["rebound_from"] is None
    assert cli.ok("begin", "--attempt", "att-1", "--item", "edit")["child_session"] == "child-A", "no --session: no change"

    taken = cli.ok("begin", "--attempt", "att-1", "--item", "edit", "--session", "child-B", "--rebind")
    assert taken["child_session"] == "child-B" and taken["rebound_from"] == "child-A"
    document = json.loads((cli.store / "att-1.json").read_text())
    assert document["child_session"] == "child-B" and document["rebound_from"] == "child-A"
    assert cli.refused("begin", "--attempt", "att-1", "--item", "edit", "--session", "child-A")["bound_session"] == "child-B"
    assert cli.refused("begin", "--attempt", "att-1", "--item", "edit", "--rebind")["refused"] == "usage"
    for command in ("status", "handback"):
        assert cli.ok(command, "--attempt", "att-1")["rebound_from"] == "child-A"


def test_done_and_report_check_the_callers_session_but_never_rebind(cli, repo, base):
    cli.create(plan(repo, base))
    cli.ok("begin", "--attempt", "att-1", "--item", "edit", "--session", "child-A")
    for command, extra in (("done", []), ("report", ["--note", "n"])):
        base_args = [command, "--attempt", "att-1", "--item", "edit", *extra]
        out = cli.refused(*base_args, "--session", "child-B")
        assert out["refused"] == "bound_to_other_session" and out["bound_session"] == "child-A", out
        assert cli.ok(*base_args, "--session", "child-A")["item"] == "edit"
        assert cli.ok(*base_args)["item"] == "edit", "a plain call without --session stays as it was"
    after = cli.ok("status", "--attempt", "att-1")
    assert after["child_session"] == "child-A" and after["rebound_from"] is None
    assert after["items"][0]["notes"] == 2, "the refused report recorded nothing"
    assert after["items"][0]["state"] == "fail", "the refused done measured nothing; the allowed ones did"
    assert cli.refused("done", "--attempt", "att-1", "--item", "edit", "--session", "child-B", "--rebind")["refused"] == "usage"


def test_a_first_binding_needs_no_rebind(cli, repo, base):
    cli.create(plan(repo, base))
    out = cli.ok("begin", "--attempt", "att-1", "--item", "edit", "--session", "child-A", "--rebind")
    assert out["child_session"] == "child-A" and out["rebound_from"] is None, "nothing was bound before"


def test_status_and_handback_report_how_long_each_item_may_take_to_measure(cli, repo, base):
    timed = item("timed", [{"predicate": "command_exit", "args": {"argv": PASS_ARGV, "timeout": 1800}},
                           {"predicate": "command_exit", "args": {"argv": PASS_ARGV, "timeout": 7.5}},
                           {"predicate": "files_present", "args": {"pattern": "*.py"}}])
    untimed = item("untimed", [{"predicate": "command_exit", "args": {"argv": PASS_ARGV}}])
    cli.create(plan(repo, base, [timed, untimed]))
    for command in ("status", "handback"):
        out = cli.ok(command, "--attempt", "att-1")
        assert out["timeout_budget_seconds"] == {"timed": 1807.5, "untimed": 0, "deliver": 0}
        assert out["finish_budget_seconds"] == 1807.5


def test_a_terminated_cli_leaves_no_running_command_behind(cli, repo, base, tmp_path):
    import signal
    import time
    pidfile = tmp_path / "child.pid"
    code = ("import subprocess, sys, time; "
            "p = subprocess.Popen(['sleep', '60']); open(sys.argv[1], 'w').write(str(p.pid)); time.sleep(60)")
    gate = item("gate", [{"predicate": "command_exit", "args": {"argv": [sys.executable, "-c", code, str(pidfile)],
                                                                 "timeout": 120}}])
    cli.create(plan(repo, base, [gate]))
    cli.ok("begin", "--attempt", "att-1", "--item", "gate")
    cli_proc = subprocess.Popen([sys.executable, "-m", "core.todo_cli", "done", "--attempt", "att-1", "--item", "gate",
                                 "--store", str(cli.store)], cwd=REPO_ROOT, stdout=subprocess.PIPE, text=True)
    deadline = time.time() + 60
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    grandchild = int(pidfile.read_text())
    cli_proc.send_signal(signal.SIGTERM)
    try:
        assert cli_proc.wait(timeout=30) == 143
    except subprocess.TimeoutExpired:      # the CLI blocked on a command group that was never killed
        cli_proc.kill()
        if not process_is_stopped(grandchild):
            os.kill(grandchild, signal.SIGKILL)
        pytest.fail("the terminated CLI did not exit: it was still waiting on the command's child")
    for _ in range(100):
        if process_is_stopped(grandchild):
            break
        time.sleep(0.05)
    else:
        os.kill(grandchild, signal.SIGKILL)
        pytest.fail("the command's own child survived the CLI being terminated")


def test_done_before_begin_is_refused(cli, repo, base):
    cli.create(plan(repo, base))
    assert cli.refused("done", "--attempt", "att-1", "--item", "edit")["refused"] == "not_begun"
    assert cli.refused("done", "--attempt", "att-1", "--item", "nope")["refused"] == "unknown_item"
    assert cli.refused("status", "--attempt", "missing")["refused"] == "no_such_plan"


def test_done_passes_for_an_edited_file(cli, repo, base):
    cli.create(plan(repo, base))
    cli.ok("begin", "--attempt", "att-1", "--item", "edit")
    (repo / "app.py").write_text("x = 1\ny = 2\n")
    out = cli.ok("done", "--attempt", "att-1", "--item", "edit")
    assert out["outcome"] == "pass" and out["results"][0]["predicate"] == "file_changed"
    assert out["next"] == "deliver" and "edit" not in out["brief"].split("Remaining")[1]


def test_done_passes_when_the_file_already_held_the_marker_and_was_edited_again(cli, repo, base):
    """No redness demanded: a file that is already right may still be what the step edits."""
    (repo / "app.py").write_text("x = 1\ny = 2\n")
    commit_all(repo, "already has y")
    cli.create(plan(repo, git(repo, "rev-parse", "HEAD")))
    cli.ok("begin", "--attempt", "att-1", "--item", "edit")
    (repo / "app.py").write_text("x = 1\ny = 2\nz = 3\n")
    assert cli.ok("done", "--attempt", "att-1", "--item", "edit")["outcome"] == "pass"


def test_done_fails_for_an_untouched_file_even_when_the_worker_says_it_is_done(cli, repo, base):
    cli.create(plan(repo, base))
    cli.ok("begin", "--attempt", "att-1", "--item", "edit", "--session", "child-A")
    cli.ok("report", "--attempt", "att-1", "--item", "edit", "--note", "edited app.py, done, y = 2 added")
    out = cli.ok("done", "--attempt", "att-1", "--item", "edit")
    assert out["outcome"] == "fail", "the worker's report is not an input to the verdict"
    assert "byte-identical" in out["results"][0]["detail"]
    assert out["next"] == "edit", "a failing item stays open"


def test_done_fails_for_an_edit_lacking_the_contains_marker(cli, repo, base):
    cli.create(plan(repo, base))
    cli.ok("begin", "--attempt", "att-1", "--item", "edit")
    (repo / "app.py").write_text("x = 1  # touched\n")
    out = cli.ok("done", "--attempt", "att-1", "--item", "edit")
    assert out["outcome"] == "fail" and "y = 2" in out["results"][0]["detail"]


# -- deliver: changes_within and git_delivered --------------------------------------

def reach_deliver(cli, repo, base, **plan_over) -> None:
    cli.create(plan(repo, base, **plan_over))
    cli.ok("begin", "--attempt", "att-1", "--item", "edit")
    (repo / "app.py").write_text("x = 1\ny = 2\n")
    assert cli.ok("done", "--attempt", "att-1", "--item", "edit")["outcome"] == "pass"


def deliver_results(cli) -> dict:
    cli.ok("begin", "--attempt", "att-1", "--item", "deliver")
    out = cli.ok("done", "--attempt", "att-1", "--item", "deliver")
    return {r["predicate"]: r for r in out["results"]} | {"_outcome": out["outcome"]}


def test_deliver_fails_while_the_tree_is_dirty_then_passes_once_committed(cli, repo, base):
    reach_deliver(cli, repo, base)
    got = deliver_results(cli)
    assert got["_outcome"] == "fail"
    assert got["git_delivered"]["outcome"] == "fail" and "not clean" in got["git_delivered"]["detail"]
    commit_all(repo)
    got = deliver_results(cli)
    assert got["_outcome"] == "pass", got


def test_deliver_fails_when_head_is_still_the_base(cli, repo, base):
    quiet = item("look", [{"predicate": "files_present", "args": {"pattern": "app.py"}}])
    cli.create(plan(repo, base, [quiet]))
    begin_done(cli, "look")
    got = deliver_results(cli)
    assert got["_outcome"] == "fail"
    assert got["git_delivered"]["outcome"] == "fail" and "still the base" in got["git_delivered"]["detail"]


def test_deliver_fails_when_a_file_outside_scope_changed(cli, repo, base):
    reach_deliver(cli, repo, base)
    (repo / "other.py").write_text("o = 2\n")                           # outside scope ["app.py"]
    commit_all(repo)
    got = deliver_results(cli)
    assert got["_outcome"] == "fail"
    assert got["git_delivered"]["outcome"] == "pass", "the tree is clean and HEAD moved"
    assert got["changes_within"]["outcome"] == "fail" and "other.py" in got["changes_within"]["detail"]


# -- absence: the ledger episode ---------------------------------------------------

def test_a_missing_binary_is_absent_not_a_failure(cli, repo, base):
    gate = item("gate", [{"predicate": "command_exit", "args": {"argv": ["definitely-not-a-real-binary-xyz"]}}])
    cli.create(plan(repo, base, [gate]))
    out = begin_done(cli, "gate")
    assert out["outcome"] == "absent" and out["unmeasurable"] is False
    assert "not installed" in out["results"][0]["detail"]
    assert out["episode"]["gate"] == "command_exit"


def slow_gate_plan(repo, base) -> dict:
    gate = item("gate", [{"predicate": "command_exit", "args": {"argv": SLOW, "timeout": 0.3}}])
    docs = item("docs", [{"predicate": "files_present", "args": {"pattern": "*.py"}}])
    return plan(repo, base, [gate, docs])


def counting_gate_plan(repo, base, counter: Path) -> dict:
    """A gate that leaves a line in `counter` each time it is really executed, then outlives its timeout."""
    code = "import sys, time; open(sys.argv[1], 'a').write('ran\\n'); time.sleep(30)"
    gate = item("gate", [{"predicate": "command_exit",
                          "args": {"argv": [sys.executable, "-c", code, str(counter)], "timeout": 0.5}}])
    return plan(repo, base, [gate])


def executions(counter: Path) -> int:
    return len(counter.read_text().splitlines()) if counter.exists() else 0


def test_a_second_done_inside_the_wait_does_not_restart_the_episode(cli, repo, base, tmp_path):
    """The wait is enforced: inside it the gate is not asked again, so nothing restarts and nothing is counted.

    Each call below is a separate process, so started_at surviving proves the
    ledger really is rehydrated from the plan file.
    """
    counter = tmp_path / "ran.txt"
    cli.create(counting_gate_plan(repo, base, counter))
    first = begin_done(cli, "gate", now=T0)
    assert executions(counter) == 1
    assert first["episode"]["started_at"] == T0 and first["episode"]["gate_runs"] == 1

    again = cli.ok("done", "--attempt", "att-1", "--item", "gate", now=T0 + 100)
    assert executions(counter) == 1, "inside the wait the stored absence is returned, the gate is not run again"
    assert again["outcome"] == "absent" and again["results"] == first["results"]
    episode = again["episode"]
    assert episode["started_at"] == T0, "the episode start never moves"
    assert episode["last_absence_at"] == T0, "looking at the same report again must not restart the wait"
    assert episode["gate_runs"] == 1, "a re-read report is not another gate run"
    assert episode["absences"] == 2
    assert episode["hold_remaining"] == pytest.approx(WAIT - 100)
    assert cli.ok("status", "--attempt", "att-1", now=T0 + 100)["deferred"] == ["gate"]


def test_after_the_wait_the_gate_is_asked_again_and_that_is_one_more_run_and_a_new_wait(cli, repo, base, tmp_path):
    counter = tmp_path / "ran.txt"
    cli.create(counting_gate_plan(repo, base, counter))
    begin_done(cli, "gate", now=T0)
    later = cli.ok("done", "--attempt", "att-1", "--item", "gate", now=T0 + WAIT + 1)
    assert executions(counter) == 2, "past the wait the gate really runs again"
    episode = later["episode"]
    assert later["outcome"] == "absent" and later["unmeasurable"] is False
    assert episode["started_at"] == T0, "the ceiling still measures from the first sighting"
    assert episode["gate_runs"] == 2 and episode["last_absence_at"] == T0 + WAIT + 1
    assert episode["hold_remaining"] == pytest.approx(WAIT)


def test_past_the_ceiling_the_item_is_unmeasurable_skipped_and_named(cli, repo, base):
    cli.create(slow_gate_plan(repo, base))
    begin_done(cli, "gate", now=T0)
    cli.ok("begin", "--attempt", "att-1", "--item", "docs")
    late = cli.ok("done", "--attempt", "att-1", "--item", "gate", now=T0 + CEILING + 1)
    assert late["outcome"] == "absent" and late["unmeasurable"] is True
    assert ABSENCE_TERMINAL in late["reason"] and absent_terminal_names_no_failure(late["reason"])

    status = cli.ok("status", "--attempt", "att-1", now=T0 + CEILING + 1)
    assert status["unmeasurable"] == [{"id": "gate", "reason": late["reason"]}]
    assert status["deferred"] == [], "an unmeasurable item is no longer deferred"
    assert status["next"] == "docs", "next_item must skip the unmeasurable item"
    assert "Unmeasurable: gate" in status["brief"] and late["reason"] in status["brief"]
    assert len(status["brief"]) <= 600
    states = {i["id"]: i["state"] for i in status["items"]}
    assert states["gate"] == "unmeasurable"

    refusal = cli.refused("finish", "--attempt", "att-1", "--out", str(cli.tmp_path / "out"), now=T0 + CEILING + 2)
    assert refusal["refused"] == "unfinished"
    assert "gate" in {f["item"] for f in refusal["failing"]}


def test_a_failing_predicate_beside_an_absent_one_fails_and_is_never_unmeasurable(cli, repo, base):
    mixed = item("mixed", [{"predicate": "file_changed", "args": {"path": "app.py"}},
                           {"predicate": "command_exit", "args": {"argv": SLOW, "timeout": 0.3}}])
    cli.create(plan(repo, base, [mixed]))
    cli.ok("begin", "--attempt", "att-1", "--item", "mixed", now=T0)
    for moment in (T0, T0 + CEILING + 1):
        out = cli.ok("done", "--attempt", "att-1", "--item", "mixed", now=moment)
        assert out["outcome"] == "fail" and out["unmeasurable"] is False and "episode" not in out
    status = cli.ok("status", "--attempt", "att-1", now=T0 + CEILING + 1)
    assert status["unmeasurable"] == [] and status["next"] == "mixed" and status["episodes"] == {}


# -- the harness refuses what it should not do -------------------------------------------

def test_the_clock_override_is_refused_unless_the_test_environment_allows_it(cli, repo, base, monkeypatch):
    cli.create(plan(repo, base))
    monkeypatch.delenv("AITELIER_TODO_CLOCK_OVERRIDE")
    assert cli.refused("status", "--attempt", "att-1", now=T0)["refused"] == "clock_override_disabled"
    assert cli.ok("status", "--attempt", "att-1", now=None)["attempt_id"] == "att-1"
    monkeypatch.setenv("AITELIER_TODO_CLOCK_OVERRIDE", "0")
    assert cli.refused("status", "--attempt", "att-1", now=T0)["refused"] == "clock_override_disabled"


def test_a_damaged_plan_file_is_a_refusal_not_a_traceback(cli, repo, base):
    cli.create(plan(repo, base))
    path = cli.store / "att-1.json"
    original = path.read_text()
    for damaged in ("{not json", "[]", "{}", json.dumps({**json.loads(original), "items": None})):
        path.write_text(damaged)
        for command in (["status"], ["handback"], ["done", "--item", "edit"]):
            out = cli.refused(command[0], "--attempt", "att-1", *command[1:])
            assert out["refused"] == "corrupt_plan", (damaged, out)
    path.write_text(original)
    assert cli.ok("status", "--attempt", "att-1")["attempt_id"] == "att-1"


def test_anything_unforeseen_is_reported_as_an_internal_refusal(tmp_path, capsys, monkeypatch):
    from core import todo_cli

    def boom(args, store, now):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(todo_cli.COMMANDS, "status", boom)
    assert todo_cli.main(["status", "--attempt", "x", "--store", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out) == {"refused": "internal", "reason": "RuntimeError: kaboom"}


@pytest.mark.parametrize("predicate,args", [
    ("command_exit", {"argv": "true"}),
    ("command_exit", {"argv": []}),
    ("command_exit", {"argv": ["true", 1]}),
    ("report_json", {"path": "r.json", "require_keys": "passed"}),
    ("files_present", {"pattern": 5}),
])
def test_create_refuses_wrongly_typed_predicate_arguments(cli, repo, base, predicate, args):
    bad = item("edit", [{"predicate": predicate, "args": args}])
    code, out = cli.create(plan(repo, base, [bad]))
    assert code == 2 and out["refused"] == "invalid_plan", out
    assert cli.create(plan(repo, base, scope="app.py"))[1]["refused"] == "invalid_plan"


def test_finish_refuses_a_head_that_was_reset_behind_the_base(cli, repo, base, tmp_path):
    (repo / "app.py").write_text("x = 1\ny = 2\n")
    second = commit_all(repo, "second")
    look = item("look", [{"predicate": "files_present", "args": {"pattern": "app.py"}}])
    cli.create(plan(repo, second, [look]))
    begin_done(cli, "look")
    git(repo, "reset", "-q", "--hard", base)                              # clean tree, HEAD an ancestor of the base
    got = deliver_results(cli)
    assert got["_outcome"] == "fail" and "not a descendant" in got["git_delivered"]["detail"]
    refusal = cli.refused("finish", "--attempt", "att-1", "--out", str(tmp_path / "out"))
    assert refusal["refused"] == "unfinished" and "deliver" in {f["item"] for f in refusal["failing"]}
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


# -- status / handback / report / list ------------------------------------------------

def test_status_and_handback_carry_the_contracted_keys_and_a_bounded_brief(cli, repo, base):
    cli.create(plan(repo, base, goal="g" * 3000))
    cli.ok("begin", "--attempt", "att-1", "--item", "edit", "--session", "child-A")
    keys = {"brief", "goal", "next", "done", "remaining", "deferred", "unmeasurable", "child_session", "items"}
    for command in ("status", "handback"):
        out = cli.ok(command, "--attempt", "att-1")
        assert keys <= set(out), (command, keys - set(out))
        assert len(out["brief"]) <= 600 and out["next"] == "edit" and out["remaining"] == ["edit", "deliver"]
        assert out["child_session"] == "child-A"
    handback = cli.ok("handback", "--attempt", "att-1")
    assert handback["reason"] == "todo_unfinished" and "edit" in handback["last_observations"]
    for command, extra in (("begin", ["--item", "edit"]), ("report", ["--item", "edit", "--note", "n"])):
        assert len(cli.ok(command, "--attempt", "att-1", *extra)["brief"]) <= 600


def test_report_records_a_note_and_changes_no_state(cli, repo, base):
    cli.create(plan(repo, base))
    before = cli.ok("status", "--attempt", "att-1")
    cli.ok("report", "--attempt", "att-1", "--item", "edit", "--note", "tried approach B")
    after = cli.ok("status", "--attempt", "att-1")
    assert after["items"][0]["notes"] == 1 and after["items"][0]["last_note"] == "tried approach B"
    assert [i["state"] for i in after["items"]] == [i["state"] for i in before["items"]] == ["open", "open"]
    assert after["done"] == before["done"] and after["next"] == before["next"]


def test_concurrent_writers_do_not_lose_updates(cli, repo, base):
    cli.create(plan(repo, base))
    procs = [subprocess.Popen([sys.executable, "-m", "core.todo_cli", "report", "--attempt", "att-1",
                               "--item", "edit", "--note", f"n{k}", "--store", str(cli.store), "--now", str(T0 + k)],
                              cwd=REPO_ROOT, stdout=subprocess.PIPE, text=True) for k in range(8)]
    assert [p.wait(timeout=120) for p in procs] == [0] * 8
    notes = json.loads((cli.store / "att-1.json").read_text())["items"]["edit"]["notes"]
    assert sorted(n["text"] for n in notes) == sorted(f"n{k}" for k in range(8))
    assert sorted(p.name for p in cli.store.iterdir()) == ["att-1.json", "att-1.lock"], "no temp files left behind"


def test_list_names_every_plan_and_the_default_store_comes_from_the_environment(cli, repo, base, tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_DSH_STATE_DIR", str(tmp_path / "state"))
    path = tmp_path / "p.json"
    path.write_text(json.dumps(plan(repo, base)))
    code, out = cli.run("create", "--plan-json", str(path), store=False)
    assert code == 0 and (tmp_path / "state" / "todo" / "att-1.json").is_file()
    listed = cli.ok("list", store=False)["plans"]
    assert [(p["attempt_id"], p["next"], p["remaining"]) for p in listed] == [("att-1", "edit", ["edit", "deliver"])]
    monkeypatch.delenv("AITELIER_DSH_STATE_DIR")
    assert cli.refused("list", store=False)["refused"] == "usage"


# -- finish ----------------------------------------------------------------------------

def test_finish_refuses_while_any_item_fails(cli, repo, base, tmp_path):
    cli.create(plan(repo, base))
    cli.ok("begin", "--attempt", "att-1", "--item", "edit")
    out_dir = tmp_path / "out"
    refusal = cli.refused("finish", "--attempt", "att-1", "--out", str(out_dir))
    assert refusal["refused"] == "unfinished"
    failing = {f["item"]: f for f in refusal["failing"]}
    assert failing["edit"]["outcome"] == "fail" and "byte-identical" in failing["edit"]["reason"]
    assert "deliver" in failing, "deliver was never begun"
    assert not out_dir.exists() or not list(out_dir.iterdir()), "no bundle or report without a full pass"


def test_finish_writes_a_bundle_git_can_verify_and_a_report_state_accepts(cli, repo, base, tmp_path):
    from core.state_graph import StateConflict  # noqa: F401  (import proves the validator's module loads)
    from core.state_report_integrity import _structured_terminal, validate_external_semantics

    reach_deliver(cli, repo, base)
    commit_all(repo)
    assert deliver_results(cli)["_outcome"] == "pass"
    out_dir = tmp_path / "out"
    done = cli.ok("finish", "--attempt", "att-1", "--out", str(out_dir))

    head = git(repo, "rev-parse", "HEAD")
    bundle = Path(done["git_bundle"]["path"])
    assert bundle == (out_dir / "att-1.bundle").resolve() and bundle.is_file()
    assert done["git_bundle"]["sha256"] == hashlib.sha256(bundle.read_bytes()).hexdigest()

    # Verified from an EMPTY repository: the bundle needs nothing from the original.
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q", "--bare")
    git(empty, "bundle", "verify", str(bundle))
    assert "prerequisite" not in subprocess.run(["git", "-C", str(empty), "bundle", "verify", str(bundle)],
                                                capture_output=True, text=True).stdout.lower()
    git(empty, "fetch", "-q", str(bundle), head)
    assert git(empty, "rev-parse", "FETCH_HEAD") == head

    report_path = Path(done["report"])
    assert report_path == (out_dir / "att-1-candidate.json").resolve()
    report_bytes = report_path.read_bytes()
    assert done["report_sha256"] == hashlib.sha256(report_bytes).hexdigest()
    report = json.loads(report_bytes)
    assert report["status"] == "candidate" and report["settled"] is True and report["usable"] is True
    assert report["artifact"] == head and len(head) == 40 and report["artifact_kind"] == "git-sha1"
    assert report["git_bundle"] == done["git_bundle"] and report["quiescent"] is True
    assert report["plan_hash"] == done["plan_hash"]
    assert [o["item"] for o in report["observations"]] == ["edit", "deliver"]
    assert all(o["outcome"] == "pass" for o in report["observations"])
    validate_external_semantics(report_bytes, "candidate", head)
    _structured_terminal(report_bytes)


# -- the predicate library a plan was frozen under ------------------------------------------

_MISSING = object()


def rewrite_version(cli: Cli, value) -> dict:
    """Set (or, with _MISSING, delete) the stored library_version; returns the document written."""
    path = cli.store / "att-1.json"
    document = json.loads(path.read_text())
    if value is _MISSING:
        document.pop("library_version", None)
    else:
        document["library_version"] = value
    path.write_text(json.dumps(document))
    return document


def test_a_plan_frozen_under_another_library_is_refused_naming_both_versions_and_the_plan(cli, repo, base):
    """Every command that rehydrates the document hits the same guard, before it measures anything."""
    cli.create(plan(repo, base))
    document = rewrite_version(cli, "1")
    out_dir = cli.tmp_path / "out"
    for argv in (["status"], ["handback"], ["begin", "--item", "edit"], ["done", "--item", "edit"],
                 ["report", "--item", "edit", "--note", "n"], ["finish", "--out", str(out_dir)]):
        out = cli.refused(argv[0], "--attempt", "att-1", *argv[1:])
        assert out["refused"] == "library_mismatch", (argv, out)
        assert out["plan_library_version"] == "1" and out["running_library_version"] == "2", (argv, out)
        assert out["attempt_id"] == "att-1" and out["node_key"] == "execution.demo", (argv, out)
        assert "v1" in out["reason"] and "v2" in out["reason"] and "att-1" in out["reason"], (argv, out)
    assert json.loads((cli.store / "att-1.json").read_text()) == document, "a refusal rewrites nothing"
    assert sorted(p.name for p in cli.store.iterdir()) == ["att-1.json", "att-1.lock"], \
        "no temp file, and the sidecar lock file only - never a plan left half-written"
    assert not out_dir.exists() or not list(out_dir.iterdir())
    listed = cli.ok("list")["plans"]
    assert [p["attempt_id"] for p in listed] == ["att-1"], listed
    assert "v1" in listed[0]["error"] and "v2" in listed[0]["error"], listed[0]


def test_the_version_is_named_before_a_predicate_this_library_lacks(cli, repo, base):
    """Loaded in the wrong order, a v1 plan would be reported as a broken binding, not a wrong library."""
    cli.create(plan(repo, base))
    path = cli.store / "att-1.json"
    document = json.loads(path.read_text())
    document["library_version"] = "1"
    document["plan"]["items"][0]["predicates"] = [{"predicate": "file_rewritten", "args": {}}]
    path.write_text(json.dumps(document))
    out = cli.refused("status", "--attempt", "att-1")
    assert out["refused"] == "library_mismatch", f"the library is named first, not the missing predicate: {out}"
    assert "file_rewritten" not in out["reason"]


def test_a_document_with_no_library_version_is_refused_as_unmeasured(cli, repo, base):
    cli.create(plan(repo, base))
    rewrite_version(cli, _MISSING)
    out = cli.refused("status", "--attempt", "att-1")
    assert out["refused"] == "library_unmeasured", out
    assert out["plan_library_version"] is None and out["running_library_version"] == "2", out
    assert "library_version" in out["reason"] and "att-1" in out["reason"] and "v2" in out["reason"], out
    # A version of the wrong TYPE measured nothing either: it is unmeasured, not "equal-looking".
    rewrite_version(cli, 2)
    out = cli.refused("done", "--attempt", "att-1", "--item", "edit")
    assert out["refused"] == "library_unmeasured" and out["plan_library_version"] == 2, out


def test_the_library_refusal_is_a_json_error_and_not_a_traceback(cli, repo, base):
    cli.create(plan(repo, base))
    rewrite_version(cli, "1")
    done = subprocess.run([sys.executable, "-m", "core.todo_cli", "status", "--attempt", "att-1",
                           "--store", str(cli.store), "--now", str(T0)], cwd=REPO_ROOT,
                          capture_output=True, text=True, env=os.environ.copy(), timeout=120)
    assert done.returncode == 2 and "Traceback" not in done.stderr, done.stderr
    assert json.loads(done.stdout)["refused"] == "library_mismatch", done.stdout


def test_a_refusal_leaves_the_store_usable_and_the_running_version_working_end_to_end(cli, repo, base):
    cli.create(plan(repo, base))
    path = cli.store / "att-1.json"
    good = path.read_text()
    assert json.loads(good)["library_version"] == "2", "what the running library writes is what loads"
    rewrite_version(cli, "9")
    assert cli.refused("status", "--attempt", "att-1")["refused"] == "library_mismatch"
    path.write_text(good)                                     # the document exactly as the running version wrote it
    assert cli.ok("status", "--attempt", "att-1")["next"] == "edit", "the lock was released; the plan opens again"
    cli.ok("begin", "--attempt", "att-1", "--item", "edit")
    (repo / "app.py").write_text("x = 1\ny = 2\n")
    assert cli.ok("done", "--attempt", "att-1", "--item", "edit")["outcome"] == "pass", \
        "a plan at the running version keeps working end to end after a refusal"


def test_a_mismatched_plan_refuses_before_the_gate_command_is_spawned(cli, repo, base, tmp_path):
    counter = tmp_path / "ran.txt"
    cli.create(counting_gate_plan(repo, base, counter))
    cli.ok("begin", "--attempt", "att-1", "--item", "gate")    # a pre-state exists: `done` really would evaluate
    assert executions(counter) == 0
    rewrite_version(cli, "1")
    out = cli.refused("done", "--attempt", "att-1", "--item", "gate", now=T0)
    assert out["refused"] == "library_mismatch"
    assert executions(counter) == 0, "the gate command was never spawned: nothing was measured"
    rewrite_version(cli, "2")                                 # the positive control: at its own version it does run
    assert cli.ok("done", "--attempt", "att-1", "--item", "gate", now=T0)["outcome"] == "absent"
    assert executions(counter) == 1


def test_the_refusal_precedes_any_predicate_evaluation(tmp_path):
    """Both libraries define `probe`, so the WRONG library could have evaluated it: `ran` says it did not."""
    from core.todo_plan import Refusal, TodoPlan, load_run, new_document
    from core.todo_runtime import PredicateLibrary, PredicateResult

    ran: list[str] = []

    def library_at(version: str) -> PredicateLibrary:
        library = PredicateLibrary(version=version)

        @library.define("probe")
        def probe(*, workspace, pre_state, label: str) -> PredicateResult:
            ran.append(f"{label}@{version}")
            return PredicateResult("probe", "pass", "evaluated")
        return library

    frozen, running = library_at("2"), library_at("3")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    plan_json = {"attempt_id": "att-lib", "node_key": "execution.lib", "context_hash": "c" * 64,
                 "base_sha": "a" * 40, "workspace": str(workspace), "scope": ["app.py"], "goal": "probe it",
                 "items": [{"id": "probe", "title": "probe", "predicates": [
                     {"predicate": "probe", "args": {"label": "first"}}]}]}
    plan = TodoPlan.from_json(plan_json, frozen)
    document = new_document(plan, frozen, T0)
    assert document["library_version"] == "2", "the document records the library that froze it"

    with pytest.raises(Refusal) as refused:
        load_run(document, plan, running, lambda: T0)
    assert refused.value.code == "library_mismatch", refused.value
    assert "v2" in refused.value.reason and "v3" in refused.value.reason and "att-lib" in refused.value.reason
    assert ran == [], "the guard refused before the library was asked to evaluate the bound name"

    # Positive control: judged by the library it recorded, the very same document DOES measure.
    run = load_run(document, plan, frozen, lambda: T0)
    run.begin("probe")
    assert run.observe("probe").outcome == "pass" and ran == ["first@2"], \
        "`ran == []` above was the guard's doing, not a predicate that could never run"

    # An unmeasured document is refused by load_run itself, not only by the CLI in front of it.
    del document["library_version"]
    with pytest.raises(Refusal) as unmeasured:
        load_run(document, plan, frozen, lambda: T0)
    assert unmeasured.value.code == "library_unmeasured" and ran == ["first@2"]
