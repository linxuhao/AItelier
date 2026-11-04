"""implement_evidence — judge the implement step by OBSERVED change.

`coding_impl`'s implement step is an agent step that writes code and then says it
is done. Until now the only account of what it produced was its own: the step's
flags (`wrote_files`), its closing message, and the report `run_tests` produces
one step later. That catches broken code; it does not catch a step that claims a
change the repository does not carry — a reverted edit, a path the write tool
recorded but whose content never landed, a receipt whose file list and whose
tree disagree.

This tool observes instead of listening. It takes the implement step's own
`code_changes.json` delivery receipt — the engine's record of the base commit,
the delivered commit and the files it recorded — and holds the ACTUAL worktree
against it with the shared predicate library:

* one `file_changed` predicate per declared file, with the pre-state captured
  from the BASE COMMIT's content, plus the declared `contains` marker when one
  is given. Deliberately NOT "the file must be absent or wrong first": a write
  step's file already exists, and demanding redness would make deleting it the
  only way to satisfy the check;
* one cheap `command_exit` on the delivered commit object, so a receipt naming a
  commit this worktree does not contain is visible as evidence, not prose.

The verdict is `confirmed`, `unconfirmed` (naming the file — and, when a
`contains` marker was declared, the content — that is missing) or `absent`.
`absent` is NOT a failure: when nothing can be measured (no receipt yet, git
missing, a timeout, a base commit this worktree does not have) the episode goes
to `core/gate_deferral.DeferralLedger`, which already models an absence as
bounded, cheaply retried, and self-naming. A missing binary must never be
charged to the implementer.

Non-routing by construction. The step that runs this sits between `implement` and
`test` and its only edge goes to `test`, so `implement -> test -> test_outcome`
is exactly what it was. A red verdict here cannot make a green run red, and a
green verdict here cannot make a red gate green: the report is evidence, and the
decision stays where it was.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from core.gate_deferral import LEDGER
from core.todo_runtime import Binding, builtin_library, capture_pre_state

#: The engine's code-delivery receipt, published into the implement step's own
#: output dir at `finish_step` (skillflow `output_targets.CodeOutput.commit`).
RECEIPT_NAME = "code_changes.json"

#: The report this tool promotes into ITS step dir, so the verdict is step
#: OUTPUT — readable by get_step_output — and not a line in a log.
REPORT_NAME = "implement_evidence.json"

#: One library, borrowed — not a second definition of what "changed" means.
_LIBRARY = builtin_library()

#: A change set larger than this is not observed in full, and the verdict says
#: so instead of confirming something it never looked at.
MAX_DECLARED_FILES = 500


def _episode_key(run_id: str, step_id: str) -> str:
    """The DeferralLedger key for one observation.

    Namespaced on purpose: the scheduler's own deferral accounting asks the
    ledger about a bare run id, and an observation episode must never read as a
    silent repository gate to that code path.
    """
    return f"implement_evidence/{run_id or '-'}/{step_id}"


def _absent(reason: str, waiting_for: str, *, key: str, gate: str,
            now: float | None = None) -> dict:
    """Record an absence as a bounded episode, and say what it waits for."""
    LEDGER.note_absence(key, gate=gate, now=now)
    body = {"verdict": "absent", "state": "absent", "confirmed": None,
            "waiting_for": waiting_for, "reason": reason, "gate": gate,
            "episode": {"key": key, "absences": LEDGER.episode_count(key),
                        "seconds": round(LEDGER.episode_seconds(key, now=now), 3)}}
    if LEDGER.expired(key, now=now):
        # The sentence an absence ends with belongs to the ledger.
        body["terminal_reason"] = LEDGER.terminal_reason(key)
    return body


def _git(root: Path, args: list[str], timeout: float) -> tuple[int | None, bytes, str]:
    """One git command: (exit, stdout, stderr), or (None, b"", reason) if it could
    not be run at all. Callers treat None as an ABSENCE, never as a failure."""
    try:
        done = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return None, b"", "'git' is not installed here"
    except subprocess.TimeoutExpired:
        return None, b"", f"git {args[0]} timed out after {timeout}s"
    return done.returncode, done.stdout, done.stderr.decode("utf-8", "replace")


def _last_line(text: str, limit: int = 200) -> str:
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    return lines[-1][:limit] if lines else "no output"


def _read_receipt(path: Path) -> tuple[dict | None, str]:
    """The implement step's own declaration of what it changed, or why not."""
    if not path.is_file():
        return None, f"no {RECEIPT_NAME} at {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{RECEIPT_NAME} is unreadable ({type(exc).__name__}: {exc})"
    if not isinstance(data, dict):
        return None, f"{RECEIPT_NAME} is not a JSON object"
    return data, ""


