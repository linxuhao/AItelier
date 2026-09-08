"""Migration preparation: validate a manifest and stage ONLY a held shadow graph.

The CLI deliberately has no --apply/production database option. Historical
references do not create attempts or acceptance receipts. The caller of the
library must supply an explicit isolated StateService; never resolve a live DB.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from core.state_graph import StateConflict, StateGraphError, digest, key


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Project(Strict):
    project_id: str
    title: str


class Source(Strict):
    repo_path: str
    head_sha: str


class Reference(Strict):
    reference_id: str
    node_key: str
    kind: str
    ref: str
    label: str
    provenance_actor: str
    observed_status: str = "historical"
    artifact_ref: str | None = None
    report_sha256: str | None = None
    protect: bool = False


class Hold(Strict):
    node_key: str
    reason: str


class ExcludedRun(Strict):
    run_id: str
    reason: str


class FilePin(Strict):
    path: str
    sha256: str


class Manifest(Strict):
    format_version: int
    project: Project
    source: Source
    dispatch: str
    dispatch_reason: str
    nodes: list[dict] = Field(min_length=1, max_length=200)
    references: list[Reference] = Field(default_factory=list, max_length=200)
    node_holds: list[Hold] = Field(default_factory=list, max_length=200)
    excluded_runs: list[ExcludedRun] = Field(default_factory=list, max_length=1000)
    file_pins: list[FilePin] = Field(default_factory=list, max_length=1000)
    assumptions: list[str] = Field(default_factory=list, max_length=100)


def read_manifest(path: Path) -> dict:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise StateGraphError("migration manifest exceeds 2 MiB")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return validate_manifest(value)
    except (ValueError, TypeError) as exc:
        raise StateGraphError(f"invalid migration manifest: {type(exc).__name__}") from exc


def validate_manifest(value: dict) -> dict:
    try:
        model = Manifest.model_validate(value)
    except ValidationError as exc:
        raise StateGraphError(str(exc.errors(include_input=False)[:8])) from exc
    result = model.model_dump()
    if model.format_version != 1 or model.dispatch not in {"hold", "archive"}:
        raise StateGraphError("preparation requires format_version=1 and project hold/archive; active import is forbidden")
    key(model.project.project_id, "state project")
    if not model.dispatch_reason.strip():
        raise StateGraphError("migration hold needs a reason")
    if len(model.source.head_sha) != 40 or any(ch not in "0123456789abcdef" for ch in model.source.head_sha):
        raise StateGraphError("pin the exact 40-hex source commit")
    keys = {n.get("key") for n in model.nodes if isinstance(n.get("key"), str)}
    seen = set()
    excluded = {r.run_id for r in model.excluded_runs}
    for r in model.excluded_runs:
        key(r.run_id, "excluded run")
        if not r.reason.strip():
            raise StateGraphError("each excluded run needs its exclusion reason")
    for r in model.references:
        if r.node_key not in keys or r.reference_id in seen:
            raise StateGraphError("reference points to an unknown goal or repeats an ID")
        seen.add(r.reference_id)
        if r.kind == "run" and r.ref in excluded:
            raise StateGraphError("an excluded run cannot simultaneously be a current-goal reference")
        if r.kind == "run" and r.observed_status in {"running", "paused", "pending", "unknown"} and not r.protect:
            raise StateGraphError("active/unknown external runs require explicit protection")
    for h in model.node_holds:
        if h.node_key not in keys or not h.reason.strip():
            raise StateGraphError("invalid node hold")
    for pin in model.file_pins:
        if len(pin.sha256) != 64 or any(ch not in "0123456789abcdef" for ch in pin.sha256):
            raise StateGraphError("each input file must have a SHA-256 pin")
    return result


def verify_inputs(bundle: dict) -> dict:
    """Read-only identity checks. No fetch, reset, checkout, network or imports."""
    bundle = validate_manifest(bundle)
    repo = Path(bundle["source"]["repo_path"])
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                            timeout=20, env={**os.environ, "GIT_OPTIONAL_LOCKS":"0", "GIT_TERMINAL_PROMPT":"0"})
    if result.returncode or result.stdout.strip() != bundle["source"]["head_sha"]:
        raise StateConflict("canonical source HEAD changed; review the migration manifest before proceeding")
    clean = subprocess.run(["git", "-C", str(repo), "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "--untracked-files=normal"],
                           capture_output=True, text=True, timeout=20,
                           env={**os.environ, "GIT_OPTIONAL_LOCKS":"0", "GIT_TERMINAL_PROMPT":"0"})
    if clean.returncode or clean.stdout.strip():
        raise StateConflict("canonical source contains uncommitted work; review before preparing migration")
    checked = []
    for pin in bundle["file_pins"]:
        p = Path(pin["path"])
        if not p.is_file() or p.is_symlink() or p.stat().st_size > 32 * 1024 * 1024:
            raise StateConflict("pinned evidence is missing, a symlink, or exceeds the bounded size")
        with p.open("rb") as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != pin["sha256"]:
            raise StateConflict("pinned evidence changed; do not silently migrate against new evidence")
        checked.append({"path":str(p), "sha256":pin["sha256"]})
    return {"source_head":result.stdout.strip(),"checked_files":checked,"network_used":False}


def _snapshot(service, project_id):
    return {"graph":service.store.get_graph(project_id),
            "policy":service.portfolio.overview(project_id)["policy"],
            "source":service.portfolio.source_binding(project_id),
            "references":service.portfolio.references(project_id,limit=200)["references"]}


def stage_shadow(service, value: dict) -> dict:
    """Prepare a held graph in an explicitly isolated service, never dispatch it.

    A partially failed preview is not reused; its TemporaryDirectory is dropped.
    Completed identical staging is idempotent and refuses later manual changes.
    """
    bundle = validate_manifest(value)
    pid = bundle["project"]["project_id"]
    manifest_hash = digest(bundle)
    existing = [p for p in service.store.list_projects() if p["project_id"] == pid]
    if existing:
        with service.store.transaction() as conn:
            row = conn.execute("SELECT payload_json FROM state_events WHERE project_id=? AND event_type='migration_prepared' "
                               "ORDER BY seq DESC LIMIT 1", (pid,)).fetchone()
        if not row:
            raise StateConflict("project exists without a complete migration receipt; refuse to merge implicitly")
        receipt = json.loads(row[0])
        if receipt["manifest_hash"] != manifest_hash or receipt["snapshot_hash"] != digest(_snapshot(service,pid)):
            raise StateConflict("manifest or staged project changed; a new review is required")
        return {**receipt,"idempotent":True}
    service.create_project(pid,bundle["project"]["title"])
    service.portfolio.set_dispatch(pid,bundle["dispatch"],0,bundle["dispatch_reason"])
    service.store.add_nodes(pid,bundle["nodes"])
    service.bind_source(pid,bundle["source"]["repo_path"],0)
    for hold in bundle["node_holds"]:
        service.portfolio.set_hold(pid,hold["node_key"],True,0,hold["reason"])
    for ref in bundle["references"]:
        # Explicit historical observations, not a fabricated SkillFlow run.
        service.portfolio.add_reference(project_id=pid,**ref)
    with service.store.transaction(write=True) as conn:
        for excluded in bundle["excluded_runs"]:
            service.store._event(conn,pid,None,"migration_run_excluded",excluded)
    graph = service.store.get_graph(pid)
    assert all(n["status"] == "OPEN" and n["readiness"] == "held" for n in graph["nodes"])
    assert service.store.frontier(pid)["total"] == 0
    with service.store.transaction() as conn:
        attempts=conn.execute("SELECT COUNT(*) FROM state_attempts WHERE project_id=?",(pid,)).fetchone()[0]
        receipts=conn.execute("SELECT COUNT(*) FROM state_acceptances WHERE project_id=?",(pid,)).fetchone()[0]
    assert attempts == receipts == 0
    receipt={"manifest_hash":manifest_hash,"snapshot_hash":digest(_snapshot(service,pid)),"project_id":pid,
             "nodes":len(graph["nodes"]),"references":len(bundle["references"]),"excluded_runs":len(bundle["excluded_runs"]),
             "verified":0,"attempts":0,"ready":0,"dispatch":bundle["dispatch"],"source_head":bundle["source"]["head_sha"]}
    with service.store.transaction(write=True) as conn:
        service.store._event(conn,pid,None,"migration_prepared",receipt)
    return {**receipt,"idempotent":False}


def dry_run(value: dict, *, check_files=True) -> dict:
    """No production database option exists here: always a TemporaryDirectory."""
    bundle=validate_manifest(value)
    inputs=verify_inputs(bundle) if check_files else {"skipped_input_check":True}
    from core.db_manager import DBManager
    from core.state_service import StateService
    with tempfile.TemporaryDirectory(prefix="state-migration-preview-") as tmp:
        service=StateService(DBManager(str(Path(tmp)/"shadow.sqlite")),actor="migration-preview")
        first=stage_shadow(service,bundle)
        second=stage_shadow(service,bundle)
        assert second["idempotent"] is True
        graph=service.portfolio.overview(bundle["project"]["project_id"])
        return {"result":"PASS","mode":"temporary-shadow-only","receipt":first,"idempotent_replay":True,
                "inputs":inputs,"overview":graph,"assumptions":bundle["assumptions"],"production_database_written":False}
