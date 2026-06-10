from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str = Field(..., description="assistant or user")
    content: str = Field(..., min_length=1, max_length=4000)


class ExplorerAgentRespondRequest(BaseModel):
    sessionId: Optional[str] = Field(default=None)
    allowUiActions: Optional[bool] = Field(default=False)
    conversationHistory: List[ChatMessage] = Field(default_factory=list)
    queryPreview: str = Field(..., min_length=1, max_length=400)
    queryContext: Dict[str, Any] = Field(default_factory=dict)
    querySummary: Dict[str, Any] = Field(default_factory=dict)
    currentUserMessage: str = Field(..., min_length=1, max_length=1600)


class ExplorerAgentRespondResponse(BaseModel):
    reply: str
    actions: List[Dict[str, Any]] = Field(default_factory=list)
    followUps: List[str] = Field(default_factory=list)
    model: Optional[str] = None


class SafeRouteAreaRiskResearchRequest(BaseModel):
    sessionId: Optional[str] = Field(default=None)
    aoi: Dict[str, Any] = Field(..., description="Sanitized AOI bounds and labels; must not include tenant or route identifiers.")
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    maxZones: int = Field(default=8, ge=1, le=20)


class SafeRouteAreaRiskResearchResponse(BaseModel):
    zones: List[Dict[str, Any]] = Field(default_factory=list)
    model: Optional[str] = None
    notes: Optional[str] = None
