"""DSH coding through the existing Coding Mode workspace and owned executor.

The authenticated per-project driver owns the State attempt; a native session
selects its own independent workspace. No model-supplied source paths or owner
credentials, and no additional planning/approval workflow.
"""
from __future__ import annotations

import hashlib
import subprocess

from core import coding_workspace as cw
from core.state_commands import execute
from core.state_graph import StateConflict


def pipeline_source(service, project_id):
    """Resolve a State project through its existing authenticated source binding."""
    execute(service, "project_overview", {"project_id": project_id})
    source = service._source(project_id)
    if not source:
        raise StateConflict("State project has no bound source repository")
    return source


def _scope(db, service, project_id, driver_id, session_id, attempt_id, fence, action, commit_message=None):
    execute(service, "attempt_detail", {"attempt_id": attempt_id})
    attempt = service.attempts.get(attempt_id)
    if (not attempt or attempt["project_id"] != project_id
            or attempt.get("owner_driver_id") != driver_id
            or attempt.get("owner_fence") != fence):
        raise StateConflict("project_work requires your own project attempt and current owner fence")
    if attempt["status"] in {"failed", "abandoned"}:
        raise StateConflict("this attempt is no longer executable")
    if attempt["status"] == "candidate" and action not in {"read", "deliver", "push"}:
        raise StateConflict("candidate work is immutable; register the next attempt before changing it")
    source = pipeline_source(service, project_id)
    ident = hashlib.sha256(f"{driver_id}\0{session_id}\0{attempt_id}\0{fence}".encode()).hexdigest()[:32]
    pid, sid = "dsh-work-" + ident, "dsh-" + ident
    project = db.ensure_project(pid, name=f"DSH {project_id} / {session_id}",
                                owner_email=service.actor, repo_type="existing", repo_path=source,
                                config_name=cw.GRAPH)
    if project.get("repo_path") != source:
        raise StateConflict("workspace source binding changed")
    with db.get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO sessions(id,mode) VALUES(?, 'coding')", (sid,))
        conn.commit()
    return cw.authorize_entry(db, session_id=sid, project_id=pid, writer=service.actor,
                              commit_message=commit_message,
                              allow_unrelated_head_movement=True), source


def _git(source, *args):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(source), *args],
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        from core.git_ops import redact_url_credentials
        raise cw.Unavailable(redact_url_credentials(result.stderr[-4000:]) or "git failed")
    return result.stdout.strip()


def project_work(db, service, *, project_id, driver_id, session_id, attempt_id, fence,
                 action, arguments=None, sf=None):
    args = arguments or {}
    commit_message = args.get("commit_message") if action == "deliver" else None
    scope, source = _scope(db, service, project_id, driver_id, session_id, attempt_id, fence, action,
                           commit_message=commit_message)
    if action == "push":
        # Only the project's configured origin and current branch; ordinary
        # fast-forward push. No force flag, caller URL, arbitrary ref or hooks.
        expected = args.get("expected_head")
        head = _git(source, "rev-parse", "HEAD")
        if not expected or expected != head:
            raise cw.Unavailable("push requires expected_head matching current Source HEAD")
        branch = _git(source, "symbolic-ref", "--short", "HEAD")
        remote = _git(source, "remote", "get-url", "origin")
        _git(source, "push", "origin", head + ":refs/heads/" + branch)
        from core.git_ops import redact_url_credentials
        return {"status": "pushed", "commit": head, "branch": branch,
                "remote": redact_url_credentials(remote)}
    req = cw.CodingRequest(db, scope, sf=sf)
    try:
        if action == "deliver":
            result = req.finish(note=f"DSH {driver_id}/{session_id} / {attempt_id}")
            return {**(result or {"status": "nothing_to_deliver"}),
                    "source_head": _git(source, "rev-parse", "HEAD")}
        ws = req.workspace(create=action == "open")
        if ws is None:
            raise cw.Unavailable("open this native session's project_work workspace first")
        if action == "open":
            return {"run_id": ws.run_id, "workspace": str(ws.root), "source": str(ws.source),
                    "base_sha": ws.base_sha, "session_id": session_id,
                    "execution_project_id": scope.project_id,
                    "note": "Use project_work read/write/edit/bash on this tree; deliver applies to Source and can commit. No additional plan gate."}
        if action == "read":
            rel, content = cw.read_text(ws, args.get("path", ""))
            return {"path": rel, "content": content}
        req.require_writable(ws)
        if action in {"write", "edit"}:
            path = args.get("path", "")
            if action == "edit":
                from skillflow.write_tools import _unique_replace
                _, old = cw.read_text(ws, path)
                old_str, new_str = args.get("old_str"), args.get("new_str")
                if not isinstance(old_str, str) or not old_str or not isinstance(new_str, str):
                    raise cw.Unavailable("edit requires nonempty old_str and string new_str")
                content, error = _unique_replace(old, old_str, new_str, tool="project_work.edit", name=path)
                if error:
                    raise cw.Unavailable(str(error))
                create = False
            else:
                content, create = args.get("content"), bool(args.get("create", False))
                if not isinstance(content, str):
                    raise cw.Unavailable("write requires string content")
            rel = cw.write_text(db, ws, path, content, create=create,
                                detail=f"DSH {driver_id}/{session_id} {action}")
            return {"path": rel, "status": "written"}
        if action == "bash":
            from core.coding_shell import execute_coding_shell
            result = execute_coding_shell(req.shell_grant(ws), args.get("command", ""),
                                          args.get("timeout", 120))
            return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                    "timed_out": result.timed_out, "truncated": result.output_truncated,
                    "run_id": ws.run_id, "workspace": str(ws.root)}
        raise cw.Unavailable("unknown project_work action")
    finally:
        req.release()
