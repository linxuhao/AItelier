"""MCP adapters for the same typed State DAG commands used by REST/driver."""
import inspect
import anyio

from core.state_driver_guide import STATE_DRIVER_GUIDE
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
        return {**describe(), "driver_guide": STATE_DRIVER_GUIDE,
                "driver_prompt": "state_graph_driver", "driver_resource": "aitelier://state/driver-guide"}

    @tool("state_graph_read", "read", "Private State DAG query. Requires writer authorization even though it does not mutate. Actions: list_projects, get_graph, get_node, frontier, events, get_attempt, list_attempts, evidence, wait_for_state_change. Use cursor-based waits for updates. Exact arguments: state_graph_help.")
    async def state_graph_read(action: str, arguments: dict) -> dict:
        from mcp.server.fastmcp.exceptions import ToolError as MCPToolError
        try:
            result = await anyio.to_thread.run_sync(invoke, action, arguments, False)
            if inspect.isawaitable(result):
                result = await result
            return {"result": result}
        except StateGraphError as exc:
            raise MCPToolError(str(exc)) from exc

    @tool("state_graph_write", "write", "Manage State DAG goals/attempts using typed state_graph_help contracts. Actions include create_project, add_nodes, revise_node, split_node, supersede_node, start_attempt, recover_attempt, reconcile_attempt, start_external_attempt, report_external_attempt, record_evidence, verify_node, import_tasks. Checkpoints stay ask; completion never implies verification. Evidence must come from an actual verifier, not invented passing results.")
    def state_graph_write(action: str, arguments: dict) -> dict:
        return {"result": invoke(action, arguments, True)}

    @mcp.prompt(name="state_graph_driver", description="State DAG driver: register, wait by cursor, inspect evidence and accept; supports workflows and external subagents")
    def state_graph_driver() -> str:
        return STATE_DRIVER_GUIDE

    @mcp.resource("aitelier://state/driver-guide", name="state_driver_guide",
                  description="Static State driver protocol; also available in state_graph_help for clients without prompts/resources",
                  mime_type="text/markdown")
    def state_driver_guide() -> str:
        return STATE_DRIVER_GUIDE
