"""A todo batch: K finders in parallel, one verifier per finding, a deterministic fan-in - ONE State attempt.

A batch extends the plan of one State attempt; it adds no State record and no attempt.
It is expanded from a `kind: batch` repository template (`todo_templates/find_verify.yaml`)
and reuses the plan machinery for every child: each sub-plan is an ordinary frozen
`TodoPlan` (`dsh_todo/plan.py`) whose `batch` names the attempt, bound to one child
session at `begin` and judged by its own predicates at `done`. What this module adds is
only what a fan-out needs on top of that:

* The record `<store>/<attempt_id>.batch.json`: the template (with its sha256 and the
  parsed document, so expansion never depends on the file staying put), the params, the
  budget, and the sub-plans of each stage with their rendered child prompts. The frozen
  part hashes to `batch_hash`: re-creating the same batch is idempotent, a different one is
  refused.
* Sub-plan ids `<attempt_id>~find-<key>` / `<attempt_id>~verify-<key>`. A find key is the
  dimension normalised (`slug`): dimensions that normalise to the same key - differing
  only in spacing, case or punctuation - are refused as duplicates when the batch is
  created, so two finders can never share a plan file. A verify key is
  `<find key>_<finding id>`; finding ids are plain and unique per report
  (`dsh_todo.runtime.findings_problem`), so verify keys cannot collide either.
* Sub-plans evaluate in the batch's report directory `<store>/batch/<attempt_id>/`, not in
  the reviewed checkout: children only write reports there, nothing is committed, and the
  checkout the batch attempt delivers stays exactly as reviewed. A sub-plan therefore has
  no appended `deliver`; the batch attempt's own `finish` delivers once.
* Expansion (`expand`) only after EVERY find sub-plan has passed: the find reports are read
  again (and re-checked), findings with the same (file, line, claim) across finders are
  merged with every source kept, the first `cap` (bounded by `max_children`) become verify
  sub-plans and the rest are recorded as `unverified` - listed in the summary, never dropped.
* Binding (`check_binding`, called by `todo_cli begin` under the batch lock): one session
  holds at most one sub-plan of a batch, ever; a verify sub-plan is refused to any session
  that is or was bound to a find sub-plan of the batch (`distinct_from`); and a verify
  sub-plan is bound - or REBOUND - only while every find report it cites is on disk and
  still contains that finding.
  "Never read the find report" cannot be enforced; one session per sub-plan per batch, for
  good, is the approximation of a verifier's independence.
* Budget, frozen in the record: `max_children` (finders + cap), `max_parallel` finders,
  `max_parallel_verify` verifiers (concurrent containers; v1 counts concurrent sub-plans per
  stage, offered or bound and not passed, and cuts `dispatch` to the free slots), and
  `deadline_minutes`, which only ever produces a notice (`deadline_passed`, `overdue`).
* Dispatch is offered once: `status(record=True)` (what the runtime's tools call) records
  each sub-plan it puts in `dispatch` with the session it replaces; an offered sub-plan
  waits in `awaiting_begin` until its child binds, and is offered again only when the
  session it is bound to is gone from the live set the runtime passes - never on a timer,
  never while that session is live.
* Prompt integrity: every rendered prompt is stored with its sha256 and returned with it;
  `begin --prompt-sha256` records the sha the child says it was given in `binding_log`, so a
  pilot can compare it with the runtime's bytes. Stored and exposed, not enforced.
* Fan-in (`write_fan_in`): a summary JSON derived from the record and the verify reports
  only, with top-level `"evidence": false` - it is not evidence; the director records
  evidence from each verify report. Written once; re-running it on the same reports is
  byte-identical, and reports that changed after the summary was written are refused.
* Completion (`completion`) reads every sub-plan's PERSISTED observations; it never runs a
  predicate again, so finishing a batch costs a git bundle, not every child's predicates.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from dsh_todo import templates
from dsh_todo.plan import (_ID, _SHA, _SUB_ID, PlanStore, Refusal, TodoPlan, item_state, new_document,
                           require_library_version)
from dsh_todo.runtime import VERDICTS, PredicateLibrary, findings_problem

RECORD_SUFFIX = ".batch"
_SLUG_CHARS = 48
_BRIEF_CHARS = 600
#: `finish` on a batch runs no predicate; its bound covers the git bundle and its verification.
FINISH_BUDGET_SECONDS = 300


def record_key(attempt_id: str) -> str:
    """The store key of a batch's record: `<attempt_id>.batch` -> `<store>/<attempt_id>.batch.json`."""
    return attempt_id + RECORD_SUFFIX


