"""
Authentication & Access Control Module for the Quant Platform.
Protects the web dashboard and REST/WebSocket APIs with secure session tokens.
"""

import hmac
import hashlib
import time
import base64
from typing import Optional, Dict, Any
from fastapi import Request, HTTPException, WebSocket, status, Depends
from pydantic import BaseModel, Field

from app.config import settings


class LoginRequest(BaseModel):
    username: str = Field(..., description="Administrator username")
    password: str = Field(..., description="Administrator password")


class LoginResponse(BaseModel):
    success: bool
    token: Optional[str] = None
    username: Optional[str] = None
    expires_in_seconds: int = 86400 * 7
    error: Optional[str] = None


def create_access_token(username: str, expires_in_seconds: int = 86400 * 7) -> str:
    """Generate a cryptographically signed, tamper-proof session token."""
    expiry = int(time.time() + expires_in_seconds)
    payload = f"{username}:{expiry}"
    sig = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def verify_token(token: Optional[str]) -> Optional[str]:
    """Verify cryptographic signature and expiration of session token. Returns username if valid."""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        parts = raw.split(":")
        if len(parts) != 3:
            return None
        username, expiry_str, sig = parts
        expiry = int(expiry_str)

        if time.time() > expiry:
            return None  # Token expired

        payload = f"{username}:{expiry_str}"
        expected_sig = hmac.new(
            settings.SECRET_KEY.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        if hmac.compare_digest(sig, expected_sig):
            return username
        return None
    except Exception:
        return None


def get_token_from_request(request: Request) -> Optional[str]:
    """Extract auth token from Authorization header, X-Auth-Token header, or cookie."""
    # 1. Check Authorization Bearer header
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    # 2. Check custom X-Auth-Token header
    x_token = request.headers.get("X-Auth-Token")
    if x_token:
        return x_token.strip()

    # 3. Check Cookie
    cookie_token = request.cookies.get("auth_token")
    if cookie_token:
        return cookie_token.strip()

    # 4. Check Query Parameter (optional)
    return request.query_params.get("token")


def require_auth(request: Request) -> Dict[str, Any]:
    """FastAPI route dependency enforcing authentication."""
    if not settings.AUTH_ENABLED:
        return {"username": settings.ADMIN_USERNAME, "auth_disabled": True}

    token = get_token_from_request(request)
    username = verify_token(token)

    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please sign in.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return {"username": username, "authenticated": True}


async def verify_websocket_auth(websocket: WebSocket) -> bool:
    """Verify WebSocket upgrade request has a valid token."""
    if not settings.AUTH_ENABLED:
        return True

    # Check query param ?token=...
    token = websocket.query_params.get("token")
    if not token:
        # Check cookie
        token = websocket.cookies.get("auth_token")

    if not token or not verify_token(token):
        await websocket.close(code=1008, reason="Unauthorized: Valid authentication token required")
        return False

    return True

