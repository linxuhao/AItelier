"""One authentic owned working tree per Coding Mode interaction, and its handoff.

Coding Mode's file tools, owned shell and session file view all work in ONE
tree, W: the worktree the run-isolation record of a real host-held
``coding_workspace`` SDK run owns. Source C (the project's canonical checkout)
changes only through the trusted handoff at the end of a successful
interaction. This module is that controller. It is not a runtime or a ledger:

* **Trusted entry.** ``authorize_entry`` is called by the authenticated
  writer/controller chat entry, which fixes session and project before any
  model sampling. Its scope object cannot be built from model arguments. The
  ``sessions`` table has no owner column: a session id is a bearer handle
  among authenticated writers and this module claims no per-user tenancy.
* **Project request lock.** A kernel ``flock`` keyed by project covers
  discovery, creation, binding, every repository tool, plan control and the
  handoff of one request. A second request receives ``Busy``; nothing queues.
  A different session stays busy while this project has an unresolved owner.
* **Durable records** under ``datadir.native_context_dir()/coding_workspace``
  (outside every sandbox mount): creation intent BEFORE the engine start,
  sealed origin binding, seed manifest, governing plan association, decision
  and handoff intents with frozen blobs. Temporary write + rename + file and
  directory fsync; an unreadable record refuses.
* **Real SDK lifecycle.** ``create_run``/``start_run``/``advance_run``/
  ``claim_next_step`` of the static graph produce the actual claim the owned
  shell grant requires. A restart never manufactures a claim: the same run is
  reclaimed through the ordinary engine recovery only after positive old-writer
  quiet, and the new epoch is sealed.
* **Handoff.** The W delta relative to its seed B is frozen with raw blobs
  before an exclusive ``coding_workspace`` admission on C; each Source path is
  compared before/after under that exclusion, ``applying`` is persisted before
  the first effect, unknown/partial effects keep the admission pending (no
  ``finally`` retirement) and recovery distinguishes before/after/third state.
  An explicitly requested commit uses the SAME admission through a private
  index, hook/signature-free commit, ref CAS and owned index settlement.

The engine-side fences (``fence_create``/``fence_control``) are called by
``AItelierSkillFlow`` so every ingress — REST, MCP, CLI, scheduler, model
proxies — meets them, not just the paths this module wraps.
"""
from __future__ import annotations

import contextvars
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from core import closed_git, datadir

GRAPH = "coding_workspace"
WORK_STEP = "work"
SUMMARY_SLOT = "summary"
PLAN_GRAPH = "coding_task"
PLAN_STEP = "plan"
STATIC_GRAPH_PATH = Path(__file__).resolve().parent.parent / "configs" / "coding_workspace.yaml"
SCHEMA = 1
CLOSED = ("closed", "dismissed", "abandoned")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_BLOB = 64 << 20
_MAX_DELTA = 2000


class Unavailable(RuntimeError):
    """The owned workspace cannot serve this request; nothing fell back to Source."""
    status = "unavailable"


class Busy(Unavailable):
    status = "busy"


class PlanGate(Unavailable):
    status = "plan_gate"


# ── trusted context (engine fences) ──────────────────────────────────

_TRUSTED = contextvars.ContextVar("aitelier_coding_workspace_trusted", default=None)


@contextmanager
def _trusted(purpose: str):
    token = _TRUSTED.set(purpose)
    try:
        yield
    finally:
        _TRUSTED.reset(token)


def _refused(message: str):
    from skillflow.exceptions import SkillFlowError
    return SkillFlowError(message)


def fence_create(graph_name: str) -> None:
    """No route but the controller's own creation may create a coding_workspace run."""
    if graph_name == GRAPH and _TRUSTED.get() != "create":
        raise _refused("coding_workspace runs are created only by the authenticated "
                       "Coding Mode controller; generic runner/config/MCP starts are refused")


def fence_control(run_id: str, action: str) -> None:
    """Approve/reject/stop/fail of an owned workspace or governing plan needs the controller.

    Every such transition goes through the authenticated decision boundary,
    which holds the project request lock. A generic route (model approval tool,
    REST checkpoint, MCP, CLI, stop button) is refused for these runs; other
    runs are untouched.
    """
    if _TRUSTED.get() is not None or not isinstance(run_id, str) or not run_id:
        return
    try:
        role = _index_get(run_id)
    except Unavailable:
        role = {"role": "unknown"}
    if role is not None:
        raise _refused(
            f"{action} of run {run_id} is refused: it is a Coding Mode "
            f"{role.get('role', 'governed')} run. Use the Coding Mode plan decision "
            f"controls of its session.")


# ── durable records ──────────────────────────────────────────────────

def _root() -> Path:
    return datadir.native_context_dir() / "coding_workspace"


def _k(text: str) -> str:
    return hashlib.sha256(str(text).encode()).hexdigest()[:32]


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdirs(path: Path) -> None:
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        _fsync_dir(d.parent)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise Unavailable(f"record directory {path} is not a directory")


def _write_bytes(path: Path, data: bytes) -> None:
    _mkdirs(path.parent)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        closed_git._write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _write(path: Path, obj: dict) -> dict:
    obj = {**obj, "schema": SCHEMA, "written_at": time.time()}
    _write_bytes(path, json.dumps(obj, sort_keys=True, indent=1).encode())
    return obj


