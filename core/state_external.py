"""External harness adapter: store observations, never execute remote work.

Custom harnesses/director subagents own their workers and their verifier. This
adapter checks immutable scope, optimistic ordering, source identity and final
quiescence declarations. It does not pretend to inspect a remote process or
certify the honesty of a report. Acceptance itself is shared with SkillFlow.
"""
from __future__ import annotations

import json
import re

from core.state_graph import StateConflict, StateGraphError, digest, integer, key, now, text
from core.drivers import actor_continues
from core.state_attempts import _public
from core.state_claims import ClaimError
from core.state_enforcement import enforced
from core.state_report_integrity import retain_report, store_report_blob, validate_external_semantics


class ExternalAttempts:
    def __init__(self, attempts, actor: str, driver_id: str | None = None, is_admin: bool = False):
        self.attempts = attempts
        self.store = attempts.store
        self.actor = text(actor, 'authenticated reporter', 300)
        # Recorded as the attempt's owner only where multi_driver=on (P1 leases).
        self.driver_id = driver_id
        self.is_admin = is_admin is True

    def register(self, project_id, node_key, expected_revision, harness, external_id,
                 request_key, instruction='', base_sha=None, preflight=None, claim_id=None, fence=None):
        identity = {'harness': key(harness, 'harness'),
                    'external_id': text(external_id, 'external execution identity', 500),
                    'reporting_actor': self.actor}
        from core import deployment_quiescence as dq
        with dq.operation_admission_fence():
            return self.attempts._reserve(
                project_id, node_key, expected_revision, None, request_key,
                instruction, external=identity, base_sha=base_sha, preflight=preflight,
                owner_driver_id=self.driver_id, claim_id=claim_id, fence=fence, is_admin=self.is_admin)

    def _authorize_report(self, conn, a, fence) -> bool:
        """Who may append an observation, and with which fence (P3, §7.3 rule 2).

        Returns True when this is a LATE report on an abandoned attempt: it is
        recorded (bytes are never dropped) but can never become current.
        Rules, in order:
        * an attempt with no owner (legacy, or identity off): the recorded
          reporter continues it, exactly as before;
        * an abandoned attempt: its original reporter may still file, with any
          fence - the row is marked late_after_abandon;
        * an owned, active attempt: the owner driver, and in an enforced
          project with its current fence (``fence_required`` / ``stale_fence``);
          the previous owner after a takeover or handoff gets ``stale_fence``,
          anybody else ``not_attempt_owner``.
        """
        continues = actor_continues(a['reporting_actor'], self.actor)
        owner = a['owner_driver_id']
        if owner is None or self.driver_id is None:
            if not continues:
                raise StateConflict('external observation belongs to a different authenticated reporter')
            if fence is not None and a['owner_fence'] and fence != a['owner_fence']:
                raise ClaimError('stale_fence', f"attempt owner fence is {a['owner_fence']}, not {fence}")
            return a['status'] == 'abandoned'
        if a['status'] == 'abandoned':
            if not continues and owner != self.driver_id:
                raise StateConflict('external observation belongs to a different authenticated reporter')
            return True
        if owner != self.driver_id:
            if continues:
                raise ClaimError('stale_fence', f"attempt {a['attempt_id']} is now owned by driver {owner} "
                                 f"(fence {a['owner_fence']}); your fence is stale", owner_driver_id=owner)
            raise ClaimError('not_attempt_owner', f"attempt belongs to driver {owner}", owner_driver_id=owner)
        if fence is None:
            if enforced(conn, a['project_id']):
                raise ClaimError('fence_required', 'this project enforces claims: pass fence=<owner_fence> '
                                 'with every report', owner_fence=a['owner_fence'])
        elif fence != a['owner_fence']:
            raise ClaimError('stale_fence', f"attempt owner fence is {a['owner_fence']}, not {fence}; reload",
                             owner_fence=a['owner_fence'])
        return False

    def register_relay_handoff(self, project_id, node_key, expected_revision,
                               harness, external_id, request_key, instruction,
                               relay_handoff):
        """Register an external owner against one immutable failed-run relay.

        The State service constructs and validates ``relay_handoff`` from the
        public relay inventory immediately before this call. It lives in the
        attempt's existing frozen context; no second handoff store can drift
        away from the context hash that external observations must report.
        """
        identity = {'harness': key(harness, 'harness'),
                    'external_id': text(external_id, 'external execution identity', 500),
                    'reporting_actor': self.actor}
        from core import deployment_quiescence as dq
        with dq.operation_admission_fence():
            return self.attempts._reserve(
                project_id, node_key, expected_revision, None, request_key,
                instruction, external=identity, relay_handoff=relay_handoff,
                owner_driver_id=self.driver_id)

    @staticmethod
    def _terminal_report(report_ref: str, report_sha256: str) -> tuple[str, bytes]:
        return retain_report(report_ref, report_sha256, completed=True)

    def observe(self, attempt_id, observation_id, expected_version, context_hash,
                status, report_ref, report_sha256, *, quiescent=False,
                artifact=None, artifact_kind=None, detail='', fence=None):
        """Append one explicit harness observation. Never accept a goal here.

        register's context_hash identifies the exact revision/criteria/dependency
        receipts the harness agreed to inspect. Terminal observations require a
        pinned report and an explicit 'all workers settled' declaration. Neither
        a timeout nor a missing callback clears the active-attempt lock.
        """
        key(observation_id, 'observation id')
        integer(expected_version, 'expected observation version', 0, 2**31-2)
        if not isinstance(status, str) or status not in {'running','paused','unknown','candidate','failed'}:
            raise StateGraphError('external status must be running, paused, unknown, candidate or failed; never VERIFIED')
        if type(quiescent) is not bool:
            raise StateGraphError('quiescent must be an explicit boolean')
        for value, label in [(context_hash, 'context hash'), (report_sha256, 'report SHA-256')]:
            if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
                raise StateGraphError(label + ' must be 64 lowercase hexadecimal characters')
        report_ref = text(report_ref, 'report reference', 2000)
        if not isinstance(detail, str) or len(detail)>8000:
            raise StateGraphError('detail must be bounded text')
        if fence is not None and type(fence) is not int:
            raise StateGraphError('fence must be an integer')
        terminal = status in {'candidate','failed'}
        if terminal and not quiescent:
            raise StateConflict('terminal result needs the external harness to attest all relevant workers/operations are quiescent')
        with self.store.transaction() as conn:
            owner = self.attempts._attempt(conn, attempt_id)
            if owner['execution_kind'] != 'external':
                raise StateConflict('external reports cannot complete or override a SkillFlow attempt')
            self._authorize_report(conn, owner, fence)
            if context_hash != digest(json.loads(owner['context_json'])):
                raise StateConflict('report describes a different frozen goal/contract/dependency context')
            prior_observation = conn.execute(
                'SELECT * FROM state_external_observations WHERE attempt_id=? AND observation_id=?',
                (attempt_id, observation_id)).fetchone()
            if prior_observation:
                # A retry binds the accepted retained report identity, not the
                # producer pathname which cleanup may already have removed.
                retry = {'attempt_id':attempt_id, 'observation_id':observation_id,
                    'expected_version':expected_version, 'context_hash':context_hash,
                    'status':status, 'report_ref':prior_observation['report_ref'],
                    'report_sha256':report_sha256, 'quiescent':quiescent,
                    'artifact':artifact, 'artifact_kind':artifact_kind,
                    'detail':detail, 'actor':self.actor}
                if digest(retry) != prior_observation['payload_hash']:
                    raise StateConflict('observation ID was already used with a different payload; append a new correction')
                return {**_public(owner), 'observation':dict(prior_observation), 'idempotent':True}
            if not prior_observation and owner['observation_version'] != expected_version:
                raise StateConflict('external observation version changed; reload before appending')
            if owner['status'] in {'failed','superseded'}:
                raise StateConflict('this external attempt is terminal; use a new attempt')
            if status == 'candidate':
                used = conn.execute(
                    'SELECT attempt_id,observation_id FROM state_external_observations '
                    'WHERE report_sha256=? LIMIT 1', (report_sha256,)).fetchone()
                if used and (used['attempt_id'] != attempt_id
                             or used['observation_id'] != observation_id):
                    raise StateConflict(
                        'candidate report digest was already bound to an earlier observation; '
                        'produce a fresh report for this candidate')
        report_bytes = None
        if terminal:
            report_ref, report_bytes = self._terminal_report(report_ref, report_sha256)
            validate_external_semantics(report_bytes, status, artifact)
        git_artifact = None
        if status == 'candidate':
            size = {'git-sha1': 40, 'sha256': 64}.get(artifact_kind) if isinstance(artifact_kind,str) else None
            if size is None or not isinstance(artifact,str) or not re.fullmatch('[0-9a-f]{'+str(size)+'}', artifact):
                raise StateGraphError('candidate requires an exact artifact digest and explicit git-sha1 or sha256 kind')
            if artifact_kind == 'git-sha1':
                from core.state_git_artifacts import retain_candidate
                git_artifact = retain_candidate(report_bytes, artifact)
        elif artifact is not None or artifact_kind is not None:
            raise StateGraphError('only a candidate report may declare the immutable artifact')
        payload = {'attempt_id':attempt_id, 'observation_id':observation_id, 'expected_version':expected_version,
                   'context_hash':context_hash, 'status':status, 'report_ref':report_ref, 'report_sha256':report_sha256,
                   'quiescent':quiescent, 'artifact':artifact, 'artifact_kind':artifact_kind,
                   'detail':detail, 'actor':self.actor}
        fingerprint = digest(payload)
        with self.store.transaction(write=True) as conn:
            a = self.attempts._attempt(conn, attempt_id)
            if a['execution_kind'] != 'external':
                raise StateConflict('external reports cannot complete or override a SkillFlow attempt')
            late = self._authorize_report(conn, a, fence)
            prior = conn.execute('SELECT * FROM state_external_observations WHERE attempt_id=? AND observation_id=?',
                                 (attempt_id, observation_id)).fetchone()
            if prior:
                if prior['payload_hash'] != fingerprint:
                    raise StateConflict('observation ID was already used with a different payload; append a new correction')
                return {**_public(a), 'observation':dict(prior), 'idempotent':True}
            if context_hash != digest(json.loads(a['context_json'])):
                raise StateConflict('report describes a different frozen goal/contract/dependency context')
            if a['observation_version'] != expected_version:
                raise StateConflict('external observation version changed; reload before appending')
            if a['status'] in {'failed','superseded'}:
                raise StateConflict('this external attempt is terminal; use a new attempt')
            # Once settled, success may only be retracted as a settled failure.
            # The original artifact/report stays immutable; changed code means
            # a new attempt, not a silently edited completion receipt.
            if a['status'] == 'candidate' and status != 'failed':
                raise StateConflict('candidate completion is immutable; record evidence or start a new attempt')
            if status == 'candidate':
                used = conn.execute(
                    'SELECT attempt_id,observation_id FROM state_external_observations '
                    'WHERE report_sha256=? LIMIT 1', (report_sha256,)).fetchone()
                if used:
                    raise StateConflict(
                        'candidate report digest was already bound to an earlier observation; '
                        'produce a fresh report for this candidate')
            current = self.attempts._pins_current(conn, a)
            result_status = 'superseded' if terminal and not current else status
            if late:
                # §4.4: the attempt was abandoned after its lease lapsed. The
                # bytes are kept, the row says late_after_abandon, and the result
                # is superseded: it cannot make the node CANDIDATE or revive the
                # attempt. A finished worker registers its artifact anew.
                result_status = 'superseded'
            version = expected_version + 1
            if report_bytes is not None:
                store_report_blob(conn, report_ref, report_sha256, report_bytes)
            if git_artifact is not None:
                from core.state_git_artifacts import store_candidate
                store_candidate(conn, git_artifact)
            conn.execute('INSERT INTO state_external_observations(attempt_id,observation_id,version,status,resulting_status,'
                         'quiescent,artifact_ref,artifact_kind,report_ref,report_sha256,actor,detail,payload_hash,context_hash,created_at,'
                         'late_after_abandon,fence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (attempt_id,observation_id,version,status,result_status,int(quiescent),artifact,artifact_kind,
                          report_ref,report_sha256,self.actor,detail,fingerprint,context_hash,now(),int(late),fence))
            if late:
                conn.execute('UPDATE state_attempts SET observation_version=?,updated_at=? WHERE attempt_id=?',
                             (version,now(),attempt_id))
                self.store._event(conn, a['project_id'], a['node_key'], 'external_attempt_observed',
                    {'attempt_id':attempt_id,'observation_id':observation_id,'version':version,'status':result_status,
                     'reported_status':status,'report_sha256':report_sha256,'quiescent':quiescent,'actor':self.actor,
                     'stale_inputs':not current,'late_after_abandon':True,'fence':fence})
                updated = self.attempts._attempt(conn, attempt_id)
                row = conn.execute('SELECT * FROM state_external_observations WHERE attempt_id=? AND observation_id=?',
                                   (attempt_id,observation_id)).fetchone()
                return {**_public(updated),'observation':dict(row),'idempotent':False,'stale_inputs':not current,
                        'late_after_abandon':True}
            owner_status = {'running':'active','paused':'paused','unknown':'unknown'}.get(
                result_status, 'settled')
            updated_owner = conn.execute(
                'UPDATE state_external_owners SET status=?,updated_at=?,settled_at=? WHERE attempt_id=?',
                (owner_status, now(), now() if owner_status == 'settled' else None, attempt_id))
            if updated_owner.rowcount != 1:
                raise StateConflict('external attempt has no durable owner registration')
            node = self.store._node(conn, a['project_id'], a['node_key'])
            if result_status != 'candidate' and node['verified_receipt']:
                rec = conn.execute('SELECT attempt_id FROM state_acceptances WHERE receipt_id=?', (node['verified_receipt'],)).fetchone()
                if rec and rec[0] == attempt_id:
                    affected = self.store._invalidate(conn, a['project_id'], a['node_key'])
                    self.store._event(conn, a['project_id'], a['node_key'], 'acceptance_invalidated',
                        {'reason':'external harness retracted its accepted candidate','attempt_id':attempt_id,'invalidated':affected})
            conn.execute('UPDATE state_attempts SET status=?,observation_version=?,artifact_ref=?,artifact_kind=?,'
                         'terminal_observation_id=?,error=?,updated_at=? WHERE attempt_id=?',
                         (result_status,version,artifact if status=='candidate' else a['artifact_ref'],
                          artifact_kind if status=='candidate' else a['artifact_kind'],
                          observation_id if terminal else a['terminal_observation_id'],
                          detail if status=='failed' else None,now(),attempt_id))
            if result_status == 'candidate':
                latest = conn.execute('SELECT attempt_id FROM state_attempts WHERE project_id=? AND node_key=? ORDER BY seq DESC LIMIT 1',
                                      (a['project_id'],a['node_key'])).fetchone()
                if latest[0] == attempt_id and node['status'] != 'VERIFIED':
                    conn.execute("UPDATE state_nodes SET status='CANDIDATE',updated_at=? WHERE project_id=? AND node_key=?",
                                 (now(),a['project_id'],a['node_key']))
            self.store._event(conn, a['project_id'], a['node_key'], 'external_attempt_observed',
                {'attempt_id':attempt_id,'observation_id':observation_id,'version':version,'status':result_status,
                 'report_sha256':report_sha256,'quiescent':quiescent,'actor':self.actor,'stale_inputs':not current,
                 'fence':fence})
            if terminal and a['owner_driver_id']:
                # A settled attempt releases the claims bound to it (design §4.3).
                for claim in conn.execute("SELECT * FROM state_node_claims WHERE attempt_id=? AND status='live'",
                                          (attempt_id,)).fetchall():
                    conn.execute("UPDATE state_node_claims SET status='released',updated_at=? WHERE claim_id=?",
                                 (now(), claim['claim_id']))
                    conn.execute("INSERT INTO state_claim_history(claim_id,project_id,node_key,driver_id,purpose,status,"
                                 "fence,lease_expires_at,actor,reason,created_at) VALUES(?,?,?,?,?,'released',?,?,?,?,?)",
                                 (claim['claim_id'], claim['project_id'], claim['node_key'], claim['driver_id'],
                                  claim['purpose'], claim['fence'], claim['lease_expires_at'], self.actor,
                                  f'attempt settled as {result_status}', now()))
                    self.store._event(conn, a['project_id'], a['node_key'], 'claim_released', {
                        'claim_id': claim['claim_id'], 'driver_id': claim['driver_id'], 'purpose': claim['purpose'],
                        'fence': claim['fence'], 'reason': f'attempt settled as {result_status}',
                        'attempt_id': attempt_id, 'actor': self.actor, 'break_glass': False})
            updated = self.attempts._attempt(conn, attempt_id)
            row = conn.execute('SELECT * FROM state_external_observations WHERE attempt_id=? AND observation_id=?',
                               (attempt_id,observation_id)).fetchone()
            return {**_public(updated),'observation':dict(row),'idempotent':False,'stale_inputs':not current}

    def inspect(self, attempt_id):
        """Read-only observation view; never calls a runtime or opens a report URL."""
        with self.store.transaction() as conn:
            a = self.attempts._attempt(conn, attempt_id)
            if a['execution_kind'] != 'external':
                raise StateConflict('not an external attempt')
            last = conn.execute('SELECT * FROM state_external_observations WHERE attempt_id=? ORDER BY version DESC LIMIT 1',
                                (attempt_id,)).fetchone()
            return {**_public(a),'stale_inputs':not self.attempts._pins_current(conn,a),
                    'last_observation':dict(last) if last else None,
                    'execution_owner':'external harness; no AItelier worker or SkillFlow run',
                    'remote_liveness':'reported, not independently probed'}
