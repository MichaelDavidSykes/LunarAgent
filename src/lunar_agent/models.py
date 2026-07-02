from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

EXPLORER_AGENT_MESSAGE_MAX_CHARS = 2800
EXPLORER_AGENT_HISTORY_MAX_MESSAGES = 10


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
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    maxZones: int = Field(default=8, ge=1, le=20)


class SafeRouteAreaRiskResearchResponse(BaseModel):
    zones: list[dict[str, Any]] = Field(default_factory=list)
    model: str | None = None
    notes: str | None = None
