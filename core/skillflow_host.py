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

import logging
from typing import Any

from skillflow import SkillFlow
from skillflow.identity import owner_is_dead


class AItelierSkillFlow(SkillFlow):
    """SkillFlow with durable tool-operation recovery enforced at host ingress."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._operation_recovery_decisions: set[tuple[Any, ...]] = set()

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
        derived_tool_identity = False
        if kind == "tool_step" and step_instance_id is None:
            step_instance_id, claim_epoch = self._tool_step_identity(run_id, detail)
            derived_tool_identity = step_instance_id is not None
        # The inline operation is admitted immediately BEFORE its claim CAS.
        # SkillFlow's generic epoch check therefore cannot validate the next
        # epoch yet. Admit under the existing cancellation transaction, then
        # bind the already-created operation row to the pending instance/next
        # epoch before any tool effect starts.
        op_id = super()._admit_op(
            kind, run_id,
            step_instance_id=None if derived_tool_identity else step_instance_id,
            claim_epoch=0 if derived_tool_identity else claim_epoch,
            detail=detail)
        if derived_tool_identity:
            with self._tx() as conn:
                conn.execute(
                    "UPDATE skillflow_active_ops SET step_instance_id=?, "
                    "claim_epoch=? WHERE id=?",
                    (step_instance_id, claim_epoch, op_id))
        try:
            row = next(r for r in self.unsettled_operations(run_id)
                       if r["id"] == op_id)
            step_id = None
            if row.get("step_instance_id"):
                with self._ro() as conn:
                    step = conn.execute(
                        "SELECT step_id FROM skillflow_steps WHERE id = ?",
                        (row["step_instance_id"],)).fetchone()
                    step_id = step["step_id"] if step else None
            self.trace(
                run_id, "step", "operation_admitted",
                {"operation_id": op_id, "kind": row["kind"],
                 "detail": row["detail"], "owner": row["owner"],
                 "admitted_at": row["admitted_at"],
                 "step_instance_id": row["step_instance_id"],
                 "claim_epoch": row["claim_epoch"]},
                step_id=step_id, step_instance_id=row["step_instance_id"])
        except Exception:
            # Admission is the safety record. A diagnostic trace failure must not
            # strand that record or turn an admitted operation into a crash.
            logging.getLogger(__name__).debug(
                "operation admission trace unavailable", exc_info=True)
        return op_id

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
            self._operation_recovery_decisions.add(key)
            step_id = None
            step_instance_id = row.get("step_instance_id")
            if step_instance_id:
                with self._ro() as conn:
                    step = conn.execute(
                        "SELECT step_id FROM skillflow_steps WHERE id = ?",
                        (step_instance_id,)).fetchone()
                    step_id = step["step_id"] if step else None
            self.trace(
                row["run_id"], "step", "operation_recovery_decision",
                {"trigger": trigger, "decision": decision,
                 "owner_state": state, "operation_id": row["id"],
                 "kind": row["kind"], "detail": row["detail"],
                 "owner": row["owner"], "owner_lost_at": row["owner_lost_at"],
                 "admitted_at": row["admitted_at"],
                 "step_instance_id": step_instance_id,
                 "claim_epoch": row["claim_epoch"]},
                step_id=step_id, step_instance_id=step_instance_id)
        return {**audit, "operations": operations}

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