def _read_bytes(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Unavailable(f"record {path} is unreadable: {exc}") from exc
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise Unavailable(f"record {path} is not a regular file")
        return fh.read()


def _read(path: Path) -> dict | None:
    data = _read_bytes(path)
    if data is None:
        return None
    try:
        obj = json.loads(data)
    except ValueError as exc:
        raise Unavailable(f"record {path} is partial or corrupt; refusing to guess") from exc
    if not isinstance(obj, dict) or obj.get("schema") != SCHEMA:
        raise Unavailable(f"record {path} has an unknown shape; refusing to guess")
    return obj


def _project_dir(project_id: str) -> Path:
    return _root() / "projects" / _k(project_id)


def _session_dir(project_id: str, session_id: str) -> Path:
    return _project_dir(project_id) / "sessions" / _k(session_id)


def _run_dir(project_id: str, session_id: str, run_id: str) -> Path:
    if not _SAFE_ID.match(run_id or ""):
        raise Unavailable(f"{run_id!r} is not a usable run id")
    return _session_dir(project_id, session_id) / "runs" / run_id


def _index_path(run_id: str) -> Path:
    return _root() / "index" / f"{_k(run_id)}.json"


def _index_put(run_id: str, **kw) -> None:
    _write(_index_path(run_id), {"run_id": run_id, **kw})


def _index_get(run_id: str) -> dict | None:
    rec = _read(_index_path(run_id))
    if rec is not None and rec.get("run_id") != run_id:
        raise Unavailable("run index collision")
    return rec


def _identity() -> str:
    from core import run_isolation as ri
    return ri._worker_identity("coding_workspace")


def _owner_dead(owner: str):
    try:
        from skillflow.identity import owner_is_dead
        return owner_is_dead(owner)
    except Exception:  # noqa: BLE001 -- unknown is not dead
        return None


def _writer_quiet(owner: str) -> bool:
    """Positive quiet of a prior writer: its process is gone, or it is this one.

    "This one" is safe only because the caller holds the project request lock,
    which every writer of these records holds for its whole critical section;
    the earlier holder in this process has therefore returned.
    """
    return owner == _identity() or _owner_dead(owner) is True


# ── trusted entry scope ──────────────────────────────────────────────

_ISSUER = object()


@dataclass(frozen=True)
class EntryScope:
    """Session/project fixed by an authenticated writer before model sampling."""
    session_id: str
    project_id: str
    writer: str
    commit_message: str | None = None
    issuer: object = field(default=None, repr=False, compare=False)


def authorize_entry(db, *, session_id: str, project_id: str, writer: str,
                    commit_message: str | None = None) -> EntryScope:
    """Only the authenticated chat/controller entry calls this.

    The session must already exist server-side; the project must be a known
    project record. ``commit_message`` is the authenticated user's explicit
    commit request for this interaction (optional; never a model field).
    """
    if not isinstance(session_id, str) or not _SAFE_ID.match(session_id):
        raise Unavailable("coding session id is not usable")
    if not isinstance(project_id, str) or not _SAFE_ID.match(project_id):
        raise Unavailable("coding project id is not usable")
    with db.get_connection() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise Unavailable("coding session is unknown to the server")
    try:
        db.get_repo_info(project_id)
    except Exception as exc:
        raise Unavailable(f"project {project_id!r} is not a known project") from exc
    if commit_message is not None:
        if not isinstance(commit_message, str) or not commit_message.strip() or len(commit_message) > 4000:
            raise Unavailable("commit message must be bounded nonempty text")
    return EntryScope(session_id, project_id, str(writer or "")[:200],
                      commit_message, _ISSUER)


def _require_scope(scope) -> EntryScope:
    if type(scope) is not EntryScope or scope.issuer is not _ISSUER:
        raise Unavailable("Coding Mode requires the authenticated entry scope")
    return scope


# ── the project request lock ─────────────────────────────────────────

class ProjectLock:
    """Non-blocking kernel lock for one project's coding request. Not a token."""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.path = _root() / "locks" / f"{_k(project_id)}.lock"
        self.fd = None

    def acquire(self) -> "ProjectLock":
        if self.fd is not None:
            return self
        _mkdirs(self.path.parent)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise Busy(f"project {self.project_id!r} has another active coding request; "
                           f"try again when it finishes") from exc
            raise Unavailable(f"project request lock unavailable: {exc}") from exc
        self.fd = fd
        return self

    def release(self) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


# ── host-held claims of this process ─────────────────────────────────

_CLAIMS: dict[str, object] = {}
_CLAIMS_LOCK = threading.Lock()


def _claim_put(run_id: str, claimed) -> None:
    with _CLAIMS_LOCK:
        _CLAIMS[run_id] = claimed


def _claim_get(run_id: str):
    with _CLAIMS_LOCK:
        return _CLAIMS.get(run_id)


def _claim_drop(run_id: str) -> None:
    with _CLAIMS_LOCK:
        _CLAIMS.pop(run_id, None)


def _token_dict(claimed) -> dict:
    t = claimed.token
    return {"step_id": t.step_id, "step_instance_id": t.step_instance_id,
            "version": t.version, "claim_epoch": t.claim_epoch}


def static_graph_digest() -> str:
    """Digest of the trusted static registration, computed the way the engine pins it."""
    from skillflow import PipelineGraph
    from skillflow.core import graph_digest
    return graph_digest(PipelineGraph.from_yaml(str(STATIC_GRAPH_PATH)).to_dict())


def _sf():
    from api.dependencies import get_skillflow
    return get_skillflow()


def _runner(sf):
    from skillflow.plugins.skill_runner import RunnerService
    return RunnerService(sf)


@dataclass
class Workspace:
    run_id: str
    root: Path
    source: Path
    base_sha: str
    claimed: object
    origin: dict
    grant: object = None


# ── W file operations (confined, no-follow writes) ───────────────────

def _w_path(ws: Workspace, path: str, *, must_exist: bool = True) -> tuple[str, Path]:
    if not isinstance(path, str) or not path.strip() or "\0" in path or len(path) > 4096:
        raise Unavailable("path must be bounded repository-relative text")
    root = ws.root
    target = Path(os.path.realpath(root / path.lstrip("/") if not os.path.isabs(path) else path))
    # Component containment, never a string prefix: a sibling sharing the
    # prefix is outside.
    if not target.is_relative_to(root):
        raise Unavailable("Path traversal denied: the path leaves the session working tree")
    rel = target.relative_to(root)
    rel_s = rel.as_posix()
    if rel_s in ("", ".") or any(p.lower() == ".git" for p in rel.parts):
        raise Unavailable("Repository metadata is not a file-tool target")
    if must_exist and not target.exists():
        raise Unavailable(f"File not found: {path}")
    return rel_s, target


def read_text(ws: Workspace, path: str) -> tuple[str, str]:
    rel, target = _w_path(ws, path)
    if not target.is_file():
        raise Unavailable(f"File not found: {path}")
    with WorkTreeReader(ws.root) as wt:
        got = wt.read(rel, keep=True)
    if got.kind != "file":
        raise Unavailable(f"{path} is not a regular file")
    return rel, got.data.decode("utf-8", errors="replace")


class WorkTreeReader(closed_git.WorkTree):
    def __init__(self, root):
        super().__init__(root, follow_root=False)


def write_text(db, ws: Workspace, path: str, content: str, *, create: bool, detail: str) -> str:
    """Write W through a no-follow walk of the canonical in-tree target."""
    from core import run_isolation as ri
    rel, target = _w_path(ws, path, must_exist=not create)
    data = content.encode("utf-8")
    with ri.write_admission(db, str(ws.root), kind="coding_workspace", detail=detail[:500]):
        with closed_git.WorkTree(ws.root, follow_root=False) as wt:
            got = wt.read(rel)
            if create and got.kind != "absent":
                raise Unavailable(f"create_file: '{path}' already exists")
            if not create and got.kind != "file":
                raise Unavailable(f"edit_file: '{path}' is not a regular file")
            mode = got.mode if got.kind == "file" else 0o100644
            wt.write(rel, mode, data)
    return rel


# ── the per-request controller ───────────────────────────────────────

class CodingRequest:
    """One authenticated Coding Mode request: lock, binding, tools, plan and handoff."""

    def __init__(self, db, scope: EntryScope, *, sf=None):
        self.db = db
        self.scope = _require_scope(scope)
        self._sf = sf
        self._lock: ProjectLock | None = None
        self._ws: Workspace | None = None
        self.recovered: list[dict] = []

    # -- plumbing --
    @property
    def sf(self):
        return self._sf if self._sf is not None else _sf()

    @property
    def project_id(self) -> str:
        return self.scope.project_id

    @property
    def session_id(self) -> str:
        return self.scope.session_id

    def acquire(self) -> None:
        if self._lock is None:
            self._lock = ProjectLock(self.project_id).acquire()

    def release(self) -> None:
        self._ws = None
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def _held(self) -> None:
        if self._lock is None or self._lock.fd is None:
            raise Unavailable("internal: the project request lock is not held")

    # -- discovery --
    def _origins(self) -> list[tuple[Path, dict]]:
        out = []
        base = _project_dir(self.project_id) / "sessions"
        if not base.exists():
            return out
        for sess in sorted(base.iterdir()):
            runs = sess / "runs"
            if not runs.is_dir():
                continue
            for rd in sorted(runs.iterdir()):
                origin = _read(rd / "origin.json")
                if origin is None:
                    continue
                if origin.get("project_id") != self.project_id or origin.get("run_id") != rd.name:
                    raise Unavailable(f"origin record {rd} does not describe its own address")
                out.append((rd, origin))
        return out

    def _creations(self) -> list[tuple[Path, dict]]:
        out = []
        base = _project_dir(self.project_id) / "sessions"
        if not base.exists():
            return out
        for sess in sorted(base.iterdir()):
            cdir = sess / "creations"
            if not cdir.is_dir():
                continue
            for f in sorted(cdir.glob("*.json")):
                rec = _read(f)
                if rec is not None and rec.get("state") not in ("bound", "not_started", "orphan_settled"):
                    out.append((f, rec))
        return out

    def _live_owner(self) -> tuple[Path, dict] | None:
        """This session's unresolved owner, refusing other sessions' and conflicts."""
        mine, foreign = [], []
        for rd, origin in self._origins():
            if origin.get("state") in CLOSED:
                continue
            (mine if origin.get("session_id") == self.session_id else foreign).append((rd, origin))
        if foreign:
            raise Busy("another Coding Mode session owns this project's working tree "
                       "(an unresolved interaction, plan or handoff); this session cannot "
                       "adopt it")
        if len(mine) > 1:
            raise Unavailable("this session has several unresolved workspace owners; "
                              "refusing to choose one")
        return mine[0] if mine else None

    def _settle_creations(self) -> None:
        sf = self.sf
        for path, rec in self._creations():
            if rec.get("session_id") != self.session_id:
                raise Busy("another Coding Mode session has an unresolved workspace creation "
                           "for this project")
            if not _writer_quiet(rec.get("writer", "")):
                raise Unavailable("an earlier workspace creation's writer is still alive; "
                                  "its outcome is unknown")
            since = rec.get("created_at", 0)
            candidates = [r for r in sf.list_runs(project_id=self.project_id)
                          if r.get("graph_name") == GRAPH]
            rid = rec.get("run_id")
            if rid:
                orphans = [r for r in candidates if r["id"] == rid]
            else:
                orphans = [r for r in candidates if _ts(r.get("created_at")) >= since - 1
                           and not self._run_is_bound(r["id"])]
            for r in orphans:
                # Never exposed or seeded: an authentic but unbound orphan is
                # stopped through the ordinary engine path, never adopted.
                if r.get("status") in ("pending", "running", "paused"):
                    with _trusted("recover"):
                        sf.stop_run(r["id"], "unbound coding_workspace creation settled by "
                                              "its originating session")
                _index_put(r["id"], role="orphan", project_id=self.project_id,
                           session_id=self.session_id)
            _write(path, {**rec, "state": "orphan_settled" if orphans else "not_started",
                          "settled_runs": [r["id"] for r in orphans]})

    def _run_is_bound(self, run_id: str) -> bool:
        return any(o.get("run_id") == run_id for _, o in self._origins())

    def _unbound_conflicts(self) -> list[str]:
        out = []
        for r in self.sf.list_runs(project_id=self.project_id):
            if (r.get("graph_name") == GRAPH and r.get("status") in ("pending", "running", "paused")
                    and not self._run_is_bound(r["id"])):
                out.append(r["id"])
        return out

    # -- binding --
    def workspace(self, *, create: bool = True) -> Workspace | None:
        """Bind W for this request: reuse this session's owner or create one."""
        if self._ws is not None:
            self._validate(self._ws)
            return self._ws
        self.acquire()
        self._settle_creations()
        owner = self._live_owner()
        if owner is not None:
            ws = self._resume(*owner)
            if ws is not None:
                self._ws = ws
                return ws
        if not create:
            return None
        conflicts = self._unbound_conflicts()
        if conflicts:
            raise Unavailable(f"unbound coding_workspace run(s) {conflicts} exist for this "
                              f"project; they are never adopted")
        self._ws = self._create()
        return self._ws

    def _resume(self, rd: Path, origin: dict) -> Workspace | None:
        state = origin.get("state")
        if state == "bound":
            claimed = self._current_claim(rd, origin)
            ws = self._workspace_from(origin, claimed)
            self._seed(rd, origin, ws)
            return ws
        if state == "seeding":
            self._abandon_partial_seed(rd, origin)
            return None
        if state not in ("ready", "handoff_pending", "closing"):
            raise Unavailable(f"workspace owner is in unknown state {state!r}")
        claimed = self._current_claim(rd, origin)
        ws = self._workspace_from(origin, claimed)
        self._validate(ws)
        self._recover_handoffs(rd, ws)
        if _read(rd / "origin.json").get("state") == "closing":
            self._close(rd, ws, None)
            return None
        return ws

    def _workspace_from(self, origin: dict, claimed) -> Workspace:
        iso = origin["isolation"]
        return Workspace(origin["run_id"], Path(iso["worktree_path"]), Path(iso["source_repo"]),
                         iso["base_sha"], claimed, origin)

    def _current_claim(self, rd: Path, origin: dict):
        rid = origin["run_id"]
        claimed = _claim_get(rid)
        if claimed is not None and _token_dict(claimed) == origin.get("claim"):
            return claimed
        return self._reclaim(rd, origin)

    def _reclaim(self, rd: Path, origin: dict):
        """Ordinary engine recovery of the SAME run, only after positive quiet."""
        from core import run_isolation as ri
        sf, rid = self.sf, origin["run_id"]
        run = sf.get_run(rid)
        if not run or run.get("status") != "running" or run.get("cancel_requested_at"):
            raise Unavailable(f"workspace run {rid} is {run and run.get('status')!r}; retained")
        w = origin["isolation"]["worktree_path"]
        if ri.write_admissions(self.db, ri.canonical_checkout(w)):
            raise Unavailable("the working tree has an unsettled shell/edit admission; retained")
        audit = sf.audit_operation_owners(rid)
        if len(audit.get("lost", [])) + len(audit.get("unknown", [])) + int(audit.get("alive", 0)):
            raise Unavailable("the workspace run has unretired engine operations; retained")
        rows = [s for s in sf.get_steps(rid) if s.get("step_id") == WORK_STEP]
        latest = max(rows, key=lambda s: s["id"]) if rows else None
        if latest and latest.get("status") == "claimed":
            if not _owner_dead(latest.get("claimed_by") or "") is True:
                raise Unavailable("the old workspace claim's owner is alive or unknown; "
                                  "reclaim needs positive quiet")
            with _trusted("recover"):
                sf.recover_stale_claims(stale_threshold_seconds=10 ** 9)
        with _trusted("recover"):
            sf.advance_run(rid)
            claimed = sf.claim_next_step(rid)
        old = origin.get("claim") or {}
        if (claimed is None or claimed.step_id != WORK_STEP
                or claimed.token.claim_epoch <= int(old.get("claim_epoch") or 0)):
            raise Unavailable("the engine returned no fresh workspace claim; retained")
        origin = _write(rd / "origin.json", {
            **origin, "claim": _token_dict(claimed),
            "claim_history": (origin.get("claim_history") or []) + [old],
            "reclaimed_by": _identity()})
        _claim_put(rid, claimed)
        return claimed

    def _validate(self, ws: Workspace) -> None:
        """Exact run/claim/epoch/pin/isolation/origin; raises before any effect."""
        from core import coding_shell, run_isolation as ri
        from core.repository_executor import IsolationUnavailable
        origin = _read(_run_dir(self.project_id, self.session_id, ws.run_id) / "origin.json")
        if (origin is None or origin.get("session_id") != self.session_id
                or origin.get("project_id") != self.project_id
                or origin.get("claim") != _token_dict(ws.claimed)
                or origin.get("state") in CLOSED):
            raise Unavailable("the session's workspace binding changed; refusing")
        try:
            grant = coding_shell.bind_coding_shell(self.db, self.sf, ws.claimed)
        except IsolationUnavailable as exc:
            raise Unavailable(f"workspace claim is not current: {exc}") from exc
        b = grant.binding
        if (b["graph_name"] != GRAPH or b["graph_digest"] != static_graph_digest()
                or b["graph_digest"] != origin["graph"]["digest"]
                or b["repo"] != str(ws.root) or b["source_repo"] != str(ws.source)
                or b["base_sha"] != ws.base_sha or b["project_id"] != self.project_id
                or b["step_id"] != WORK_STEP):
            raise Unavailable("workspace run is not the trusted static coding_workspace "
                              "graph on its recorded worktree")
        rec = ri.record(self.db, ws.run_id)
        if not rec or rec["mode"] != ri.MODE_WORKTREE or rec.get("released_at"):
            raise Unavailable("workspace isolation record is not a live worktree")
        ws.grant = grant
        ws.origin = origin

    def _create(self) -> Workspace:
        from core import run_isolation as ri
        self._held()
        sf = self.sf
        live = sf.get_run_by_project(self.project_id, GRAPH)
        if live and live.get("status") in ("pending", "running", "paused"):
            raise Unavailable(f"a coding_workspace run {live['id']} is already live for this "
                              f"project without a binding; never adopted")
        nonce = uuid.uuid4().hex
        cpath = _session_dir(self.project_id, self.session_id) / "creations" / f"{nonce}.json"
        intent = _write(cpath, {"kind": "creation", "state": "starting", "nonce": nonce,
                                "project_id": self.project_id, "session_id": self.session_id,
                                "writer": _identity(), "created_at": time.time()})
        with _trusted("create"):
            rid = sf.create_run(GRAPH, {"project_id": self.project_id,
                                        "coding_session": self.session_id},
                                project_id=self.project_id)
        intent = _write(cpath, {**intent, "state": "created", "run_id": rid})
        _index_put(rid, role="owner", project_id=self.project_id, session_id=self.session_id)
        with _trusted("create"):
            sf.start_run(rid)
            sf.advance_run(rid)
            claimed = sf.claim_next_step(rid)
        if claimed is None or claimed.step_id != WORK_STEP or not claimed.token.claim_epoch:
            raise Unavailable(f"the engine gave run {rid} no work claim; retained unbound")
        rec = ri.record(self.db, rid)
        run = sf.get_run(rid)
        if not rec or rec["mode"] != ri.MODE_WORKTREE or not rec.get("worktree_path"):
            raise Unavailable(f"run {rid} was not provisioned an owned worktree")
        try:
            self.db.link_run_to_session(self.session_id, rid)
        except Exception:  # noqa: BLE001 -- discovery index only, never authority
            pass
        rd = _run_dir(self.project_id, self.session_id, rid)
        origin = _write(rd / "origin.json", {
            "kind": "origin", "state": "bound", "run_id": rid,
            "project_id": self.project_id, "session_id": self.session_id,
            "writer": self.scope.writer, "creation": nonce,
            "claim": _token_dict(claimed),
            "graph": {"name": GRAPH, "version": run["graph_version"],
                      "digest": run["graph_digest"]},
            "isolation": {k: rec[k] for k in ("worktree_path", "source_repo", "base_sha", "branch")}})
        _write(cpath, {**intent, "state": "bound"})
        _claim_put(rid, claimed)
        ws = self._workspace_from(origin, claimed)
        self._seed(rd, origin, ws)
        return ws

    def _seed(self, rd: Path, origin: dict, ws: Workspace) -> None:
        """Dirty-Source seed under the exclusive admission; W is ready only after it."""
        from core import run_isolation as ri
        from core.run_isolation import CheckoutLeased
        self._validate(ws)
        detail = json.dumps({"purpose": "seed", "run_id": ws.run_id,
                             "record": str(rd)}, sort_keys=True)
        try:
            adm = ri.admit_write(self.db, str(ws.source), kind="coding_workspace", detail=detail)
        except CheckoutLeased as exc:
            raise Busy(f"Source is being written ({exc}); the workspace will be seeded "
                       f"on the next request") from exc
        origin = _write(rd / "origin.json", {**ws.origin, "state": "seeding",
                                             "seed_admission": {"id": adm["id"], "owner": adm["owner"]}})
        try:
            manifest = ri.seed_worktree_from_source(self.db, ws.run_id, admission=adm)
        except BaseException as exc:
            # Seeding only READS Source: nothing there is unknown, so the row is
            # retired. The partially seeded W is preserved for diagnosis and
            # never returned as ready.
            ri.retire_write(self.db, adm["id"])
            self._abandon_partial_seed(rd, {**origin, "seed_error": str(exc)[:500]}, retire=False)
            if isinstance(exc, Exception):
                raise Unavailable(f"Source could not be seeded into the working tree: {exc}") from exc
            raise
        _write(rd / "seed.json", {"kind": "seed", "run_id": ws.run_id, **manifest})
        ri.retire_write(self.db, adm["id"])
        ws.origin = _write(rd / "origin.json", {**origin, "state": "ready", "seed_admission": None})

    def _abandon_partial_seed(self, rd: Path, origin: dict, *, retire: bool = True) -> None:
        from core import run_isolation as ri
        adm = origin.get("seed_admission") or {}
        if retire and adm.get("id"):
            rows = [r for r in ri.write_admissions(self.db) if r["id"] == adm["id"]]
            if rows:
                if rows[0]["owner"] != adm.get("owner") or not _writer_quiet(adm.get("owner", "")):
                    raise Unavailable("an interrupted seed's writer is not positively quiet; retained")
                ri.retire_write(self.db, adm["id"])
        with _trusted("recover"):
            self.sf.stop_run(origin["run_id"], "coding workspace seed did not complete; "
                                               "partial tree retained for diagnosis")
        _claim_drop(origin["run_id"])
        _write(rd / "origin.json", {**origin, "state": "abandoned",
                                    "abandoned_reason": "partial or failed seed"})

    # -- shell grant --
    def shell_grant(self, ws: Workspace):
        self._validate(ws)
        return ws.grant

    # -- plan association and gate --
    def _rd(self, ws: Workspace) -> Path:
        return _run_dir(self.project_id, self.session_id, ws.run_id)

    def plan_record(self, ws: Workspace) -> dict | None:
        return _read(self._rd(ws) / "plan.json")

    def plan_state(self, ws: Workspace) -> dict:
        return governing_plan_state(self.sf, self._rd(ws))

    def require_writable(self, ws: Workspace) -> None:
        st = self.plan_state(ws)
        if st["state"] not in ("none", "approved", "completed"):
            raise PlanGate(f"the governing plan of this workspace is {st['state']}: file "
                           f"mutations, writable shell and the Source handoff wait for an "
                           f"applied user approval of exactly that plan")

    def plan_start(self, task: str) -> dict:
        ws = self.workspace()
        rd = self._rd(ws)
        current = _read(rd / "plan.json")
        if current is not None and current.get("state") not in ("resolved", "not_started"):
            raise PlanGate(f"this workspace already has a governing plan "
                           f"({current.get('state')}); it cannot be replaced")
        if not isinstance(task, str) or not task.strip() or len(task) > 100_000:
            raise Unavailable("runner_start: 'task' is required bounded text")
        started_at = time.time()
        plan = _write(rd / "plan.json", {"kind": "plan", "state": "starting", "writer": _identity(),
                                         "started_at": started_at, "owner_run": ws.run_id,
                                         "task_sha256": hashlib.sha256(task.encode()).hexdigest()})
        try:
            with _trusted("plan"):
                result = _runner(self.sf).start(PLAN_GRAPH, project_id=self.project_id,
                                                seeds={"task.md": task})
        except BaseException:
            result = {}
            raise
        finally:
            rid = (result or {}).get("run_id") if isinstance(result, dict) else None
            if not rid:
                found = [r for r in self.sf.list_runs(project_id=self.project_id)
                         if r.get("graph_name") == PLAN_GRAPH
                         and _ts(r.get("created_at")) >= started_at - 1
                         and r.get("status") in ("pending", "running", "paused")]
                rid = found[0]["id"] if len(found) == 1 else None
                if len(found) > 1:
                    _write(rd / "plan.json", {**plan, "state": "unknown",
                                              "candidates": [r["id"] for r in found]})
            if rid:
                _index_put(rid, role="plan", project_id=self.project_id,
                           session_id=self.session_id, owner_run=ws.run_id)
                _write(rd / "plan.json", {**plan, "state": "associated", "run_id": rid})
                try:
                    self.db.link_run_to_session(self.session_id, rid)
                except Exception:  # noqa: BLE001 -- discovery only
                    pass
            elif _read(rd / "plan.json").get("state") == "starting":
                _write(rd / "plan.json", {**plan, "state": "not_started",
                                          "error": str((result or {}).get("error", ""))[:500]})
        return result

    def _plan_run(self, ws: Workspace) -> str:
        plan = self.plan_record(ws)
        if not plan or plan.get("state") != "associated" or not plan.get("run_id"):
            raise PlanGate("this workspace has no governing plan run")
        return plan["run_id"]

    def plan_submit(self, step_id: str, result: dict | None) -> dict:
        ws = self.workspace()
        rid = self._plan_run(ws)
        result = result if isinstance(result, dict) else {}
        if step_id == PLAN_STEP:
            # The checkpoint retains ONE canonical plan text in the completed
            # row's outputs; a proxy-written slot with an empty result would
            # leave the user approving bytes the engine did not retain.
            if not isinstance(result.get("plan"), str) or not result["plan"].strip():
                return {"error": "runner_submit(plan) must carry the plan text in "
                                 "result={'plan': ...}; it is the exact text the user approves"}
        elif step_id == "implement":
            self.require_writable(ws)
        with _trusted("plan"):
            return _runner(self.sf).submit(rid, step_id, result)

    def plan_tool(self, step_id: str, name: str, params: dict | None) -> dict:
        ws = self.workspace()
        rid = self._plan_run(ws)
        if name.startswith(("write_", "create_", "edit_")):
            return {"error": "plan outputs are submitted with runner_submit(result=...), "
                             "not written through skillflow_tool"}
        with _trusted("plan"):
            return _runner(self.sf).execute_step_tool(rid, step_id, name, params or {},
                                                      project_root="")

    # -- handoff and closure --
    def finish(self, note: str = "") -> dict | None:
        """At a final successful response: hand W to Source, then close the owner."""
        self.acquire()
        self._settle_creations()
        owner = self._live_owner()
        if owner is None:
            return None
        rd, origin = owner
        ws = self._ws if self._ws is not None and self._ws.run_id == origin["run_id"] else None
        if ws is None:
            ws = self._resume(rd, origin)
            if ws is None:
                return {"status": "closed", "run_id": origin["run_id"]}
        self._validate(ws)
        st = self.plan_state(ws)
        if st["state"] not in ("none", "completed"):
            return {"status": "retained", "run_id": ws.run_id, "plan": st,
                    "reason": ("the governing plan awaits an applied user decision"
                               if st["state"] != "approved" else
                               "the approved plan's implementation is not submitted yet")}
        receipt = self._handoff(rd, ws)
        if self.recovered:
            receipt["recovered"] = self.recovered
        if receipt["status"] in ("applied", "no_changes"):
            self._close(rd, ws, receipt, note=note)
        return receipt

    def _handoff(self, rd: Path, ws: Workspace) -> dict:
        return Handoff(self, rd, ws).run()

    def _recover_handoffs(self, rd: Path, ws: Workspace) -> None:
        for hd in sorted((rd / "handoffs").glob("*")) if (rd / "handoffs").is_dir() else []:
            intent = _read(hd / "intent.json")
            if intent and intent.get("state") not in Handoff.SETTLED:
                h = Handoff(self, rd, ws, hd=hd, intent=intent)
                h.recover()
                self.recovered.append({k: h.intent.get(k) for k in
                                       ("handoff_id", "state", "paths", "commit")})

    def _close(self, rd: Path, ws: Workspace, receipt: dict | None, *, note: str = "") -> None:
        from skillflow.core import StepResult
        sf = self.sf
        origin = _read(rd / "origin.json")
        if origin.get("state") != "closing":
            origin = _write(rd / "origin.json", {**origin, "state": "closing",
                                                 "closing_receipt": receipt})
        summary = json.dumps({"coding_workspace": ws.run_id, "handoff": origin.get("closing_receipt"),
                              "note": note[:2000]}, sort_keys=True, indent=1)
        run = sf.get_run(ws.run_id)
        if run and run.get("status") == "running":
            t = ws.claimed.token
            with _trusted("close"):
                reply = sf.execute_tool("write_" + SUMMARY_SLOT, {"content": summary},
                                        run_id=ws.run_id, step_id=WORK_STEP,
                                        step_instance_id=t.step_instance_id,
                                        claim_epoch=t.claim_epoch)
                if isinstance(reply, dict) and reply.get("error"):
                    raise Unavailable(f"workspace summary was refused: {reply['error']}")
                sf.confirm_step(t, StepResult(outputs={SUMMARY_SLOT: summary}, flags={}))
                sf.advance_run(ws.run_id)
        run = sf.get_run(ws.run_id)
        if not run or run.get("status") != "completed":
            raise Unavailable(f"workspace run {ws.run_id} did not complete "
                              f"({run and run.get('status')!r}); closure stays pending")
        plan = _read(rd / "plan.json")
        if plan and plan.get("state") == "associated":
            _write(rd / "plan.json", {**plan, "state": "resolved"})
        _write(rd / "closure.json", {"kind": "closure", "run_id": ws.run_id,
                                     "receipt": origin.get("closing_receipt"),
                                     "source_confirmed": bool((origin.get("closing_receipt") or {})
                                                              .get("source_confirmed"))})
        _claim_drop(ws.run_id)
        _write(rd / "origin.json", {**_read(rd / "origin.json"), "state": "closed"})
        self._ws = None


def _ts(value) -> float:
    from datetime import UTC, datetime
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return dt.replace(tzinfo=UTC).timestamp() if dt.tzinfo is None else dt.timestamp()


# ── governing plan state ─────────────────────────────────────────────

def _decisions(rd: Path) -> list[dict]:
    out = []
    ddir = rd / "decisions"
    if ddir.is_dir():
        for f in sorted(ddir.glob("*.json")):
            rec = _read(f)
            if rec is not None:
                out.append(rec)
    return out


def governing_plan_state(sf, rd: Path) -> dict:
    """The exact state of the plan associated with this workspace (or none).

    ``approved`` requires an APPLIED host decision record for the latest
    completed plan instance — not a current node, a stop or a later run.
    """
    plan = _read(rd / "plan.json")
    if plan is None or plan.get("state") in ("not_started",):
        return {"state": "none"}
    if plan.get("state") == "resolved":
        return {"state": "completed", "run_id": plan.get("run_id")}
    if plan.get("state") == "dismissed":
        return {"state": "dismissed", "run_id": plan.get("run_id")}
    if plan.get("state") != "associated" or not plan.get("run_id"):
        return {"state": "unknown", "detail": f"plan association is {plan.get('state')}"}
    rid = plan["run_id"]
    run = sf.get_run(rid)
    if run is None:
        return {"state": "unknown", "run_id": rid, "detail": "plan run is unknown"}
    decisions = _decisions(rd)
    if any(d.get("state") == "pending" for d in decisions):
        return {"state": "unknown", "run_id": rid, "detail": "a decision outcome is pending"}
    rows = [s for s in sf.get_steps(rid) if s.get("step_id") == PLAN_STEP]
    latest = max(rows, key=lambda s: s["id"]) if rows else None
    status = run.get("status")
    if status == "paused":
        try:
            cp = sf.get_checkpoint(rid)
        except Exception as exc:  # noqa: BLE001 -- not a valid checkpoint is not approval
            return {"state": "unknown", "run_id": rid, "detail": str(exc)[:300]}
        rejected = any(d.get("decision") == "reject" and d.get("state") == "applied"
                       and (d.get("expected") or {}).get("step_instance_id") ==
                       cp["expected_checkpoint"]["step_instance_id"] for d in decisions)
        return {"state": "pending", "run_id": rid, "rejected_before": rejected,
                "checkpoint": {"expected_checkpoint": cp["expected_checkpoint"],
                               "plan": (cp.get("outputs") or {}).get("plan", ""),
                               "label": cp.get("label") or ""}}
    if status in ("running", "completed"):
        if latest is None or latest.get("status") != "completed":
            return {"state": "pending", "run_id": rid,
                    "detail": "the plan is being (re)written after a rejection"}
        applied = [d for d in decisions if d.get("decision") == "approve"
                   and d.get("state") == "applied"
                   and (d.get("expected") or {}).get("step_instance_id") == latest["id"]]
        if len(applied) != 1:
            return {"state": "unknown", "run_id": rid,
                    "detail": "the plan advanced without an applied host decision"}
        return {"state": "completed" if status == "completed" else "approved",
                "run_id": rid, "decision_id": applied[0]["decision_id"]}
    return {"state": "stopped", "run_id": rid, "detail": f"plan run is {status}"}


# ── authenticated decision boundary ──────────────────────────────────

def _session_owner(project_id: str | None, session_id: str) -> tuple[Path, dict] | None:
    base = _root() / "projects"
    if not base.is_dir():
        return None
    found = []
    for pdir in base.iterdir():
        sdir = pdir / "sessions" / _k(session_id) / "runs"
        if not sdir.is_dir():
            continue
        for rd in sdir.iterdir():
            origin = _read(rd / "origin.json")
            if origin and origin.get("session_id") == session_id and (
                    project_id is None or origin.get("project_id") == project_id):
                found.append((rd, origin))
    live = [f for f in found if f[1].get("state") not in CLOSED]
    if len(live) > 1:
        raise Unavailable("this session has several unresolved workspace owners")
    if live:
        return live[0]
    return max(found, key=lambda f: f[1].get("written_at", 0)) if found else None


def decide_plan(db, *, session_id: str, writer: str, decision: str,
                expected_checkpoint: dict | None = None, decision_id: str | None = None,
                feedback: str = "", sf=None) -> dict:
    """The ONE route that approves/rejects/dismisses a governing plan.

    Called only by the authenticated writer/controller endpoint, never by
    backend model tool dispatch. Takes the project request lock; persists the
    pending decision BEFORE the engine call; the approval goes through the
    SDK's exact retained-instance CAS with this decision id.
    """
    sf = sf if sf is not None else _sf()
    if decision not in ("approve", "reject", "dismiss"):
        raise Unavailable("decision must be approve, reject or dismiss")
    owner = _session_owner(None, session_id)
    if owner is None or owner[1].get("state") in CLOSED:
        raise Unavailable("this session has no unresolved Coding Mode workspace")
    rd, origin = owner
    lock = ProjectLock(origin["project_id"]).acquire()
    try:
        plan = _read(rd / "plan.json")
        if not plan or plan.get("state") != "associated" or not plan.get("run_id"):
            raise Unavailable("this session's workspace has no governing plan")
        rid = plan["run_id"]
        did = decision_id or uuid.uuid4().hex
        if not isinstance(did, str) or not _SAFE_ID.match(did):
            raise Unavailable("decision_id is not usable")
        dpath = rd / "decisions" / f"{did}.json"
        prior = _read(dpath)
        if prior is not None:
            if prior.get("decision") != decision or (
                    decision == "approve" and prior.get("expected") != expected_checkpoint):
                raise Unavailable("decision_id was already used for another decision")
            if prior.get("state") != "pending":
                return {"status": prior["state"], "decision_id": did, "run_id": rid,
                        "replayed": True, "result": prior.get("result")}
        if decision == "approve":
            if not isinstance(expected_checkpoint, dict):
                raise Unavailable("approval requires the exact rendered plan token")
            rec = prior or _write(dpath, {"kind": "decision", "decision": "approve",
                                          "decision_id": did, "state": "pending",
                                          "expected": expected_checkpoint, "writer": writer,
                                          "run_id": rid})
            with _trusted("decide"):
                res = _runner(sf).approve(rid, expected_checkpoint=expected_checkpoint,
                                          decision_id=did)
            ok = res.get("status") == "approved" and res.get("decision_id") == did
            _write(dpath, {**rec, "state": "applied" if ok else "refused", "result": res})
            return {"status": "applied" if ok else "refused", "decision_id": did,
                    "run_id": rid, "result": res}
        if decision == "reject":
            if not isinstance(feedback, str) or not feedback.strip():
                raise Unavailable("rejection requires the user's feedback")
            cp = sf.get_checkpoint(rid)
            rec = prior or _write(dpath, {"kind": "decision", "decision": "reject",
                                          "decision_id": did, "state": "pending",
                                          "expected": cp["expected_checkpoint"],
                                          "writer": writer, "run_id": rid})
            with _trusted("decide"):
                res = _runner(sf).reject(rid, feedback)
            ok = not res.get("error")
            _write(dpath, {**rec, "state": "applied" if ok else "refused", "result": res})
            return {"status": "applied" if ok else "refused", "decision_id": did,
                    "run_id": rid, "result": res}
        rec = prior or _write(dpath, {"kind": "decision", "decision": "dismiss",
                                      "decision_id": did, "state": "pending",
                                      "writer": writer, "run_id": rid})
        with _trusted("decide"):
            stopped = sf.stop_run(rid, "governing plan dismissed by the user")
            owner_run = sf.get_run(origin["run_id"])
            if owner_run and owner_run.get("status") in ("pending", "running", "paused"):
                sf.stop_run(origin["run_id"], "coding workspace closed by plan dismissal; "
                                              "working tree retained as discard-pending evidence")
        _write(rd / "plan.json", {**plan, "state": "dismissed"})
        _write(dpath, {**rec, "state": "applied", "result": stopped})
        _claim_drop(origin["run_id"])
        _write(rd / "origin.json", {**_read(rd / "origin.json"), "state": "dismissed",
                                    "retained": "discard-pending evidence; not delivered"})
        return {"status": "applied", "decision_id": did, "run_id": rid, "dismissed": True}
    finally:
        lock.release()


# ── session view (UI) ────────────────────────────────────────────────

def session_view(db, session_id: str, sf=None) -> dict:
    """What the coding session's file view shows: W while owned, C once confirmed."""
    sf = sf if sf is not None else _sf()
    owner = _session_owner(None, session_id)
    if owner is None:
        return {"view": "none", "session_id": session_id}
    rd, origin = owner
    out = {"session_id": session_id, "project_id": origin["project_id"],
           "run_id": origin["run_id"], "state": origin.get("state")}
    handoffs = []
    if (rd / "handoffs").is_dir():
        for hd in sorted((rd / "handoffs").iterdir()):
            intent = _read(hd / "intent.json")
            if intent:
                handoffs.append({k: intent.get(k) for k in
                                 ("handoff_id", "state", "paths", "conflicts", "commit",
                                  "source_confirmed", "error")})
    out["handoffs"] = handoffs
    if origin.get("state") == "closed":
        closure = _read(rd / "closure.json") or {}
        out["view"] = "source" if closure.get("source_confirmed") or not handoffs else "working_tree"
        out["label"] = ("Source (delivered bytes confirmed equal)" if out["view"] == "source"
                        else "Working tree (retained)")
    else:
        out["view"] = "working_tree"
        out["label"] = f"Working tree of session {session_id} (not Source)"
    out["plan"] = governing_plan_state(sf, rd)
    return out


def session_root(db, session_id: str) -> tuple[str, Path]:
    """The W a session's file pane may read: its own unreleased owned worktree only."""
    from core import run_isolation as ri
    owner = _session_owner(None, session_id)
    if owner is None or owner[1].get("state") in ("abandoned",):
        raise Unavailable("this session has no working tree")
    origin = owner[1]
    rec = ri.record(db, origin["run_id"])
    w = origin["isolation"]["worktree_path"]
    if (not rec or rec["mode"] != ri.MODE_WORKTREE or rec["worktree_path"] != w
            or not ri._is_worktree_of(Path(w), origin["isolation"]["source_repo"])):
        raise Unavailable("the session's working tree is no longer a live owned worktree")
    return origin["run_id"], Path(w)


def _fence(run_id: str, run_fence: str | None) -> None:
    if run_fence and run_fence != run_id:
        raise Unavailable("the session's working-tree view moved to another run; reload it")


def session_tree(db, session_id: str, *, subdir: str = "", run_fence: str | None = None) -> dict:
    from skillflow.source_visibility import iter_visible_source_files
    run_id, root = session_root(db, session_id)
    _fence(run_id, run_fence)
    base = Path(os.path.realpath(root / (subdir or "")))
    if not base.is_relative_to(root) or not base.is_dir():
        raise Unavailable("not a directory of the session's working tree")
    files = []
    for item in iter_visible_source_files(base):
        rel = item.relative_to(root)
        if ".git" in rel.parts or item.is_symlink() or not item.is_file():
            continue
        files.append(rel.as_posix())
        if len(files) >= 2000:
            break
    return {"view": "working_tree", "session_id": session_id, "run_id": run_id,
            "files": files, "truncated": len(files) >= 2000}


def session_file(db, session_id: str, path: str, *, run_fence: str | None = None) -> dict:
    run_id, root = session_root(db, session_id)
    _fence(run_id, run_fence)
    ws = Workspace(run_id, root, Path("/"), "", None, {})
    rel, text = read_text(ws, path)
    if len(text) > 1 << 20:
        text, truncated = text[:1 << 20], True
    else:
        truncated = False
    return {"view": "working_tree", "session_id": session_id, "run_id": run_id,
            "path": rel, "content": text, "truncated": truncated}


# ── the Source handoff ───────────────────────────────────────────────

def _state_of(got) -> dict:
    if got.kind in ("file", "link"):
        return {"kind": got.kind, "mode": got.mode, "sha": got.sha}
    return {"kind": "absent"}


def _same(a: dict, b: dict) -> bool:
    if a["kind"] == "absent" or b["kind"] == "absent":
        return a["kind"] == b["kind"]
    return (a["kind"], a["mode"], a["sha"]) == (b["kind"], b["mode"], b["sha"])


class Handoff:
    """Freeze → admit → compare → applying → apply → applied → [commit] → settled."""

    SETTLED = ("settled", "conflict", "abandoned", "no_changes")

    def __init__(self, req: CodingRequest, rd: Path, ws: Workspace, *, hd: Path | None = None,
                 intent: dict | None = None):
        self.req, self.rd, self.ws = req, rd, ws
        self.db = req.db
        self.hd = hd
        self.intent = intent

    def _save(self, **kw) -> dict:
        self.intent = _write(self.hd / "intent.json", {**self.intent, **kw})
        return self.intent

    def _blob_path(self, sha: str) -> Path:
        return self.hd / "blobs" / sha

    def _blob(self, sha: str) -> bytes:
        data = _read_bytes(self._blob_path(sha))
        if data is None or closed_git.blob_sha1(data) != sha:
            raise Unavailable(f"frozen blob {sha} is missing or altered; refusing")
        return data

    # -- B, the seed baseline --
    def _baseline(self) -> dict:
        seed = _read(self.rd / "seed.json")
        if seed is None:
            raise Unavailable("the workspace has no seed record; refusing to compute a delta")
        base = {}
        for rel, t in closed_git.ls_tree(self.ws.root, self.ws.base_sha).items():
            if t.type == "commit":
                raise Unavailable(f"{rel}: submodules are not delivered by this handoff")
            base[rel] = {"kind": "link" if t.mode == 0o120000 else "file",
                         "mode": t.mode, "sha": t.sha}
        for rel, m in (seed.get("paths") or {}).items():
            base[rel] = ({"kind": "absent"} if m.get("kind") == "absent" else
                         {"kind": m["kind"], "mode": int(m["mode"], 8), "sha": m["sha1"]})
        # What an earlier handoff of this same owner already delivered and
        # verified in Source is part of B for the next one.
        hdir = self.rd / "handoffs"
        for hd in sorted(hdir.iterdir()) if hdir.is_dir() else []:
            prior = _read(hd / "intent.json")
            if prior and prior.get("state") == "settled":
                for d in prior["deltas"]:
                    base[d["path"]] = d["after"]
        return base

    def _freeze(self) -> dict | None:
        from core import run_isolation as ri
        if ri.write_admissions(self.db, ri.canonical_checkout(str(self.ws.root))):
            raise Unavailable("a shell or edit in the working tree has not positively "
                              "settled; the handoff waits")
        baseline = self._baseline()
        extra = closed_git.untracked(self.ws.root)
        nested = [p for p in extra if p.endswith("/")]
        if nested:
            raise Unavailable(f"{nested[0]!r} is an embedded repository in the working tree; "
                              f"not deliverable")
        ignored = closed_git.untracked(self.ws.root, ignored=True)
        paths = sorted(set(baseline) | set(extra), key=os.fsencode)
        deltas = []
        hid = uuid.uuid4().hex
        self.hd = self.rd / "handoffs" / f"{int(time.time() * 1000):015d}-{hid[:8]}"
        with closed_git.WorkTree(self.ws.root, follow_root=False) as wt:
            for rel in paths:
                got = wt.read(rel, keep=True)
                if got.kind == "dir":
                    got = closed_git.WorkFile("absent")
                if got.kind == "other":
                    raise Unavailable(f"{rel}: unsupported file type in the working tree")
                after = _state_of(got)
                before = baseline.get(rel, {"kind": "absent"})
                if _same(before, after):
                    continue
                if got.kind == "link" and not closed_git.link_is_confined(rel, got.data):
                    raise Unavailable(f"{rel}: a symlink leaving the repository is not deliverable")
                if got.kind in ("file", "link"):
                    if len(got.data) > _MAX_BLOB:
                        raise Unavailable(f"{rel}: larger than the handoff blob bound")
                    _write_bytes(self._blob_path(got.sha), got.data)
                deltas.append({"path": rel, "before": before, "after": after})
                if len(deltas) > _MAX_DELTA:
                    raise Unavailable("the working-tree change set exceeds the handoff bound")
        self.intent = {"kind": "handoff", "handoff_id": hid, "run_id": self.ws.run_id,
                       "source": str(self.ws.source), "base_sha": self.ws.base_sha,
                       "deltas": deltas, "paths": [d["path"] for d in deltas],
                       "excluded_ignored": sorted(ignored)[:200],
                       "excluded_ignored_count": len(ignored),
                       "commit_requested": self.req.scope.commit_message is not None,
                       "commit_message": self.req.scope.commit_message,
                       "writer": _identity(), "state": "frozen"}
        if not deltas:
            return None
        return self._save()

    def _plan_token(self) -> dict:
        st = governing_plan_state(self.req.sf, self.rd)
        if st["state"] not in ("none", "completed"):
            raise PlanGate(f"the governing plan is {st['state']}; no Source effect")
        return {"state": st["state"], "decision_id": st.get("decision_id"), "run_id": st.get("run_id")}

    def run(self) -> dict:
        from core import run_isolation as ri
        from core.run_isolation import CheckoutLeased
        token = self._plan_token()
        if self._freeze() is None:
            return {"status": "no_changes", "run_id": self.ws.run_id,
                    "source_confirmed": True, "paths": []}
        self._save(plan_token=token)
        detail = json.dumps({"purpose": "handoff", "run_id": self.ws.run_id,
                             "record": str(self.hd)}, sort_keys=True)
        try:
            adm = ri.admit_write(self.db, str(self.ws.source), kind="coding_workspace", detail=detail)
        except CheckoutLeased as exc:
            self._save(state="abandoned", error=f"Source busy: {exc}"[:500])
            raise Busy(f"Source is being written ({exc}); the working tree is retained and "
                       f"the handoff will be retried") from exc
        self._save(state="admitted", admission={"id": adm["id"], "owner": adm["owner"],
                                                "canonical": adm["canonical_checkout"]})
        try:
            return self._effects()
        except BaseException as exc:
            self._interrupted(adm["id"], exc)
            raise

    def _interrupted(self, admission_id: int, exc: BaseException) -> None:
        """No finally retirement: once an effect may have started, the row stays pending."""
        from core import run_isolation as ri
        state = self.intent.get("state")
        if state == "admitted":
            # Persisted `applying` precedes the first Source effect, so none started.
            ri.retire_write(self.db, admission_id)
            self._save(state="abandoned", error=str(exc)[:500])
            return
        if state in self.SETTLED:
            return
        ri.mark_write_admission_pending(self.db, admission_id,
                                        f"coding handoff {self.intent['handoff_id']} effects "
                                        f"unknown: {type(exc).__name__}: {exc}")
        self._save(interrupted=True, error=f"{type(exc).__name__}: {exc}"[:500])
        _write(self.rd / "origin.json", {**_read(self.rd / "origin.json"),
                                         "state": "handoff_pending"})

    def _admission_row(self) -> dict:
        from core import run_isolation as ri
        adm = self.intent.get("admission") or {}
        rows = [r for r in ri.write_admissions(self.db, adm.get("canonical"))
                if r["id"] == adm.get("id")]
        if (len(rows) != 1 or rows[0]["owner"] != adm.get("owner")
                or rows[0]["kind"] != "coding_workspace"
                or f'"record": "{self.hd}"' not in rows[0]["detail"]):
            raise Unavailable("the handoff's own exclusive Source admission is not held")
        return rows[0]

    def _effects(self) -> dict:
        from core import run_isolation as ri
        self._admission_row()
        # Recheck the governing decision INSIDE the exclusion, before effects.
        if self._plan_token() != self.intent.get("plan_token"):
            raise PlanGate("the governing plan decision changed during the handoff")
        source = self.ws.source
        deltas = self.intent["deltas"]
        commit_eval = self._commit_eligibility() if self.intent.get("commit_requested") else None
        with closed_git.WorkTree(source) as wt:
            current = {d["path"]: _state_of(wt.read(d["path"])) for d in deltas}
            blocked = [d["path"] for d in deltas if self._parent_blocked(wt, d["path"])]
        third = [d["path"] for d in deltas
                 if not _same(current[d["path"]], d["before"])
                 and not _same(current[d["path"]], d["after"])]
        if (third or blocked) and self.intent["state"] == "admitted":
            # Nothing applied yet: a conflict changes nothing in Source.
            ri.retire_write(self.db, self.intent["admission"]["id"])
            self._save(state="conflict", conflicts=sorted(set(third) | set(blocked)))
            _write(self.rd / "origin.json", {**_read(self.rd / "origin.json"), "state": "ready"})
            return {"status": "conflict", "run_id": self.ws.run_id,
                    "conflicts": sorted(set(third) | set(blocked)),
                    "detail": "Source changed these paths since the workspace was seeded; "
                              "nothing was applied and the working tree is retained"}
        if third:
            self._save(state="ambiguous", conflicts=third)
            raise Unavailable(f"Source paths {third[:5]} are in neither the frozen before nor "
                              f"after state; the handoff is retained ambiguous for a user "
                              f"decision")
        if self.intent["state"] == "admitted":
            self._save(state="applying", commit_eval=commit_eval)
        with closed_git.repo_lock(source):
            with closed_git.WorkTree(source) as wt:
                for d in deltas:
                    rel, before, after = d["path"], d["before"], d["after"]
                    now = _state_of(wt.read(rel))
                    if _same(now, after):
                        continue
                    if not _same(now, before):
                        raise Unavailable(f"{rel} changed under the handoff; retained pending")
                    if after["kind"] == "absent":
                        wt.remove(rel)
                    else:
                        wt.write(rel, after["mode"], self._blob(after["sha"]))
                after_states = {d["path"]: _state_of(wt.read(d["path"])) for d in deltas}
        wrong = [p for p, s in after_states.items()
                 if not _same(s, next(d["after"] for d in deltas if d["path"] == p))]
        if wrong:
            raise Unavailable(f"Source after-state did not verify for {wrong[:5]}")
        self._save(state="applied", source_confirmed=True)
        return self._settle()

    def _settle(self) -> dict:
        """After verified working bytes: optional commit under the same admission, then retire."""
        from core import run_isolation as ri
        commit = self.intent.get("commit")
        if self.intent.get("commit_requested") and commit is None:
            commit = self._commit()
            self._save(commit=commit)
        ri.retire_write(self.db, self.intent["admission"]["id"])
        self._save(state="settled", interrupted=False)
        _write(self.rd / "origin.json", {**_read(self.rd / "origin.json"), "state": "ready"})
        return {"status": "applied", "run_id": self.ws.run_id, "paths": self.intent["paths"],
                "source_confirmed": True, "commit": commit,
                "excluded_ignored_count": self.intent.get("excluded_ignored_count", 0),
                "handoff_id": self.intent["handoff_id"]}

    @staticmethod
    def _parent_blocked(wt, rel: str) -> bool:
        parts = rel.split("/")
        for depth in range(1, len(parts)):
            st = wt.lstat("/".join(parts[:depth]))
            if st is None:
                return False
            if not stat.S_ISDIR(st.st_mode):
                return True
        return False

    # -- optional commit under the SAME admission --
    def _commit_eligibility(self) -> dict:
        source = self.ws.source
        repo = closed_git.discover(source)
        head = closed_git.resolve_commit(repo.top, "HEAD")
        if head != self.ws.base_sha:
            return {"eligible": False, "reason": f"Source HEAD moved to {head} since the seed"}
        tree = closed_git.ls_tree(repo.top, head)
        idx = closed_git.read_index(repo.git_dir)
        if idx.unmerged_paths():
            return {"eligible": False, "reason": "Source has unmerged index entries"}
        stage0 = idx.stage0()
        for d in self.intent["deltas"]:
            rel = d["path"]
            t = tree.get(rel)
            head_state = ({"kind": "absent"} if t is None else
                          {"kind": "link" if t.mode == 0o120000 else "file", "mode": t.mode, "sha": t.sha})
            if not _same(head_state, d["before"]):
                return {"eligible": False,
                        "reason": f"{rel} already differed from HEAD when seeded; the commit "
                                  f"would fold in preexisting edits"}
            e = stage0.get(rel)
            if (e is None) != (t is None) or (e and (e.mode, e.sha) != (t.mode, t.sha)) or (
                    e and (e.intent_to_add or e.skip_worktree or e.assume_valid)):
                return {"eligible": False,
                        "reason": f"{rel} has staged or flagged index state; not overwritten"}
        cfg = closed_git.config_list(repo.top)
        attrs = []
        with closed_git.CatFile(repo.top) as cat:
            for rel, t in tree.items():
                if rel.rsplit("/", 1)[-1] == ".gitattributes" and t.type == "blob":
                    attrs.append(cat.read(t.sha)[1])
        for d in self.intent["deltas"]:
            if d["path"].rsplit("/", 1)[-1] == ".gitattributes" and d["after"]["kind"] == "file":
                attrs.append(self._blob(d["after"]["sha"]))
        profile = closed_git.transform_profile(repo, cfg, attrs)
        if profile.active_filters:
            return {"eligible": False, "reason": "configured filter drivers would convert "
                                                 "these files; never run by the backend"}
        for d in self.intent["deltas"]:
            if d["after"]["kind"] == "file":
                why = closed_git.commit_content_refusal(profile, d["path"], self._blob(d["after"]["sha"]))
                if why:
                    return {"eligible": False, "reason": why}
        return {"eligible": True, "head": head, "pre_index_sha256": idx.raw_sha256}

    def _commit(self) -> dict:
        ev = self.intent.get("commit_eval") or {}
        if not ev.get("eligible"):
            return {"status": "unavailable", "reason": ev.get("reason", "not evaluated")}
        try:
            return closed_git.commit_delivered(
                self.ws.source, expected_head=ev["head"],
                expected_index_sha256=ev["pre_index_sha256"],
                deltas={d["path"]: d["after"] for d in self.intent["deltas"]},
                blob=self._blob, message=self.intent["commit_message"],
                journal=self._journal)
        except closed_git.GitRefused as exc:
            return {"status": "unavailable", "reason": str(exc)[:500]}

    def _journal(self, **kw) -> None:
        self._save(commit_journal={**(self.intent.get("commit_journal") or {}), **kw})

    # -- recovery --
    def recover(self) -> None:
        """Reconcile an unsettled handoff BEFORE any further W tool runs.

        Requires positive quiet of the interrupted writer and its own retained
        admission row. Each Source path is compared to the frozen before/after
        identities: after is applied, before rolls forward from the frozen
        blob, a third state stays ambiguous with all evidence retained.
        """
        from core import run_isolation as ri
        state = self.intent.get("state")
        if state == "frozen":
            self._save(state="abandoned", error="no admission was taken; no Source effect")
            return
        if not _writer_quiet(self.intent.get("writer", "")):
            raise Unavailable("the interrupted handoff's writer is not positively quiet; retained")
        adm = self.intent.get("admission") or {}
        rows = [r for r in ri.write_admissions(self.db) if r["id"] == adm.get("id")]
        if state == "admitted":
            if rows:
                ri.retire_write(self.db, adm["id"])
            self._save(state="abandoned", error="interrupted before the first Source effect")
            return
        if state == "ambiguous":
            raise Unavailable("a previous handoff is ambiguous (Source paths in a third "
                              "state); it needs a user resolution and stays retained")
        if not rows:
            raise Unavailable("an unsettled handoff lost its exclusive admission row; retained")
        self._save(writer=_identity(), recovered_from=self.intent.get("writer"))
        try:
            if state == "applying":
                self._effects()
                return
            if state == "applied":
                journal = self.intent.get("commit_journal") or {}
                if journal and self.intent.get("commit") is None:
                    result = closed_git.recover_commit_delivered(self.ws.source, journal)
                    if result.get("status") not in ("committed", "not_committed"):
                        self._save(state="ambiguous", commit=result)
                        raise Unavailable(f"the interrupted commit is not reconcilable: {result}")
                    self._save(commit=result)
                self._settle()
                return
        except BaseException as exc:
            self._interrupted(adm["id"], exc)
            raise
        raise Unavailable(f"unknown handoff state {state!r}; retained")
