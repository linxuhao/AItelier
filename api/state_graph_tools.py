"""MCP adapters for the same typed State DAG commands used by REST/driver."""
from core.state_commands import describe, execute
from core.state_graph import StateGraphError
from core.state_service import StateService


def register_state_tools(tool, mcp, service_factory=None):
    def service():
        if service_factory is not None:
            return service_factory()
        from api.dependencies import get_db_manager, get_workspace_manager, get_skillflow, get_config_registry
        from api.mcp_router import _request_from, _start_driver
        from api.state_graph_routers import authenticated_actor
        try:
            request = _request_from(mcp.get_context())
        except Exception:
            request = None
        return StateService(get_db_manager(), get_workspace_manager(), attach_driver=_start_driver,
                            actor=authenticated_actor(request),
                            runtime_factory=lambda: (get_skillflow(), get_config_registry()))

    def invoke(action, arguments, write):
        from mcp.server.fastmcp.exceptions import ToolError as MCPToolError
        try:
            return execute(service(), action, arguments, allow_write=write)
        except StateGraphError as exc:
            # Unlike a success-shaped {error: ...}, this sets MCP isError=true.
            raise MCPToolError(str(exc)) from exc

    @tool("state_graph_help", "read", "Read the State DAG command schemas and trust boundary. State facts are separate from workflow execution. Use this before state_graph_read/write.")
    def state_graph_help() -> dict:
        return describe()

    @tool("state_graph_read", "read", "Private State DAG query. Requires writer authorization even though it does not mutate. Actions: list_projects, get_graph, get_node, frontier, events, get_attempt, list_attempts, evidence. Exact arguments: state_graph_help.")
    def state_graph_read(action: str, arguments: dict) -> dict:
        return {"result": invoke(action, arguments, False)}

    @tool("state_graph_write", "write", "Manage State DAG goals/attempts using typed state_graph_help contracts. Actions include create_project, add_nodes, revise_node, split_node, supersede_node, start_attempt, recover_attempt, reconcile_attempt, start_external_attempt, report_external_attempt, record_evidence, verify_node, import_tasks. Checkpoints stay ask; completion never implies verification. Evidence must come from an actual verifier, not invented passing results.")
    def state_graph_write(action: str, arguments: dict) -> dict:
        return {"result": invoke(action, arguments, True)}

    @mcp.prompt(name="state_graph_driver", description="How to drive long projects without putting project state inside a workflow")
    def state_graph_driver() -> str:
        return ("For your own harness/subagents, start_external_attempt freezes the goal context without creating a workflow. "
                "Run your own verifier, report_external_attempt with its exact context_hash/artifact/report/quiescence, "
                "then record real per-criterion evidence and verify_node. Never invent evidence or fake SkillFlow runs. "
                "Use state_graph_help for schemas. Read a project's frontier; read get_node for its revision, "
                "contract, dependency receipts and attempts. Choose a seeded SkillFlow workflow, then "
                "start_attempt with the expected revision and a stable request_key. Preserve the returned attempt_id "
                "and run_id; use normal wait_for_run/checkpoint tools with review gates. Reconcile the exact attempt. "
                "Completed workflow means CANDIDATE only. Record real verifier evidence against the pinned artifact "
                "and every contract criterion; verify_node checks completeness and stale dependencies. Never invent "
                "passing evidence. Failed/skipped/missing evidence cannot unlock dependents. On context loss, recover "
                "the existing attempt; do not relaunch with a new key. Split goals transactionally when research finds "
                "subgoals. Changing requirements invalidates prior acceptance and dependent receipts. SkillFlow owns "
                "step progression, retries and checkpoints; the driver chooses goals and workflows.")