def _safe_rel(path: object) -> str | None:
    """The declared path as a relative path inside the workspace, or None.

    A receipt records the worker's own writes; one that names a path outside the
    worktree is not something to resolve against the host.
    """
    if not isinstance(path, str) or not path.strip():
        return None
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    return candidate.as_posix()


def _extra_declarations(raw: str) -> tuple[list[tuple[str, str | None]], str]:
    """Parse the optional `declared` param: [{"path": ..., "contains": ...}]."""
    text = (raw or "").strip()
    if not text:
        return [], ""
    try:
        data = json.loads(text)
    except ValueError as exc:
        return [], f"declared is not JSON ({exc})"
    if not isinstance(data, list):
        return [], "declared must be a JSON list of {path, contains?} objects"
    out: list[tuple[str, str | None]] = []
    for entry in data:
        if not isinstance(entry, dict) or "path" not in entry:
            return [], "every declared entry needs a 'path'"
        rel = _safe_rel(entry.get("path"))
        if rel is None:
            return [], f"declared path {entry.get('path')!r} is not inside the workspace"
        contains = entry.get("contains")
        if contains is not None and not isinstance(contains, str):
            return [], "declared 'contains' must be a string"
        out.append((rel, contains))
    return out, ""


def _materialise_base(root: Path, base: str, paths: list[str], refdir: Path,
                      timeout: float) -> str:
    """Copy each declared path AS THE BASE COMMIT HAS IT into `refdir`.

    `capture_pre_state` digests a directory, so the base commit's version of the
    same relative paths has to exist as files. A path the base did not have is
    left absent on purpose: `file_changed` then compares against "was not
    there", which is exactly what a newly created file is.

    Returns "" on success, else the absence reason — never a failure verdict: a
    base that cannot be read measured nothing.
    """
    for rel in paths:
        # `ls-tree` (not `cat-file -e`): the latter exits 128, not 1, for a path
        # the commit does not hold, and 128 is also what a broken object looks
        # like — "absent from the base" and "git failed" must not be one thing.
        code, listed, err = _git(root, ["ls-tree", "-z", "--name-only", base,
                                        "--", rel], timeout)
        if code is None:
            return err
        if code != 0:
            return f"git ls-tree {base} -- {rel} failed (exit {code}): {_last_line(err)}"
        if not listed.strip(b"\0"):
            continue                      # the base commit has no such path
        code, out, err = _git(root, ["show", f"{base}:{rel}"], timeout)
        if code != 0:
            return (f"git show {base}:{rel} failed (exit {code}): {_last_line(err)}")
        target = refdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(out)
    return ""


