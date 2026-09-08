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
from core.state_attempts import _public


class ExternalAttempts:
    def __init__(self, attempts, actor: str):
        self.attempts = attempts
        self.store = attempts.store
        self.actor = text(actor, 'authenticated reporter', 300)

    def register(self, project_id, node_key, expected_revision, harness, external_id,
                 request_key, instruction=''):
        identity = {'harness': key(harness, 'harness'),
                    'external_id': text(external_id, 'external execution identity', 500),
                    'reporting_actor': self.actor}
        return self.attempts._reserve(project_id, node_key, expected_revision, None, request_key,
                                      instruction, external=identity)

    def observe(self, attempt_id, observation_id, expected_version, context_hash,
                status, report_ref, report_sha256, *, quiescent=False,
                artifact=None, artifact_kind=None, detail=''):
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
        terminal = status in {'candidate','failed'}
        if terminal and not quiescent:
            raise StateConflict('terminal result needs the external harness to attest all relevant workers/operations are quiescent')
        if status == 'candidate':
            size = {'git-sha1': 40, 'sha256': 64}.get(artifact_kind) if isinstance(artifact_kind,str) else None
            if size is None or not isinstance(artifact,str) or not re.fullmatch('[0-9a-f]{'+str(size)+'}', artifact):
                raise StateGraphError('candidate requires an exact artifact digest and explicit git-sha1 or sha256 kind')
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
            if a['reporting_actor'] != self.actor:
                raise StateConflict('external observation belongs to a different authenticated reporter')
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
            current = self.attempts._pins_current(conn, a)
            result_status = 'superseded' if terminal and not current else status
            version = expected_version + 1
            conn.execute('INSERT INTO state_external_observations(attempt_id,observation_id,version,status,resulting_status,'
                         'quiescent,artifact_ref,artifact_kind,report_ref,report_sha256,actor,detail,payload_hash,context_hash,created_at) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (attempt_id,observation_id,version,status,result_status,int(quiescent),artifact,artifact_kind,
                          report_ref,report_sha256,self.actor,detail,fingerprint,context_hash,now()))
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
                 'report_sha256':report_sha256,'quiescent':quiescent,'actor':self.actor,'stale_inputs':not current})
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
