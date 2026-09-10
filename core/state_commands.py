"""Typed, finite command vocabulary shared by REST, MCP and the driver agent.

This is not reflection RPC: only the handlers enumerated below can be called.
Extra fields (including status=VERIFIED and spoofed reviewer identity) fail.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.state_graph import StateGraphError


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Empty(Request):
    pass


class Project(Request):
    project_id: str


class CreateProject(Project):
    title: str
    source_project_id: str | None = None


class Node(Project):
    node_key: str


class AddNodes(Project):
    nodes: list[dict] = Field(min_length=1, max_length=200)


class ReviseNode(Node):
    expected_revision: int
    reason: str
    goal: str | None = None
    acceptance: list[dict] | None = None
    dependencies: list[str] | None = None


class SplitNode(Node):
    expected_revision: int
    children: list[dict]
    reason: str


class SupersedeNode(Node):
    expected_revision: int
    reason: str


class SetNodeFacet(Node):
    facet: str


class Frontier(Project):
    limit: int = 30


class Events(Project):
    after: int = 0
    limit: int = 100


class WaitForStateChange(Project):
    after: int = Field(default=0, ge=0, le=2**63-1)
    node_keys: list[str] | None = Field(default=None, min_length=1, max_length=100)
    attempt_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    actionable_only: bool = True
    return_when_idle: bool = False
    timeout_seconds: float = Field(default=30.0, ge=0, le=900)
    limit: int = Field(default=100, ge=1, le=500)


class Attempt(Request):
    attempt_id: str


class RetireReservation(Attempt):
    reason: str


class ListAttempts(Node):
    limit: int = 100


class StartAttempt(Node):
    expected_revision: int
    workflow: str
    request_key: str
    instruction: str = ""
    # Continue a FAILED SkillFlow attempt of this node: its branch head becomes
    # the new run's base and its staged draft is seeded into the new staging.
    # Explicit, so a relay is a recorded decision taken after inspecting the
    # draft (relay_inventory on the failed attempt), never a blind retry.
    continue_from: str | None = None
    # The `digest` read from that attempt's relay_inventory; REQUIRED with
    # continue_from. The relay is refused if the branch head or any staged
    # file differs from what the director read — the copy is bound to the
    # inspected draft.
    relay_digest: str | None = None


class StartExternalAttempt(Node):
    expected_revision: int
    harness: str
    external_id: str
    request_key: str
    instruction: str = ""


class ExternalObservation(Attempt):
    observation_id: str
    expected_version: int
    context_hash: str
    status: str
    report_ref: str
    report_sha256: str
    quiescent: bool = False
    artifact: str | None = None
    artifact_kind: str | None = None
    detail: str = ""


class Evidence(Attempt):
    evidence_id: str
    criterion_id: str
    verdict: str
    artifact: str
    report_ref: str
    report_sha256: str
    detail: str = ""


class Verify(Node):
    expected_revision: int
    attempt_id: str


class ImportTasks(Project):
    source_project_id: str


class ProjectCatalog(Request):
    repo_path: str | None = None
    after: str = ""
    limit: int = 100


class ProjectAttempts(Project):
    after: int = 0
    limit: int = 30


class References(Project):
    node_key: str | None = None
    after: str = ""
    limit: int = 100


class RunOwner(Request):
    run_id: str


class BindSource(Project):
    repo_path: str
    expected_revision: int = 0


class DispatchPolicy(Project):
    dispatch: str
    expected_revision: int
    reason: str


class NodeHold(Node):
    held: bool
    expected_revision: int
    reason: str


class HistoricalReference(Node):
    reference_id: str
    kind: str
    ref: str
    label: str
    provenance_actor: str
    artifact_ref: str | None = None
    report_sha256: str | None = None
    protect: bool = False


class RefreshProject(Project):
    after: int = 0
    limit: int = 20


class DesignRevision(Project):
    design_id: str
    revision: int


class SearchNodes(Project):
    query: str
    limit: int = 20


class SearchDesignItems(Project):
    query: str
    baseline_id: str | None = None
    scope: dict[str, str] | None = None
    limit: int = 20
    offset: int = 0


class DesignImpact(DesignRevision):
    baseline_id: str | None = None
    limit: int = 50
    max_visits: int = 1000


class CreateDesignRevision(Project):
    design_id: str
    expected_revision: int
    title: str
    statement: str
    rationale: str
    open_questions: list[str]
    scope: dict[str, str]
    lifecycle_status: str = "draft"
    kind: str = "rule"
    relations: list[dict] = Field(default_factory=list)


class DesignBaseline(Project):
    baseline_id: str


class CreateDesignBaseline(DesignBaseline):
    selected_revisions: list[dict]
    expected_baseline_id: str | None = None


class BindDesign(Node):
    expected_revision: int
    baseline_id: str
    bindings: list[dict]
    reason: str


class CheckDesignMarkdown(DesignBaseline):
    markdown: str


READ_REQUESTS = {
    "design_catalog": Project, "get_design_revision": DesignRevision,
    "search_design_items": SearchDesignItems, "design_impact": DesignImpact,
    "get_design_baseline": DesignBaseline, "get_design_bindings": Node,
    "export_design_markdown": DesignBaseline, "check_design_markdown": CheckDesignMarkdown,
    "list_projects": Empty, "get_graph": Project, "get_node": Node, "search_nodes": SearchNodes, "facet_lint": Project,
    "frontier": Frontier, "events": Events, "wait_for_state_change": WaitForStateChange, "get_attempt": Attempt,
    "list_attempts": ListAttempts, "evidence": Attempt,
    "project_catalog": ProjectCatalog, "project_overview": Project,
    "project_run_summary": Project,
    "project_attempts": ProjectAttempts, "references": References,
    "run_owners": RunOwner, "attempt_detail": Attempt,
}
WRITE_REQUESTS = {
    "create_design_revision": CreateDesignRevision, "create_design_baseline": CreateDesignBaseline,
    "bind_node_design": BindDesign,
    "create_project": CreateProject, "add_nodes": AddNodes, "revise_node": ReviseNode,
    "split_node": SplitNode, "supersede_node": SupersedeNode, "set_node_facet": SetNodeFacet,
    "start_attempt": StartAttempt, "recover_attempt": Attempt, "reconcile_attempt": Attempt, "retire_reservation": RetireReservation, "record_evidence": Evidence,
    "verify_node": Verify, "import_tasks": ImportTasks,
    "bind_source": BindSource, "set_dispatch": DispatchPolicy, "set_node_hold": NodeHold,
    "add_reference": HistoricalReference, "refresh_project": RefreshProject,
    "start_external_attempt": StartExternalAttempt, "report_external_attempt": ExternalObservation,
}
REQUESTS = READ_REQUESTS | WRITE_REQUESTS


def describe() -> dict:
    return {"architecture": "State DAG owns facts; optional SkillFlow or an external harness owns execution. Completion is only a candidate.",
            "trust": "Evidence is an authorized verifier attestation, not an automatic guarantee of truth.",
            "operations": {name: {"mutates": name in WRITE_REQUESTS, "arguments": model.model_json_schema()}
                           for name, model in REQUESTS.items()}}


def execute(service, action: str, arguments: dict, *, allow_write: bool = False):
    if not isinstance(action, str) or action not in REQUESTS:
        raise StateGraphError("unknown state graph action; use state_graph_help")
    if action in WRITE_REQUESTS and not allow_write:
        raise StateGraphError("mutating action is not available on the read surface")
    if not isinstance(arguments, dict):
        raise StateGraphError("arguments must be an object")
    try:
        args = REQUESTS[action].model_validate(arguments).model_dump()
    except ValidationError as exc:
        # Bounded validation errors without echoing whole inputs into logs.
        details = [{"field": ".".join(map(str, e["loc"])), "error": e["msg"]} for e in exc.errors(include_input=False)[:10]]
        raise StateGraphError(str(details)) from exc
    handlers = {
        "design_catalog": service.design.catalog, "get_design_revision": service.design.get_revision,
        "search_design_items": service.design.search, "design_impact": service.design.impact,
        "get_design_baseline": service.design.get_baseline, "get_design_bindings": service.design.node_bindings,
        "export_design_markdown": service.design.export_markdown, "check_design_markdown": service.design.check_markdown,
        "create_design_revision": service.design.create_revision, "create_design_baseline": service.design.create_baseline,
        "bind_node_design": service.design.bind_node,
        "list_projects": service.store.list_projects, "get_graph": service.store.get_graph,
        "get_node": service.node_context, "search_nodes": service.store.search_nodes, "frontier": service.store.frontier, "facet_lint": service.store.facet_lint,
        "wait_for_state_change": service.wait_for_state_change, "events": service.store.events, "get_attempt": service.get_attempt,
        "list_attempts": service.attempts.list, "evidence": service.attempts.evidence,
        "create_project": service.create_project, "add_nodes": service.store.add_nodes,
        "revise_node": service.store.revise_node, "split_node": service.store.split_node,
        "supersede_node": service.store.supersede_node, "set_node_facet": service.store.set_node_facet,
        "start_attempt": service.start_attempt,
        "recover_attempt": service.recover_attempt, "reconcile_attempt": service.reconcile_attempt,
        "retire_reservation": service.attempts.retire_reservation,
        "record_evidence": service.record_evidence, "verify_node": service.verify_node,
        "import_tasks": service.import_tasks,
        "project_catalog": service.portfolio.projects, "project_overview": service.portfolio.overview,
        "project_run_summary": service.project_run_summary,
        "project_attempts": service.portfolio.project_attempts, "references": service.portfolio.references,
        "run_owners": service.portfolio.run_owners, "attempt_detail": service.portfolio.attempt_detail,
        "bind_source": service.bind_source, "set_dispatch": service.portfolio.set_dispatch,
        "set_node_hold": service.set_node_hold, "add_reference": service.add_reference,
        "refresh_project": service.refresh_project,
        "start_external_attempt": service.start_external_attempt, "report_external_attempt": service.report_external_attempt,
    }
    return handlers[action](**args)


# Compact entry points for the internal driver. Exact operation schemas are
# available on demand rather than expanding every schema into every prompt.
DRIVER_TOOL_DEFINITIONS = [
    {"type": "function", "function": {"name": "state_graph_help", "description": "Read the typed State DAG operation contracts before planning or modifying persistent project goals.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "state_graph_read", "description": "Read persistent state projects, node context, dependency frontier, attempts or evidence. This does not start workflows or certify completion.",
     "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": list(READ_REQUESTS)}, "arguments": {"type": "object"}},
                    "required": ["action", "arguments"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "state_graph_write", "description": "Manage State DAG goals and SkillFlow/external harness attempts using state_graph_help contracts. Start only ready nodes, preserve checkpoints, and never invent passing evidence. Workflow completion is not verification.",
     "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": list(WRITE_REQUESTS)}, "arguments": {"type": "object"}},
                    "required": ["action", "arguments"], "additionalProperties": False}}},
]
