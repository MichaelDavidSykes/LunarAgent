import logging
import secrets

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
logger = logging.getLogger(__name__)


def raise_internal_server_error(exc: Exception, public_detail: str) -> None:
    logger.exception(public_detail)
    raise HTTPException(status_code=500, detail=public_detail) from exc


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = str(settings.shared_token or "").strip()
    if not expected:
        logger.error("LUNAR_AGENT_SHARED_TOKEN is not configured")
        raise HTTPException(status_code=503, detail="Agent authentication is unavailable")

    provided = str(authorization or "").strip()
    scheme, separator, credential = provided.partition(" ")
    if (
        separator
        and scheme.casefold() == "bearer"
        and credential
        and secrets.compare_digest(credential.encode("utf-8"), expected.encode("utf-8"))
    ):
        return
    raise HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.get("/health")
async def health() -> dict:
    if not str(settings.shared_token or "").strip():
        raise HTTPException(status_code=503, detail="Agent authentication is unavailable")
    return {
        "status": "ok",
        "service": settings.project_name,
        "model": settings.model,
        "authConfigured": True,
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
        raise_internal_server_error(exc, "Explorer agent response failed.")


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
        raise_internal_server_error(exc, "Area risk research failed.")


@app.post("/v1/threatscape/query-risk/research", response_model=SafeRouteAreaRiskResearchResponse, dependencies=[Depends(require_token)])
async def threatscape_query_risk_research(request: SafeRouteAreaRiskResearchRequest) -> SafeRouteAreaRiskResearchResponse:
    """Compatibility alias for the ThreatScape query-risk pipeline."""
    return await safe_route_area_risk_research(request)
