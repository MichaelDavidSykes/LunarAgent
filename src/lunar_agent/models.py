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
