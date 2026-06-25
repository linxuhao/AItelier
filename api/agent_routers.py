# api/agent_routers.py
# Streaming SSE endpoint for the Meta Agent chat interface.
# Also provides REST endpoints for chat history persistence:
#   GET  /api/agent/chat/history   — session message history
#   GET  /api/agent/sessions       — session list with metadata
#   POST /api/agent/chat/message   — save a single message

import json
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
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


class AgentSaveMessageRequest(BaseModel):
    session_id: str
    project_id: str
    role: str
    content: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("user", "assistant", "system"):
            raise ValueError("role must be one of: 'user', 'assistant', 'system'")
        return v


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
        # Persist the user message up-front: the backend is the single owner of
        # chat-history persistence (the frontend no longer fire-and-forgets a
        # duplicate save). Saving before the stream means it survives a
        # mid-stream disconnect and works for any client of this endpoint.
        if request.session_id:
            try:
                db.save_chat_message_with_session(
                    request.session_id, request.current_project or "",
                    "user", request.message,
                )
            except Exception:
                pass  # Best-effort persistence
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

        # Persist the assistant response if session-scoped
        if request.session_id:
            try:
                # Accumulate ALL streamed prose, not just the final turn. The agent
                # loop can emit prose across several turns (e.g. it presents the
                # brief, then makes a tool call, then closes); capturing only the
                # `done` message dropped the earlier brief, so reload/soft-nav lost
                # it. `done` content is the last turn's text — already in the
                # accumulated deltas — so it's only a fallback when no deltas ran.
                streamed_text = ""
                final_text = ""
                surfaced = []  # brief/question text delivered via tool results
                clean_finish = False  # a `done` event = the loop ended cleanly
                for evt in collected_events:
                    etype = evt.get("type")
                    if etype == "text_delta":
                        streamed_text += evt.get("content", "")
                    elif etype == "done":
                        final_text = (evt.get("message", {}) or {}).get("content", "") or ""
                        clean_finish = True
                    elif etype == "tool_result":
                        res = evt.get("result", {})
                        if isinstance(res, dict):
                            if res.get("status") == "brief_review" and res.get("brief_markdown"):
                                surfaced.append(res["brief_markdown"])
                            elif res.get("status") == "question" and res.get("question"):
                                surfaced.append(res["question"])

                # Fix G: only persist the narrative on a clean finish — a stream
                # that errored mid-way leaves partial prose we must NOT commit.
                narrative = (streamed_text or final_text).strip()
                to_save = []
                saved_narrative = ""
                if narrative and clean_finish:
                    to_save.append(narrative)
                    saved_narrative = narrative
                # Safety net: a brief/question surfaced via a completed tool result
                # is a finished artifact — persist it (deduped against any narrative
                # we actually saved) so it survives reload even if the model emitted
                # no prose or the stream later aborted.
                for content in surfaced:
                    c = (content or "").strip()
                    if c and c not in saved_narrative:
                        to_save.append(c)

                for content in to_save:
                    db.save_chat_message_with_session(
                        request.session_id, request.current_project or "",
                        "assistant", content,
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


# ── Chat history persistence endpoints ─────────────────────────────────


@router.get("/chat/history")
def get_chat_history(
    session_id: str,
    db: DBManager = Depends(get_db_manager),
):
    """Return the full message history for a session in chronological order.

    The DB stores messages newest-first; this endpoint returns them
    oldest-first for the frontend to render sequentially.
    """
    if not session_id or not session_id.strip():
        raise HTTPException(status_code=422, detail="session_id is required")

    # get_chat_history_by_session already returns oldest-first (chronological):
    # it queries ORDER BY id DESC then reverses the rows. Do NOT reverse again
    # here — a second reverse flips restored sessions to newest-first, so old
    # sessions render agent-answer-before-question while live chat is correct.
    messages = db.get_chat_history_by_session(session_id, limit=100)

    return {
        "session_id": session_id,
        "messages": messages,
    }


@router.get("/sessions")
def list_sessions(
    project_id: str | None = None,
    limit: int = 200,
    db: DBManager = Depends(get_db_manager),
):
    """List chat sessions with message count and last message preview.

    Supports optional project_id filter. Only returns sessions with
    at least one message. Ordered by most recent activity first.
    """
    sessions = db.list_chat_sessions(project_id=project_id, limit=limit)
    return {"sessions": sessions}


@router.post("/chat/message")
def save_chat_message(
    request: AgentSaveMessageRequest,
    db: DBManager = Depends(get_db_manager),
):
    """Save a single chat message to the database immediately.

    Used by the frontend to persist user messages right after sending,
    before the streaming response completes. The existing streaming
    endpoint also persists; this is an additional safety net.
    """
    db.save_chat_message_with_session(
        request.session_id,
        request.project_id,
        request.role,
        request.content,
    )
    return {"status": "saved"}