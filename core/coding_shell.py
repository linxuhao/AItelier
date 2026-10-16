"""Host-only grants for one owned CPU shell invocation; never a model tool."""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote

PROFILE = "coding_shell"
ADMISSION_KIND = "coding_workspace"
OUTPUT_LIMIT = 12_000
_ISSUER = object()


def _detail(binding):
    return json.dumps(binding, sort_keys=True, separators=(",", ":"))


def _claim_binding(run, step, token, isolation):
    from core.repository_executor import IsolationUnavailable
    if (not run or run.get("status") != "running" or run.get("cancel_requested_at") or not run.get("graph_version")
            or not run.get("graph_digest") or not isolation
            or isolation.get("mode") != "worktree" or isolation.get("released_at")
            or isolation.get("project_id") != run.get("project_id")
            or not step or step.get("status") != "claimed"
            or step.get("run_id") != run.get("id")
            or run.get("current_node") != step.get("step_id")
            or not step.get("claimed_by") or not token.claim_epoch
            or step.get("id") != token.step_instance_id
            or step.get("step_id") != token.step_id
            or step.get("version") != token.version
            or step.get("claim_epoch") != token.claim_epoch):
        raise IsolationUnavailable("coding shell requires a current pinned run/worktree claim")
    root = Path(isolation.get("worktree_path") or "")
    source = Path(isolation.get("source_repo") or "")
    if (not root.is_absolute() or root.resolve(strict=True) != root
            or not source.is_absolute() or source.resolve(strict=True) != source
            or root == source or root in source.parents or source in root.parents):
        raise IsolationUnavailable("coding shell requires a distinct canonical owned worktree")
    return {"run_id": run["id"], "project_id": run["project_id"],
            "graph_name": run["graph_name"], "graph_version": run["graph_version"],
            "graph_digest": run["graph_digest"], "step_id": token.step_id,
            "step_instance_id": token.step_instance_id, "claim_epoch": token.claim_epoch,
            "version": token.version, "claimed_by": step["claimed_by"],
            "repo": str(root), "source_repo": str(source), "base_sha": isolation["base_sha"]}


@dataclass(frozen=True)
class _Grant:
    db: object
    sf: object
    claimed: object
    binding: dict
    issuer: object


def bind_coding_shell(db, sf, claimed):
    """Trusted host callback only: derive scope from public SDK/host records.

    No literal run/path/grant from model arguments is accepted. This does not
    authenticate a Meta session; that controller must authorize this callback.
    """
    from skillflow import SkillFlow
    from skillflow.core import ClaimedStep, ClaimToken
    from core import run_isolation as ri
    from core.repository_executor import IsolationUnavailable
    if not isinstance(sf, SkillFlow) or type(claimed) is not ClaimedStep or type(claimed.token) is not ClaimToken:
        raise IsolationUnavailable("coding shell requires the host's actual SDK claim")
    token = claimed.token
    run = sf.get_run(token.run_id)
    pinned = (sf.get_graph_version(run["graph_name"], run["graph_version"])
              if run and run.get("graph_version") else None)
    if not pinned or pinned.get("digest") != run.get("graph_digest"):
        raise IsolationUnavailable("coding shell requires its recorded historical graph pin")
    steps = [s for s in sf.get_steps(token.run_id) if s["id"] == token.step_instance_id]
    try:
        binding = _claim_binding(run, steps[0] if len(steps) == 1 else None,
                                 token, ri.record(db, token.run_id))
    except OSError as exc:
        raise IsolationUnavailable("coding shell owned worktree is unavailable") from exc
    return _Grant(db, sf, claimed, binding, _ISSUER)


def execute_coding_shell(grant, command: str, timeout: int = 300):
    """Execute only through the owned launcher; retain unknown write effects."""
    from core import repository_executor as ex, run_isolation as ri
    if type(grant) is not _Grant or grant.issuer is not _ISSUER:
        raise ex.IsolationUnavailable("caller cannot supply a coding shell grant")
    if not isinstance(command, str) or not command or "\0" in command or len(command.encode()) > 65536:
        raise ex.IsolationUnavailable("coding shell command must be bounded nonempty text")
    if type(timeout) is not int or not 1 <= timeout <= 7200:
        raise ex.IsolationUnavailable("coding shell timeout must be within1..7200")
    socket_path = os.environ.get(ex.HOST_LAUNCHER_ENV, "")
    if not socket_path:
        raise ex.IsolationUnavailable("coding shell requires the owned host launcher; no local fallback")
    current = bind_coding_shell(grant.db, grant.sf, grant.claimed)
    if current.binding != grant.binding:
        raise ex.IsolationUnavailable("coding shell grant changed")
    root = grant.binding["repo"]
    if ri.write_admissions(grant.db, root):
        raise ex.IsolationUnavailable("owned worktree has an unsettled write admission")
    binding = {**grant.binding, "command_sha256": hashlib.sha256(command.encode()).hexdigest()}
    admission = ri.admit_write(grant.db, root, kind=ADMISSION_KIND, detail=_detail(binding))
    packet = {**binding, "admission_id": admission["id"], "admission_owner": admission["owner"]}
    try:
        result = ex._launch_via_host(socket_path, Path(root), ["/bin/sh", "-c", command], timeout,
                                    run_id=binding["run_id"], profile=PROFILE, coding_grant=packet)
    except BaseException as exc:
        if getattr(exc, "physical_quiet", False) is True:
            ri.retire_write(grant.db, admission["id"])
        else:
            ri.mark_write_admission_pending(grant.db, admission["id"],
                                             "coding shell physical settlement unknown: " + str(exc)[:500])
        raise
    ri.retire_write(grant.db, admission["id"])
    result.admission_id = admission["id"]
    return result


