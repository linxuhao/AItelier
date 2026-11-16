"""`python -m dsh_todo <create|status|begin|done|report|handback|finish|list|batch-*>`

The harness side of a todo plan. Every command prints one JSON object on stdout
and exits 0, or - when the harness refuses - prints {"refused": code, "reason": ...}
and exits 2. A failing item is NOT a refusal: `done` reports `outcome: fail` and
exits 0, because the verdict is the product.

`list` is the one sweep: it prints one row per plan and exits 0 whatever it finds,
so a plan it could not open cannot be answered as a refusal on that plan's behalf.
Its row carries the same finding as fields instead - `code` (and both library
versions when the refusal compared them) beside the human `error` - so a caller
reads a code rather than parsing prose that any later wording change would break.

Every state-changing command takes the plan's flock, rehydrates a TodoRun from
`<store>/<attempt_id>.json` (ledger episodes included, so `started_at` never
moves), acts, and replaces the file atomically. `--now` fixes the clock so tests can
walk the ledger's wait and ceiling deterministically; it is refused unless the
environment sets AITELIER_TODO_CLOCK_OVERRIDE=1, because whoever picks the clock picks
when an absence expires. Anything that is not a refusal is reported as
{"refused": "internal"}: the exit status is 0 or 2, never a traceback.

Batches (dsh_todo/batch.py): `batch-create` freezes a `kind: batch` template's find
sub-plans, `batch-status` says what to dispatch, `batch-expand` expands the verify stage once
every finder passed, `batch-fanin` writes the summary. A sub-plan (`<attempt>~find-<key>`,
`<attempt>~verify-<key>`) is driven by the ordinary commands; `begin` on one enforces the
batch's binding rules under the batch lock, and `finish` on the batch attempt checks every
sub-plan's persisted observations (no predicate runs again) and produces one candidate.

Trust: `create --plan-json` and AITELIER_TODO_TEMPLATES are not gated here. They rely on
the DSH tool that calls `create` being root-only: a child must never pick the plan source
or the template directory.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator

from dsh_todo.plan import (MAX_NOTES, PlanStore, Refusal, TodoPlan, item_state, load_run,
                           new_document, require_library_version, save_observations)
from dsh_todo import batch as todo_batch, templates
from dsh_todo.hooks import candidate_rejection
from dsh_todo.runtime import builtin_library

_BRIEF_CHARS = 600


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # a usage error is a refusal like any other
        print(json.dumps(Refusal("usage", message).as_json()))
        raise SystemExit(2)


class _Open:
    """One plan, open for a command: the document, the frozen plan, the rehydrated run."""

    def __init__(self, store: PlanStore, document: dict, now: float):
        self.store, self.document, self.now = store, document, now
        self.library = builtin_library()
        # Before the plan is even parsed: a document frozen under another library must
        # be named as such, not reported as a plan whose predicates this library lacks.
        require_library_version(document, self.library)
        try:
            self.plan = TodoPlan.from_stored(document["plan"], self.library)
            if self.plan.plan_hash != document["plan_hash"]:
                raise Refusal("corrupt_plan", "the stored plan does not match its plan_hash")
            self.run = load_run(document, self.plan, self.library, lambda: now)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise Refusal("corrupt_plan", f"the plan file is damaged: {type(exc).__name__}: {str(exc)[:200]}") from exc

    def item(self, item_id: str):
        try:
            return self.run.plan.item(item_id)
        except KeyError:
            raise Refusal("unknown_item", f"no item {item_id!r} in plan {self.plan.attempt_id}") from None

    def save(self) -> None:
        save_observations(self.document, self.plan, self.run, self.now)
        self.store.write(self.plan.attempt_id, self.document)

    def episode(self, item_id: str) -> dict | None:
        key = self.run.episode_key(item_id)
        episode = self.run.ledger.episodes.get(key)
        if episode is None:
            return None
        return {"started_at": episode.started_at, "last_absence_at": episode.last_absence_at,
                "absences": episode.absences, "gate": episode.gate, "gate_runs": episode.gate_runs,
                "hold_remaining": self.run.ledger.hold_remaining(key, now=self.now)}

    def view(self) -> dict:
        report = self.run.report()
        entries = self.document["items"]
        # Longest this item can legitimately take to measure: the sum of its predicates' timeouts.
        budget = {i.id: sum(b.args["timeout"] for b in i.predicates if b.args.get("timeout")) for i in self.plan.items}
        return {
            "attempt_id": self.plan.attempt_id, "node_key": self.plan.node_key,
            "plan_hash": self.plan.plan_hash, "goal": self.plan.goal,
            "brief": self.run.brief(max_chars=_BRIEF_CHARS),
            "next": report["next"], "done": report["done"],
            "remaining": [i.id for i in self.plan.items if i.id not in report["done"]],
            "deferred": report["deferred"], "unmeasurable": report["unmeasurable"],
            "child_session": self.document["child_session"],
            "rebound_from": self.document.get("rebound_from"),
            **({"batch": self.plan.batch} if self.plan.batch else {}),
            "timeout_budget_seconds": budget, "finish_budget_seconds": sum(budget.values()),
            "episodes": {i.id: self.episode(i.id) for i in self.plan.items if self.episode(i.id)},
            "items": [{"id": i.id, "title": i.title, "needs": list(i.needs),
                       "state": item_state(entries[i.id]), "begun": entries[i.id]["pre_state"] is not None,
                       "results": entries[i.id]["results"], "observed_at": entries[i.id]["observed_at"],
                       "notes": len(entries[i.id]["notes"]),
                       "last_note": (entries[i.id]["notes"] or [{}])[-1].get("text")}
                      for i in self.plan.items],
        }


@contextmanager
def _plan(store: PlanStore, attempt_id: str, now: float, *, locked: bool) -> Iterator[_Open]:
    """Open an existing plan. Writers hold the flock; readers see the last atomic replace."""
    def load() -> _Open:
        document = store.read(attempt_id)
        if document is None:
            raise Refusal("no_such_plan", f"no plan for attempt {attempt_id!r} in {store.root}")
        return _Open(store, document, now)

    if locked:
        with store.lock(attempt_id):
            yield load()
    else:
        yield load()


# -- commands ---------------------------------------------------------------

_TEMPLATE_FLAGS = ("attempt", "node_key", "context_hash", "base_sha", "workspace", "goal")


def cmd_templates(args, store: None, now: float) -> dict:
    library = builtin_library()
    directory = templates.default_dir()
    listed = []
    for name in templates.available(directory):
        try:
            listed.append(templates.load_template(name, library, directory).as_json())
        except Refusal as refusal:
            listed.append({"name": name, "error": refusal.reason})
        except Exception as exc:  # noqa: BLE001 - one broken file must not hide the other templates
            listed.append({"name": name, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
    return {"directory": str(directory), "templates": listed}


def _plan_from_args(args, library) -> tuple[TodoPlan, dict | None, dict | None, "templates.PathAudit"]:
    """The frozen plan, (for a template) where it came from, and the declared-path audit.

    Both routes validate identically, the audit included: a declared path whose current state has
    no file digest (an existing directory, FIFO, socket, device) cannot be SEEN to change unless
    the step first replaces it with a regular file, so it is refused here before any plan exists.
    """
    if bool(args.plan_json) == bool(args.template):
        raise Refusal("usage", "give exactly one of --template NAME (with --params) or --plan-json FILE")
    if args.plan_json:
        try:
            raw = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Refusal("invalid_plan", f"cannot read plan JSON {args.plan_json}: {exc}") from exc
        plan, template, params = TodoPlan.from_json(raw, library), None, None
    else:
        missing = [f"--{f.replace('_', '-')}" for f in _TEMPLATE_FLAGS if not getattr(args, f)]
        if missing:
            raise Refusal("usage", f"--template needs {', '.join(missing)}")
        try:
            supplied = json.loads(args.params or "{}")
        except ValueError as exc:
            raise Refusal("bad_param", f"--params is not JSON: {exc}") from exc
        source = templates.load_template(args.template, library)
        if source.kind == "batch":
            raise Refusal("batch_template", f"template {source.name} is a batch template: freeze it with todo_batch_plan "
                                            "(batch-create), not todo_plan")
        params = templates.resolve_params(source, supplied)
        plan = TodoPlan.from_json(templates.plan_data(
            source, params, attempt_id=args.attempt, node_key=args.node_key, context_hash=args.context_hash,
            base_sha=args.base_sha, workspace=args.workspace, goal=args.goal), library)
        template = {"name": source.name, "version": source.version, "sha256": source.sha256}
    return plan, template, params, templates.check_declared_paths(plan)


def cmd_create(args, store: PlanStore, now: float) -> dict:
    library = builtin_library()
    plan, template, params, paths = _plan_from_args(args, library)
    if plan.batch is not None:
        raise Refusal("invalid_plan", "a batch sub-plan is frozen by batch-create or batch-expand, never by create")
    if plan.attempt_id.endswith(todo_batch.RECORD_SUFFIX):
        raise Refusal("invalid_attempt", f"attempt ids ending in {todo_batch.RECORD_SUFFIX!r} name batch records")
    with store.lock(plan.attempt_id):
        if todo_batch.exists(store, plan.attempt_id):
            raise Refusal("batch_exists", f"attempt {plan.attempt_id} already has a batch; one attempt is either a "
                                          "plan or a batch")
        existing = store.read(plan.attempt_id)
        if existing is not None:
            if existing["plan_hash"] != plan.plan_hash:
                raise Refusal("plan_differs", f"attempt {plan.attempt_id} already has a different frozen plan",
                              plan_hash=existing["plan_hash"], offered_plan_hash=plan.plan_hash)
            created = False
        else:
            store.write(plan.attempt_id, new_document(plan, library, now, template, params))
            created = True
    with _plan(store, plan.attempt_id, now, locked=False) as open_plan:
        return {"created": created, "attempt_id": plan.attempt_id, "plan_hash": plan.plan_hash,
                "template": template, "params": params, "library_version": library.version, "items": [i.id for i in plan.items],
                "paths": paths.as_json(), "brief": open_plan.run.brief(max_chars=_BRIEF_CHARS)}


def cmd_status(args, store: PlanStore, now: float) -> dict:
    if store.read(args.attempt) is None and todo_batch.exists(store, args.attempt):
        return todo_batch.status(store, builtin_library(), args.attempt, now)
    with _plan(store, args.attempt, now, locked=False) as p:
        return p.view()


def cmd_handback(args, store: PlanStore, now: float) -> dict:
    """What a director needs when the worker stopped: status, plus why and what was last seen."""
    with _plan(store, args.attempt, now, locked=False) as p:
        view = p.view()
        view["reason"] = "todo_complete" if not view["remaining"] else "todo_unfinished"
        view["last_observations"] = {i["id"]: i["results"] for i in view["items"] if i["state"] != "pass"}
        return view


def cmd_begin(args, store: PlanStore, now: float) -> dict:
    sub = todo_batch.parse_subplan(args.attempt)
    if sub is None:
        if args.prompt_sha256:
            raise Refusal("usage", "--prompt-sha256 is recorded for batch sub-plans only")
        return _begin(args, store, now)
    if not args.session:
        raise Refusal("usage", "a batch sub-plan binds the session that works it: begin needs --session")
    # The batch lock first, then the sub-plan's (the order every batch writer takes them in): the
    # binding rules read and extend the batch's binding history, so check and record are one step.
    with store.lock(todo_batch.record_key(sub[0])):
        record = todo_batch.read_record(store, sub[0])
        todo_batch.check_binding(record, args.attempt, args.session)
        out = _begin(args, store, now)
        todo_batch.record_binding(store, record, args.attempt, args.session, now, args.prompt_sha256)
    return out


def _begin(args, store: PlanStore, now: float) -> dict:
    with _plan(store, args.attempt, now, locked=True) as p:
        item = p.item(args.item)
        done = set(p.run.report()["done"])
        unmet = [n for n in item.needs if n not in done]
        if unmet:
            raise Refusal("unmet_needs", f"item {item.id} needs {unmet} to pass first", unmet=unmet)
        bound = p.document["child_session"]
        if args.rebind and not args.session:
            raise Refusal("usage", "--rebind needs --session")
        if args.session and bound and bound != args.session and not args.rebind:
            raise Refusal("bound_to_other_session",
                          f"plan {p.plan.attempt_id} is bound to session {bound}; pass --rebind only once that session is dead",
                          bound_session=bound)
        first = p.document["items"][item.id]["pre_state"] is None
        p.run.begin(item.id)
        if args.session:
            if bound and bound != args.session:
                p.document["rebound_from"] = bound
            p.document["child_session"] = args.session
        p.save()
        return {"item": item.id, "begun": True, "first_begin": first,
                "child_session": p.document["child_session"], "rebound_from": p.document.get("rebound_from"),
                "pre_state": p.document["items"][item.id]["pre_state"], **_tail(p)}


def _check_session(p: _Open, session: str | None) -> None:
    """A caller that names its session must be the bound one (only `begin --rebind` can change it)."""
    bound = p.document["child_session"]
    if session and bound and session != bound:
        raise Refusal("bound_to_other_session", f"plan {p.plan.attempt_id} is bound to session {bound}, not {session}",
                      bound_session=bound)


def cmd_done(args, store: PlanStore, now: float) -> dict:
    with _plan(store, args.attempt, now, locked=True) as p:
        _check_session(p, args.session)
        item = p.item(args.item)
        if p.document["items"][item.id]["pre_state"] is None:
            raise Refusal("not_begun", f"item {item.id} was never begun: begin it first so there is a state to compare against")
        report = p.run.observe(item.id)
        p.document["items"][item.id]["observed_at"] = now
        p.save()
        out: dict[str, Any] = {"item": item.id, "outcome": report.outcome,
                               "results": [r.as_json() for r in report.results],
                               "unmeasurable": report.unmeasurable}
        if report.outcome == "absent":
            out["episode"] = p.episode(item.id)
        if report.unmeasurable:
            out["reason"] = p.run.terminal_reason(item.id)
        return {**out, **_tail(p)}


def cmd_report(args, store: PlanStore, now: float) -> dict:
    """A free-text note. It is recorded and never read as a verdict."""
    with _plan(store, args.attempt, now, locked=True) as p:
        _check_session(p, args.session)
        item = p.item(args.item)
        notes = p.document["items"][item.id]["notes"]
        notes.append({"at": now, "text": args.note[:2000]})
        del notes[:-MAX_NOTES]
        p.save()
        return {"item": item.id, "recorded": True, "notes": len(notes), **_tail(p)}


_LIST_KEYS = ("attempt_id", "node_key", "plan_hash", "next", "done", "remaining", "unmeasurable", "child_session")
#: The identity of the two libraries a refusal compared, when the refusal compared any.
_LIBRARY_FIELDS = ("plan_library_version", "running_library_version")


def _list_row(attempt_id: str, exc: Exception) -> dict:
    """A plan a sweep could not open, as a row carrying the same refusal a single command answers.

    `status`/`begin`/`done`/... answer a refusal as {"refused": code, ...} at exit 2; `list`
    answers one row per plan at exit 0, so the row has to carry that code as a FIELD.
    `error` stays the human string it always was - a sweep that parses prose to find out
    why a plan refuses is a sweep that breaks the next time the wording is improved.

    `plan_library_version` / `running_library_version` are copied only when the refusal
    named them: a damaged plan records no comparable version, and filling those fields
    with a placeholder would make "damaged" read like "frozen under no library".
    """
    row = {"attempt_id": attempt_id, "error": str(exc)[:200],
           "code": exc.code if isinstance(exc, Refusal) else "corrupt_plan"}
    if isinstance(exc, Refusal):
        row.update({k: exc.extra[k] for k in _LIBRARY_FIELDS if k in exc.extra})
    return row


def cmd_list(args, store: PlanStore, now: float) -> dict:
    plans = []
    for attempt_id in store.attempts():
        if attempt_id.endswith(todo_batch.RECORD_SUFFIX):     # a batch record, not a plan
            batch_id = attempt_id[:-len(todo_batch.RECORD_SUFFIX)]
            try:
                row = todo_batch.list_row(store, builtin_library(), batch_id, now)
            except (Refusal, OSError, ValueError, KeyError) as exc:
                row = _list_row(batch_id, exc)
            if row is not None:
                plans.append(row)
            continue
        try:
            with _plan(store, attempt_id, now, locked=False) as p:
                v = p.view()
            plans.append({k: v[k] for k in _LIST_KEYS} | ({"batch": v["batch"]} if "batch" in v else {}))
        except (Refusal, OSError, ValueError, KeyError) as exc:
            plans.append(_list_row(attempt_id, exc))
    return {"plans": plans}


def _tail(p: _Open) -> dict:
    return {"next": p.run.report()["next"], "brief": p.run.brief(max_chars=_BRIEF_CHARS)}


def _git(workspace: str, *argv: str) -> str:
    try:
        done = subprocess.run(["git", "-C", workspace, *argv], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal("git_unavailable", f"git {argv[0]}: {exc}") from exc
    if done.returncode:
        raise Refusal("git_failed", f"git {argv[0]} exited {done.returncode}: {(done.stderr or done.stdout).strip()[-300:]}")
    return done.stdout.strip()


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def cmd_finish(args, store: PlanStore, now: float) -> dict:
    """Re-evaluate every item; only when all pass, bundle HEAD and write the candidate report.

    A batch attempt is finished from its sub-plans' PERSISTED observations instead (no predicate
    runs again), and its candidate also names the fan-in summary and its sha256."""
    if todo_batch.parse_subplan(args.attempt):
        raise Refusal("subplan_finish", f"{args.attempt} is a batch sub-plan: finish its batch attempt instead")
    if store.read(args.attempt) is None and todo_batch.exists(store, args.attempt):
        return _finish_batch(args, store, now)
    with _plan(store, args.attempt, now, locked=True) as p:
        failing, observations = [], []
        for item in p.plan.items:
            if p.document["items"][item.id]["pre_state"] is None:
                failing.append({"item": item.id, "outcome": "open", "reason": "never begun"})
                continue
            report = p.run.observe(item.id)
            p.document["items"][item.id]["observed_at"] = now
            results = [r.as_json() for r in report.results]
            observations.append({"item": item.id, "outcome": report.outcome, "results": results})
            if not report.complete:
                failing.append({"item": item.id, "outcome": report.outcome, "unmeasurable": report.unmeasurable,
                                "reason": "; ".join(f"{r['predicate']}: {r['detail']}" for r in results
                                                    if r["outcome"] != "pass")})
        p.save()
        if failing:
            raise Refusal("unfinished", f"{len(failing)} item(s) do not pass; no candidate was produced",
                          failing=failing, brief=p.run.brief(max_chars=_BRIEF_CHARS))
        return _candidate(args.out, p.plan.workspace, p.plan.attempt_id, p.plan.plan_hash, observations,
                          p.run.brief(max_chars=_BRIEF_CHARS))


def _finish_batch(args, store: PlanStore, now: float) -> dict:
    library = builtin_library()
    with store.lock(todo_batch.record_key(args.attempt)):
        record = todo_batch.read_record(store, args.attempt)
        failing, observations = todo_batch.completion(store, library, record)
        brief = todo_batch.status(store, library, args.attempt, now)["brief"]
        if failing:
            raise Refusal("unfinished", f"{len(failing)} sub-plan item(s) have not passed; no candidate was produced",
                          failing=failing, brief=brief)
        summary, summary_sha, content = todo_batch.write_fan_in(store, library, record)
        # The candidate is the commit the verifiers certified, never whatever HEAD is
        # now: every verify report's artifact_ref, or reviewed_sha when nothing was
        # verified. One certified commit, or the batch refuses.
        certified = {f["artifact_ref"] for f in content["findings"]} or {record["params"]["reviewed_sha"]}
        if len(certified) != 1:
            raise Refusal("certified_shas_differ", f"the verify reports certify {sorted(certified)}, not one commit")
        return _candidate(args.out, record["workspace"], args.attempt, record["batch_hash"], observations, brief,
                          {"batch": {"summary": str(summary), "summary_sha256": summary_sha, "evidence": False,
                                     "counts": content["counts"]}},
                          certified=certified.pop())


def _candidate(out: str, workspace: str, attempt_id: str, plan_hash: str, observations: list, brief: str,
               extra: dict | None = None, certified: str | None = None) -> dict:
    """Bundle the workspace's HEAD and write the candidate report - for a plan and a batch alike.

    `certified` (a batch): the commit the verifiers certified. HEAD must BE that commit,
    checked before bundling and again on the bundle's tip, or nothing is produced."""
    head = _git(workspace, "rev-parse", "HEAD")
    if certified is not None:
        resolved = subprocess.run(["git", "-C", workspace, "rev-parse", "-q", "--verify", f"{certified}^{{commit}}"],
                                  capture_output=True, text=True).stdout.strip()
        if resolved != head:
            raise Refusal("head_not_certified", f"HEAD is {head} but the verify reports certified {certified}"
                                                f"{f' ({resolved})' if resolved and resolved != certified else ''}; "
                                                "check out the certified commit or re-review the new one",
                          head=head, certified=certified)
    branch = subprocess.run(["git", "-C", workspace, "symbolic-ref", "--short", "-q", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    out_dir = Path(out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = out_dir / f"{attempt_id}.bundle"
    staged = bundle.with_name(f".{bundle.name}.{os.getpid()}.tmp")
    try:
        # No `base..head` range: the bundle carries the whole history it needs, so
        # `git bundle verify` passes in a repository that has none of it.
        _git(workspace, "bundle", "create", str(staged), branch and f"refs/heads/{branch}" or "HEAD")
        with tempfile.TemporaryDirectory(prefix="todo-bundle-verify-") as empty:
            _git(empty, "init", "--bare", "-q", ".")
            _git(empty, "bundle", "verify", str(staged))
        tips = {line.split()[0] for line in _git(workspace, "bundle", "list-heads", str(staged)).splitlines()
                if line.strip()}
        if head not in tips:
            raise Refusal("head_moved", f"HEAD was {head} when read but the bundle's tip is {sorted(tips)}; "
                                        "something committed while finishing")
        if certified is not None and tips != {head}:
            raise Refusal("head_not_certified", f"the bundle's tips {sorted(tips)} are not exactly the certified {head}",
                          head=head, certified=certified)
        # `quiescent` is attested below, so look once more, right before writing it.
        if _git(workspace, "status", "--porcelain") or _git(workspace, "rev-parse", "HEAD") != head:
            raise Refusal("not_quiescent", "the working tree changed while the candidate was being produced")
        os.replace(staged, bundle)
    finally:
        staged.unlink(missing_ok=True)
    bundle_sha = hashlib.sha256(bundle.read_bytes()).hexdigest()

    report_doc = {
        "status": "candidate", "settled": True, "usable": True,
        "artifact": head, "artifact_kind": "git-sha1",
        "git_bundle": {"path": str(bundle), "sha256": bundle_sha},
        "quiescent": True, "plan_hash": plan_hash, "observations": observations, **(extra or {}),
    }
    report_bytes = (json.dumps(report_doc, sort_keys=True, indent=2) + "\n").encode("utf-8")
    rejection = candidate_rejection(report_bytes, "candidate", head)
    if rejection is not None:
        raise Refusal("report_rejected", rejection)
    report_path = out_dir / f"{attempt_id}-candidate.json"
    _write_atomic(report_path, report_bytes)
    return {"candidate": True, "artifact": head, "artifact_kind": "git-sha1",
            "report": str(report_path), "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            "git_bundle": report_doc["git_bundle"], "plan_hash": plan_hash,
            **(extra or {}), "brief": brief}


def _live(args) -> set[str] | None:
    """--live-sessions: the sessions the caller knows to be running (a JSON list), or None when not given."""
    if args.live_sessions is None:
        return None
    try:
        live = json.loads(args.live_sessions)
    except ValueError as exc:
        raise Refusal("usage", f"--live-sessions is not JSON: {exc}") from exc
    if not isinstance(live, list) or not all(isinstance(s, str) for s in live):
        raise Refusal("usage", "--live-sessions must be a JSON list of session ids")
    return set(live)


def cmd_batch_create(args, store: PlanStore, now: float) -> dict:
    missing = [f"--{f.replace('_', '-')}" for f in ("template", *_TEMPLATE_FLAGS) if not getattr(args, f)]
    if missing:
        raise Refusal("usage", f"batch-create needs {', '.join(missing)}")
    try:
        supplied = json.loads(args.params or "{}")
    except ValueError as exc:
        raise Refusal("bad_param", f"--params is not JSON: {exc}") from exc
    library = builtin_library()
    return todo_batch.create(store, library, templates.load_template(args.template, library), supplied,
                             attempt_id=args.attempt, node_key=args.node_key, context_hash=args.context_hash,
                             base_sha=args.base_sha, workspace=args.workspace, goal=args.goal, now=now)


def cmd_batch_status(args, store: PlanStore, now: float) -> dict:
    return todo_batch.status(store, builtin_library(), args.attempt, now, _live(args), record=args.record_dispatch)


def cmd_batch_expand(args, store: PlanStore, now: float) -> dict:
    return todo_batch.expand(store, builtin_library(), args.attempt, now, _live(args),
                             record_dispatch=args.record_dispatch)


def cmd_batch_fanin(args, store: PlanStore, now: float) -> dict:
    library = builtin_library()
    with store.lock(todo_batch.record_key(args.attempt)):
        record = todo_batch.read_record(store, args.attempt)
        path, digest, summary = todo_batch.write_fan_in(store, library, record)
    return {"summary": str(path), "summary_sha256": digest, "evidence": False, "counts": summary["counts"],
            "brief": todo_batch.status(store, library, args.attempt, now)["brief"]}


COMMANDS = {"templates": cmd_templates, "create": cmd_create, "status": cmd_status, "begin": cmd_begin, "done": cmd_done,
            "report": cmd_report, "handback": cmd_handback, "finish": cmd_finish, "list": cmd_list,
            "batch-create": cmd_batch_create, "batch-status": cmd_batch_status, "batch-expand": cmd_batch_expand,
            "batch-fanin": cmd_batch_fanin}


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", help="plan directory (default: $DSH_TODO_STATE_DIR/todo, else $AITELIER_DSH_STATE_DIR/todo)")
    common.add_argument("--now", type=float,
                        help="fix the clock (seconds since epoch). TESTS ONLY: refused unless "
                             "AITELIER_TODO_CLOCK_OVERRIDE=1, since the clock decides when an absence expires")
    parser = _Parser(prog="python -m dsh_todo")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    def command(name: str, *flags: tuple[str, dict]) -> None:
        sp = sub.add_parser(name, parents=[common])
        for flag, kwargs in flags:
            sp.add_argument(flag, **kwargs)

    attempt = ("--attempt", {"required": True})
    item = ("--item", {"required": True})
    command("templates")
    command("create",
            ("--template", {"metavar": "NAME", "help": "expand a repository template (the way plans are made)"}),
            ("--params", {"metavar": "JSON", "help": "the template's params, as a JSON object"}),
            ("--attempt", {}), ("--node-key", {}), ("--context-hash", {}), ("--base-sha", {}),
            ("--workspace", {}), ("--goal", {}),
            ("--plan-json", {"metavar": "FILE", "help": "TESTS ONLY: a free-form plan, not gated here - it relies on the DSH create tool being "
                                    "root-only (as does AITELIER_TODO_TEMPLATES)"}))
    command("status", attempt)
    command("begin", attempt, item, ("--session", {"metavar": "SID"}),
            ("--rebind", {"action": "store_true",
                          "help": "take over a plan bound to another session; only after that session is known dead"}),
            ("--prompt-sha256", {"metavar": "HEX", "help": "batch sub-plans: the sha256 of the prompt the child was "
                                                          "given, recorded with the binding (not enforced)"}))
    session = ("--session", {"metavar": "SID", "help": "the caller's session; refused if the plan is bound to another"})
    command("done", attempt, item, session)
    command("report", attempt, item, ("--note", {"required": True}), session)
    command("handback", attempt)
    command("finish", attempt, ("--out", {"required": True, "metavar": "DIR"}))
    command("list")
    command("batch-create",
            ("--template", {"metavar": "NAME", "help": "a `kind: batch` repository template"}),
            ("--params", {"metavar": "JSON"}), ("--attempt", {}), ("--node-key", {}), ("--context-hash", {}),
            ("--base-sha", {}), ("--workspace", {}), ("--goal", {}))
    live = ("--live-sessions", {"metavar": "JSON", "help": "sessions known to be running; a sub-plan bound to any "
                                                           "other is offered for re-dispatch"})
    record = ("--record-dispatch", {"action": "store_true", "help": "record the sub-plans put in dispatch as offered, "
                                                                     "so they are not offered twice"})
    command("batch-status", attempt, live, record)
    command("batch-expand", attempt, live, record)
    command("batch-fanin", attempt)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.now is not None and (os.environ.get("DSH_TODO_CLOCK_OVERRIDE") or os.environ.get("AITELIER_TODO_CLOCK_OVERRIDE")) != "1":
            raise Refusal("clock_override_disabled", "--now is for tests; set AITELIER_TODO_CLOCK_OVERRIDE=1 to use it")
        # DSH_TODO_STATE_DIR is the package's own name; the legacy driver variable is
        # read only when the first is absent.
        state_dir = os.environ.get("DSH_TODO_STATE_DIR") or os.environ.get("AITELIER_DSH_STATE_DIR")
        root = args.store or (os.path.join(state_dir, "todo") if state_dir else None)
        if root is None and args.command != "templates":
            raise Refusal("usage", "no --store and neither DSH_TODO_STATE_DIR nor AITELIER_DSH_STATE_DIR is set")
        out = COMMANDS[args.command](args, None if args.command == "templates" else PlanStore(Path(root)),
                                     time.time() if args.now is None else args.now)
        code = 0
    except Refusal as refusal:
        out, code = refusal.as_json(), 2
    except Exception as exc:  # noqa: BLE001 - the contract is JSON and exit 0 or 2, whatever broke
        out, code = Refusal("internal", f"{type(exc).__name__}: {str(exc)[:300]}").as_json(), 2
    print(json.dumps(out, sort_keys=True))
    return code


if __name__ == "__main__":
    # A terminated CLI must unwind (so a running predicate command's process group is killed),
    # not vanish and leave a test or docker child behind.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    sys.exit(main())
