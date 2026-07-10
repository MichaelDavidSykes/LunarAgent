from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

EXPLORER_AGENT_MESSAGE_MAX_CHARS = 2800
EXPLORER_AGENT_HISTORY_MAX_MESSAGES = 10
EXPLORER_AGENT_CONTEXT_MAX_CHARS = 20000
SAFEROUTE_AOI_MAX_CHARS = 12000
SAFEROUTE_EVIDENCE_MAX_ITEMS = 40
SAFEROUTE_EVIDENCE_MAX_CHARS = 40000


def _json_char_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _bounded_json_value(value: Any, *, max_chars: int, field_name: str) -> Any:
    if _json_char_size(value) > max_chars:
        raise ValueError(f"{field_name} exceeds the maximum allowed JSON size")
    return value


class ChatMessage(BaseModel):
    role: str = Field(..., description="assistant or user")
    content: str = Field(..., min_length=1, max_length=4000)


class ExplorerAgentRespondRequest(BaseModel):
    sessionId: str | None = Field(default=None)
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

    @field_validator("queryContext", "querySummary")
    @classmethod
    def validate_bounded_explorer_payloads(cls, value: dict[str, Any], info) -> dict[str, Any]:
        return _bounded_json_value(
            value,
            max_chars=EXPLORER_AGENT_CONTEXT_MAX_CHARS,
            field_name=info.field_name,
        )


class ExplorerAgentRespondResponse(BaseModel):
    reply: str
    actions: list[dict[str, Any]] = Field(default_factory=list)
    followUps: list[str] = Field(default_factory=list)
    model: str | None = None


class SafeRouteAreaRiskResearchRequest(BaseModel):
    sessionId: str | None = Field(default=None)
    aoi: dict[str, Any] = Field(
        ...,
        description="Sanitized AOI bounds and labels; must not include tenant or route identifiers.",
    )
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=SAFEROUTE_EVIDENCE_MAX_ITEMS)
    maxZones: int = Field(default=8, ge=1, le=20)

    @field_validator("aoi")
    @classmethod
    def validate_bounded_aoi(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _bounded_json_value(value, max_chars=SAFEROUTE_AOI_MAX_CHARS, field_name="aoi")

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
