"""
Pydantic data models for the quant chatbot module.
"""

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str = Field(..., description="Role: 'user' or 'model' / 'assistant'")
    content: str = Field(..., description="Message text content")


class ChatbotRequest(BaseModel):
    message: str = Field(..., description="The user prompt or trading command")
    model: Optional[str] = Field(None, description="Model to use: DeepSeek 4.1 Flash or Claude Opus 4.6 Thinking")
    use_search: Optional[bool] = Field(True, description="Enable built-in Google Search grounding")
    history: Optional[List[ChatMessage]] = Field(None, description="Optional multi-turn conversation history")


class GroundingSource(BaseModel):
    title: str = Field("", description="Title of the grounded web resource")
    url: str = Field("", description="URL of the source")


class GroundingInfo(BaseModel):
    search_enabled: bool = Field(False, description="Whether Google Search was used")
    search_queries: List[str] = Field(default_factory=list, description="Market news queries executed by the app")
    sources: List[GroundingSource] = Field(default_factory=list, description="Grounding chunks and source URLs")


class ChatbotResponse(BaseModel):
    reply: str = Field(..., description="Chatbot response text")
    response: Optional[str] = Field(None, description="Compatibility alias for frontend response text")
    order_executed: bool = Field(False, description="Whether an order was executed by this message")
    model_used: str = Field(..., description="Model ID used for generation")
    trade_result: Optional[Dict[str, Any]] = Field(None, description="Trade execution result if a command was detected")
    portfolio: Optional[Dict[str, Any]] = Field(None, description="Current portfolio snapshot in INR")
    grounding: GroundingInfo = Field(default_factory=GroundingInfo, description="Google Search grounding metadata")
    success: bool = Field(True, description="Whether the request completed successfully")
    error: Optional[str] = Field(None, description="Error detail if generation failed or degraded")

    def __init__(self, **data):
        if "reply" in data and not data.get("response"):
            data["response"] = data["reply"]
        if "response" in data and not data.get("reply"):
            data["reply"] = data["response"]
        if data.get("trade_result"):
            data["order_executed"] = True
        super().__init__(**data)