def parse_subplan(attempt_id: Any) -> tuple[str, str, str] | None:
    """(batch attempt id, stage, key) of a sub-plan id, or None for any other id."""
    match = _SUB_ID.match(attempt_id) if isinstance(attempt_id, str) else None
    return match.groups() if match else None  # type: ignore[return-value]


def slug(text: str) -> str:
    """A dimension as a sub-plan key: whitespace collapsed, lowercased, every run of other characters one '-'."""
    key = re.sub(r"[^a-z0-9]+", "-", " ".join(text.split()).lower()).strip("-")
    return key[:_SLUG_CHARS].strip("-")


def reports_dir(store: PlanStore, attempt_id: str) -> Path:
    return store.root / "batch" / attempt_id


#: How keys are derived, recorded in every batch record so a reader knows what "the same" meant.
NORMALISATION = {
    "dimension_key": "whitespace collapsed, lowercased, every run of characters outside [a-z0-9] -> '-', "
                     "trimmed, at most 48 chars; two dimensions with one key are refused at freeze",
    "finding_id": "[A-Za-z0-9][A-Za-z0-9._-]{0,63}, unique per report case-insensitively (findings_shape)",
    "verify_key": "<dimension key>_<finding id lowercased>",
    "dedup_key": "(file as a normalised relative POSIX path, line or null, claim with whitespace collapsed); "
                 "case is kept",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def exists(store: PlanStore, attempt_id: str) -> bool:
    return store.read(record_key(attempt_id)) is not None


def read_record(store: PlanStore, attempt_id: str) -> dict:
    record = store.read(record_key(attempt_id))
    if record is None:
        raise Refusal("no_such_batch", f"no batch for attempt {attempt_id!r} in {store.root}")
    if record.get("kind") != "batch":
        raise Refusal("corrupt_batch", f"{store.path(record_key(attempt_id))} is not a batch record")
    return record


def _template(record: Mapping[str, Any]) -> "templates.Template":
    """The template the batch was frozen with, rebuilt from the copy the record holds."""
    meta = record["template"]
    template = templates.batch_template_from_doc(meta["name"], record["template_doc"], meta["sha256"])
    if hashlib.sha256(_canonical(record["template_doc"]).encode()).hexdigest() != record["template_doc_sha256"]:
        raise Refusal("corrupt_batch", f"batch {record['attempt_id']}: the stored template does not match its digest")
    return template


def _require_library(record: Mapping[str, Any], library: PredicateLibrary) -> None:
    if record.get("library_version") != library.version:
        raise Refusal("library_mismatch", f"batch {record.get('attempt_id')!r} was frozen under predicate library "
                                          f"v{record.get('library_version')}; this build runs v{library.version}",
                      plan_library_version=record.get("library_version"), running_library_version=library.version)


def _write_subplan(store: PlanStore, library: PredicateLibrary, plan: TodoPlan, record: Mapping[str, Any],
                   stage: str, now: float) -> None:
    with store.lock(plan.attempt_id):
        existing = store.read(plan.attempt_id)
        if existing is not None:
            if existing.get("plan_hash") != plan.plan_hash:
                raise Refusal("plan_differs", f"sub-plan {plan.attempt_id} already has a different frozen plan",
                              plan_hash=existing.get("plan_hash"), offered_plan_hash=plan.plan_hash)
            return
        store.write(plan.attempt_id, new_document(plan, library, now, {**record["template"], "stage": stage},
                                                  record["params"]))


def _subplan(store: PlanStore, library: PredicateLibrary, sub_id: str) -> tuple[dict, TodoPlan]:
    """A sub-plan's document and frozen plan, checked the way every plan load is checked."""
    document = store.read(sub_id)
    if document is None:
        raise Refusal("corrupt_batch", f"sub-plan {sub_id} is listed by its batch but has no plan file")
    require_library_version(document, library)
    try:
        plan = TodoPlan.from_stored(document["plan"], library)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise Refusal("corrupt_plan", f"sub-plan {sub_id} is damaged: {type(exc).__name__}: {str(exc)[:200]}") from exc
    if plan.plan_hash != document["plan_hash"]:
        raise Refusal("corrupt_plan", f"sub-plan {sub_id} does not match its plan_hash")
    return document, plan


def _bound_arg(plan: TodoPlan, predicate: str, arg: str) -> Any:
    for item in plan.items:
        for binding in item.predicates:
            if binding.predicate == predicate:
                return binding.args[arg]
    raise Refusal("corrupt_batch", f"sub-plan {plan.attempt_id} binds no {predicate}")


# -- create -----------------------------------------------------------------

def create(store: PlanStore, library: PredicateLibrary, template: "templates.Template", supplied: Any, *,
           attempt_id: str, node_key: str, context_hash: str, base_sha: str, workspace: str, goal: str,
           now: float) -> dict:
    """Freeze the batch and its find sub-plans; idempotent for the same input, refused for a different one."""
    if template.kind != "batch":
        raise Refusal("not_a_batch_template", f"template {template.name} is an ordinary plan template: use todo_plan")
    if not isinstance(attempt_id, str) or not _ID.match(attempt_id) or attempt_id.endswith(RECORD_SUFFIX):
        raise Refusal("invalid_attempt", f"not a usable attempt id: {attempt_id!r}")
    if not isinstance(base_sha, str) or not _SHA.match(base_sha):
        raise Refusal("invalid_plan", "base_sha must be a full lowercase commit hash")
    if not isinstance(workspace, str) or not os.path.isabs(workspace):
        raise Refusal("invalid_plan", "workspace must be an absolute path")
    params = templates.resolve_params(template, supplied)
    # finish bundles exactly this commit, so it must BE a commit: a symbolic name
    # (HEAD, a branch) moves with the checkout and would certify whatever is there.
    reviewed = params.get("reviewed_sha")
    if not isinstance(reviewed, str) or not _SHA.match(reviewed):
        raise Refusal("bad_param", f"reviewed_sha must be a full lowercase commit hash, not {reviewed!r}: "
                                   "finish bundles exactly the commit the verifiers certified", param="reviewed_sha")
    dimensions = params[template.stage("find")["per"]]
    if not dimensions:
        raise Refusal("bad_param", f"{template.stage('find')['per']} must name at least one finder")
    finders: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for text in dimensions:
        key = slug(text)
        if not key:
            raise Refusal("bad_param", f"dimension {text!r} has no letter or digit to name its finder by")
        if key in seen:
            raise Refusal("duplicate_dimension", f"dimensions {seen[key]!r} and {text!r} name the same finder "
                                                 f"{key!r}: they differ only in spacing, case or punctuation",
                          dimensions=[seen[key], text], key=key)
        seen[key] = text
        finders.append((key, " ".join(text.split())))
    cap = templates.resolve_cap(template, params)
    budget = {**(template.budget or {}), "max_children": len(finders) + cap}
    template_doc = templates.batch_doc(template)
    frozen = {"attempt_id": attempt_id, "node_key": node_key, "context_hash": context_hash, "base_sha": base_sha,
              "workspace": workspace, "goal": goal, "params": params, "budget": budget,
              "template": {"name": template.name, "version": template.version, "sha256": template.sha256}}
    batch_hash = hashlib.sha256(_canonical(frozen).encode()).hexdigest()
    reports = reports_dir(store, attempt_id)
    record = {"schema": 1, "kind": "batch", "library_version": library.version, "batch_hash": batch_hash, **frozen,
              "template_doc": template_doc,
              "template_doc_sha256": hashlib.sha256(_canonical(template_doc).encode()).hexdigest(),
              "reports_dir": str(reports), "fan_in": template.fan_in["report"], "created_at": now,
              "normalisation": NORMALISATION, "find": [], "verify": None, "unverified": [], "expanded_at": None,
              "bindings": {}, "binding_log": [], "offers": {}}
    plans = []
    for key, text in finders:
        sub = f"{attempt_id}~find-{key}"
        report = templates.stage_report(template, "find", key)
        plans.append(TodoPlan.from_json(templates.stage_plan_data(
            template, params, "find", batch=attempt_id, subplan=sub, item=key, node_key=node_key,
            context_hash=context_hash, base_sha=base_sha, workspace=str(reports), goal=goal), library))
        prompt = templates.render_prompt(template, params, "find", {
            "batch": attempt_id, "subplan": sub, "item": key, "report": str(reports / report),
            "workspace": workspace, "dimension": text})
        record["find"].append({"id": sub, "item": key, "dimension": text, "report": report,
                               "prompt": prompt, "prompt_sha256": _sha(prompt)})
    with store.lock(record_key(attempt_id)):
        existing = store.read(record_key(attempt_id))
        if existing is not None:
            if existing.get("batch_hash") != batch_hash:
                raise Refusal("batch_differs", f"attempt {attempt_id} already has a different frozen batch",
                              batch_hash=existing.get("batch_hash"), offered_batch_hash=batch_hash)
            created = False
        else:
            if store.read(attempt_id) is not None:
                raise Refusal("plan_exists", f"attempt {attempt_id} already has an ordinary plan; one attempt is "
                                             "either a plan or a batch")
            reports.mkdir(parents=True, exist_ok=True)
            for plan in plans:
                _write_subplan(store, library, plan, record, "find", now)
            store.write(record_key(attempt_id), record)
            created = True
    return {"created": created, **status(store, library, attempt_id, now, record=True)}


# -- status -----------------------------------------------------------------

def _row(store: PlanStore, library: PredicateLibrary, record: Mapping[str, Any], stage: str, entry: Mapping[str, Any],
         live: set[str] | None) -> dict:
    document, plan = _subplan(store, library, entry["id"])
    states = [item_state(document["items"][i.id]) for i in plan.items]
    state = ("pass" if all(s == "pass" for s in states) else "unmeasurable" if "unmeasurable" in states
             else "fail" if "fail" in states else "open")
    session = document["child_session"]
    return {"id": entry["id"], "stage": stage, "item": entry["item"], "state": state, "child_session": session,
            "session_gone": bool(session) and live is not None and session not in live,
            "report": str(Path(record["reports_dir"]) / entry["report"]), "prompt": entry["prompt"],
            "prompt_sha256": entry["prompt_sha256"],
            "offered": (record["offers"].get(entry["id"]) or {}).get("session", False) == session}


def status(store: PlanStore, library: PredicateLibrary, attempt_id: str, now: float,
           live: set[str] | None = None, record: bool = False) -> dict:
    """Where the batch stands, and what the director dispatches next.

    `live` is the set of sessions known to be running (the runtime passes its live children and
    itself). A sub-plan bound to a session outside it is not in flight: it is listed in
    `redispatch` with its prompt and offered again in `dispatch`. Without `live` every bound
    sub-plan counts as in flight. `dispatch` is cut to the free slots of the active stage
    (`max_parallel` finders, `max_parallel_verify` verifiers); the rest are `pending`. With
    `record`, the sub-plans put in `dispatch` are recorded as offered (under the batch lock), so
    the next call lists them in `awaiting_begin` instead of offering them twice.
    """
    if record:
        with store.lock(record_key(attempt_id)):
            view = _status(store, library, attempt_id, now, live)
            if view["dispatch"]:
                stored = read_record(store, attempt_id)
                for entry in view["dispatch"]:
                    stored["offers"][entry["subplan"]] = {"session": entry.get("replaces_session"), "at": now,
                                                          "prompt_sha256": entry["prompt_sha256"]}
                store.write(record_key(attempt_id), stored)
            return view
    return _status(store, library, attempt_id, now, live)


def _status(store: PlanStore, library: PredicateLibrary, attempt_id: str, now: float,
            live: set[str] | None) -> dict:
    record = read_record(store, attempt_id)
    _require_library(record, library)
    find = [_row(store, library, record, "find", e, live) for e in record["find"]]
    verify = [_row(store, library, record, "verify", e, live) for e in record["verify"] or []]
    find_passed = all(r["state"] == "pass" for r in find)
    expanded = record["verify"] is not None
    complete = find_passed and expanded and all(r["state"] == "pass" for r in verify)
    active = "find" if not find_passed else "verify" if expanded and not complete else None
    budget = record["budget"]
    rows = find if active == "find" else verify if active == "verify" else []
    limit = budget["max_parallel"] if active == "find" else budget["max_parallel_verify"]
    open_rows = [r for r in rows if r["state"] != "pass"]
    in_flight = [r for r in open_rows if r["child_session"] and not r["session_gone"]]
    # Offered for the session it is bound to now (None: unbound): its child has not begun yet.
    awaiting = [r for r in open_rows if r not in in_flight and r["offered"]]
    waiting = [r for r in open_rows if r["session_gone"] and not r["offered"]] + \
        [r for r in open_rows if not r["child_session"] and not r["offered"]]
    slots = max(0, limit - len(in_flight) - len(awaiting))
    bound_sha = {(e["subplan"], e["session"]): e["prompt_sha256"] for e in record["binding_log"]}
    deadline_at = record["created_at"] + budget["deadline_minutes"] * 60
    deadline_passed = not complete and now >= deadline_at
    view = {
        "attempt_id": attempt_id, "kind": "batch", "node_key": record["node_key"], "batch_hash": record["batch_hash"],
        "template": record["template"], "goal": record["goal"], "workspace": record["workspace"],
        "reports_dir": record["reports_dir"], "budget": budget,
        "stage": active or ("complete" if complete else "expand"),
        "expand_due": find_passed and not expanded, "expanded": expanded, "complete": complete,
        "subplans": [{**{k: r[k] for k in ("id", "stage", "item", "state", "child_session", "session_gone", "report",
                                           "prompt_sha256")},
                      "bound_prompt_sha256": bound_sha.get((r["id"], r["child_session"]))} for r in find + verify],
        "dispatch": [{"subplan": r["id"], "prompt": r["prompt"], "prompt_sha256": r["prompt_sha256"],
                      **({"replaces_session": r["child_session"]} if r["session_gone"] else {})} for r in waiting[:slots]],
        "pending": [r["id"] for r in waiting[slots:]],
        "in_flight": [r["id"] for r in in_flight],
        "awaiting_begin": [{"subplan": r["id"], "prompt": r["prompt"], "prompt_sha256": r["prompt_sha256"]}
                           for r in awaiting],
        "redispatch": [{"subplan": r["id"], "stage": r["stage"], "dead_session": r["child_session"], "prompt": r["prompt"],
                        "prompt_sha256": r["prompt_sha256"], "already_offered": r["offered"]}
                       for r in find + verify if r["state"] != "pass" and r["session_gone"]],
        "unverified": len(record["unverified"]),
        "deadline_at": deadline_at, "deadline_passed": deadline_passed,
        "overdue": [r["id"] for r in find + verify if r["state"] != "pass"] if deadline_passed else [],
        "finish_budget_seconds": FINISH_BUDGET_SECONDS,
        "fan_in": str(Path(record["reports_dir"]) / record["fan_in"]),
    }
    passed = lambda rows_: sum(r["state"] == "pass" for r in rows_)  # noqa: E731
    next_step = ("dispatch " + ", ".join(d["subplan"] for d in view["dispatch"]) if view["dispatch"]
                 else "expand the verify stage (todo_batch_status)" if view["expand_due"]
                 else "todo_finish" if complete
                 else "wait for " + ", ".join(view["in_flight"] + [a["subplan"] for a in view["awaiting_begin"]])
                 if in_flight or awaiting else "(none)")
    view["brief"] = (f"{record['goal'][:160]}\nBatch {attempt_id} ({record['template']['name']}): stage {view['stage']}; "
                     f"find {passed(find)}/{len(find)} passed; verify "
                     + (f"{passed(verify)}/{len(verify)} passed, {view['unverified']} unverified" if expanded else "not expanded")
                     + (f"; deadline passed, overdue: {', '.join(view['overdue'])}" if deadline_passed else "")
                     + f"\nNext: {next_step}")[:_BRIEF_CHARS]
    return view


def list_row(store: PlanStore, library: PredicateLibrary, attempt_id: str, now: float) -> dict | None:
    """The batch as a `list` row - only while the next step is the director's own.

    Its sub-plans have rows of their own (they carry `batch`) while children work them, so the
    batch adds a row only when no sub-plan stands for that work: when the verify stage is due
    to be expanded (`remaining: ["expand"]`), and when everything passed (`remaining: []`, so the
    runtime's ready-to-finish view reminds the director to run `todo_finish`).
    """
    view = status(store, library, attempt_id, now)
    if not (view["expand_due"] or view["complete"]):
        return None
    return {"attempt_id": attempt_id, "kind": "batch", "node_key": view["node_key"], "plan_hash": view["batch_hash"],
            "next": "expand" if view["expand_due"] else None, "remaining": ["expand"] if view["expand_due"] else [],
            "done": [s["id"] for s in view["subplans"] if s["state"] == "pass"], "unmeasurable": [],
            "child_session": None}


# -- expand -----------------------------------------------------------------

def _dedup_key(finding: Mapping[str, Any]) -> tuple:
    return (PurePosixPath(str(finding.get("file", "")).strip()).as_posix(), finding.get("line"),
            " ".join(str(finding.get("claim", "")).split()))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def expand(store: PlanStore, library: PredicateLibrary, attempt_id: str, now: float,
           live: set[str] | None = None, record_dispatch: bool = False) -> dict:
    """Expand the verify stage once every find sub-plan has passed; a second call changes nothing."""
    with store.lock(record_key(attempt_id)):
        record = read_record(store, attempt_id)
        _require_library(record, library)
        if record["verify"] is None:
            _expand(store, library, record, now)
    return status(store, library, attempt_id, now, live, record=record_dispatch)


def _expand(store: PlanStore, library: PredicateLibrary, record: dict, now: float) -> None:
    attempt_id = record["attempt_id"]
    unfinished = [r["id"] for r in (_row(store, library, record, "find", e, None) for e in record["find"])
                  if r["state"] != "pass"]
    if unfinished:
        raise Refusal("find_unfinished", f"{len(unfinished)} find sub-plan(s) have not passed; the verify stage "
                                         "expands only after every finder passed", unfinished=unfinished)
    template = _template(record)
    reports = Path(record["reports_dir"])
    merged: dict[tuple, dict] = {}
    order: list[dict] = []
    for entry in record["find"]:
        _document, plan = _subplan(store, library, entry["id"])
        data = _read_json(reports / entry["report"])
        problem = findings_problem(data, _bound_arg(plan, "findings_shape", "max"))
        if problem:
            raise Refusal("find_report_changed", f"{entry['id']}: {entry['report']} no longer has the shape it passed "
                                                 f"with ({problem}); nothing was expanded")
        for finding in data["findings"]:
            source = {"find": entry["id"], "report": entry["report"], "id": finding["id"]}
            key = _dedup_key(finding)
            if key in merged:                    # the same (file, line, claim): one verifier, every source kept
                merged[key]["sources"].append(source)
                continue
            merged[key] = {"finding_id": f"{entry['item']}_{finding['id'].lower()}", "file": key[0],
                           "line": finding.get("line"), "claim": finding["claim"],
                           "failure_scenario": finding["failure_scenario"], "sources": [source]}
            order.append(merged[key])
    allowed = max(0, min(templates.resolve_cap(template, record["params"]),
                         record["budget"]["max_children"] - len(record["find"])))
    entries = []
    for finding in order[:allowed]:
        key = finding["finding_id"]
        sub = f"{attempt_id}~verify-{key}"
        report = templates.stage_report(template, "verify", key)
        plan = TodoPlan.from_json(templates.stage_plan_data(
            template, record["params"], "verify", batch=attempt_id, subplan=sub, item=key,
            node_key=record["node_key"], context_hash=record["context_hash"], base_sha=record["base_sha"],
            workspace=str(reports), goal=record["goal"]), library)
        shown = {k: finding[k] for k in ("finding_id", "file", "line", "claim", "failure_scenario")}
        prompt = templates.render_prompt(template, record["params"], "verify", {
            "batch": attempt_id, "subplan": sub, "item": key, "report": str(reports / report),
            "workspace": record["workspace"], "finding": json.dumps(shown, sort_keys=True, ensure_ascii=False),
            "finding_id": key, "find_report": str(reports / finding["sources"][0]["report"])})
        _write_subplan(store, library, plan, record, "verify", now)
        entries.append({"id": sub, "item": key, "report": report, "prompt": prompt, "prompt_sha256": _sha(prompt),
                        "finding": finding})
    record["verify"] = entries
    record["unverified"] = [{**f, "reason": f"beyond the verify cap: {allowed} verifier(s) for {len(order)} finding(s)"}
                            for f in order[allowed:]]
    record["expanded_at"] = now
    store.write(record_key(attempt_id), record)


# -- binding ----------------------------------------------------------------

def check_binding(record: Mapping[str, Any], sub_id: str, session: str) -> None:
    """Refuse binding `session` to `sub_id` unless the batch's binding rules allow it (see the module doc)."""
    stage = parse_subplan(sub_id)[1]
    entries = {e["id"]: e for e in record["find"] + (record["verify"] or [])}
    if sub_id not in entries:
        raise Refusal("unknown_subplan", f"{sub_id} is not a sub-plan of batch {record['attempt_id']}")
    held = record["bindings"].get(session)
    finders = {e["id"] for e in record["find"]}
    if stage == "verify" and held in finders:
        raise Refusal("distinct_from", f"session {session} was bound to finder {held}; a verify sub-plan of this batch "
                                       "must be worked by a session that found nothing in it", bound_subplan=held)
    if held is not None and held != sub_id:
        raise Refusal("session_holds_other_subplan", f"session {session} is bound to {held}; one session works one "
                                                     "sub-plan of a batch", bound_subplan=held)
    if stage == "verify":
        _check_cited(record, entries[sub_id]["finding"])


def _check_cited(record: Mapping[str, Any], finding: Mapping[str, Any]) -> None:
    """Every find report the finding cites is on disk and still contains it."""
    key = _dedup_key(finding)
    for source in finding["sources"]:
        path = Path(record["reports_dir"]) / source["report"]
        if not path.is_file():
            raise Refusal("find_report_missing", f"{path} is gone: the finding {finding['finding_id']} it reported "
                                                 "cannot be verified against a report nobody can read",
                          find_report=str(path))
        data = _read_json(path)
        findings = data.get("findings") if isinstance(data, dict) else None
        if not isinstance(findings, list) or not any(
                isinstance(f, dict) and f.get("id") == source["id"] and _dedup_key(f) == key for f in findings):
            raise Refusal("finding_not_in_report", f"{path} no longer contains finding {source['id']} "
                                                   f"({finding['file']}: {finding['claim'][:80]})", find_report=str(path))


def record_binding(store: PlanStore, record: dict, sub_id: str, session: str, now: float,
                   prompt_sha256: str | None = None) -> None:
    """Remember the binding for good: a session that held a sub-plan keeps that history in the batch.

    `binding_log` gets one entry per first binding of a session, with the prompt sha the child
    says it was given (or None) - stored and exposed for a pilot to compare, never enforced."""
    if record["bindings"].get(session) != sub_id:
        record["bindings"][session] = sub_id
        record["binding_log"].append({"session": session, "subplan": sub_id, "at": now, "prompt_sha256": prompt_sha256})
        store.write(record_key(record["attempt_id"]), record)


# -- completion and fan-in --------------------------------------------------

def completion(store: PlanStore, library: PredicateLibrary, record: Mapping[str, Any]) -> tuple[list, list]:
    """(failing, observations) from every sub-plan's persisted observations - no predicate runs here."""
    _require_library(record, library)
    failing: list[dict] = []
    observations: list[dict] = []
    if record["verify"] is None:
        failing.append({"subplan": None, "item": None, "state": "not_expanded",
                        "reason": "the verify stage was never expanded"})
    for entry in record["find"] + (record["verify"] or []):
        document, plan = _subplan(store, library, entry["id"])
        for item in plan.items:
            stored = document["items"][item.id]
            state = item_state(stored)
            observations.append({"subplan": entry["id"], "item": item.id, "outcome": stored["outcome"],
                                 "results": stored["results"], "observed_at": stored["observed_at"]})
            if state != "pass":
                failing.append({"subplan": entry["id"], "item": item.id, "state": state})
    return failing, observations


def write_fan_in(store: PlanStore, library: PredicateLibrary, record: Mapping[str, Any]) -> tuple[Path, str, dict]:
    """Write the summary once every sub-plan passed; the caller holds the batch lock."""
    failing, _observations = completion(store, library, record)
    if failing:
        raise Refusal("unfinished", f"{len(failing)} sub-plan item(s) have not passed; no summary was written",
                      failing=failing)
    reports = Path(record["reports_dir"])
    findings = []
    for entry in record["verify"]:
        _document, plan = _subplan(store, library, entry["id"])
        artifact = _bound_arg(plan, "verdict_enum", "artifact_ref")
        path = reports / entry["report"]
        data = _read_json(path)
        if not (isinstance(data, dict) and data.get("verdict") in VERDICTS and data.get("finding_id") == entry["item"]
                and data.get("artifact_ref") == artifact):
            raise Refusal("verify_report_changed", f"{path} no longer carries the verdict its sub-plan passed with "
                                                   f"(finding {entry['item']}, artifact {artifact})")
        finding = entry["finding"]
        findings.append({**{k: finding[k] for k in ("finding_id", "file", "line", "claim", "failure_scenario")},
                         "sources": [{**s, "report": str(reports / s["report"])} for s in finding["sources"]],
                         "verify_subplan": entry["id"], "verify_report": str(path), "verdict": data["verdict"],
                         "artifact_ref": data["artifact_ref"], "verify_evidence": data.get("evidence")})
    unverified = [{**{k: f[k] for k in ("finding_id", "file", "line", "claim", "failure_scenario", "reason")},
                   "sources": [{**s, "report": str(reports / s["report"])} for s in f["sources"]]}
                  for f in record["unverified"]]
    counts = {v: sum(f["verdict"] == v for f in findings) for v in VERDICTS}
    summary = {"evidence": False,
               "note": "Not evidence. Each verify report is: record_evidence from verify_report/artifact_ref yourself.",
               "batch": record["attempt_id"], "batch_hash": record["batch_hash"], "node_key": record["node_key"],
               "template": record["template"], "findings": findings, "unverified": unverified,
               "counts": {**counts, "unverified": len(unverified)}}
    data = (json.dumps(summary, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    target = reports / record["fan_in"]
    if target.is_file():
        if target.read_bytes() != data:
            raise Refusal("fan_in_differs", f"{target} was already written from other reports; the verify reports "
                                            "changed after the summary was made")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    return target, hashlib.sha256(data).hexdigest(), summary
