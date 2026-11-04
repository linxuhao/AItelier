"""`python -m core.todo_cli <create|status|begin|done|report|handback|finish|list>`

The harness side of a todo plan. Every command prints one JSON object on stdout
and exits 0, or - when the harness refuses - prints {"refused": code, "reason": ...}
and exits 2. A failing item is NOT a refusal: `done` reports `outcome: fail` and
exits 0, because the verdict is the product.

Every state-changing command takes the plan's flock, rehydrates a TodoRun from
`<store>/<attempt_id>.json` (ledger episodes included, so `started_at` never
moves), acts, and replaces the file atomically. `--now` fixes the clock so tests can
walk the ledger's wait and ceiling deterministically; it is refused unless the
environment sets AITELIER_TODO_CLOCK_OVERRIDE=1, because whoever picks the clock picks
when an absence expires. Anything that is not a refusal is reported as
{"refused": "internal"}: the exit status is 0 or 2, never a traceback.

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

from core.todo_plan import (MAX_NOTES, PlanStore, Refusal, TodoPlan, item_state, load_run,
                            new_document, save_observations)
from core import todo_templates as templates
from core.todo_runtime import builtin_library

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


def _plan_from_args(args, library) -> tuple[TodoPlan, dict | None, dict | None]:
    """The frozen plan, and (for a template) where it came from. Both routes validate identically."""
    if bool(args.plan_json) == bool(args.template):
        raise Refusal("usage", "give exactly one of --template NAME (with --params) or --plan-json FILE")
    if args.plan_json:
        try:
            raw = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Refusal("invalid_plan", f"cannot read plan JSON {args.plan_json}: {exc}") from exc
        return TodoPlan.from_json(raw, library), None, None
    missing = [f"--{f.replace('_', '-')}" for f in _TEMPLATE_FLAGS if not getattr(args, f)]
    if missing:
        raise Refusal("usage", f"--template needs {', '.join(missing)}")
    try:
        supplied = json.loads(args.params or "{}")
    except ValueError as exc:
        raise Refusal("bad_param", f"--params is not JSON: {exc}") from exc
    template = templates.load_template(args.template, library)
    resolved = templates.resolve_params(template, supplied)
    plan = TodoPlan.from_json(templates.plan_data(
        template, resolved, attempt_id=args.attempt, node_key=args.node_key, context_hash=args.context_hash,
        base_sha=args.base_sha, workspace=args.workspace, goal=args.goal), library)
    return plan, {"name": template.name, "version": template.version, "sha256": template.sha256}, resolved


def cmd_create(args, store: PlanStore, now: float) -> dict:
    library = builtin_library()
    plan, template, params = _plan_from_args(args, library)
    with store.lock(plan.attempt_id):
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
                "brief": open_plan.run.brief(max_chars=_BRIEF_CHARS)}


def cmd_status(args, store: PlanStore, now: float) -> dict:
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


def cmd_done(args, store: PlanStore, now: float) -> dict:
    with _plan(store, args.attempt, now, locked=True) as p:
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
        item = p.item(args.item)
        notes = p.document["items"][item.id]["notes"]
        notes.append({"at": now, "text": args.note[:2000]})
        del notes[:-MAX_NOTES]
        p.save()
        return {"item": item.id, "recorded": True, "notes": len(notes), **_tail(p)}


def cmd_list(args, store: PlanStore, now: float) -> dict:
    plans = []
    for attempt_id in store.attempts():
        try:
            with _plan(store, attempt_id, now, locked=False) as p:
                v = p.view()
            plans.append({k: v[k] for k in ("attempt_id", "node_key", "plan_hash", "next", "done",
                                            "remaining", "unmeasurable", "child_session")})
        except (Refusal, OSError, ValueError, KeyError) as exc:
            plans.append({"attempt_id": attempt_id, "error": str(exc)[:200]})
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
    """Re-evaluate every item; only when all pass, bundle HEAD and write the candidate report."""
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

        workspace = p.plan.workspace
        head = _git(workspace, "rev-parse", "HEAD")
        branch = subprocess.run(["git", "-C", workspace, "symbolic-ref", "--short", "-q", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        out_dir = Path(args.out).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        bundle = out_dir / f"{p.plan.attempt_id}.bundle"
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
            "quiescent": True, "plan_hash": p.plan.plan_hash, "observations": observations,
        }
        report_bytes = (json.dumps(report_doc, sort_keys=True, indent=2) + "\n").encode("utf-8")
        from core.state_graph import StateConflict
        from core.state_report_integrity import validate_external_semantics
        try:
            validate_external_semantics(report_bytes, "candidate", head)
        except StateConflict as exc:
            raise Refusal("report_rejected", str(exc)) from exc
        report_path = out_dir / f"{p.plan.attempt_id}-candidate.json"
        _write_atomic(report_path, report_bytes)
        return {"candidate": True, "artifact": head, "artifact_kind": "git-sha1",
                "report": str(report_path), "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
                "git_bundle": report_doc["git_bundle"], "plan_hash": p.plan.plan_hash,
                "brief": p.run.brief(max_chars=_BRIEF_CHARS)}


COMMANDS = {"templates": cmd_templates, "create": cmd_create, "status": cmd_status, "begin": cmd_begin, "done": cmd_done,
            "report": cmd_report, "handback": cmd_handback, "finish": cmd_finish, "list": cmd_list}


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", help="plan directory (default: $AITELIER_DSH_STATE_DIR/todo)")
    common.add_argument("--now", type=float,
                        help="fix the clock (seconds since epoch). TESTS ONLY: refused unless "
                             "AITELIER_TODO_CLOCK_OVERRIDE=1, since the clock decides when an absence expires")
    parser = _Parser(prog="python -m core.todo_cli")
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
                          "help": "take over a plan bound to another session; only after that session is known dead"}))
    command("done", attempt, item)
    command("report", attempt, item, ("--note", {"required": True}))
    command("handback", attempt)
    command("finish", attempt, ("--out", {"required": True, "metavar": "DIR"}))
    command("list")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.now is not None and os.environ.get("AITELIER_TODO_CLOCK_OVERRIDE") != "1":
            raise Refusal("clock_override_disabled", "--now is for tests; set AITELIER_TODO_CLOCK_OVERRIDE=1 to use it")
        root = args.store or (os.path.join(os.environ["AITELIER_DSH_STATE_DIR"], "todo")
                              if os.environ.get("AITELIER_DSH_STATE_DIR") else None)
        if root is None and args.command != "templates":
            raise Refusal("usage", "no --store and AITELIER_DSH_STATE_DIR is not set")
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
