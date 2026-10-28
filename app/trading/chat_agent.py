"""
Live Interactive Chat Agent (Legacy Compatibility Module).
Delegates all functionality to app.chatbot package.
"""

from app.chatbot import (
    process_chat_message,
    CHATBOT_SYSTEM_PROMPT as CHAT_SYSTEM_PROMPT,
    chatbot_service,
)

__all__ = ["process_chat_message", "CHAT_SYSTEM_PROMPT", "chatbot_service"]
