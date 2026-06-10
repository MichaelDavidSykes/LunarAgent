from fastapi import Depends, FastAPI, Header, HTTPException

from .config import settings
from .models import (
    ExplorerAgentRespondRequest,
    ExplorerAgentRespondResponse,
    SafeRouteAreaRiskResearchRequest,
    SafeRouteAreaRiskResearchResponse,
)
from .service import research_safe_route_area_risk, respond

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


@app.post("/v1/safe-route/area-risk/research", response_model=SafeRouteAreaRiskResearchResponse, dependencies=[Depends(require_token)])
async def safe_route_area_risk_research(request: SafeRouteAreaRiskResearchRequest) -> SafeRouteAreaRiskResearchResponse:
    try:
        payload = await research_safe_route_area_risk(
            session_id=request.sessionId,
            aoi=request.aoi,
            evidence=request.evidence,
            max_zones=request.maxZones,
        )
        return SafeRouteAreaRiskResearchResponse(**payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
