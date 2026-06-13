# api/agent_routers.py
# Streaming SSE endpoint for the Meta Agent chat interface.

import json
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from core.meta_agent import MetaAgent
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager
from core.scheduler import wake_scheduler
from api.dependencies import get_db_manager, get_workspace_manager
from api.auth import CurrentUser, get_optional_user

router = APIRouter(prefix="/api/agent", tags=["Meta Agent"])


class AgentChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    current_project: str | None = None
    session_id: str | None = None


@router.post("/chat")
async def agent_chat(
    request: AgentChatRequest,
    user: CurrentUser | None = Depends(get_optional_user),
    db: DBManager = Depends(get_db_manager),
    ws: WorkspaceManager = Depends(get_workspace_manager),
):
    """Stream agent response as SSE events."""
    owner = user.email if user else "cli@local"

    # Load history from DB when session-scoped (cross-project, cross-restart)
    history = list(request.history)
    if request.session_id:
        db_msgs = db.get_chat_history_by_session(request.session_id, limit=100)
        # Append DB messages NOT already in the client-provided history
        # (simple dedup: client history comes first, then older DB messages)
        client_keys = {(m.get("role"), m.get("content", "")[:100]) for m in history}
        for m in db_msgs:
            key = (m["role"], m.get("content", "")[:100])
            if key not in client_keys:
                history.append({"role": m["role"], "content": m["content"]})

    agent = MetaAgent(db, ws, owner_email=owner, session_id=request.session_id)

    async def event_stream():
        collected_events = []  # persist after streaming
        try:
            async for event in agent.chat(
                request.message, history, request.current_project
            ):
                collected_events.append(event)
                # If a tool result carries _wake, trigger scheduler poll
                if event.get("type") == "tool_result":
                    result = event.get("result", {})
                    if isinstance(result, dict) and result.get("_wake"):
                        result.pop("_wake", None)  # clean internal flag
                        wake_scheduler()
                yield f"data: {json.dumps(event, default=str, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

        # Persist to chat history if session-scoped
        if request.session_id:
            try:
                # Save user message
                db.save_chat_message_with_session(
                    request.session_id, request.current_project or "",
                    "user", request.message,
                )
                # Save assistant response (last text_delta or done event)
                assistant_text = ""
                for evt in collected_events:
                    if evt.get("type") == "text_delta":
                        assistant_text += evt.get("content", "")
                    elif evt.get("type") == "done":
                        msg = evt.get("message", {})
                        assistant_text = msg.get("content", "") or assistant_text
                if assistant_text.strip():
                    db.save_chat_message_with_session(
                        request.session_id, request.current_project or "",
                        "assistant", assistant_text.strip(),
                    )
            except Exception:
                pass  # Best-effort persistence

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/session/create")
async def create_session(
    db: DBManager = Depends(get_db_manager),
):
    """Create a new chat session and return its ID."""
    session_id = db.create_session()
    return {"session_id": session_id}
