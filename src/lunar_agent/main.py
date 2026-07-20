import asyncio
import hashlib
import json
import logging
import secrets
import time

from fastapi import Depends, FastAPI, Header, HTTPException

from .config import settings
from .models import (
    ExplorerAgentCancelRequest,
    ExplorerAgentCancelResponse,
    ExplorerAgentRespondRequest,
    ExplorerAgentRespondResponse,
    SafeRouteAreaRiskResearchRequest,
    SafeRouteAreaRiskResearchResponse,
)
from .codex_agent import codex_auth_status, execution_policy_status, run_explorer_codex_turn
from .service import research_safe_route_area_risk
from .turn_registry import (
    ExplorerTurnRegistry,
    TurnAlreadyActiveError,
    TurnCancelledError,
    TurnIdentityConflictError,
)

app = FastAPI(title=settings.project_name)
logger = logging.getLogger(__name__)
_request_semaphore = asyncio.Semaphore(max(1, min(int(settings.max_concurrent_requests or 8), 32)))
_codex_request_semaphore = asyncio.Semaphore(
    max(1, min(int(settings.codex_agent_max_concurrent_requests or 2), 8))
)
_quota_lock = asyncio.Lock()
_quota_events: dict[str, list[float]] = {}
_explorer_turn_registry = ExplorerTurnRegistry()


def _explorer_turn_fingerprint(request: ExplorerAgentRespondRequest) -> str:
    payload = request.model_dump(mode="json", exclude_none=False)
    # A recovering backend worker may rebuild a fresher graph snapshot while
    # joining the same immutable user turn. The already-running canonical
    # Codex execution owns its original evidence snapshot. Its durable SDK
    # thread and final-result checkpoints can also materialize between exact
    # deliveries. Neither recovery field may start another execution or create
    # an identity error; graph bootstrap independently validates both.
    payload.pop("querySummary", None)
    payload.pop("codexThreadId", None)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    codex_auth = await codex_auth_status()
    return {
        "status": "ok",
        "service": settings.project_name,
        "model": settings.model,
        "authConfigured": True,
        "dependenciesConfigured": True,
        "lunarAgent": {
            "enabled": bool(settings.codex_agent_enabled),
            "model": settings.codex_agent_model,
            "reasoningEffort": settings.codex_agent_reasoning_effort,
            "billingMode": "chatgpt-plan",
            "auth": codex_auth,
        },
    }


@app.get("/live")
async def live() -> dict:
    return {"status": "ok", "service": settings.project_name}


@app.get("/v1/explorer-agent/health", dependencies=[Depends(require_token)])
async def explorer_agent_health() -> dict:
    auth = await codex_auth_status()
    if not settings.codex_agent_enabled:
        raise HTTPException(status_code=503, detail="LunarAgent Codex runtime is disabled")
    if not auth["configured"]:
        raise HTTPException(status_code=503, detail="ChatGPT-managed Codex authentication is unavailable")
    if not str(settings.backend_base_url or "").strip() or not str(
        settings.backend_shared_token or ""
    ).strip():
        raise HTTPException(status_code=503, detail="LunarAgent backend bridge is unavailable")
    try:
        execution_policy = execution_policy_status()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="LunarAgent read-only execution policy is unavailable",
        ) from exc
    return {
        "status": "ok",
        "service": settings.project_name,
        "model": settings.codex_agent_model,
        "reasoningEffort": settings.codex_agent_reasoning_effort,
        "auth": auth,
        "billingMode": "chatgpt-plan",
        # Preserve the response field for backend compatibility while the mode
        # explicitly proves that host commands and file writes are unavailable.
        "commandSandbox": execution_policy,
    }


@app.post("/v1/explorer-agent/respond", response_model=ExplorerAgentRespondResponse, dependencies=[Depends(require_token)])
async def explorer_agent_respond(request: ExplorerAgentRespondRequest) -> ExplorerAgentRespondResponse:
    session_id = str(request.sessionId or "").strip()
    request_id = str(request.requestId or "").strip()

    async def execute() -> ExplorerAgentRespondResponse:
        await enforce_request_quota(request.quotaKey, "explorer")
        async with _codex_request_semaphore:
            return await run_explorer_codex_turn(request)

    try:
        if session_id and request_id:
            return await _explorer_turn_registry.run_or_join(
                session_id,
                request_id,
                _explorer_turn_fingerprint(request),
                execute,
            )
        return await execute()
    except TurnCancelledError as exc:
        raise HTTPException(status_code=409, detail="Explorer agent turn was cancelled.") from exc
    except TurnIdentityConflictError as exc:
        raise HTTPException(status_code=409, detail="Explorer agent turn identity conflict.") from exc
    except TurnAlreadyActiveError as exc:
        raise HTTPException(status_code=409, detail="Explorer agent turn is already active.") from exc
    except asyncio.CancelledError as exc:
        raise HTTPException(status_code=409, detail="Explorer agent turn was cancelled.") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise_internal_server_error(exc, "Explorer agent response failed.")


@app.post(
    "/v1/explorer-agent/cancel",
    response_model=ExplorerAgentCancelResponse,
    dependencies=[Depends(require_token)],
)
async def explorer_agent_cancel(
    request: ExplorerAgentCancelRequest,
) -> ExplorerAgentCancelResponse:
    result = await _explorer_turn_registry.cancel(request.sessionId, request.requestId)
    return ExplorerAgentCancelResponse(
        sessionId=request.sessionId,
        requestId=request.requestId,
        cancelled=result.active,
    )


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
