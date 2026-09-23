"""MCP adapters for the same typed State DAG commands used by REST/driver."""
import inspect
import anyio

from core.state_driver_guide import (STATE_DRIVER_GUIDE, STATE_DRIVER_GUIDE_INDEX,
                                     guide_index_addresses)
from api.authz import may_read_private
from core.state_commands import describe, execute
from core.state_graph import StateGraphError
from core.state_service import StateService


def mcp_read_trust(request) -> bool:
    """Whether the MCP caller behind ``request`` may read private State.

    The credential is the request itself: ``api.authz.may_read_private`` derives
    the verdict from the raw request. NO request object is NO credential, so this
    returns False - a `_request_from` failure (an exception swallowed into None)
    must never be a way into private reads.
    """
    if request is None:
        return False
    return may_read_private(request)


def register_state_tools(tool, mcp, service_factory=None):
    def service():
        if service_factory is not None:
            return service_factory()
        from api.dependencies import get_db_manager, get_workspace_manager, get_skillflow, get_config_registry
        from api.mcp_router import _request_from, _start_driver
        from api.state_graph_routers import authenticated_actor
        request = None
        try:
            request = _request_from(mcp.get_context())
        except Exception:
            request = None
        # Trust comes from a REAL credential, never from a default and never from
        # "there was no request object". An HTTP request inherits its anonymous /
        # reader verdict; a transport that produced no request object carries no
        # credential at all and is UNTRUSTED, exactly like an anonymous visitor.
        return StateService(get_db_manager(), get_workspace_manager(), attach_driver=_start_driver,
                            actor=authenticated_actor(request),
                            runtime_factory=lambda: (get_skillflow(), get_config_registry()),
                            project_read_trusted=mcp_read_trust(request))
    def invoke(action, arguments, write):
        from mcp.server.fastmcp.exceptions import ToolError as MCPToolError
        try:
            return execute(service(), action, arguments, allow_write=write)
        except StateGraphError as exc:
            # Unlike a success-shaped {error: ...}, this sets MCP isError=true.
            raise MCPToolError(str(exc)) from exc

    @tool("state_graph_help", "read", "Read the State DAG command schemas and trust boundary. State facts are separate from workflow execution. Use this before state_graph_read/write.")
    def state_graph_help() -> dict:
        # driver_guide is the INDEX, because this is the field every recovery hook
        # injects verbatim on every compaction. The full text is one fetch away.
        return {**describe(), "driver_guide": STATE_DRIVER_GUIDE_INDEX,
                "driver_guide_sections": guide_index_addresses(),
                "driver_guide_full_chars": len(STATE_DRIVER_GUIDE),
                "driver_prompt": "state_graph_driver", "driver_resource": "aitelier://state/driver-guide"}

    @tool("state_graph_read", "read", "Private State query. Requires writer authorization even though it does not mutate. Actions include list_director_messages, list_issues, get_issue, get_driver_note, driver_note_index, get_driver_note_entry, check_driver_note_index, get_driver_guide_section, driver_note_history, search_driver_note_history, list_projects, get_graph, get_node, facet_lint, frontier, events, get_attempt, list_attempts, evidence, search_design_items, design_impact, wait_for_state_change. Driver-note search returns bounded redacted excerpts in revision order. Design queries return candidates/review hints, not semantic proof. Use cursor-based waits for updates. Exact arguments: state_graph_help.")
    async def state_graph_read(action: str, arguments: dict) -> dict:
        from mcp.server.fastmcp.exceptions import ToolError as MCPToolError
        try:
            result = await anyio.to_thread.run_sync(invoke, action, arguments, False)
            if inspect.isawaitable(result):
                result = await result
            return {"result": result}
        except StateGraphError as exc:
            raise MCPToolError(str(exc)) from exc

    @tool("state_graph_write", "write", "Manage State facts using typed state_graph_help contracts. Actions include report_issue, link_issue, resolve_issue, send_director_message, acknowledge_director_message, resolve_director_message, update_driver_note, write_driver_note_entry, supersede_driver_note_entry, delist_driver_note_entry, create_project, add_nodes, revise_node, split_node, supersede_node, set_node_facet, start_attempt, recover_attempt, reconcile_attempt, disposition_failed_attempt, start_external_attempt, report_external_attempt, record_evidence, verify_node, import_tasks. Report observations (defects, gaps, hand-offs, questions) with report_issue, not add_nodes; nodes are acceptance-bearing goals. Checkpoints stay ask; completion never implies verification. Evidence must come from an actual verifier, not invented passing results. Failed external attempts may retain scoped evidence only after a terminal quiescent report; they remain failed and unverifiable.")
    def state_graph_write(action: str, arguments: dict) -> dict:
        return {"result": invoke(action, arguments, True)}

    @mcp.prompt(name="state_graph_driver", description="State DAG protocol: register, wait by cursor, inspect evidence and accept across authorized transports")
    def state_graph_driver() -> str:
        return STATE_DRIVER_GUIDE

    @mcp.resource("aitelier://state/driver-guide", name="state_driver_guide",
                  description="Static State driver protocol; also available in state_graph_help for clients without prompts/resources",
                  mime_type="text/markdown")
    def state_driver_guide() -> str:
        return STATE_DRIVER_GUIDE
