"""Typed, finite command vocabulary shared by REST, MCP and the driver agent.

This is not reflection RPC: only the handlers enumerated below can be called.
Extra fields (including status=VERIFIED and spoofed reviewer identity) fail.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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
    note_after_revision: int | None = Field(default=None, ge=0, le=2**63-1)
    filter_mode: Literal["all", "any"] = "all"
    actionable_only: bool = True
    return_when_idle: bool = False
    timeout_seconds: float = Field(default=30.0, ge=0, le=900)
    limit: int = Field(default=100, ge=1, le=500)


class SendDirectorMessage(Request):
    sender_project_id: str
    director_identity: str
    request_key: str
    subject: str
    body: str
    target_project_id: str | None = None
    broadcast: bool = False
    reply_to_delivery_id: str | None = None
    delivery_mode: Literal["transient", "standing"] = "transient"


class ListDirectorMessages(Project):
    after: int = 0
    limit: int = 100
    delivery_mode: Literal["transient", "standing"] | None = None
    statuses: list[Literal["unread", "acknowledged", "resolved"]] | None = Field(
        default=None, min_length=1)

    @model_validator(mode="after")
    def statuses_are_unique(self):
        if self.statuses is not None and len(set(self.statuses)) != len(self.statuses):
            raise ValueError("statuses must be duplicate-free")
        return self


class TransitionDirectorMessage(Project):
    delivery_id: str
    expected_version: int
    request_key: str


class DriverNote(Project):
    pass


class DriverNoteHistory(Project):
    after_revision: int = Field(default=0, ge=0, le=2**63-1)
    limit: int = Field(default=100, ge=1, le=500)


class SearchDriverNoteHistory(Project):
    query: str = Field(default="", max_length=500,
                       description="Unicode case-insensitive literal text; empty lists filtered revisions.")
    section: Literal["permanent", "temporary"] | None = Field(
        default=None, description="Exact changed-section filter.")
    actor: str | None = Field(
        default=None, min_length=1, max_length=320,
        description="Exact authenticated actor filter; returned metadata is redacted.")
    director_identity: str | None = Field(
        default=None, min_length=1, max_length=320,
        description="Exact recorded director identity filter; returned metadata is redacted.")
    after_revision: int = Field(default=0, ge=0, le=2**63-1,
                                description="Exclusive stable pagination cursor.")
    min_revision: int | None = Field(
        default=None, ge=1, le=2**63-1, description="Inclusive minimum revision.")
    max_revision: int | None = Field(
        default=None, ge=1, le=2**63-1, description="Inclusive maximum revision.")
    created_after: str | None = Field(default=None, max_length=64,
                                       description="Exclusive timezone-aware ISO-8601 lower bound.",
                                       json_schema_extra={"format": "date-time"})
    created_before: str | None = Field(default=None, max_length=64,
                                        description="Exclusive timezone-aware ISO-8601 upper bound.",
                                        json_schema_extra={"format": "date-time"})
    limit: int = Field(default=20, ge=1, le=100,
                       description="Maximum entries returned per page, ordered by revision ascending.")
    excerpt_chars: int = Field(default=320, ge=64, le=1000,
                               description="Maximum characters in each redacted excerpt.")


class UpdateDriverNote(Project):
    section: Literal["permanent", "temporary"]
    content: str = Field(max_length=100000)
    expected_revision: int = Field(ge=0, le=2**63-1)
    director_identity: str = Field(min_length=1, max_length=320)
    operation: Literal["replace", "append"] = "replace"


class Attempt(Request):
    attempt_id: str


class RequestAttemptBase(Attempt):
    base_sha: str


class RetireReservation(Attempt):
    reason: str


class ListAttempts(Node):
    limit: int = 100


class FrozenPrerequisiteCheck(Request):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    probe: Literal["source_head", "sha256_file", "runtime_capability"]
    arguments: dict
    expected: Any


class FrozenPrerequisites(Request):
    version: Literal[1]
    checks: list[FrozenPrerequisiteCheck] = Field(min_length=1, max_length=64)


class StartAttempt(Node):
    expected_revision: int
    workflow: str
    request_key: str
    instruction: str = ""
    base_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
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
    # Exact, executor-neutral checks interpreted by SkillFlow before launch.
    # The descriptor is frozen into the attempt and validated fail-closed by
    # the framework; AItelier supplies only read-only host probes.
    frozen_prerequisites: FrozenPrerequisites | None = None


class StartExternalAttempt(Node):
    expected_revision: int
    harness: str
    external_id: str
    request_key: str
    instruction: str = ""


class DispositionFailedAttempt(Attempt):
    disposition: Literal["continue-workflow", "handoff-external", "leave-stopped"]
    request_key: str | None = None
    relay_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    instruction: str = ""
    harness: str | None = None
    external_id: str | None = None

    @model_validator(mode="after")
    def exact_disposition_shape(self):
        launch = self.disposition != "leave-stopped"
        if launch and (self.request_key is None or self.relay_digest is None):
            raise ValueError("continue-workflow and handoff-external require request_key and relay_digest")
        if not launch and any(v is not None for v in (
                self.request_key, self.relay_digest, self.harness, self.external_id)):
            raise ValueError("leave-stopped takes only attempt_id, disposition and optional instruction")
        if self.disposition == "continue-workflow" and (self.harness is not None or self.external_id is not None):
            raise ValueError("continue-workflow does not accept external harness identity")
        if self.disposition == "handoff-external" and (self.harness is None or self.external_id is None):
            raise ValueError("handoff-external requires harness and external_id")
        return self


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
    verdict: str = Field(description=(
        "Evidence verdict. Failed external attempts accept only pass or fail; "
        "candidate evidence retains the general pass/fail/skip contract."))
    artifact: str = Field(description=(
        "Exact artifact reviewed. Candidate evidence must match the attempt artifact. "
        "A terminal quiescent failed external attempt may retain criterion evidence "
        "against one shared artifact, exactly once per criterion, but this never promotes "
        "the attempt or permits verification."))
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
    "frontier": Frontier, "events": Events, "wait_for_state_change": WaitForStateChange,
    "get_driver_note": DriverNote, "driver_note_history": DriverNoteHistory,
    "search_driver_note_history": SearchDriverNoteHistory, "get_attempt": Attempt,
    "list_attempts": ListAttempts, "evidence": Attempt,
    "project_catalog": ProjectCatalog, "project_overview": Project,
    "project_run_summary": Project,
    "project_attempts": ProjectAttempts, "references": References,
    "run_owners": RunOwner, "attempt_detail": Attempt,
    "list_director_messages": ListDirectorMessages,
}
WRITE_REQUESTS = {
    "create_design_revision": CreateDesignRevision, "create_design_baseline": CreateDesignBaseline,
    "bind_node_design": BindDesign,
    "create_project": CreateProject, "add_nodes": AddNodes, "revise_node": ReviseNode,
    "split_node": SplitNode, "supersede_node": SupersedeNode, "set_node_facet": SetNodeFacet,
    "start_attempt": StartAttempt, "request_attempt_base": RequestAttemptBase,
    "recover_attempt": Attempt, "reconcile_attempt": Attempt,
    "disposition_failed_attempt": DispositionFailedAttempt,
    "retire_reservation": RetireReservation, "record_evidence": Evidence,
    "verify_node": Verify, "import_tasks": ImportTasks,
    "bind_source": BindSource, "set_dispatch": DispatchPolicy, "set_node_hold": NodeHold,
    "add_reference": HistoricalReference, "refresh_project": RefreshProject,
    "start_external_attempt": StartExternalAttempt, "report_external_attempt": ExternalObservation,
    "update_driver_note": UpdateDriverNote,
    "send_director_message": SendDirectorMessage,
    "acknowledge_director_message": TransitionDirectorMessage,
    "resolve_director_message": TransitionDirectorMessage,
}
REQUESTS = READ_REQUESTS | WRITE_REQUESTS


def describe() -> dict:
    return {"architecture": "State DAG owns facts; a separately authorized execution transport owns execution. Completion is only a candidate.",
            "trust": "Evidence is an authorized verifier attestation, not an automatic guarantee of truth.",
            "operations": {name: {"mutates": name in WRITE_REQUESTS, "arguments": model.model_json_schema()}
                           for name, model in REQUESTS.items()}}


def execute(service, action: str, arguments: dict, *, allow_write: bool = False):
    if not isinstance(action, str) or action not in REQUESTS:
        raise StateGraphError("unknown state graph action; use state_graph_help")
    if action in WRITE_REQUESTS and not allow_write:
        raise StateGraphError("mutating action is not available on the read surface")
    director_action = action in {
        "send_director_message", "list_director_messages",
        "acknowledge_director_message", "resolve_director_message"}
    if not isinstance(arguments, dict):
        if director_action:
            from core.director_messaging_protocol import DirectorMessageError
            return DirectorMessageError("invalid_request").as_dict()
        raise StateGraphError("arguments must be an object")
    try:
        args = REQUESTS[action].model_validate(arguments).model_dump()
    except ValidationError as exc:
        if director_action:
            from core.director_messaging_protocol import DirectorMessageError
            return DirectorMessageError("invalid_request").as_dict()
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
        "wait_for_state_change": service.wait_for_state_change, "events": service.store.events,
        "get_driver_note": service.driver_notes.get, "driver_note_history": service.driver_notes.history,
        "search_driver_note_history": service.driver_notes.search,
        "get_attempt": service.get_attempt,
        "list_attempts": service.attempts.list, "evidence": service.attempts.evidence,
        "create_project": service.create_project, "add_nodes": service.store.add_nodes,
        "revise_node": service.store.revise_node, "split_node": service.store.split_node,
        "supersede_node": service.store.supersede_node, "set_node_facet": service.store.set_node_facet,
        "start_attempt": service.start_attempt,
        "request_attempt_base": service.request_attempt_base,
        "recover_attempt": service.recover_attempt, "reconcile_attempt": service.reconcile_attempt,
        "disposition_failed_attempt": service.disposition_failed_attempt,
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
        "update_driver_note": service.driver_notes.update,
        "send_director_message": service.director_messages.send_director_message,
        "list_director_messages": service.director_messages.list_director_messages,
        "acknowledge_director_message": service.director_messages.acknowledge_director_message,
        "resolve_director_message": service.director_messages.resolve_director_message,
    }
    try:
        return handlers[action](**args)
    except Exception as exc:
        if director_action:
            from core.director_messaging_protocol import DirectorMessageError
            if isinstance(exc, DirectorMessageError):
                return exc.as_dict()
        raise


# Compact entry points for the internal driver. Exact operation schemas are
# available on demand rather than expanding every schema into every prompt.
DRIVER_TOOL_DEFINITIONS = [
    {"type": "function", "function": {"name": "state_graph_help", "description": "Read the typed State DAG operation contracts before planning or modifying persistent project goals.",
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "state_graph_read", "description": "Read persistent state projects, node context, dependency frontier, attempts or evidence. This does not start workflows or certify completion.",
     "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": list(READ_REQUESTS)}, "arguments": {"type": "object"}},
                    "required": ["action", "arguments"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "state_graph_write", "description": "Manage State DAG goals and workflow-backed or externally observed attempts using state_graph_help contracts. Start only ready nodes, preserve checkpoints, and never invent passing evidence. Workflow completion is not verification.",
     "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": list(WRITE_REQUESTS)}, "arguments": {"type": "object"}},
                    "required": ["action", "arguments"], "additionalProperties": False}}},
]
