"""A driver-facing boundary: choose work in StateGraph, execute in SkillFlow.

No scheduler, retry engine, implicit approval or universal status setter. Launch
uses the host's existing launcher; an interrupted launch is recovered by its
persisted execution-project identity, never by starting an uncorrelated run.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from core.state_graph import StateConflict, StateGraphError, StateGraphStore, canonical, digest, key, text
from core.state_attempts import StateAttempts, artifact_ref




SEED_HEADING = "# State goal attempt"


def state_seed_text(context: dict, dependency_receipts, relay: bool) -> str:
    """The seed file a launched attempt reads.

    FORMAT CONTRACT, and it has exactly one consumer's assumption behind it:
    ``SEED_HEADING``, then ONE JSON value, then optional trailing prose. The
    generated ``*__prepare_seed`` tool decodes that one value and hands the tail
    to the agent. It used to json.loads() the whole remainder, which meant the
    relay section added below killed every relayed attempt at its first step
    ("Extra data: line 3 column 1"; measured 2026-09-11, release.mainline-green
    r5 relay, state_seed crashed 3x and failed the run before it began).

    So: anything appended after the JSON must be prose the agent can read, and
    nothing may be inserted BETWEEN the heading and the JSON. The reader lives
    outside this repository (generated tools are not tracked), which is why the
    contract is pinned here, by test, rather than left to the two sides to
    agree on implicitly.
    """
    seed = SEED_HEADING + "\n\n" + canonical(
        context | {"accepted_dependencies": dependency_receipts}) + "\n"
    if relay:
        seed += ("\n## Relay\n\nThis attempt CONTINUES a prior attempt that ran out of budget. Its commits are "
                 "already in your repository baseline; recovered code is UNVALIDATED in that worktree, "
                 "and artifact drafts remain in artifact folders. Read `relay.code_changes`, `relay.staged_files` "
                 "and `relay.commits`, verify, finish what is missing, then `finish_step`.\n")
    return seed


class StateService:
    def __init__(self, db, ws=None, sf=None, registry=None, attach_driver=None, actor="local-operator", runtime_factory=None, project_read_trusted=True):
        # Whether the caller behind THIS service may read a project nobody opened.
        # The State HTTP transport sets it False for an unauthenticated visitor; the
        # internal driver, MCP and every direct construction default to True, so the
        # privacy gate in `core.state_commands.execute` only ever NARROWS a surface
        # a caller already had — it can never widen what is public.
        self.project_read_trusted = project_read_trusted
        self.db, self.ws, self.sf, self.registry = db, ws, sf, registry
        self.store = StateGraphStore(db)
        from core.state_driver_notes import StateDriverNotes
        self.driver_notes = StateDriverNotes(self.store, actor)
        from core.state_issues import StateIssues
        self.issues = StateIssues(self.store, actor)
        self.attempts = StateAttempts(self.store)
        from core.state_external import ExternalAttempts
        self.external = ExternalAttempts(self.attempts, actor)
        self.attach_driver = attach_driver
        self.actor = actor
        from core.state_design import StateDesign
        self.design = StateDesign(self.store, actor)
        self.runtime_factory = runtime_factory
        from core.state_portfolio import StatePortfolio
        self.portfolio = StatePortfolio(self.store, actor)
        from core.director_messaging import SQLiteDirectorMessaging
        self.director_messages = SQLiteDirectorMessaging(self.store, actor)

    async def wait_for_state_change(self, project_id, after=0, node_keys=None, attempt_ids=None,
                                    note_after_revision=None, filter_mode="all", actionable_only=True,
                                    timeout_seconds=30.0, limit=100, return_when_idle=False):
        from core.state_changes import wait_for_state_change
        return await wait_for_state_change(
            self, project_id, after, node_keys, attempt_ids, note_after_revision,
            filter_mode, actionable_only, timeout_seconds, limit, return_when_idle)

    def create_project(self, project_id, title, source_project_id=None):
        if source_project_id and not self.db.get_project(source_project_id):
            raise StateGraphError("source_project_id must name an existing AItelier source project")
        return self.store.create_project(project_id, title, source_project_id)

    def open_project(self, project_id):
        """The recorded, writer-only decision to publish a project to anonymous
        readers. Opening happens HERE and nowhere else — no other write path
        reaches it, so creating a project, running a round, verifying a node or
        importing never opens it. It reads back state/who/when and reverses."""
        return self.store.set_project_access(project_id, "public", self.actor)

    def close_project(self, project_id):
        """The recorded decision to withdraw a project from anonymous readers."""
        return self.store.set_project_access(project_id, "private", self.actor)

    def project_visibility(self, project_id):
        """Read back the privacy record: current state, who changed it, when."""
        return self.store.get_project_access(project_id)

    def project_run_summary(self, project_id):
        from core.state_run_summary import project_run_summary
        return project_run_summary(self, project_id)

    def node_context(self, project_id, node_key):
        node = self.store.get_node(project_id, node_key)
        receipts = {}
        with self.store.transaction() as conn:
            for dep in node["dependencies"]:
                d = self.store._node(conn, project_id, dep)
                r = conn.execute("SELECT * FROM state_acceptances WHERE receipt_id=?", (d["verified_receipt"],)).fetchone()
                receipts[dep] = {"goal": d["goal"], "revision": d["revision"], "status": d["status"],
                                 "acceptance": dict(r) if r else None}
            open_issues = self.issues.open_for_node(conn, project_id, node_key)
        return {"node": node, "dependency_receipts": receipts, "open_issues": open_issues,
                "attempts": self.attempts.list(project_id, node_key, limit=10),
                "references": self.portfolio.references(project_id, node_key, limit=100),
                "design": self.design.node_bindings(project_id, node_key)}

    def _components(self):
        # Goal inspection/planning must survive an unavailable executor. Only
        # execution/acceptance operations ask the runtime composition root.
        if self.runtime_factory is not None and (self.sf is None or self.registry is None):
            try:
                self.sf, self.registry = self.runtime_factory()
            except Exception as exc:  # noqa: BLE001 - all probe failures must refuse launch
                raise StateConflict(f"workflow runtime is unavailable: {type(exc).__name__}") from exc
        if self.ws is None or self.sf is None or self.registry is None:
            raise StateGraphError("workflow composition is unavailable")

    def _source(self, project_id):
        binding = self.portfolio.source_binding(project_id)
        if binding:
            path = Path(binding["repo_path"])
            if not path.is_dir():
                raise StateConflict("bound source repository is missing")
            top = self._git(path, "rev-parse", "--show-toplevel")
            common = self._git(path, "rev-parse", "--git-common-dir")
            if str(Path(top).resolve()) != binding["repo_path"] or str((path / common).resolve()) != binding["common_dir"]:
                raise StateConflict("bound source identity changed; do not substitute another repository")
            return binding["repo_path"]
        project = self.store.get_project(project_id)
        source_id = project["source_project_id"]
        if not source_id:
            return None
        source = self.db.get_project(source_id)
        if not source or not source.get("repo_path"):
            raise StateConflict("registered source project no longer has a repository")
        path = Path(source["repo_path"])
        if not path.is_dir():
            raise StateConflict("source repository is missing; do not substitute another checkout")
        return str(path)

    @staticmethod
    def _git(path, *args):
        result = subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=path,
                                capture_output=True, text=True, timeout=20,
                                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
        if result.returncode != 0:
            raise StateConflict("Git could not prove the requested source/artifact property")
        return result.stdout.strip()

    def _dependency_context(self, attempt, source, ref="HEAD"):
        """Pass accepted contracts/artifact references, not an entire project log.

        `ref` is the commit this attempt will actually build on: HEAD for a
        fresh attempt, the failed attempt's branch head for a relay."""
        out = {}
        with self.store.transaction() as conn:
            for dep, pin in attempt["dependencies"].items():
                rec = conn.execute("SELECT * FROM state_acceptances WHERE receipt_id=?", (pin["verified_receipt"],)).fetchone()
                if not rec:
                    raise StateConflict("verified dependency has no acceptance receipt")
                out[dep] = dict(rec)
        # A Git-backed dependency is useful only if this attempt's source
        # actually includes it. Output-bundle digests are non-code dependencies.
        if source:
            for rec in out.values():
                if len(rec["artifact_ref"]) == 40:
                    try:
                        self._git(source, "merge-base", "--is-ancestor", rec["artifact_ref"], ref)
                    except StateConflict as exc:
                        raise StateConflict("accepted dependency commit is not in the source; integrate it before launching") from exc
        return out

    def start_external_attempt(self, project_id, node_key, expected_revision, harness, external_id,
                               request_key, instruction=""):
        """Register external execution scope; never compose or dispatch a workflow."""
        return self.external.register(project_id, node_key, expected_revision, harness, external_id,
                                      request_key, instruction)

    def request_attempt_base(self, attempt_id, base_sha):
        """Choose the commit for an unlaunched SkillFlow attempt's worktree."""
        attempt = self.attempts.get(attempt_id)
        execution_project_id = attempt["execution_project_id"]
        if attempt["execution_kind"] == "external" or execution_project_id is None:
            raise StateConflict(
                "external attempt has no execution project; a base can be chosen only for a non-external attempt")
        if attempt["status"] != "reserved" or attempt["run_id"]:
            raise StateConflict(
                "attempt is past the dispatch window; a base can be chosen only while status=reserved "
                f"with no run (status={attempt['status']}, run_id={attempt['run_id']!r})")
        from core import run_isolation
        from skillflow.exceptions import IsolationUnavailable
        try:
            request = run_isolation.request_base(
                self.db, execution_project_id, base_sha,
                note=f"director-chosen base for State attempt {attempt_id}")
        except IsolationUnavailable as exc:
            raise StateConflict(str(exc)) from exc
        return {"attempt_id": attempt_id, "execution_project_id": execution_project_id,
                "status": attempt["status"], "base_sha": request["base_sha"]}

    def report_external_attempt(self, attempt_id, observation_id, expected_version, context_hash,
                                status, report_ref, report_sha256, quiescent=False,
                                artifact=None, artifact_kind=None, detail=""):
        return self.external.observe(attempt_id, observation_id, expected_version, context_hash,
                                     status, report_ref, report_sha256, quiescent=quiescent,
                                     artifact=artifact, artifact_kind=artifact_kind, detail=detail)

    def start_attempt(self, project_id, node_key, expected_revision, workflow, request_key, instruction="",
                      base_sha=None, continue_from=None, relay_digest=None, frozen_prerequisites=None):
        if relay_digest is not None and continue_from is None:
            raise StateGraphError("relay_digest only accompanies continue_from")
        if continue_from is not None and relay_digest is None:
            raise StateGraphError("continue_from requires relay_digest: read the failed attempt's relay_inventory "
                                  "and pass its digest, so the relay is bound to the draft you inspected")
        if base_sha is not None:
            if continue_from is not None:
                raise StateGraphError("base_sha applies to a fresh attempt; continue_from already determines the relay base")
            from core import run_isolation
            from skillflow.exceptions import IsolationUnavailable
            try:
                run_isolation.validate_base_sha(base_sha)
            except IsolationUnavailable as exc:
                raise StateGraphError(str(exc)) from exc
        from core.state_metadata import require_dispatch
        with self.store.transaction() as conn:
            self.store._node(conn, project_id, node_key)
            require_dispatch(conn, project_id, node_key)
        self._components()
        manifest = self.registry.get(key(workflow, "workflow"))
        if manifest is None:
            raise StateGraphError("unknown workflow; use list_pipelines")
        if not manifest.seed_file:
            raise StateGraphError("this workflow has no seed-file contract; use a seeded node workflow")
        source = self._source(project_id)
        if manifest.repo_mode == "code" and not source:
            raise StateGraphError("code-producing attempts require a registered source_project_id")
        if base_sha is not None and manifest.repo_mode != "code":
            raise StateGraphError("base_sha requires a code-producing workflow with an isolated worktree")
        # All state writes below use a separate intent identity. No old DPE rows
        # are reused as the long-lived state project.
        if continue_from is not None and manifest.repo_mode != "code":
            raise StateGraphError("continue_from needs a code-producing workflow; only a worktree can carry a draft forward")
        attempt = self.attempts.reserve(project_id, node_key, expected_revision, workflow, request_key, instruction,
                                        continue_from=continue_from, relay_digest=relay_digest,
                                        frozen_prerequisites=frozen_prerequisites, base_sha=base_sha)
        return self._launch_or_recover(attempt, manifest, source)

    def _launch_or_recover(self, attempt, manifest, source):
        aid = attempt["attempt_id"]
        if attempt["run_id"]:
            return self.reconcile_attempt(aid)
        # The standard launcher can finish after its HTTP caller disappears.
        # One durable execution project identifies that run on a later request.
        matches = self.sf.list_runs(project_id=attempt["execution_project_id"])
        if len(matches) > 1:
            raise StateConflict("multiple runs occupy this attempt's execution project; operator reconciliation required")
        if matches:
            self.attempts.bind_run(aid, matches[0]["id"], self.sf)
            return self._attach_and_observe(aid, manifest)
        if attempt["status"] in {"launching", "unknown"}:
            return {**attempt, "recovery_required": True,
                    "note": "Launch outcome is unknown. No duplicate run was started; retain and inspect the execution project."}
        if attempt["status"] != "reserved":
            return attempt
        if attempt["context"].get("base_sha"):
            self.request_attempt_base(aid, attempt["context"]["base_sha"])
        attempt = self.attempts.pin_host_contract(aid, {
            "source_repo": source, "seed_file": manifest.seed_file, "output_step": manifest.output_step,
            "scheduler_owned": bool(manifest.scheduler_owned), "repo_mode": manifest.repo_mode})
        prerequisites = attempt["context"].get("frozen_prerequisites")
        if prerequisites is not None:
            trace = []
            try:
                from core.frozen_attempt import materialize
                report = materialize(prerequisites, source=source, sf=self.sf,
                                     trace=trace.append)
                # A successful source identity check must also pin the future
                # worktree. Comparing HEAD and then launching from a moving HEAD
                # would leave a time-of-check/time-of-use gap.
                source_checks = [c for c in report["checks"]
                                 if c.get("probe") == "source_head"]
                if (source_checks and manifest.repo_mode == "code"
                        and not attempt["context"].get("base_sha")):
                    if len(source_checks) != 1:
                        raise StateConflict("a code attempt may freeze exactly one source_head")
                    from core import run_isolation
                    run_isolation.request_base(
                        self.db, attempt["execution_project_id"],
                        source_checks[0]["actual"],
                        note=f"frozen State attempt {aid}",
                    )
            except Exception as exc:
                report = getattr(exc, "report", None) or {
                    "version": 1, "checks": trace, "passed": False,
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                }
                return self.attempts.record_preflight(
                    aid, report, f"frozen prerequisite refusal: {exc}")
            attempt = self.attempts.record_preflight(aid, report)
        relay = None
        if attempt["context"].get("relay_of"):
            try:
                relay = self._prepare_relay(attempt, source)
            except StateConflict as e:
                # A refused relay must not strand the node behind a reserved
                # attempt: retire the intent so the director can re-read the
                # inventory and start again under a new request key.
                self.attempts.retire_reservation(aid, f"relay refused: {e}")
                raise StateConflict(f"{e}; this reservation was retired, start a new attempt") from e
            attempt = self.attempts.pin_relay(aid, relay)
        dependency_receipts = self._dependency_context(attempt, source, ref=relay["base_sha"] if relay else "HEAD")
        from core.run_launcher import missing_cross_config_inputs, start_config_run
        missing = missing_cross_config_inputs(self.sf, attempt["workflow"], attempt["execution_project_id"])
        if missing:
            raise StateConflict("workflow requires producer outputs not present for this attempt; use a self-contained "
                                "node workflow or prepare its prerequisites through the standard producer: " + canonical(missing))
        if not self.attempts.claim_launch(aid):
            return self.attempts.get(aid)
        seed = state_seed_text(attempt["context"], dependency_receipts, bool(relay))
        try:
            result = start_config_run(self.db, self.ws, attempt["workflow"], attempt["execution_project_id"],
                                      seed_text=seed, name=f"State {attempt['project_id']}/{attempt['node_key']}",
                                      owner_email=self.actor, repo_type="existing" if source else "none", repo_path=source)
        except Exception as exc:
            return self.attempts.launch_uncertain(aid, f"launcher raised {type(exc).__name__}; recover by execution_project_id")
        if result.get("status") == "error":
            return self.attempts.launch_uncertain(aid, str(result.get("message") or "launcher reported an error")[:4000])
        if not result.get("run_id"):
            return self.attempts.launch_uncertain(aid, "launcher returned no run identity")
        self.attempts.bind_run(aid, result["run_id"], self.sf)
        return self._attach_and_observe(aid, manifest)

    def _attach_and_observe(self, attempt_id, manifest):
        attempt = self.attempts.get(attempt_id)
        attached = False
        host = attempt["context"].get("host_contract")
        if host is None:
            raise StateConflict("attempt lacks its host contract pin")
        owned = host["scheduler_owned"]
        if self.attach_driver and attempt["status"] in {"running", "paused"}:
            # Reuse the existing driver, with review gates always enabled.
            attached = bool(self.attach_driver(attempt["run_id"], scheduler_owned=owned, auto_approve=False))
        return {**self.reconcile_attempt(attempt_id), "checkpoints": "ask", "driver_attached": attached,
                "scheduler_owned": owned}

    def _relay_inventory(self, attempt):
        """What a failed SkillFlow attempt left behind, as a director-readable
        inventory: the run branch and its commits beyond base, and every staged
        (unpromoted) regular file per step with its sha256 — complete, never
        truncated. `digest` binds branch head + manifest so the director can
        pass it back as `relay_digest` and have the relay refused if either
        moved between reading and copying. Read-only; None when nothing is
        recoverable."""
        from core import run_isolation
        rec = run_isolation.record(self.db, attempt["run_id"]) if attempt.get("run_id") else None
        if not rec or rec["mode"] != run_isolation.MODE_WORKTREE or not rec.get("branch"):
            return None
        source = rec["source_repo"]
        try:
            head = self._git(source, "rev-parse", "--verify", rec["branch"] + "^{commit}")
            log = self._git(source, "log", "--format=%H %s", f"{rec['base_sha']}..{head}")
            behind = int(self._git(source, "rev-list", "--count", f"{head}..HEAD") or 0)
        except StateConflict:
            return None
        commits = [line.split(" ", 1) for line in log.splitlines() if line]
        staged = {}
        pid = attempt["execution_project_id"]
        try:
            config_dir = self.ws._get_secure_path(pid) / attempt["workflow"]
        except Exception:
            config_dir = None
        if config_dir and config_dir.is_dir():
            for tmp in sorted(config_dir.glob("*.tmp")):
                if tmp.is_symlink() or not tmp.is_dir():
                    continue
                manifest = self.ws.relay_manifest(tmp)
                if manifest:
                    staged[tmp.name[:-4]] = manifest
        from core.code_relay import inventory as code_inventory
        code = {"files": {}, "steps": {}, "unowned": []}
        code_error = ""
        tree = Path(rec.get("worktree_path") or "")
        if config_dir and rec.get("worktree_path") and tree.is_dir():
            try:
                code = code_inventory(tree, config_dir, attempt["run_id"])
            except (ValueError, RuntimeError, OSError) as exc:
                code_error = str(exc)
        result = {"run_id": attempt["run_id"], "branch": rec["branch"], "base_sha": rec["base_sha"],
                "head_sha": head, "commits": [{"sha": c[0], "subject": c[1] if len(c) > 1 else ""} for c in commits],
                "mainline_ahead_by": behind, "staged_files": staged,
                "code_changes": code, "code_error": code_error,
                "digest": digest({"head_sha": head, "staged_files": staged, "code_changes": code}),
                "error": attempt.get("error")}
        result.update(self._relay_failure_metadata(attempt))
        return result

    def _trace_rows(self, run_id):
        """Read the bounded failure tail through SkillFlow's public trace API."""
        try:
            return self.sf.get_trace(run_id, order="desc", limit=500)
        except TypeError:  # compatible with a host one release behind
            return list(reversed(self.sf.get_trace(run_id)[-500:]))
        except Exception:
            return []

    def _attempt_for_run(self, run_id):
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM state_attempts WHERE run_id=?", (run_id,)).fetchone()
            attempt_id = row["attempt_id"] if row else None
        return self.attempts.get(attempt_id) if attempt_id else None

    def _relay_failure_metadata(self, attempt):
        """Keep the exact unfinished-work report and original failure visible.

        A later relay may fail with a smaller remainder than its origin. The
        handoff needs both facts: the latest run's exact remaining delivery and
        the first failed run in the relay chain. Trace rows remain owned by
        SkillFlow; this view only references/copies their bounded failure event
        into the already-frozen attempt context when a handoff is chosen.
        """
        rows = self._trace_rows(attempt["run_id"])
        latest_budget = next((r for r in rows if r.get("event") == "turn_budget_exhausted"), None)
        payload = latest_budget.get("payload") if latest_budget else {}
        remaining = payload.get("remaining_delivery") if isinstance(payload, dict) else None
        if not isinstance(remaining, list) or any(not isinstance(v, str) for v in remaining):
            remaining = []
        relay_of = attempt.get("context", {}).get("relay_of")
        if not isinstance(relay_of, dict):
            relay_of = {}
        first_run_id = (relay_of.get("first_failure_run_id")
                        or relay_of.get("run_id") or attempt["run_id"])
        original = self._attempt_for_run(first_run_id)
        first_rows = rows if first_run_id == attempt["run_id"] else self._trace_rows(first_run_id)
        first_budget = next((r for r in reversed(first_rows)
                             if r.get("event") == "turn_budget_exhausted"), None)
        first_failure = None
        try:
            from core.run_driver import summarise_run
            first_failure = summarise_run(self.sf, self.ws, self.registry, first_run_id).get("first_failure")
        except Exception:
            pass
        return {
            "remaining_delivery": remaining,
            "remaining_delivery_trace": self._failure_trace_ref(latest_budget),
            "original_first_failure": {
                "attempt_id": original["attempt_id"] if original else None,
                "run_id": first_run_id,
                "error": original.get("error") if original else None,
                "first_failure": first_failure,
                "trace": self._failure_trace_ref(first_budget, include_payload=True),
            },
        }

    @staticmethod
    def _failure_trace_ref(row, include_payload=False):
        if not isinstance(row, dict):
            return None
        fields = ("seq", "step_id", "step_instance_id", "category", "event", "created_at")
        result = {name: row.get(name) for name in fields}
        if include_payload:
            result["payload"] = row.get("payload")
        return result

    @staticmethod
    def _disposition_result(disposition, source, attempt=None, *, idempotent=False,
                            dispatchable=False, handoff=None):
        result = {
            "disposition": disposition,
            "source_attempt_id": source["attempt_id"],
            "source_status": source["status"],
            "automatic_retry": False,
            "dispatchable": dispatchable,
            "idempotent": idempotent,
            "attempt": attempt,
            "checkpoints": "ask",
            "later_gates": ["report completion", "record criterion evidence", "verify_node"],
        }
        if handoff is not None:
            result["handoff"] = handoff
        return result

    def _existing_disposition(self, source, request_key, disposition, relay_digest,
                              instruction, harness=None, external_id=None):
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM state_attempts WHERE project_id=? AND node_key=? AND request_key=?",
                (source["project_id"], source["node_key"], request_key),
            ).fetchone()
        if row is None:
            return None
        existing = self.attempts.get(row["attempt_id"])
        context = existing["context"]
        if disposition == "continue-workflow":
            relay = context.get("relay_of") or {}
            matches = (existing["execution_kind"] == "skillflow"
                       and existing["workflow"] == source["workflow"]
                       and context.get("instruction") == instruction
                       and relay.get("attempt_id") == source["attempt_id"]
                       and relay.get("expected_digest") == relay_digest)
            if not matches:
                raise StateConflict("request key already used by a different disposition")
            return self.reconcile_attempt(existing["attempt_id"]), None
        handoff = context.get("relay_handoff") or {}
        matches = (existing["execution_kind"] == "external"
                   and existing.get("harness") == harness
                   and existing.get("external_id") == external_id
                   and context.get("instruction") == instruction
                   and handoff.get("source_attempt_id") == source["attempt_id"]
                   and handoff.get("relay_digest") == relay_digest)
        if not matches:
            raise StateConflict("request key already used by a different disposition")
        return self.external.inspect(existing["attempt_id"]), handoff

    def disposition_failed_attempt(self, attempt_id, disposition, request_key=None,
                                   relay_digest=None, instruction="", harness=None,
                                   external_id=None):
        """Make one explicit director choice after a failed workflow attempt."""
        if disposition not in {"continue-workflow", "handoff-external", "leave-stopped"}:
            raise StateGraphError("unknown failed-attempt disposition")
        if disposition == "leave-stopped":
            if any(value is not None for value in (request_key, relay_digest, harness, external_id)):
                raise StateGraphError("leave-stopped cannot carry dispatch arguments")
        elif request_key is None or relay_digest is None:
            raise StateGraphError("dispatch disposition requires request_key and relay_digest")
        if disposition == "continue-workflow" and (harness is not None or external_id is not None):
            raise StateGraphError("continue-workflow cannot carry external harness identity")
        if disposition == "handoff-external" and (harness is None or external_id is None):
            raise StateGraphError("handoff-external requires harness and external_id")
        source = self.attempts.get(attempt_id)
        if source["execution_kind"] != "skillflow" or source["status"] != "failed":
            raise StateConflict("director disposition requires a FAILED SkillFlow attempt")
        if disposition == "leave-stopped":
            return self._disposition_result(disposition, source)

        self._components()
        existing = self._existing_disposition(
            source, request_key, disposition, relay_digest, instruction,
            harness=harness, external_id=external_id)
        if existing is not None:
            attempt, handoff = existing
            return self._disposition_result(
                disposition, source, attempt, idempotent=True,
                dispatchable=True, handoff=handoff)

        source = self.reconcile_attempt(attempt_id)
        inventory = source.get("relay_inventory")
        if inventory is None:
            raise StateConflict("failed attempt has no crash-safe relay inventory; leave it stopped or start fresh")
        if relay_digest != inventory["digest"]:
            raise StateConflict("failed attempt relay changed since it was read; reconcile_attempt and choose again")

        if disposition == "continue-workflow":
            successor = self.start_attempt(
                source["project_id"], source["node_key"], source["node_revision"],
                source["workflow"], request_key, instruction,
                continue_from=attempt_id, relay_digest=relay_digest)
            return self._disposition_result(
                disposition, source, successor, dispatchable=True)

        handoff = {
            "version": 1,
            "source_attempt_id": source["attempt_id"],
            "source_run_id": source["run_id"],
            "source_execution_project_id": source["execution_project_id"],
            "source_context_hash": source["context_hash"],
            "workflow": source["workflow"],
            "relay_digest": inventory["digest"],
            "relay_inventory": inventory,
            "remaining_delivery": inventory["remaining_delivery"],
            "original_first_failure": inventory["original_first_failure"],
            "authority": {
                "scope": "execute the frozen node contract and retained delivery only",
                "state_acceptance": "record_evidence and verify_node remain separate authorized gates",
                "checkpoint_policy": "ask",
            },
        }
        # Re-read immediately before admitting an external owner. A changed
        # branch, staged byte or pending code byte never becomes dispatchable.
        current = self._relay_inventory(source)
        if current is None or current["digest"] != relay_digest:
            raise StateConflict("failed attempt relay drifted before external handoff admission")
        successor = self.external.register_relay_handoff(
            source["project_id"], source["node_key"], source["node_revision"],
            harness, external_id, request_key, instruction, handoff)
        return self._disposition_result(
            disposition, source, successor, dispatchable=True, handoff=handoff)

    def _prepare_relay(self, attempt, source):
        """Make the failed attempt's work physically reachable by the new run:
        request its branch head as the new worktree's base and park its staged
        drafts where the engine seeds them into the new staging. The failed
        attempt's own worktree and staging are never modified."""
        prior = self.attempts.get(attempt["context"]["relay_of"]["attempt_id"])
        if not source:
            raise StateConflict("a relay needs a registered source repository")
        inventory = self._relay_inventory(prior)
        if inventory is None:
            raise StateConflict("the failed attempt has no run branch to continue from; start a fresh attempt")
        expected = attempt["context"]["relay_of"].get("expected_digest")
        if expected and expected != inventory["digest"]:
            raise StateConflict("the failed attempt's branch or staged draft changed since it was read "
                                f"(relay_digest {expected} != {inventory['digest']}); read relay_inventory again")
        from core import run_isolation
        from core.workspace_manager import RelayDraftChanged
        # Artifact drafts remain relayable. Old CODE drafts cannot be copied
        # into a new direct-code attempt and then silently ignored by its runner.
        from skillflow.output_targets import target_for
        from skillflow.write_tools import _get_pattern
        from pathlib import PurePath
        # by-name-ok: creation-time relay admission; the new attempt has no run/pin yet.
        current_graph = getattr(self.sf, "_graphs", {}).get(attempt["workflow"])
        current_nodes = {n.id: n for n in getattr(current_graph, "steps", [])}
        for prior_step, files in inventory.get("staged_files", {}).items():
            node = current_nodes.get(prior_step)
            for filename in files:
                target = target_for(node)
                for slot in (getattr(node, "output_fixed", {}) or {}):
                    if PurePath(filename).match(_get_pattern(slot, node.output_fixed)):
                        target = target_for(node, slot)
                        break
                if target == "code":
                    raise StateConflict("Legacy code draft retained at " + prior_step + "/" + filename
                        + "; explicitly recover it into a code worktree before relaying to the new output target.")
        base = inventory["head_sha"]
        code = inventory.get("code_changes") or {"files": {}, "steps": {}, "unowned": []}
        if inventory.get("code_error"):
            raise StateConflict("Cannot inventory failed code: " + inventory["code_error"])
        if code["files"]:
            from core.code_relay import recovery_commit, require_quiet
            from skillflow.output_targets import atomic_json
            rec = run_isolation.record(self.db, prior["run_id"])
            prior_dir = self.ws._get_secure_path(prior["execution_project_id"]) / prior["workflow"]
            try:
                require_quiet(self.sf, prior["run_id"])
                base = recovery_commit(Path(rec["worktree_path"]), prior_dir, prior["run_id"],
                                       inventory["head_sha"], code, attempt["attempt_id"])
            except (ValueError, RuntimeError, OSError) as exc:
                raise StateConflict(str(exc)) from exc
            target_dir = self.ws._get_secure_path(attempt["execution_project_id"]) / attempt["workflow"]
            atomic_json(target_dir / ".code-output-relay.json", {
                "recovery_commit": base, "base_commit": inventory["head_sha"],
                "steps": code["steps"], "validated": False})
        run_isolation.request_base(self.db, attempt["execution_project_id"], base,
                                   note=f"relay of {prior['attempt_id']} (run {prior['run_id']}); code remains unvalidated")
        parked = {}
        prior_config = self.ws._get_secure_path(prior["execution_project_id"]) / prior["workflow"]
        for step, manifest in inventory["staged_files"].items():
            try:
                copied = self.ws.stage_relay_draft(prior_config / f"{step}.tmp", attempt["execution_project_id"],
                                                   step, attempt["workflow"], manifest=manifest)
            except RelayDraftChanged as e:
                raise StateConflict(f"the failed attempt's staged draft changed while being copied ({e}); "
                                    "read relay_inventory again") from e
            if copied:
                parked[step] = copied
        return {"attempt_id": prior["attempt_id"], "run_id": inventory["run_id"], "branch": inventory["branch"],
                "base_sha": base, "commits": inventory["commits"],
                "code_changes": code, "code_recovery_validated": False,
                "mainline_ahead_by": inventory["mainline_ahead_by"], "staged_files": parked,
                "digest": inventory["digest"], "error": inventory["error"]}

    # An attempt in one of these states has stopped producing work, so what it
    # did and did not do is now the whole record.
    _TERMINAL = ("candidate", "failed", "superseded")

    def get_attempt(self, attempt_id):
        """The read surface's view of one attempt; a failed SkillFlow attempt
        carries its relay_inventory so the director can inspect the retained
        draft without a write call."""
        attempt = self.attempts.get(attempt_id)
        if attempt["status"] == "failed" and attempt["execution_kind"] == "skillflow":
            return self._with_refusals({**attempt, "relay_inventory": self._relay_inventory(attempt)})
        return self._with_refusals(attempt)

    def _with_refusals(self, envelope):
        """Carry the run's refused-call count into a TERMINAL attempt envelope.

        A refused call produced no output at all, so the only place its absence
        can still be noticed is the report that closes the attempt. Leaving it
        in ``get_run_summary`` meant the director had to already suspect it to
        go looking — which is the failure mode: run 0cf3c10b's review shipped
        without its commit history because two ``git_history`` calls never ran,
        and nothing in the attempt's terminal report said so.

        It stays its OWN field. Folding it into any failure count would erase
        the distinction the count exists to carry: a failed call left a result
        to read, a refused one left a hole. ``None`` for ``total`` means the
        count could not be taken and must never be read as "nothing was
        refused".
        """
        if (not isinstance(envelope, dict)
                or envelope.get("execution_kind") != "skillflow"
                or envelope.get("status") not in self._TERMINAL
                or not envelope.get("run_id")):
            return envelope
        from core.run_driver import refused_tool_calls
        try:
            self._components()
            refused = refused_tool_calls(self.sf, envelope["run_id"])
        except Exception as exc:  # noqa: BLE001
            refused = {"total": None, "unreadable": f"{type(exc).__name__}: {str(exc)[:160]}"}
        return {**envelope, "refused_tool_calls": refused}

    def recover_attempt(self, attempt_id):
        attempt = self.attempts.get(attempt_id)
        if attempt["execution_kind"] == "external":
            return self.external.inspect(attempt_id)
        self._components()
        manifest = self.registry.get(attempt["workflow"])
        if manifest is None:
            raise StateConflict("workflow registration unavailable; preserve the attempt")
        if attempt["run_id"]:
            return self._attach_and_observe(attempt_id, manifest)
        return self._launch_or_recover(attempt, manifest, self._source(attempt["project_id"]))

    def _artifact(self, attempt):
        from core import run_isolation
        rec = run_isolation.record(self.db, attempt["run_id"])
        if not rec:
            raise StateConflict("candidate lacks its run-isolation provenance")
        if rec["mode"] == run_isolation.MODE_READ_SNAPSHOT:
            # A read snapshot is handed to a run that owns NO repository: it
            # cannot commit, so the commit it was pinned to READ is provably not
            # its output. Pinning that commit as the artifact is the
            # pass-on-absence shape: record_evidence binds a verdict to
            # (attempt, artifact_ref), so the reviewer would be attesting about
            # a tree containing none of the attempt's work. What such a run
            # delivers is its promoted output step, so hash that.
            path = run_isolation.resolve_for_resolver(self.db, attempt["run_id"])
            if not isinstance(path, str):
                raise StateConflict("candidate source root is not an isolated tree")
            # Still prove the accepted dependencies are in the tree it read.
            self._dependency_context(attempt, path)
            return self._output_digest(attempt)
        if rec["mode"] == run_isolation.MODE_WORKTREE:
            path = run_isolation.resolve_for_resolver(self.db, attempt["run_id"])
            if not isinstance(path, str):
                raise StateConflict("candidate source root is not an isolated tree")
            if self._git(path, "status", "--porcelain=v1", "--untracked-files=all"):
                raise StateConflict("candidate has uncommitted source; commit before reporting an artifact")
            # The source could have changed between preflight and isolation.
            # Check actual candidate ancestry as well, before it can be certified.
            self._dependency_context(attempt, path)
            commit = self._git(path, "rev-parse", "HEAD")
            # v1 uses 40-hex Git commits and 64-hex output-bundle digests.
            # Do not mistake a SHA-256-format Git commit for a non-code bundle
            # and thereby bypass downstream code-ancestry checks.
            if len(commit) != 40:
                raise StateConflict("this version requires SHA-1-format Git repositories; candidate retained")
            if rec.get("base_sha") and commit == rec["base_sha"]:
                # FAIL LOUD, do not pin the base commit. A code-declared run
                # whose worktree HEAD never moved committed nothing: that sha
                # holds none of this attempt's bytes, and every downstream
                # reader (evidence, acceptance receipt, dependency ancestry)
                # would treat an unrelated commit as the delivery.
                raise StateConflict(
                    "this attempt committed nothing: its worktree HEAD is still the commit it "
                    "started from, which contains none of its own output, so there is no code "
                    "artifact to pin. If this workflow delivers findings/analysis rather than "
                    "code, declare x-aitelier.repo_mode: none on its config so its output step "
                    "is hashed as the artifact instead")
            return artifact_ref(commit)
        if rec["mode"] != run_isolation.MODE_NONE:
            raise StateConflict("direct-mode source cannot be silently used as an isolated state artifact")
        return self._output_digest(attempt)

    def _output_digest(self, attempt):
        """Hash what the attempt actually produced: its promoted output step.

        The artifact of a run that owns no repository is its own deliverable,
        never a commit it merely read. Absence is an error here, never an
        empty pass: no declared output_step, a missing directory or an empty
        one all raise, so a bytes-less attempt cannot acquire an artifact for
        evidence to bind to.
        """
        host = attempt["context"].get("host_contract") or {}
        output_step = host.get("output_step")
        if not output_step:
            raise StateConflict("output-only workflow must declare an output_step for artifact hashing")
        base = self.ws.get_final_path(attempt["execution_project_id"], output_step, attempt["workflow"])
        workspace_root = self.ws.base_path.resolve()
        if (not base.is_dir() or base.is_symlink() or not base.resolve().is_relative_to(workspace_root)
                or any(parent.is_symlink() for parent in base.parents if parent != workspace_root)):
            raise StateConflict("final artifact directory is absent or outside the workspace")
        manifest_hashes = {}
        total = 0
        # Only hash files produced at this registered output location. Never
        # follow symlinks to secrets or other workspaces.
        for directory, dirs, files in os.walk(base, followlinks=False):
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    raise StateConflict("output contains a symlink; artifact scope is ambiguous")
            for name in files:
                path = Path(directory) / name
                if path.is_symlink() or not path.is_file():
                    raise StateConflict("output contains a non-regular file")
                total += path.stat().st_size
                if total > 32 * 1024 * 1024 or len(manifest_hashes) >= 1000:
                    raise StateConflict("output exceeds the bounded artifact hashing budget")
                size = path.stat().st_size
                with path.open("rb") as stream:
                    body = stream.read(size + 1)
                if len(body) != size:
                    raise StateConflict("artifact changed during hashing; retry after the output settles")
                manifest_hashes[str(path.relative_to(base))] = hashlib.sha256(body).hexdigest()
        if not manifest_hashes:
            raise StateConflict("an empty output directory is not an artifact")
        return digest(manifest_hashes)

    def reconcile_attempt(self, attempt_id):
        attempt = self.attempts.get(attempt_id)
        if attempt["execution_kind"] == "external":
            return self.external.inspect(attempt_id)
        self._components()
        if not attempt["run_id"]:
            return {**attempt, "note": "No bound run; recover_attempt may bind an existing launch."}
        observed = self.attempts.reconcile(attempt_id, self.sf)
        if observed["status"] == "failed":
            # What the director needs to choose between continue_from and a
            # fresh attempt: the retained commits and staged files, by step.
            return self._with_refusals({**observed, "relay_inventory": self._relay_inventory(observed)})
        if observed["status"] == "candidate" and not observed["artifact_ref"]:
            try:
                artifact = self._artifact(observed)
            except StateConflict as exc:
                return self._with_refusals({**observed, "artifact_pending": True, "note": str(exc)})
            observed = self.attempts.reconcile(attempt_id, self.sf, artifact)
        return self._with_refusals(observed)

    def record_evidence(self, attempt_id, evidence_id, criterion_id, verdict, artifact, report_ref, report_sha256, detail=""):
        self.reconcile_attempt(attempt_id)
        from core.state_report_integrity import retain_report, validate_evidence_semantics
        report_ref, report_bytes = retain_report(report_ref, report_sha256, completed=True)
        validate_evidence_semantics(report_bytes, criterion_id, verdict, artifact)
        return self.attempts.record_evidence(attempt_id, evidence_id, criterion_id, verdict, artifact,
                                             report_ref, report_sha256, self.actor, detail,
                                             report_bytes=report_bytes)

    def verify_node(self, project_id, node_key, expected_revision, attempt_id):
        self.reconcile_attempt(attempt_id)
        return self.attempts.verify(project_id, node_key, expected_revision, attempt_id, self.actor,
                                    require_report_bytes=True)

    def import_tasks(self, project_id, source_project_id):
        """Explicit legacy snapshot; completed tasks never become verified facts."""
        key(source_project_id, "source project")
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
                raise StateGraphError("legacy task import is unavailable in a State-only database")
            if not conn.execute("SELECT 1 FROM runs WHERE project_id=?", (source_project_id,)).fetchone():
                raise StateGraphError("unknown legacy source project")
            rows = [dict(r) for r in conn.execute("SELECT * FROM tasks WHERE project_id=? ORDER BY id", (source_project_id,))]
            if not rows:
                raise StateGraphError("legacy source has no tasks")
            specs, statuses = [], {}
            ids = {r["id"]: "legacy-" + str(r["id"]) for r in rows}
            for row in rows:
                try:
                    deps = json.loads(row["dependencies"] or "[]")
                except (TypeError, ValueError) as exc:
                    raise StateGraphError("legacy dependencies are malformed") from exc
                if not isinstance(deps, list) or any(type(d) is not int or d not in ids for d in deps):
                    raise StateGraphError("legacy task has a dangling or cross-project dependency")
                nk = ids[row["id"]]
                specs.append({"key": nk, "goal": text(row["prompt"], "legacy task goal"),
                              "dependencies": [ids[d] for d in deps],
                              "acceptance": [{"id": "legacy-revalidation", "kind": "review",
                                              "description": "Replace this import contract with explicit criteria and revalidate the historical result"}]})
                statuses[nk] = row["status"]
            created = self.store._add(conn, project_id, specs)
            for nk, status in statuses.items():
                if status in {"completed", "superseded"}:
                    imported = "CANDIDATE" if status == "completed" else "SUPERSEDED"
                    conn.execute("UPDATE state_nodes SET status=? WHERE project_id=? AND node_key=?", (imported, project_id, nk))
                self.store._event(conn, project_id, nk, "legacy_task_imported", {"source_project_id": source_project_id,
                                  "legacy_status": status, "requires_new_contract_and_validation": True})
            return {"created": created, "verified": 0, "note": "Legacy completion is historical, not acceptance."}


    def bind_source(self, project_id, repo_path, expected_revision=0):
        path = Path(text(repo_path, "repo_path", 4000)).expanduser().resolve()
        if not path.is_dir():
            raise StateGraphError("source repository must already exist")
        top = Path(self._git(path, "rev-parse", "--show-toplevel")).resolve()
        if top != path:
            raise StateGraphError("bind the repository root, not a subdirectory")
        common = str((path / self._git(path, "rev-parse", "--git-common-dir")).resolve())
        return self.portfolio.bind_source(project_id, str(path), common, expected_revision)

    def set_node_hold(self, project_id, node_key, held, expected_revision, reason):
        if held is False:
            # Protected references may be legacy runs, never reparented attempts.
            with self.store.transaction() as conn:
                refs = [r[0] for r in conn.execute("SELECT ref FROM state_history_links WHERE project_id=? "
                        "AND node_key=? AND kind='run' AND protection=1", (project_id, node_key))]
            if refs:
                self._components()
                for rid in refs:
                    row = self.sf.get_run(rid)
                    if not row or row.get("id") != rid or row.get("status") not in {"completed", "failed"}:
                        raise StateConflict("protected external run is active or unknown; retain its hold")
                    audit = self.sf.audit_operation_owners(rid)
                    if (not isinstance(audit, dict) or audit.get("lost") != [] or audit.get("unknown") != []
                            or type(audit.get("alive")) is not int or audit["alive"] != 0):
                        raise StateConflict("protected external run has unretired/unknown operations")
        return self.portfolio.set_hold(project_id, node_key, held, expected_revision, reason)

    def add_reference(self, project_id, node_key, reference_id, kind, ref, label, provenance_actor,
                      artifact_ref=None, report_sha256=None, protect=False):
        status = "historical"
        if kind == "run":
            key(ref, "run id")
            self._components()
            row = self.sf.get_run(ref)
            if not row or row.get("id") != ref:
                raise StateConflict("historical run identity is unknown")
            status = row["status"]
            protect = protect or status not in {"completed", "failed"}
        return self.portfolio.add_reference(project_id, node_key, reference_id, kind, ref, label,
                    provenance_actor, status, artifact_ref, report_sha256, protect)

    def refresh_project(self, project_id, after=0, limit=20):
        from core.state_graph import integer
        integer(after, "after", 0, 2**63-1)
        integer(limit, "limit", 1, 50)
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            rows = [dict(r) for r in conn.execute("SELECT seq,attempt_id,run_id FROM state_attempts "
                    "WHERE project_id=? AND seq>? AND (run_id IS NOT NULL OR execution_kind='external') ORDER BY seq LIMIT ?", (project_id, after, limit + 1))]
        results = []
        for row in rows[:limit]:
            try:
                updated = self.reconcile_attempt(row["attempt_id"])
                results.append({"attempt_id": row["attempt_id"], "status": updated["status"],
                                "artifact_pending": updated.get("artifact_pending", False)})
            except Exception as exc:
                # Observation failure retains state; one broken run must not hide
                # all others. No attempt is launched, stopped, or approved here.
                results.append({"attempt_id": row["attempt_id"], "error": type(exc).__name__})
        return {"results": results, "next_after": rows[limit-1]["seq"] if len(rows)>limit else None,
                "effects": "observations only; no launch, approval or acceptance"}
