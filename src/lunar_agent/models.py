from __future__ import annotations

import json
import math
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

EXPLORER_AGENT_MESSAGE_MAX_CHARS = 2800
EXPLORER_AGENT_HISTORY_MAX_MESSAGES = 10
EXPLORER_AGENT_CONTEXT_MAX_CHARS = 20000
SAFEROUTE_AOI_MAX_CHARS = 12000
SAFEROUTE_EVIDENCE_MAX_ITEMS = 40
SAFEROUTE_EVIDENCE_MAX_CHARS = 40000
HOME_AGENT_HISTORY_MAX_MESSAGES = 20
HOME_AGENT_CONTEXT_MAX_CHARS = 40000


def _bounded_json_value(value: Any, *, max_chars: int, field_name: str) -> Any:
    if len(json.dumps(value, ensure_ascii=False, default=str)) > max_chars:
        raise ValueError(f"{field_name} exceeds the maximum allowed JSON size")
    return value


class ChatMessage(BaseModel):
    role: Literal["assistant", "user"] = Field(..., description="assistant or user")
    content: str = Field(..., min_length=1, max_length=4000)


class ExplorerAgentRespondRequest(BaseModel):
    sessionId: str | None = Field(default=None, max_length=120)
    quotaKey: str | None = Field(default=None, max_length=120)
    requestId: str | None = Field(default=None, max_length=120)
    allowUiActions: bool | None = Field(default=False)
    conversationHistory: list[ChatMessage] = Field(
        default_factory=list, max_length=EXPLORER_AGENT_HISTORY_MAX_MESSAGES
    )
    queryPreview: str = Field(..., min_length=1, max_length=400)
    queryContext: dict[str, Any] = Field(default_factory=dict)
    querySummary: dict[str, Any] = Field(default_factory=dict)
    currentUserMessage: str = Field(
        ..., min_length=1, max_length=EXPLORER_AGENT_MESSAGE_MAX_CHARS
    )

    @field_validator("queryContext")
    @classmethod
    def validate_bounded_explorer_context(cls, value: dict[str, Any]) -> dict[str, Any]:
        bounded = _bounded_json_value(
            value,
            max_chars=EXPLORER_AGENT_CONTEXT_MAX_CHARS,
            field_name="queryContext",
        )
        return _strip_private_context_fields(bounded)

    @field_validator("querySummary")
    @classmethod
    def validate_bounded_explorer_summary(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _bounded_json_value(
            value,
            max_chars=EXPLORER_AGENT_CONTEXT_MAX_CHARS,
            field_name="querySummary",
        )


class ExplorerAgentRespondResponse(BaseModel):
    reply: str
    actions: list[dict[str, Any]] = Field(default_factory=list)
    followUps: list[str] = Field(default_factory=list)
    model: str | None = None


class SafeRouteAreaRiskResearchRequest(BaseModel):
    sessionId: str | None = Field(default=None, max_length=120)
    aoi: dict[str, Any] = Field(
        ...,
        description="Sanitized AOI bounds and labels; must not include tenant or route identifiers.",
    )
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=SAFEROUTE_EVIDENCE_MAX_ITEMS)
    maxZones: int = Field(default=8, ge=1, le=20)

    @field_validator("aoi")
    @classmethod
    def validate_bounded_aoi(cls, value: dict[str, Any]) -> dict[str, Any]:
        bounded = _bounded_json_value(value, max_chars=SAFEROUTE_AOI_MAX_CHARS, field_name="aoi")
        return _sanitize_public_aoi(bounded)

    @field_validator("evidence")
    @classmethod
    def validate_bounded_evidence(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _bounded_json_value(
            value,
            max_chars=SAFEROUTE_EVIDENCE_MAX_CHARS,
            field_name="evidence",
        )


class SafeRouteAreaRiskResearchResponse(BaseModel):
    zones: list[dict[str, Any]] = Field(default_factory=list)
    model: str | None = None
    notes: str | None = None


class HomeAgentSelectedEntity(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(..., min_length=1, max_length=240)
    type: str = Field(..., min_length=1, max_length=80)
    label: str = Field(..., min_length=1, max_length=240)
    graph_ref: str | None = Field(
        default=None,
        validation_alias=AliasChoices("graph_ref", "graphRef"),
        max_length=500,
    )


class HomeAgentHistoryMessage(BaseModel):
    role: Literal["assistant", "user"]
    content: str = Field(..., min_length=1, max_length=12000)


class HomeAgentRespondRequest(BaseModel):
    threadId: str = Field(..., min_length=1, max_length=120)
    turnId: str = Field(..., min_length=1, max_length=120)
    codexThreadId: str | None = Field(default=None, max_length=180)
    clientId: str = Field(..., min_length=1, max_length=160)
    currentUserMessage: str = Field(..., min_length=1, max_length=6000)
    selectedEntities: list[HomeAgentSelectedEntity] = Field(default_factory=list, max_length=24)
    conversationHistory: list[HomeAgentHistoryMessage] = Field(
        default_factory=list,
        max_length=HOME_AGENT_HISTORY_MAX_MESSAGES,
    )

    @field_validator("currentUserMessage")
    @classmethod
    def validate_home_message(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if len(normalized.split()) > 900:
            raise ValueError("Home Agent messages cannot exceed 900 words")
        return normalized

    @field_validator("selectedEntities", "conversationHistory")
    @classmethod
    def validate_home_context_size(cls, value):
        return _bounded_json_value(
            value,
            max_chars=HOME_AGENT_CONTEXT_MAX_CHARS,
            field_name="Home Agent context",
        )


class HomeAgentRespondResponse(BaseModel):
    final_response: str = Field(..., max_length=60000)
    codex_thread_id: str | None = Field(default=None, max_length=180)
    model: str | None = Field(default=None, max_length=120)
    entities: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    citations: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


def _finite_number(value: Any, minimum: float, maximum: float) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and minimum <= parsed <= maximum else None


def _clean_public_text(value: Any, max_chars: int) -> str:
    return str(value or "").strip()[:max_chars]


def _sanitize_public_aoi(value: dict[str, Any]) -> dict[str, Any]:
    """Allow only public geographic/search context; discard tenant and route metadata."""
    bounds = value.get("bounds") if isinstance(value.get("bounds"), dict) else {}
    min_lat = _finite_number(bounds.get("minLat"), -90, 90)
    max_lat = _finite_number(bounds.get("maxLat"), -90, 90)
    min_lon = _finite_number(bounds.get("minLon"), -180, 180)
    max_lon = _finite_number(bounds.get("maxLon"), -180, 180)
    if None in {min_lat, max_lat, min_lon, max_lon} or min_lat >= max_lat or min_lon >= max_lon:
        raise ValueError("aoi.bounds must contain valid min/max latitude and longitude")

    sanitized: dict[str, Any] = {
        "bounds": {"minLat": min_lat, "minLon": min_lon, "maxLat": max_lat, "maxLon": max_lon},
    }
    center = value.get("center") if isinstance(value.get("center"), dict) else {}
    center_lat = _finite_number(center.get("lat"), -90, 90)
    center_lon = _finite_number(center.get("lon") if center.get("lon") is not None else center.get("lng"), -180, 180)
    if center_lat is not None and center_lon is not None:
        sanitized["center"] = {"lat": center_lat, "lon": center_lon}

    for key, limit in (("scope", 80), ("label", 160), ("country", 120)):
        clean = _clean_public_text(value.get(key), limit)
        if clean:
            sanitized[key] = clean

    country_hints = value.get("countryHints")
    if isinstance(country_hints, list):
        cleaned_hints = [_clean_public_text(item, 80) for item in country_hints[:8]]
        sanitized["countryHints"] = [item for item in cleaned_hints if item]

    label_context = value.get("labelContext") if isinstance(value.get("labelContext"), dict) else {}
    allowed_context: dict[str, Any] = {}
    for key, limit in (
        ("place", 160), ("country", 120), ("display", 240), ("source", 80),
        ("savedQueryName", 240), ("queryPreview", 1200), ("contextSummary", 1200),
    ):
        clean = _clean_public_text(label_context.get(key), limit)
        if clean:
            allowed_context[key] = clean
    terms = label_context.get("terms")
    if isinstance(terms, list):
        allowed_context["terms"] = [
            clean for clean in (_clean_public_text(item, 120) for item in terms[:20]) if clean
        ]
    if allowed_context:
        sanitized["labelContext"] = allowed_context

    hint = value.get("hint") if isinstance(value.get("hint"), dict) else {}
    allowed_hint: dict[str, Any] = {}
    for key in ("name", "code", "country", "label"):
        clean = _clean_public_text(hint.get(key), 160)
        if clean:
            allowed_hint[key] = clean
    if allowed_hint:
        sanitized["hint"] = allowed_hint
    return sanitized


_PRIVATE_CONTEXT_KEY_FRAGMENTS = {
    "clientid", "clientname", "tenant", "workspaceid", "userid", "username", "email", "token",
    "secret", "password", "sessionid",
}


def _strip_private_context_fields(value: Any, depth: int = 0) -> Any:
    if depth >= 5:
        return None
    if isinstance(value, list):
        return [
            cleaned for item in value[:40]
            if (cleaned := _strip_private_context_fields(item, depth + 1)) is not None
        ]
    if not isinstance(value, dict):
        return value
    cleaned: dict[str, Any] = {}
    for raw_key, item in list(value.items())[:60]:
        key = str(raw_key or "").strip()
        normalized = key.casefold().replace("-", "_")
        compact = normalized.replace("_", "")
        if any(fragment in compact for fragment in _PRIVATE_CONTEXT_KEY_FRAGMENTS):
            continue
        next_value = _strip_private_context_fields(item, depth + 1)
        if next_value is not None:
            cleaned[key] = next_value
    return cleaned
