"""AItelier's recovery boundary around the public SkillFlow runtime.

SkillFlow owns operation admission and settlement.  AItelier owns process
restart and every production driver that may call ``advance_run`` or
``claim_next_step``.  SkillFlow 1.5.75 records an admitted inline tool operation
durably, but its generic stale-claim recovery does not use that record as a
re-admission fence.  This host class supplies the missing controller boundary:
an unsettled operation blocks every new claim/advance until its own ``finally``
or an evidence-bearing ``release_operation`` removes the record.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from typing import Any

from skillflow import SkillFlow
from skillflow.exceptions import StaleClaimFenced, TerminalRunFenced
from skillflow.identity import owner_is_dead, worker_identity


class AItelierSkillFlow(SkillFlow):
    """SkillFlow with durable tool-operation recovery enforced at host ingress."""

    #: Every keyword this host binds into a tool call's CALLER params.  A
    #: keyword listed here is subject to ``_tool_accepts_keyword`` on every
    #: tool, and ``scripts/audit_injected_kwargs.py`` enumerates the live
    #: registry against this tuple — so a second injected keyword inherits the
    #: guard and the audit instead of replaying the ``project_id`` outage.
    HOST_INJECTED_KEYWORDS: tuple[str, ...] = ("project_id",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._operation_recovery_decisions: set[tuple[Any, ...]] = set()

    def _execute_tool_impl(self, name: str, params: dict, *, run_id: str = "",
                           step_id: str = "", project_root: str = "") -> dict:
        """Preserve the owning project identity for host-registered tools."""
        bound = dict(params or {})
        if run_id:
            for keyword in self.HOST_INJECTED_KEYWORDS:
                if not self._tool_accepts_keyword(name, keyword):
                    continue
                value = self._get_project_id(run_id)
                if value:
                    # Assigned, not setdefault()ed. The tools that take this
                    # keyword spend it on owner identity and commit
                    # attribution, and it is declared in their public schema —
                    # so a caller CAN name it. Whoever owns the run owns the
                    # identity; a caller-supplied value must not displace it.
                    bound[keyword] = value
        return super()._execute_tool_impl(
            name, bound, run_id=run_id, step_id=step_id,
            project_root=project_root)

    def _tool_accepts_keyword(self, name: str, keyword: str) -> bool:
        """Return whether a host-owned keyword survives to the tool's body.

        The host binds the keyword into the CALLER's ``params``.  SkillFlow then
        computes ``dropped = [k for k in params if k not in sig.parameters]`` and,
        when anything was dropped, returns ``unrecognised argument(s): ... No
        tool action was performed`` — the tool never runs and the agent sees a
        gap rather than a failure.  ``**kwargs`` is not a member of
        ``sig.parameters`` under that membership test, so a signature ending in
        ``**kwargs`` is a REFUSAL, not an acceptance.  Counting VAR_KEYWORD as
        acceptance is what stopped 58 of the 83 live tools from running when the
        host reached them (measured 2026-09-21 by
        ``scripts/audit_injected_kwargs.py``, whose loader-only view of the
        registry is 83; composing the runtime adds ``skillflow_lint`` and makes
        it 84); ``semantic_search`` and ``git_history`` are the two confirmed
        victims in the trace.  That audit measures what the guard and the
        signatures SAY; what the engine DOES under the new decision is measured
        by ``scripts/sweep_injected_kwargs_execution.py``, which calls every
        registered tool through this class and reads the result.

        Two surfaces can disagree about one keyword, so the decision is their
        intersection:

        * the callable's NAMED parameters — the set SkillFlow filters against,
          i.e. the layer that performs the refusal.  A probe through the real
          call path (4 tool shapes, 2026-09-21) shows a schema that declares the
          keyword is STILL refused when the signature only has ``**kwargs``, and
          a signature that names it runs even when the schema omits it: the
          signature's named set is what decides whether the call happens.
        * the tool's declared ``tool.yaml`` schema — the contract an agent and a
          reviewer read.  A host-owned tool that consumes a field it never
          declares is undeclared plumbing, which is exactly how five tools came
          to look refused while they were quietly being served.  AItelier owns
          those files, so the agreement is enforceable
          (``tests/unit/test_host_injected_kwargs_are_declared.py``).

        Tools whose ``tool.yaml`` this host cannot edit are held to the
        signature half only, and ``ToolLoader.is_native`` is that predicate.
        It covers more than the phrase "SkillFlow's own" suggests: the tools in
        the engine's FIRST tools directory (the wheel's, whose schema
        deliberately keeps host plumbing — ``workspace_root``, ``run_id``,
        ``project_id`` — out of the agent-facing contract), AND tools
        registered dynamically (``register_dynamic_tool``) or declared per step
        (``declare_dynamic``) that resolve to NO tool directory, and so have no
        ``tool.yaml`` on disk to hold to.  That second branch fires nowhere in
        this deployment: with the runtime fully composed, all 84 registered
        names resolve to a directory (22 are also in the dynamic cache), so
        ``is_native`` is exactly the 18 wheel tools (measured 2026-09-21).  The
        rule is still written for the predicate rather than for that count: the
        schema half applies only where AItelier owns the file and can change it.
        """
        loader = getattr(self, "_tool_loader", None)
        if loader is None:
            return False
        try:
            signature = inspect.signature(loader.load_fn(name))
        except (AttributeError, ImportError, OSError, TypeError, ValueError):
            return False
        parameter = signature.parameters.get(keyword)
        if parameter is None or parameter.kind not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY):
            return False
        try:
            if loader.is_native(name):
                return True
        except Exception:  # noqa: BLE001 — an unclassifiable tool is not native
            return False
        return keyword in self._declared_schema_fields(name)

    def _declared_schema_fields(self, name: str) -> set[str]:
        """Parameter names a tool advertises in its own ``tool.yaml``.

        Accepts both shapes seen in the registry: a bare mapping of
        ``name -> spec`` and a JSON-Schema ``{type: object, properties: {...}}``.
        An unreadable or parameter-less schema yields the empty set, which
        refuses the injection — the tool is then left exactly as the agent
        called it.
        """
        loader = getattr(self, "_tool_loader", None)
        if loader is None:
            return set()
        try:
            schema = loader.load_schema(name)
        except Exception:  # noqa: BLE001 — a schema we cannot read declares nothing
            return set()
        parameters = schema.get("parameters") if isinstance(schema, dict) else None
        if isinstance(parameters, dict) and isinstance(
                parameters.get("properties"), dict):
            parameters = parameters["properties"]
        if not isinstance(parameters, dict):
            return set()
        return {key for key in parameters if isinstance(key, str)}

    def unsettled_operations(self, run_id: str | None = None) -> list[dict]:
        with self._ro() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT * FROM skillflow_active_ops WHERE run_id = ? "
                    "ORDER BY id", (run_id,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM skillflow_active_ops ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def _tool_step_identity(self, run_id: str, detail: str) -> tuple[int | None, int]:
        """Resolve the pending inline node before admission and claim share it.

        SkillFlow admits an inline tool immediately before its CAS claim.  The
        pending row already exists, so recording its id and next epoch here gives
        a restart a stable operation/step identity without moving admission past
        the cancellation boundary.
        """
        with self._ro() as conn:
            run = conn.execute(
                "SELECT current_node FROM skillflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not run or not run["current_node"]:
                return None, 0
            step_id = run["current_node"]
            try:
                node = self._get_resolver_for_run(run_id).get_node(step_id)
            except Exception:  # noqa: BLE001
                node = None
            if not node or node.step_type != "tool" or node.tool_name != detail:
                return None, 0
            row = conn.execute(
                "SELECT id, status, claim_epoch FROM skillflow_steps "
                "WHERE run_id = ? AND step_id = ? "
                "ORDER BY id DESC LIMIT 1", (run_id, step_id)
            ).fetchone()
        if not row or row["status"] not in ("pending", "claimed"):
            return None, 0
        epoch = int(row["claim_epoch"] or 0)
        if row["status"] == "pending":
            epoch += 1
        return int(row["id"]), epoch

    def _admit_op(self, kind: str, run_id: str, *,
                  step_instance_id: int | None = None,
                  claim_epoch: int = 0, detail: str = "") -> int:
        from core import deployment_quiescence as dq
        with dq.operation_admission_fence():
            return self._admit_op_under_fence(
                kind, run_id, step_instance_id=step_instance_id,
                claim_epoch=claim_epoch, detail=detail)

    def _admit_op_under_fence(self, kind: str, run_id: str, *,
                  step_instance_id: int | None = None,
                  claim_epoch: int = 0, detail: str = "") -> int:
        """Atomically exclude an unsettled operation and admit its successor.

        The host preflight remains useful for reconciling owners before normal
        recovery, but it is not the fence: two processes can both complete that
        read before either writes.  This transaction is the final admission
        boundary. SQLite's ``BEGIN IMMEDIATE`` serialises independent SkillFlow
        connections, so the second controller necessarily observes the first
        controller's durable operation and is refused before any effect.
        """
        derived_tool_identity = kind == "tool_step" and step_instance_id is None
        resolver = self._get_resolver_for_run(run_id) if derived_tool_identity else None
        step_id = None
        with self._tx() as conn:
            run = conn.execute(
                "SELECT status, error_reason, cancel_requested_at, current_node "
                "FROM skillflow_runs WHERE id = ?", (run_id,)).fetchone()
            if run is not None:
                if run["status"] in self.TERMINAL_RUN_STATUSES:
                    raise TerminalRunFenced(
                        f"Run '{run_id}' is {run['status']}; {kind} "
                        f"{detail or '?'} refused before it started. "
                        f"Run reason: {(run['error_reason'] or '-')}")
                if run["cancel_requested_at"]:
                    raise TerminalRunFenced(
                        f"Run '{run_id}' is draining a cancellation requested at "
                        f"{run['cancel_requested_at']}; {kind} {detail or '?'} "
                        f"refused. Reason: {(run['error_reason'] or '-')}")

            if derived_tool_identity and run and run["current_node"]:
                step_id = run["current_node"]
                node = resolver.get_node(step_id) if resolver else None
                if node and node.step_type == "tool" and node.tool_name == detail:
                    row = conn.execute(
                        "SELECT id, status, claim_epoch FROM skillflow_steps "
                        "WHERE run_id = ? AND step_id = ? ORDER BY id DESC LIMIT 1",
                        (run_id, step_id)).fetchone()
                    if row and row["status"] in ("pending", "claimed"):
                        step_instance_id = int(row["id"])
                        claim_epoch = int(row["claim_epoch"] or 0)
                        if row["status"] == "pending":
                            claim_epoch += 1

            # This read and the INSERT below are one write transaction. Checking
            # every operation for the run is deliberate: a SkillFlow run has one
            # current step, and an unsettled operation from that run means the
            # prior executor may still produce effects for it.
            existing = conn.execute(
                "SELECT id, kind, detail FROM skillflow_active_ops "
                "WHERE run_id = ? ORDER BY id LIMIT 1", (run_id,)).fetchone()
            if existing:
                raise TerminalRunFenced(
                    f"Run '{run_id}' has unsettled operation {existing['id']} "
                    f"({existing['kind']}:{existing['detail']}); {kind} "
                    f"{detail or '?'} refused before it started.")

            if step_instance_id and claim_epoch and not derived_tool_identity:
                row = conn.execute(
                    "SELECT claim_epoch FROM skillflow_steps WHERE id = ?",
                    (step_instance_id,)).fetchone()
                if row is not None and (row["claim_epoch"] or 0) not in (0, claim_epoch):
                    raise StaleClaimFenced(
                        f"Step instance {step_instance_id} was reclaimed: {kind} "
                        f"refused for claim_epoch {claim_epoch}.")

            cur = conn.execute(
                "INSERT INTO skillflow_active_ops "
                "(run_id, step_instance_id, claim_epoch, owner, kind, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, step_instance_id, claim_epoch or 0,
                 worker_identity(kind), kind, detail))
            op_id = int(cur.lastrowid)
            admitted = dict(conn.execute(
                "SELECT * FROM skillflow_active_ops WHERE id = ?", (op_id,)
            ).fetchone())
        try:
            if step_id is None and admitted.get("step_instance_id"):
                with self._ro() as conn:
                    step = conn.execute(
                        "SELECT step_id FROM skillflow_steps WHERE id = ?",
                        (admitted["step_instance_id"],)).fetchone()
                    step_id = step["step_id"] if step else None
            self.trace(
                run_id, "step", "operation_admitted",
                {"operation_id": op_id, "kind": admitted["kind"],
                 "detail": admitted["detail"], "owner": admitted["owner"],
                 "admitted_at": admitted["admitted_at"],
                 "step_instance_id": admitted["step_instance_id"],
                 "claim_epoch": admitted["claim_epoch"]},
                step_id=step_id, step_instance_id=admitted["step_instance_id"])
        except Exception:
            # Admission is the safety record. A diagnostic trace failure must not
            # strand that record or turn an admitted operation into a crash.
            logging.getLogger(__name__).debug(
                "operation admission trace unavailable", exc_info=True)
        return op_id

    def _recovery_decision_id(self, key: tuple[Any, ...]) -> str:
        encoded = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def _trace_has_identity(self, run_id: str, event: str, field: str,
                            value: str | int) -> bool:
        rows = self.trace_query(
            run_id,
            "SELECT payload_json FROM skillflow_trace "
            "WHERE run_id = ? AND event = ? ORDER BY seq DESC",
            (run_id, event))
        for trace_row in rows:
            try:
                if json.loads(trace_row["payload_json"]).get(field) == value:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _append_recovery_decision(self, run_id: str, decision_id: str,
                                  payload: dict, *, step_id: str | None,
                                  step_instance_id: int | None) -> bool:
        """Atomically append one durable decision across host processes.

        The key and public trace row share one ``BEGIN IMMEDIATE`` transaction
        in the actual trace database. A failed append rolls both back, while a
        racing host that loses the primary-key insert observes the committed
        decision without writing a second trace row.
        """
        if not run_id or not self._trace_enabled:
            return False
        project_id = self._get_project_id(run_id)
        trace_conn = self._get_trace_conn(project_id) if project_id else None
        target = trace_conn or self._conn
        clean = {key: self._clip(value) for key, value in payload.items()}
        serialized = self._serialize(clean)
        try:
            with self._lock:
                target.execute("BEGIN IMMEDIATE;")
                try:
                    target.execute(
                        "CREATE TABLE IF NOT EXISTS "
                        "aitelier_operation_recovery_decisions ("
                        "decision_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
                        "payload_json TEXT NOT NULL, "
                        "created_at TEXT NOT NULL DEFAULT (datetime('now')))")
                    inserted = target.execute(
                        "INSERT OR IGNORE INTO "
                        "aitelier_operation_recovery_decisions "
                        "(decision_id, run_id, payload_json) VALUES (?, ?, ?)",
                        (decision_id, run_id, serialized)).rowcount
                    if inserted:
                        target.execute(
                            "INSERT INTO skillflow_trace "
                            "(run_id, step_id, step_instance_id, seq, category, "
                            "event, payload_json) "
                            "SELECT ?, ?, ?, COALESCE(MAX(seq), 0) + 1, "
                            "'step', 'operation_recovery_decision', ? "
                            "FROM skillflow_trace WHERE run_id = ?",
                            (run_id, step_id or None, step_instance_id,
                             serialized, run_id))
                    else:
                        existing = target.execute(
                            "SELECT run_id, payload_json FROM "
                            "aitelier_operation_recovery_decisions "
                            "WHERE decision_id = ?", (decision_id,)).fetchone()
                        if (not existing or existing["run_id"] != run_id
                                or existing["payload_json"] != serialized):
                            raise RuntimeError(
                                f"recovery decision identity collision: {decision_id}")
                    target.commit()
                    return True
                except Exception:
                    target.rollback()
                    raise
        except Exception:
            logging.getLogger(__name__).debug(
                "operation recovery decision trace unavailable", exc_info=True)
            return False

    def reconcile_active_operations(self, run_id: str | None = None, *,
                                    trigger: str) -> dict:
        """Observe owners first and durably record the recovery decision.

        Observation never retires an operation.  The only exits remain the
        operation's own ``finally`` and SkillFlow's evidence-bearing operator
        release.
        """
        audit = self.audit_operation_owners(run_id)
        operations = self.unsettled_operations(run_id)
        for row in operations:
            if (row["kind"] == "tool_step"
                    and row.get("step_instance_id") is None):
                instance, epoch = self._tool_step_identity(
                    row["run_id"], row["detail"])
                if instance is not None:
                    with self._tx() as conn:
                        conn.execute(
                            "UPDATE skillflow_active_ops SET step_instance_id=?, "
                            "claim_epoch=? WHERE id=? AND step_instance_id IS NULL",
                            (instance, epoch, row["id"]))
                    row["step_instance_id"] = instance
                    row["claim_epoch"] = epoch
            dead = owner_is_dead(row["owner"])
            state = "dead" if dead is True else "alive" if dead is False else "unknown"
            decision = ("retain_original_owner" if state == "alive"
                        else "block_retry_pending_explicit_settlement")
            key = (trigger, row["id"], state, row.get("owner_lost_at"))
            if key in self._operation_recovery_decisions:
                continue
            decision_id = self._recovery_decision_id(key)
            step_id = None
            step_instance_id = row.get("step_instance_id")
            if step_instance_id:
                with self._ro() as conn:
                    step = conn.execute(
                        "SELECT step_id FROM skillflow_steps WHERE id = ?",
                        (step_instance_id,)).fetchone()
                    step_id = step["step_id"] if step else None
            payload = {
                "decision_id": decision_id, "trigger": trigger,
                "decision": decision, "owner_state": state,
                "operation_id": row["id"], "kind": row["kind"],
                "detail": row["detail"], "owner": row["owner"],
                "owner_lost_at": row["owner_lost_at"],
                "admitted_at": row["admitted_at"],
                "step_instance_id": step_instance_id,
                "claim_epoch": row["claim_epoch"],
            }
            if self._append_recovery_decision(
                    row["run_id"], decision_id, payload, step_id=step_id,
                    step_instance_id=step_instance_id):
                self._operation_recovery_decisions.add(key)
        return {**audit, "operations": operations}

    def release_operation(self, op_id: int, *, evidence: str) -> dict:
        """Release one exact operation only after its provenance is durable."""
        if not (evidence or "").strip():
            raise ValueError(
                "release_operation requires evidence. " + self.RELEASE_EVIDENCE_HINT)
        with self._ro() as conn:
            row = conn.execute(
                "SELECT * FROM skillflow_active_ops WHERE id = ?", (op_id,)
            ).fetchone()
            row = dict(row) if row else None
        if row is None:
            return {"released": False, "reason": f"no active operation {op_id}"}

        settlement_evidence = evidence[:2000]
        payload = {
            "operation_id": op_id,
            "kind": row["kind"],
            "detail": row["detail"],
            "owner": row["owner"],
            "owner_lost_at": row["owner_lost_at"],
            "admitted_at": row["admitted_at"],
            "step_instance_id": row["step_instance_id"],
            "claim_epoch": row["claim_epoch"],
            "evidence": settlement_evidence,
            "settlement_evidence": settlement_evidence,
        }
        self.trace(
            row["run_id"], "step", "op_released_by_operator", payload,
            step_instance_id=row["step_instance_id"])
        if not self._trace_has_identity(
                row["run_id"], "op_released_by_operator", "operation_id", op_id):
            raise RuntimeError(
                f"release trace for operation {op_id} was not durably recorded; "
                "operation remains unsettled")
        self._retire_op(op_id)
        return {
            "released": True,
            "operation_id": op_id,
            "operation": f"{row['kind']}:{row['detail']}",
            "run_id": row["run_id"],
            "owner": row["owner"],
            "owner_lost_at": row["owner_lost_at"],
            "admitted_at": row["admitted_at"],
            "step_instance_id": row["step_instance_id"],
            "claim_epoch": row["claim_epoch"],
            "settlement_evidence": settlement_evidence,
            "run_status": self._run_status(row["run_id"]),
        }

    def _operation_blocks_reentry(self, run_id: str, trigger: str) -> bool:
        if not self.unsettled_operations(run_id):
            return False
        self.reconcile_active_operations(run_id, trigger=trigger)
        return True

    def advance_run(self, run_id: str):
        if self._operation_blocks_reentry(run_id, "advance_before_reclaim"):
            return None
        return super().advance_run(run_id)

    def claim_next_step(self, run_id: str):
        if self._operation_blocks_reentry(run_id, "claim_before_admission"):
            return None
        return super().claim_next_step(run_id)