def _read_rows(path, sql, args):
    uri = "file:" + quote(str(Path(path).resolve(strict=True)), safe="/") + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def validate_host_grant(cfg, request, repo, project_id):
    """Read existing ledgers only. The socket already authenticates backend UID."""
    from core.repository_host_launcher import LaunchRefused
    fields = {"run_id", "project_id", "graph_name", "graph_version", "graph_digest", "step_id",
              "step_instance_id", "claim_epoch", "version", "claimed_by", "repo", "source_repo",
              "base_sha", "command_sha256", "admission_id", "admission_owner"}
    grant = request.get("coding_grant")
    if not isinstance(grant, dict) or set(grant) != fields:
        raise LaunchRefused("coding shell requires a complete host grant")
    engine = cfg.get("coding_shell_engine_db")
    expected_entry = cfg.get("coding_shell_entry_sha256")
    if not isinstance(engine, str) or not Path(engine).is_absolute() or not expected_entry:
        raise LaunchRefused("coding shell requires operator-pinned engine DB and entry Source")
    if hashlib.sha256(Path(cfg["executor_entry"]).read_bytes()).hexdigest() != expected_entry:
        raise LaunchRefused("coding shell entry Source differs from operator pin")
    args = request.get("args")
    if (not isinstance(args, list) or len(args) != 3 or args[:2] != ["/bin/sh", "-c"]
            or not isinstance(args[2], str) or not args[2] or "\0" in args[2]
            or len(args[2].encode()) > 65536
            or hashlib.sha256(args[2].encode()).hexdigest() != grant["command_sha256"]):
        raise LaunchRefused("coding shell command differs from its host admission")
    if (request.get("writable_dirs") or request.get("relay_socket") or request.get("report_dir")
            or request.get("import_module") or request.get("pytest_timeout")
            or "workdir" in request or "npm_cache" in request):
        raise LaunchRefused("coding shell accepts no caller mounts, relay, environment or workdir")
    try:
        runs = _read_rows(engine, "SELECT id,status,project_id,current_node,graph_name,graph_version,graph_digest,cancel_requested_at "
                          "FROM skillflow_runs WHERE id=?", (request["run_id"],))
        history = _read_rows(engine, "SELECT digest FROM skillflow_graph_versions WHERE name=? AND version=?",
                             (grant["graph_name"], grant["graph_version"]))
        steps = _read_rows(engine, "SELECT id,status,run_id,step_id,version,claim_epoch,claimed_by "
                           "FROM skillflow_steps WHERE id=?", (grant["step_instance_id"],))
        owned = _read_rows(cfg["state_db"], "SELECT mode,released_at,project_id,worktree_path,source_repo,base_sha "
                           "FROM run_isolation WHERE run_id=?", (request["run_id"],))
        admissions = _read_rows(cfg["state_db"], "SELECT id,owner,kind,detail FROM checkout_write_admissions "
                                "WHERE canonical_checkout=?", (str(repo),))
        if (len(runs) != 1 or len(history) != 1 or history[0]["digest"] != grant["graph_digest"]
                or len(steps) != 1 or len(owned) != 1 or len(admissions) != 1):
            raise ValueError("ambiguous/missing current run, historical pin, claim, ownership or admission")
        run, step, isolation, adm = runs[0], steps[0], owned[0], admissions[0]
        # No SDK constructor/private operation API on the host service.
        if (run["status"] != "running" or run["cancel_requested_at"] or run["project_id"] != project_id
                or run["current_node"] != grant["step_id"]
                or any(run[k] != grant[k] for k in ("graph_name", "graph_version", "graph_digest"))
                or not run["graph_version"] or not run["graph_digest"]
                or step["status"] != "claimed" or step["run_id"] != run["id"]
                or step["id"] != grant["step_instance_id"] or step["step_id"] != grant["step_id"]
                or any(step[k] != grant[k] for k in ("version", "claim_epoch", "claimed_by"))
                or not step["claim_epoch"] or not step["claimed_by"]
                or isolation["mode"] != "worktree" or isolation["released_at"] or isolation["project_id"] != project_id
                or isolation["worktree_path"] != str(repo) or grant["repo"] != str(repo)
                or grant["run_id"] != request["run_id"] or grant["project_id"] != project_id
                or isolation["source_repo"] != grant["source_repo"] or isolation["base_sha"] != grant["base_sha"]):
            raise ValueError("stale or foreign coding shell scope")
        source = Path(isolation["source_repo"]).resolve(strict=True)
        if str(source) != isolation["source_repo"] or repo == source or repo in source.parents or source in repo.parents:
            raise ValueError("owned worktree overlaps Source")
        detail = {k: v for k, v in grant.items() if k not in {"admission_id", "admission_owner"}}
        if (adm["id"] != grant["admission_id"] or adm["owner"] != grant["admission_owner"]
                or adm["kind"] != ADMISSION_KIND or adm["detail"] != _detail(detail)):
            raise ValueError("coding shell host admission is absent, pending or foreign")
    except (sqlite3.Error, OSError, KeyError, ValueError, TypeError) as exc:
        raise LaunchRefused("coding shell host grant unavailable: " + str(exc)) from exc
    return {"profile": PROFILE, "args": args, "run_id": request["run_id"],
            "project_id": project_id, "repo": str(repo), "base_sha": grant["base_sha"],
            "timeout": request["timeout"], "writable_dirs": [], "relay_socket": "",
            "import_module": "", "pytest_timeout": False, "report_dir": "",
            "git_common": str(source / ".git"), "shell_request": request}
