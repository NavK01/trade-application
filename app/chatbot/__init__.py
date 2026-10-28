"""
Gemini Chatbot Package.
Provides conversational AI grounded with real-time Google Search and live market/portfolio execution.
"""

from app.chatbot.chatbot import ChatbotService, chatbot_service, process_chat_message
from app.chatbot.models import (
    ChatbotRequest,
    ChatbotResponse,
    ChatMessage,
    GroundingInfo,
    GroundingSource,
)
from app.chatbot.prompts import CHATBOT_SYSTEM_PROMPT

__all__ = [
    "ChatbotService",
    "chatbot_service",
    "process_chat_message",
    "ChatbotRequest",
    "ChatbotResponse",
    "ChatMessage",
    "GroundingInfo",
    "GroundingSource",
    "CHATBOT_SYSTEM_PROMPT",
]

