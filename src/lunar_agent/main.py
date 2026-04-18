from fastapi import Depends, FastAPI, Header, HTTPException

from .config import settings
from .models import ExplorerAgentRespondRequest, ExplorerAgentRespondResponse
from .service import respond

app = FastAPI(title=settings.project_name)


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = str(settings.shared_token or "").strip()
    if not expected:
        return

    provided = str(authorization or "").strip()
    if provided == f"Bearer {expected}":
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": settings.project_name,
        "model": settings.model,
    }


@app.post("/v1/explorer-agent/respond", response_model=ExplorerAgentRespondResponse, dependencies=[Depends(require_token)])
async def explorer_agent_respond(request: ExplorerAgentRespondRequest) -> ExplorerAgentRespondResponse:
    try:
        payload = await respond(
            session_id=request.sessionId,
            allow_ui_actions=bool(request.allowUiActions),
            conversation_history=[message.model_dump() for message in request.conversationHistory],
            query_preview=request.queryPreview,
            summary=request.querySummary,
            context=request.queryContext,
            user_message=request.currentUserMessage,
        )
        return ExplorerAgentRespondResponse(**payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
