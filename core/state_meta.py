"""Thin internal-driver adapter; uses the same State DAG contracts as MCP."""
import asyncio
import inspect

from core.state_commands import describe, execute
from core.state_graph import StateGraphError
from core.state_service import StateService


async def execute_state_driver_tool(agent, name: str, arguments: dict):
    if name == "state_graph_help":
        if arguments:
            raise StateGraphError("state_graph_help takes no arguments")
        from core.state_driver_guide import STATE_DRIVER_GUIDE
        return {**describe(), "driver_guide": STATE_DRIVER_GUIDE}
    if name not in {"state_graph_read", "state_graph_write"}:
        raise StateGraphError("unknown state driver tool")
    if not isinstance(arguments, dict) or set(arguments) != {"action", "arguments"}:
        raise StateGraphError("provide action and arguments; use state_graph_help for the operation schema")

    def call():
        from api.dependencies import get_skillflow, get_config_registry
        from api.mcp_router import _start_driver
        service = StateService(agent.db, agent.ws, attach_driver=_start_driver,
                               actor=getattr(agent, "owner_email", None) or "authorized-driver",
                               runtime_factory=lambda: (get_skillflow(), get_config_registry()))
        return {"result": execute(service, arguments["action"], arguments["arguments"],
                                  allow_write=name == "state_graph_write")}

    result = await asyncio.to_thread(call)
    if inspect.isawaitable(result["result"]):
        result["result"] = await result["result"]
    return result