def _write_report(out_dir: Path, body: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / REPORT_NAME).write_text(
        json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")


def _finish(out_dir: Path, body: dict) -> dict:
    try:
        _write_report(out_dir, body)
        body = {**body, "written": REPORT_NAME}
    except OSError as exc:
        body = {**body, "written": None,
                "error": f"could not write {REPORT_NAME}: {type(exc).__name__}: {exc}"}
    return body


def implement_evidence(*, project_root: str = "", out_dir: str = "",
                       declared_step: str = "implement", declared: str = "",
                       max_files: int = MAX_DECLARED_FILES,
                       timeout_seconds: float = 20.0,
                       run_id: str = "", step_id: str = "", **_ignored) -> dict:
    """Observe the implement step's declared change. Never fails the step.

    `project_root` is the run's code worktree (the engine injects it), `out_dir`
    is `$STEP_DIR` whose PARENT is the graph dir — where the implement step
    published its receipt. `declared_step` names that step, so another config can
    bind this tool without editing it.
    """
    step_id = step_id or "implement_evidence"
    key = _episode_key(run_id, step_id)
    if not out_dir or not Path(out_dir).is_absolute():
        # A binding error, not an absence: nothing can be recorded at all.
        return {"written": None, "verdict": "absent", "state": "absent",
                "confirmed": None,
                "error": "implement_evidence requires absolute out_dir=$STEP_DIR "
                         "(it writes its verdict into the step's own output dir)"}
    out_dir = Path(out_dir)
    try:
        return _observe(out_dir, project_root, declared_step, declared, max_files,
                        float(timeout_seconds), key, run_id)
    except Exception as exc:                                  # noqa: BLE001 — see below
        # The observer must never be able to fail the run it observes. A bug in
        # THIS code is an unmeasured change (the step has no failure edge to
        # route it to anyway), so it is reported as an absence with its cause,
        # not raised into the engine's tool-step error path.
        body = {"verdict": "absent", "state": "absent", "confirmed": None,
                "reason": f"observation failed: {type(exc).__name__}: {exc}",
                "waiting_for": "a working observation", "gate": "implement_evidence"}
        LEDGER.note_absence(key, gate="implement_evidence")
        return {**_finish(out_dir, body)}


def _observe(out_dir: Path, project_root: str, declared_step: str, declared: str,
             max_files: int, timeout_seconds: float, key: str, run_id: str) -> dict:
    declared_step = (declared_step or "implement").strip() or "implement"
    if not project_root:
        body = _absent("no code root resolves for this run ($PROJECT_ROOT is empty)",
                       "a code worktree to observe", key=key, gate="project_root")
        body["declared_step"] = declared_step
        return _finish(out_dir, body)
    # An absolute root or nothing. `Path(".")` IS the process CWD, so a relative
    # value would observe whatever directory the host happens to run in — the
    # exact hazard tests/unit/test_tool_root_guard_inventory.py inventories. The
    # engine assigns the resolved code path (absolute); a config that pins a
    # relative one is a binding error, and it is reported, not resolved.
    if not Path(project_root).is_absolute():
        body = _absent(f"project_root {project_root!r} is not absolute, so it would "
                       f"resolve against the process CWD",
                       "an absolute code worktree path", key=key, gate="project_root")
        body["declared_step"] = declared_step
        return _finish(out_dir, body)
    root = Path(project_root)
    if not root.is_dir():
        body = _absent(f"project_root {project_root!r} is not a directory",
                       f"a readable code worktree at {project_root}",
                       key=key, gate="project_root")
        body["declared_step"] = declared_step
        return _finish(out_dir, body)

    # 1. The DECLARED change: the implement step's own delivery receipt.
    receipt_path = out_dir.parent / declared_step / RECEIPT_NAME
    receipt, problem = _read_receipt(receipt_path)
    if receipt is None:
        body = _absent(f"{declared_step} published no readable delivery receipt: {problem}",
                       f"the {declared_step} step's {RECEIPT_NAME}",
                       key=key, gate="delivery_receipt")
        body.update(declared_step=declared_step, receipt=str(receipt_path))
        return _finish(out_dir, body)

    # A step output dir is keyed by project+config+step, NOT by run, so two runs
    # of this config on one project share it. A receipt that names another run is
    # last run's evidence; holding THIS worktree against it would compare two
    # different worlds. Say so instead (an absence, not a verdict) — the receipt
    # always carries the run it belongs to.
    receipt_run = receipt.get("run_id")
    if run_id and receipt_run and str(receipt_run) != str(run_id):
        body = _absent(f"the {RECEIPT_NAME} in {declared_step}'s output dir belongs to "
                       f"run {receipt_run}, not to this run {run_id}",
                       f"this run's own {declared_step} receipt",
                       key=key, gate="delivery_receipt")
        body.update(declared_step=declared_step, receipt=str(receipt_path),
                    receipt_run_id=str(receipt_run))
        return _finish(out_dir, body)

    recorded = receipt.get("files")
    if recorded is None:
        recorded = []
    if not isinstance(recorded, list):
        body = _absent(f"{RECEIPT_NAME} 'files' is {type(recorded).__name__}, not a list",
                       f"a readable {RECEIPT_NAME} from {declared_step}",
                       key=key, gate="delivery_receipt")
        body.update(declared_step=declared_step, receipt=str(receipt_path))
        return _finish(out_dir, body)
    base_commit = str(receipt.get("base_commit") or "")
    commit = str(receipt.get("commit") or "")

    extras, decl_problem = _extra_declarations(declared)
    if decl_problem:
        # A malformed declaration is a binding error: say so plainly and measure
        # nothing, rather than guessing what was meant.
        LEDGER.clear(key)
        body = {"verdict": "absent", "state": "absent", "confirmed": None,
                "error": decl_problem, "declared_step": declared_step,
                "waiting_for": "a well-formed declared change"}
        return _finish(out_dir, body)

    recorded_rels: list[str] = []
    refused: list[dict] = []
    for item in recorded:
        rel = _safe_rel(item)
        if rel is None:
            refused.append({"path": item, "missing": "readable path",
                            "reason": "the receipt names a path outside the workspace"})
        elif rel not in recorded_rels:
            recorded_rels.append(rel)
    declared_files: list[tuple[str, str | None]] = [(rel, None) for rel in recorded_rels]
    declared_files.extend(extras)

    body: dict = {
        "declared_step": declared_step,
        "receipt": str(receipt_path),
        "workspace": str(root),
        "declared": {
            "files": [{"path": path, **({"contains": contains} if contains else {})}
                      for path, contains in declared_files],
            "recorded_files": recorded_rels,
            "refused": refused,
            "base_commit": base_commit,
            "commit": commit,
            "run_id": receipt.get("run_id"),
            "step_instance_id": receipt.get("step_instance_id"),
        },
    }

    # Bound the work. A confirmation may not rest on files never looked at, so an
    # over-large change set is reported unconfirmed with the omitted paths named.
    bound = max(0, int(max_files))
    evaluated = declared_files[:bound]
    omitted = declared_files[bound:]
    body["declared"]["evaluated"] = [path for path, _ in evaluated]
    if omitted:
        body["omitted_files"] = [path for path, _ in omitted]

    if not evaluated:
        LEDGER.clear(key)
        body.update(verdict="unconfirmed", state="measured", confirmed=False, basis=[],
                    missing=[{"path": "", "missing": "change",
                              "reason": (f"the {declared_step} step's {RECEIPT_NAME} names no "
                                         f"changed file and no change was declared, so nothing "
                                         f"was observed to change while {declared_step} "
                                         f"claimed completion")}] + refused,
                    summary=(f"{declared_step} claimed completion and its receipt names no "
                             f"changed file: no {RECEIPT_NAME} 'files' entry."))
        return _finish(out_dir, body)

    # 2. The OBSERVED change: the worktree held against the base commit.
    bindings: list[Binding] = [
        Binding("file_changed", {"path": path, **({"contains": contains} if contains else {})})
        for path, contains in evaluated
    ]
    if commit:
        # Cheap, and it contradicts a receipt naming a commit this worktree does
        # not contain — the one thing a self-report can claim that disk cannot.
        bindings.append(Binding("command_exit", {
            "argv": ["git", "-C", str(root), "cat-file", "-e", f"{commit}^{{commit}}"],
            "expect": 0, "timeout": timeout_seconds}))

    if not base_commit:
        body.update(_absent(f"{RECEIPT_NAME} names no base_commit, so there is nothing to "
                            f"compare the worktree against",
                            f"a {RECEIPT_NAME} carrying base_commit",
                            key=key, gate="delivery_receipt"))
        return _finish(out_dir, body)

    code, out, err = _git(root, ["rev-parse", "--verify", "--quiet",
                                 f"{base_commit}^{{commit}}"], timeout_seconds)
    if code is None:
        body.update(_absent(err, "git to read the base commit",
                            key=key, gate="command_exit"))
        return _finish(out_dir, body)
    if code != 0 or not out.strip():
        body.update(_absent(f"base commit {base_commit} does not resolve in this worktree "
                            f"(exit {code}): {_last_line(err)}",
                            f"base commit {base_commit} to be present in the worktree",
                            key=key, gate="command_exit"))
        return _finish(out_dir, body)

    refdir = Path(tempfile.mkdtemp(prefix="implement-evidence-base-"))
    try:
        absence = _materialise_base(root, base_commit,
                                    [path for path, _ in evaluated],
                                    refdir, timeout_seconds)
        if absence:
            body.update(_absent(absence, "the base commit's content to be readable",
                                key=key, gate="command_exit"))
            return _finish(out_dir, body)
        pre_state = capture_pre_state(refdir, bindings)
        results = [_LIBRARY.evaluate(binding, workspace=root, pre_state=pre_state)
                   for binding in bindings]
    finally:
        shutil.rmtree(refdir, ignore_errors=True)

    body["basis"] = [result.as_json() for result in results]
    absence_results = [result for result in results if result.outcome == "absent"]
    if absence_results:
        first = absence_results[0]
        # Could not measure: not a contradiction, and not a failure.
        body.update(_absent(first.detail, first.detail, key=key, gate=first.predicate))
        return _finish(out_dir, body)

    LEDGER.clear(key)
    missing: list[dict] = []
    for binding, result in zip(bindings, results):
        if result.outcome == "pass":
            continue
        entry = {"path": str(binding.args.get("path") or ""),
                 "missing": "content" if binding.args.get("contains") else "change",
                 "reason": result.detail, "predicate": result.predicate}
        if binding.args.get("contains"):
            entry["expected_content"] = binding.args["contains"]
        missing.append(entry)
    missing.extend(refused)

    confirmed = not missing
    body.update(
        verdict="confirmed" if confirmed else "unconfirmed",
        state="measured",
        confirmed=confirmed,
        missing=missing,
        summary=("the worktree carries every declared change" if confirmed else
                 "the worktree does not carry the declared change: " + "; ".join(
                     f"{entry.get('path') or '(no file)'} ({entry['reason']})"
                     for entry in missing)),
    )
    if omitted and confirmed:
        body.update(verdict="unconfirmed", confirmed=False,
                    summary=(f"{len(omitted)} declared file(s) were not evaluated "
                             f"(max_files={max_files}); the change set is not fully observed"))
    return _finish(out_dir, body)
