import asyncio
import logging
import secrets
import time

from fastapi import Depends, FastAPI, Header, HTTPException

from .config import settings
from .models import (
    ExplorerAgentRespondRequest,
    ExplorerAgentRespondResponse,
    HomeAgentRespondRequest,
    HomeAgentRespondResponse,
    SafeRouteAreaRiskResearchRequest,
    SafeRouteAreaRiskResearchResponse,
)
from .home_agent import codex_auth_status, run_home_agent_turn
from .service import research_safe_route_area_risk, respond

app = FastAPI(title=settings.project_name)
logger = logging.getLogger(__name__)
_request_semaphore = asyncio.Semaphore(max(1, min(int(settings.max_concurrent_requests or 8), 32)))
_home_request_semaphore = asyncio.Semaphore(
    max(1, min(int(settings.home_agent_max_concurrent_requests or 2), 8))
)
_quota_lock = asyncio.Lock()
_quota_events: dict[str, list[float]] = {}


async def enforce_request_quota(quota_key: str | None, category: str) -> None:
    key = f"{category}:{str(quota_key or 'shared-service').strip().lower()}"
    now = time.time()
    hourly = max(1, min(int(settings.quota_requests_per_hour or 60), 1000))
    daily = max(hourly, min(int(settings.quota_requests_per_day or 300), 10000))
    if category == "safe-route":
        hourly = min(hourly * 4, 1000)
        daily = min(daily * 4, 10000)
    async with _quota_lock:
        events = [timestamp for timestamp in _quota_events.get(key, []) if now - timestamp <= 86400]
        _quota_events[key] = events
        if len(events) >= daily or sum(1 for timestamp in events if now - timestamp <= 3600) >= hourly:
            raise HTTPException(status_code=429, detail="LunarAgent request quota exceeded")
        events.append(now)


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
    if any(
        not str(value or "").strip()
        for value in (
            settings.openai_api_key,
            settings.backend_base_url,
            settings.backend_shared_token,
        )
    ):
        raise HTTPException(status_code=503, detail="Agent dependencies are not configured")
    home_auth = await codex_auth_status()
    return {
        "status": "ok",
        "service": settings.project_name,
        "model": settings.model,
        "authConfigured": True,
        "dependenciesConfigured": True,
        "homeAgent": {
            "enabled": bool(settings.home_agent_enabled),
            "model": settings.home_agent_model,
            "reasoningEffort": settings.home_agent_reasoning_effort,
            "auth": home_auth,
        },
    }


@app.get("/live")
async def live() -> dict:
    return {"status": "ok", "service": settings.project_name}


@app.get("/v1/home-agent/health", dependencies=[Depends(require_token)])
async def home_agent_health() -> dict:
    auth = await codex_auth_status()
    if not settings.home_agent_enabled:
        raise HTTPException(status_code=503, detail="Home Agent runtime is disabled")
    if not auth["configured"]:
        raise HTTPException(status_code=503, detail="ChatGPT-managed Codex authentication is unavailable")
    if not str(settings.backend_base_url or "").strip() or not str(
        settings.backend_shared_token or ""
    ).strip():
        raise HTTPException(status_code=503, detail="Home Agent backend bridge is unavailable")
    return {
        "status": "ok",
        "service": settings.project_name,
        "model": settings.home_agent_model,
        "reasoningEffort": settings.home_agent_reasoning_effort,
        "auth": auth,
        "billingMode": "chatgpt-plan",
    }


@app.post("/v1/explorer-agent/respond", response_model=ExplorerAgentRespondResponse, dependencies=[Depends(require_token)])
async def explorer_agent_respond(request: ExplorerAgentRespondRequest) -> ExplorerAgentRespondResponse:
    try:
        await enforce_request_quota(request.quotaKey, "explorer")
        async with _request_semaphore:
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


@app.post(
    "/v1/home-agent/respond",
    response_model=HomeAgentRespondResponse,
    dependencies=[Depends(require_token)],
)
async def home_agent_respond(request: HomeAgentRespondRequest) -> HomeAgentRespondResponse:
    try:
        await enforce_request_quota(request.clientId, "home")
        async with _home_request_semaphore:
            return await run_home_agent_turn(request)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Home Agent response failed (%s)",
            type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail="Home Agent response failed.") from exc


@app.post(
    "/v1/threatscape/query-risk/research",
    response_model=SafeRouteAreaRiskResearchResponse,
    dependencies=[Depends(require_token)],
    name="threatscape_query_risk_research",
)
@app.post(
    "/v1/safe-route/area-risk/research",
    response_model=SafeRouteAreaRiskResearchResponse,
    dependencies=[Depends(require_token)],
    name="safe_route_area_risk_research",
)
async def safe_route_area_risk_research(request: SafeRouteAreaRiskResearchRequest) -> SafeRouteAreaRiskResearchResponse:
    try:
        await enforce_request_quota("area-risk", "safe-route")
        async with _request_semaphore:
            payload = await research_safe_route_area_risk(
                aoi=request.aoi,
                evidence=request.evidence,
                max_zones=request.maxZones,
            )
        return SafeRouteAreaRiskResearchResponse(**payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise_internal_server_error(exc, "Area risk research failed.")
