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


class StateService:
    def __init__(self, db, ws=None, sf=None, registry=None, attach_driver=None, actor="local-operator", runtime_factory=None):
        self.db, self.ws, self.sf, self.registry = db, ws, sf, registry
        self.store = StateGraphStore(db)
        self.attempts = StateAttempts(self.store)
        from core.state_external import ExternalAttempts
        self.external = ExternalAttempts(self.attempts, actor)
        self.attach_driver = attach_driver
        self.actor = actor
        self.runtime_factory = runtime_factory
        from core.state_portfolio import StatePortfolio
        self.portfolio = StatePortfolio(self.store, actor)

    async def wait_for_state_change(self, project_id, after=0, node_keys=None, attempt_ids=None,
                                    actionable_only=True, timeout_seconds=30.0, limit=100, return_when_idle=False):
        from core.state_changes import wait_for_state_change
        return await wait_for_state_change(self, project_id, after, node_keys, attempt_ids,
                                           actionable_only, timeout_seconds, limit, return_when_idle)

    def create_project(self, project_id, title, source_project_id=None):
        if source_project_id and not self.db.get_project(source_project_id):
            raise StateGraphError("source_project_id must name an existing AItelier source project")
        return self.store.create_project(project_id, title, source_project_id)

    def node_context(self, project_id, node_key):
        node = self.store.get_node(project_id, node_key)
        receipts = {}
        with self.store.transaction() as conn:
            for dep in node["dependencies"]:
                d = self.store._node(conn, project_id, dep)
                r = conn.execute("SELECT * FROM state_acceptances WHERE receipt_id=?", (d["verified_receipt"],)).fetchone()
                receipts[dep] = {"goal": d["goal"], "revision": d["revision"], "status": d["status"],
                                 "acceptance": dict(r) if r else None}
        return {"node": node, "dependency_receipts": receipts,
                "attempts": self.attempts.list(project_id, node_key, limit=10),
                "references": self.portfolio.references(project_id, node_key, limit=100)}

    def _components(self):
        # Goal inspection/planning must survive an unavailable executor. Only
        # execution/acceptance operations ask the runtime composition root.
        if self.runtime_factory is not None and (self.sf is None or self.registry is None):
            try:
                self.sf, self.registry = self.runtime_factory()
            except Exception as exc:
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

    def _dependency_context(self, attempt, source):
        """Pass accepted contracts/artifact references, not an entire project log."""
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
                        self._git(source, "merge-base", "--is-ancestor", rec["artifact_ref"], "HEAD")
                    except StateConflict as exc:
                        raise StateConflict("accepted dependency commit is not in the source; integrate it before launching") from exc
        return out

    def start_external_attempt(self, project_id, node_key, expected_revision, harness, external_id,
                               request_key, instruction=""):
        """Register external execution scope; never compose or dispatch a workflow."""
        return self.external.register(project_id, node_key, expected_revision, harness, external_id,
                                      request_key, instruction)

    def report_external_attempt(self, attempt_id, observation_id, expected_version, context_hash,
                                status, report_ref, report_sha256, quiescent=False,
                                artifact=None, artifact_kind=None, detail=""):
        return self.external.observe(attempt_id, observation_id, expected_version, context_hash,
                                     status, report_ref, report_sha256, quiescent=quiescent,
                                     artifact=artifact, artifact_kind=artifact_kind, detail=detail)

    def start_attempt(self, project_id, node_key, expected_revision, workflow, request_key, instruction=""):
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
        # All state writes below use a separate intent identity. No old DPE rows
        # are reused as the long-lived state project.
        attempt = self.attempts.reserve(project_id, node_key, expected_revision, workflow, request_key, instruction)
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
        attempt = self.attempts.pin_host_contract(aid, {
            "source_repo": source, "seed_file": manifest.seed_file, "output_step": manifest.output_step,
            "scheduler_owned": bool(manifest.scheduler_owned), "repo_mode": manifest.repo_mode})
        dependency_receipts = self._dependency_context(attempt, source)
        from core.run_launcher import missing_cross_config_inputs, start_config_run
        missing = missing_cross_config_inputs(self.sf, attempt["workflow"], attempt["execution_project_id"])
        if missing:
            raise StateConflict("workflow requires producer outputs not present for this attempt; use a self-contained "
                                "node workflow or prepare its prerequisites through the standard producer: " + canonical(missing))
        if not self.attempts.claim_launch(aid):
            return self.attempts.get(aid)
        seed = "# State goal attempt\n\n" + canonical(attempt["context"] | {"accepted_dependencies": dependency_receipts}) + "\n"
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
        if rec["mode"] in {run_isolation.MODE_WORKTREE, run_isolation.MODE_READ_SNAPSHOT}:
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
            return artifact_ref(commit)
        if rec["mode"] != run_isolation.MODE_NONE:
            raise StateConflict("direct-mode source cannot be silently used as an isolated state artifact")
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
        if observed["status"] == "candidate" and not observed["artifact_ref"]:
            try:
                artifact = self._artifact(observed)
            except StateConflict as exc:
                return {**observed, "artifact_pending": True, "note": str(exc)}
            observed = self.attempts.reconcile(attempt_id, self.sf, artifact)
        return observed

    def record_evidence(self, attempt_id, evidence_id, criterion_id, verdict, artifact, report_ref, report_sha256, detail=""):
        self.reconcile_attempt(attempt_id)
        return self.attempts.record_evidence(attempt_id, evidence_id, criterion_id, verdict, artifact,
                                             report_ref, report_sha256, self.actor, detail)

    def verify_node(self, project_id, node_key, expected_revision, attempt_id):
        self.reconcile_attempt(attempt_id)
        return self.attempts.verify(project_id, node_key, expected_revision, attempt_id, self.actor)

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
