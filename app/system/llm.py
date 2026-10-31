"""Shared text-generation helpers for the two supported model providers.

DeepSeek 4.1 Flash MaaS is called through the Google GenAI client. Claude
Opus 4.6 Thinking is called through the local Antigravity CLI, so it does not
consume a cloud API key. No other model provider is used anywhere in the app.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from typing import Any, Dict, Optional

from google.genai import types

from app.config import settings, create_genai_client

logger = logging.getLogger("app.system.llm")


class LLMUnavailable(RuntimeError):
    pass


def _opus_binary() -> Optional[str]:
    candidates = [
        settings.OPUS_BIN,
        shutil.which("agy"),
        "/usr/local/bin/agy",
        os.path.expanduser("~/bin/agy"),
    ]
    return next((path for path in candidates if path and os.path.exists(path)), None)


async def _generate_deepseek(
    prompt: str,
    system_instruction: Optional[str],
    temperature: float,
    max_output_tokens: int,
    timeout: float,
) -> Dict[str, Any]:
    if not settings.DEEPSEEK_API_KEY or settings.DEEPSEEK_API_KEY.startswith("your_"):
        raise LLMUnavailable("DeepSeek API key is not configured.")

    client = create_genai_client(settings.DEEPSEEK_API_KEY, location="global")
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=settings.DEEPSEEK_MODEL,
            contents=prompt,
            config=config,
        ),
        timeout=timeout,
    )
    text = (response.text or "").strip()
    if not text:
        raise LLMUnavailable("DeepSeek returned an empty response.")
    usage = getattr(response, "usage_metadata", None)
    return {
        "text": text,
        "model": settings.DEEPSEEK_MODEL,
        "prompt_tokens": int(getattr(usage, "prompt_token_count", 0) or 0),
        "completion_tokens": int(getattr(usage, "candidates_token_count", 0) or 0),
    }


async def _generate_opus(
    prompt: str,
    system_instruction: Optional[str],
    timeout: float,
) -> Dict[str, Any]:
    binary = _opus_binary()
    if not binary:
        raise LLMUnavailable("Claude Opus CLI (agy) is not installed.")

    full_prompt = prompt
    if system_instruction:
        full_prompt = f"{system_instruction}\n\n{prompt}"
    proc = await asyncio.create_subprocess_exec(
        binary,
        "-p",
        full_prompt,
        "--model",
        settings.OPUS_MODEL,
        "--dangerously-skip-permissions",
        "--mode",
        "accept-edits",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise LLMUnavailable("Claude Opus CLI timed out.")
    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", errors="replace")[:240]
        raise LLMUnavailable(f"Claude Opus CLI failed: {detail}")
    text = (stdout or b"").decode("utf-8", errors="replace").strip()
    if not text:
        raise LLMUnavailable("Claude Opus CLI returned an empty response.")
    return {
        "text": text,
        "model": settings.OPUS_MODEL,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


async def generate_text(
    prompt: str,
    model: Optional[str] = None,
    system_instruction: Optional[str] = None,
    temperature: float = 0.2,
    max_output_tokens: int = 400,
    timeout: float = 90.0,
) -> Dict[str, Any]:
    """Generate text with DeepSeek or Claude Opus only."""
    selected = (model or settings.DEEPSEEK_MODEL).strip()
    if selected == settings.OPUS_MODEL or "claude" in selected.lower():
        return await _generate_opus(prompt, system_instruction, timeout)
    if selected != settings.DEEPSEEK_MODEL:
        logger.warning("Unsupported model %s requested; using DeepSeek.", selected)
    return await _generate_deepseek(
        prompt, system_instruction, temperature, max_output_tokens, timeout
    )


def account_for_model(model: str) -> str:
    return "opus_flash" if "claude" in (model or "").lower() else "deepseek_flash"


def extract_json_object(text: str) -> Dict[str, Any]:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in model response.")
    return json.loads(match.group(0))
