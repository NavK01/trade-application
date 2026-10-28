import asyncio
import base64
import json
import logging
from typing import Optional
from fastapi import WebSocket, WebSocketDisconnect
from google.genai import types

from app.config import settings, create_genai_client
from app.agent.prompt import LIVE_VOICE_SYSTEM_PROMPT
from app.agent.tools import (
    list_files,
    read_file,
    execute_bash,
    get_indian_stock_quote,
    get_indian_market_indices,
    search_indian_market_news,
)

logger = logging.getLogger("gemini_live")

LIVE_TOOLS_MAP = {
    "list_files": list_files,
    "read_file": read_file,
    "execute_bash": execute_bash,
    "get_indian_stock_quote": get_indian_stock_quote,
    "get_indian_market_indices": get_indian_market_indices,
    "search_indian_market_news": search_indian_market_news,
}

LIVE_TOOLS = [
    list_files,
    read_file,
    execute_bash,
    get_indian_stock_quote,
    get_indian_market_indices,
    search_indian_market_news,
]

async def handle_live_websocket(
    websocket: WebSocket,
    model: Optional[str] = None,
    voice: Optional[str] = None
):
    """
    Handle bidirectional Gemini Multimodal Live streaming over WebSockets.
    Connects client microphone to Gemini Live API and streams back Gemini audio responses.
    """
    await websocket.accept()

    api_key = settings.GEMINI_API_KEY
    if not api_key:
        await websocket.send_json({
            "type": "error",
            "message": "GEMINI_API_KEY is not configured in .env. Please set your API key to use Gemini Live voice."
        })
        await websocket.close()
        return

    selected_model = model or settings.GEMINI_LIVE_MODEL
    selected_voice = voice or settings.GEMINI_VOICE_NAME

    client = create_genai_client(api_key)

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=selected_voice
                )
            )
        ),
        system_instruction=types.Content(
            parts=[types.Part.from_text(text=LIVE_VOICE_SYSTEM_PROMPT)]
        ),
        tools=LIVE_TOOLS,
    )

    await websocket.send_json({
        "type": "status",
        "message": f"Connecting to Gemini Live with model {selected_model} and voice '{selected_voice}'..."
    })

    try:
        async with client.aio.live.connect(model=selected_model, config=config) as session:
            await websocket.send_json({
                "type": "connected",
                "message": "Connected to Gemini Multimodal Live API. Start speaking!"
            })

            async def client_to_gemini():
                """Forward incoming audio and text messages from WebSocket to Gemini."""
                try:
                    while True:
                        msg = await websocket.receive()
                        if "bytes" in msg and msg["bytes"]:
                            pcm_data = msg["bytes"]
                            await session.send_realtime_input(
                                media=types.Blob(
                                    data=pcm_data,
                                    mime_type="audio/pcm;rate=16000"
                                )
                            )
                        elif "text" in msg and msg["text"]:
                            payload = json.loads(msg["text"])
                            msg_type = payload.get("type")
                            
                            if msg_type == "audio":
                                # Base64 audio chunk
                                b64_data = payload.get("data", "")
                                pcm_bytes = base64.b64decode(b64_data)
                                await session.send_realtime_input(
                                    media=types.Blob(
                                        data=pcm_bytes,
                                        mime_type="audio/pcm;rate=16000"
                                    )
                                )
                            elif msg_type == "text":
                                user_text = payload.get("text", "")
                                if user_text:
                                    await session.send(input=user_text, end_of_turn=True)
                            elif msg_type == "ping":
                                await websocket.send_json({"type": "pong"})
                except WebSocketDisconnect:
                    logger.info("Client disconnected from Live WebSocket.")
                except Exception as e:
                    logger.error(f"Error in client_to_gemini: {e}")

            async def gemini_to_client():
                """Forward audio and transcriptions from Gemini Live to client WebSocket."""
                try:
                    async for message in session.receive():
                        # Handle server content (audio chunks & text)
                        if message.server_content:
                            server_content = message.server_content
                            if server_content.interrupted:
                                await websocket.send_json({"type": "interrupted"})

                            if server_content.model_turn:
                                for part in server_content.model_turn.parts:
                                    # Spoken audio output from Gemini
                                    if part.inline_data and part.inline_data.data:
                                        audio_b64 = base64.b64encode(part.inline_data.data).decode("utf-8")
                                        await websocket.send_json({
                                            "type": "audio",
                                            "data": audio_b64,
                                            "mime_type": part.inline_data.mime_type or "audio/pcm;rate=24000"
                                        })
                                    # Text / transcript if provided
                                    if part.text:
                                        await websocket.send_json({
                                            "type": "transcript",
                                            "text": part.text
                                        })

                            if server_content.turn_complete:
                                await websocket.send_json({"type": "turn_complete"})

                        # Handle Tool Calls invoked by Gemini during live conversation
                        if message.tool_call:
                            tool_calls = message.tool_call.function_calls or []
                            function_responses = []
                            for fc in tool_calls:
                                name = fc.name
                                call_id = fc.id
                                args = dict(fc.args) if fc.args else {}
                                await websocket.send_json({
                                    "type": "live_tool_call",
                                    "tool": name,
                                    "args": args
                                })

                                fn = LIVE_TOOLS_MAP.get(name)
                                if fn:
                                    try:
                                        if asyncio.iscoroutinefunction(fn):
                                            res = await fn(**args)
                                        else:
                                            res = fn(**args)
                                    except Exception as ex:
                                        res = {"error": str(ex)}
                                else:
                                    res = {"error": f"Tool '{name}' not found."}

                                function_responses.append(
                                    types.FunctionResponse(
                                        name=name,
                                        id=call_id,
                                        response={"result": res}
                                    )
                                )

                            if function_responses:
                                await session.send_tool_response(function_responses=function_responses)

                except Exception as e:
                    logger.error(f"Error in gemini_to_client: {e}")
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Gemini stream ended: {str(e)}"
                    })

            # Run both loops concurrently
            c_task = asyncio.create_task(client_to_gemini())
            g_task = asyncio.create_task(gemini_to_client())

            done, pending = await asyncio.wait(
                [c_task, g_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

    except Exception as e:
        logger.error(f"Failed to connect or maintain Gemini Live session: {e}")
        try:
            await websocket.send_json({
                "type": "error",
                "message": f"Live session failure: {str(e)}"
            })
            await websocket.close()
        except Exception:
            pass
