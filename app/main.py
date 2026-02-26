# -------------------- Gemini API OneClick — main.py --------------------
# Features:
# 1. Random startup delay (5-60s) - avoid simultaneous init triggering risk control
# 2. Auto-reconnect - retry on cookie expiry
# 3. Streaming response - SSE with 10-char chunks
# 4. Image support - base64 decode + upload
# 5. Thinking output - <think> tags from response.thoughts
# 6. Markdown correction - strip Google search link wrappers

import asyncio
import json
from datetime import datetime, timezone
import os
import base64
import re
import tempfile
import random

from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any, Union
import time
import uuid
import logging

from gemini_webapi import GeminiClient, set_log_level
from gemini_webapi.constants import Model

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
set_log_level("INFO")

app = FastAPI(title="Gemini API OneClick")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global client and lock
gemini_client = None
client_lock = asyncio.Lock()

# Authentication credentials
SECURE_1PSID = os.environ.get("SECURE_1PSID", "")
SECURE_1PSIDTS = os.environ.get("SECURE_1PSIDTS", "")
API_KEY = os.environ.get("API_KEY", "")

# Startup debug
logger.info("----------- COOKIE DEBUG -----------")
logger.info(f"SECURE_1PSID: '{SECURE_1PSID[:20] if SECURE_1PSID else 'EMPTY'}...' (len={len(SECURE_1PSID)})")
logger.info(f"SECURE_1PSIDTS: '{SECURE_1PSIDTS[:20] if SECURE_1PSIDTS else 'EMPTY'}...' (len={len(SECURE_1PSIDTS)})")
logger.info("------------------------------------")


async def get_or_create_client():
    """Get or create Gemini client with auto-reconnect."""
    global gemini_client

    async with client_lock:
        if gemini_client is None:
            if not SECURE_1PSID or not SECURE_1PSIDTS:
                logger.error("Cannot initialize: credentials not set.")
                raise HTTPException(status_code=503, detail="Gemini credentials not configured")

            try:
                logger.info("Initializing Gemini client...")
                gemini_client = GeminiClient(SECURE_1PSID, SECURE_1PSIDTS)
                await gemini_client.init(timeout=300)
                logger.info("Gemini client initialized successfully.")
            except Exception as e:
                logger.error(f"Failed to initialize Gemini client: {e}")
                gemini_client = None
                raise HTTPException(status_code=503, detail=f"Failed to initialize Gemini client: {str(e)}")

        return gemini_client


async def reset_client():
    """Reset client for error recovery."""
    global gemini_client
    async with client_lock:
        gemini_client = None
        logger.warning("Gemini client has been reset.")


@app.on_event("startup")
async def startup_event():
    """Warm up client on startup with random delay to avoid simultaneous logins."""
    global gemini_client

    if not SECURE_1PSID or not SECURE_1PSIDTS:
        logger.error("Cannot initialize: credentials (SECURE_1PSID, SECURE_1PSIDTS) are not set.")
        return

    # Random delay: each container waits 5-60s to simulate human login
    delay = random.randint(5, 60)
    logger.info(f"Waiting {delay}s before initializing (staggered startup)...")
    await asyncio.sleep(delay)

    try:
        await get_or_create_client()
        logger.info("Gemini client is warmed up and ready.")
    except Exception as e:
        logger.error(f"Failed to initialize Gemini client during startup: {e}")


if not SECURE_1PSID or not SECURE_1PSIDTS:
    logger.warning("Gemini API credentials are not set or empty!")
else:
    logger.info(f"Credentials found. SECURE_1PSID starts with: {SECURE_1PSID[:5]}...")
    logger.info(f"Credentials found. SECURE_1PSIDTS starts with: {SECURE_1PSIDTS[:5]}...")

if not API_KEY:
    logger.warning("API_KEY is not set or empty! API authentication will not work.")
else:
    logger.info(f"API_KEY found. Starts with: {API_KEY[:5]}...")


def correct_markdown(md_text: str) -> str:
    """Fix markdown: remove Google search link wrappers."""
    def simplify_link_target(text_content: str) -> str:
        match_colon_num = re.match(r"([^:]+:\d+)", text_content)
        if match_colon_num:
            return match_colon_num.group(1)
        return text_content

    def replacer(match: re.Match) -> str:
        outer_open_paren = match.group(1)
        display_text = match.group(2)
        new_target_url = simplify_link_target(display_text)
        new_link_segment = f"[`{display_text}`]({new_target_url})"
        if outer_open_paren:
            return f"{outer_open_paren}{new_link_segment})"
        else:
            return new_link_segment

    pattern = r"(\()?\[`([^`]+?)`\]\((https://www.google.com/search\?q=)(.*?)(?<!\\)\)\)*(\))?"
    fixed_google_links = re.sub(pattern, replacer, md_text)
    pattern = r"`(\[[^\]]+\]\([^\)]+\))`"
    return re.sub(pattern, r'\1', fixed_google_links)


# Pydantic models
class ContentItem(BaseModel):
    type: str
    text: Optional[str] = None
    image_url: Optional[Dict[str, str]] = None


class Message(BaseModel):
    role: str
    content: Union[str, List[ContentItem]]
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    stream: Optional[bool] = False
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = 0
    frequency_penalty: Optional[float] = 0
    user: Optional[str] = None


class Choice(BaseModel):
    index: int
    message: Message
    finish_reason: str


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[Choice]
    usage: Usage


class ModelData(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "google"


class ModelList(BaseModel):
    object: str = "list"
    data: List[ModelData]


async def verify_api_key(authorization: str = Header(None)):
    """Verify API Key."""
    if not API_KEY:
        logger.warning("API key validation skipped - no API_KEY set")
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authentication scheme. Use Bearer token")
        if token != API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid authorization format. Use 'Bearer YOUR_API_KEY'")
    return token


@app.middleware("http")
async def error_handling(request: Request, call_next):
    """Global error handling middleware."""
    try:
        return await call_next(request)
    except Exception as e:
        logger.error(f"Request failed: {str(e)}")
        return JSONResponse(status_code=500, content={"error": {"message": str(e), "type": "internal_server_error"}})


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy" if gemini_client else "degraded",
        "client_ready": gemini_client is not None,
        "timestamp": datetime.now(tz=timezone.utc).isoformat()
    }


@app.get("/v1/models")
async def list_models():
    """Return model list from gemini_webapi constants."""
    now = int(datetime.now(tz=timezone.utc).timestamp())
    data = [
        {
            "id": m.model_name,
            "object": "model",
            "created": now,
            "owned_by": "google-gemini-web",
        }
        for m in Model
    ]
    return {"object": "list", "data": data}


def map_model_name(openai_model_name: str) -> Model:
    """Map OpenAI model name to Gemini Model enum."""
    all_models = [m.model_name if hasattr(m, "model_name") else str(m) for m in Model]
    logger.info(f"Available models: {all_models}")

    for m in Model:
        model_name = m.model_name if hasattr(m, "model_name") else str(m)
        if openai_model_name.lower() in model_name.lower():
            return m

    return next(iter(Model))


def prepare_conversation(messages: List[Message]) -> tuple:
    """Convert OpenAI format messages to conversation string + temp image files."""
    conversation = ""
    temp_files = []

    for msg in messages:
        if isinstance(msg.content, str):
            if msg.role == "system":
                conversation += f"System: {msg.content}\n\n"
            elif msg.role == "user":
                conversation += f"Human: {msg.content}\n\n"
            elif msg.role == "assistant":
                conversation += f"Assistant: {msg.content}\n\n"
        else:
            if msg.role == "user":
                conversation += "Human: "
            elif msg.role == "system":
                conversation += "System: "
            elif msg.role == "assistant":
                conversation += "Assistant: "

            for item in msg.content:
                if item.type == "text":
                    conversation += item.text or ""
                elif item.type == "image_url" and item.image_url:
                    image_url = item.image_url.get("url", "")
                    if image_url.startswith("data:image/"):
                        try:
                            base64_data = image_url.split(",")[1]
                            image_data = base64.b64decode(base64_data)
                            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                                tmp.write(image_data)
                                temp_files.append(tmp.name)
                        except Exception as e:
                            logger.error(f"Error processing base64 image: {str(e)}")
            conversation += "\n\n"

    conversation += "Assistant: "
    return conversation, temp_files


@app.post("/v1/chat/completions")
async def create_chat_completion(request: ChatCompletionRequest, api_key: str = Depends(verify_api_key)):
    """Handle chat completion requests with retry and streaming support."""
    max_retries = 2

    for attempt in range(max_retries):
        try:
            client = await get_or_create_client()

            conversation, temp_files = prepare_conversation(request.messages)
            logger.info(f"Prepared conversation: {conversation[:200]}...")
            logger.info(f"Temp files: {temp_files}")

            model = map_model_name(request.model)
            logger.info(f"Using model: {model}")

            logger.info("Sending request to Gemini...")
            if temp_files:
                response = await client.generate_content(conversation, files=temp_files, model=model)
            else:
                response = await client.generate_content(conversation, model=model)

            for temp_file in temp_files:
                try:
                    os.unlink(temp_file)
                except Exception as e:
                    logger.warning(f"Failed to delete temp file {temp_file}: {str(e)}")

            reply_text = ""
            if hasattr(response, "thoughts"):
                reply_text += f"<think>{response.thoughts}</think>"
            if hasattr(response, "text"):
                reply_text += response.text
            else:
                reply_text += str(response)
            reply_text = reply_text.replace("&lt;", "<").replace("\\<", "<").replace("\\_", "_").replace("\\>", ">")
            reply_text = correct_markdown(reply_text)

            logger.info(f"Response: {reply_text[:200]}...")

            if not reply_text or reply_text.strip() == "":
                logger.warning("Empty response received from Gemini")
                reply_text = "Empty response from Gemini. Please check if your cookie is still valid."

            completion_id = f"chatcmpl-{uuid.uuid4()}"
            created_time = int(time.time())

            if request.stream:
                async def generate_stream():
                    data = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": request.model,
                        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(data)}\n\n"

                    chunk_size = 10
                    for i in range(0, len(reply_text), chunk_size):
                        chunk = reply_text[i:i + chunk_size]
                        data = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": request.model,
                            "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(data)}\n\n"
                        await asyncio.sleep(0.02)

                    data = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": request.model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    yield f"data: {json.dumps(data)}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(generate_stream(), media_type="text/event-stream")
            else:
                result = {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created_time,
                    "model": request.model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": reply_text}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": len(conversation.split()),
                        "completion_tokens": len(reply_text.split()),
                        "total_tokens": len(conversation.split()) + len(reply_text.split()),
                    },
                }

                logger.info("Returning response successfully")
                return result

        except HTTPException:
            raise
        except Exception as e:
            error_msg = str(e).lower()
            logger.error(f"Error generating completion (attempt {attempt + 1}/{max_retries}): {str(e)}", exc_info=True)

            if any(keyword in error_msg for keyword in ['auth', 'cookie', 'expired', 'invalid', '401', '403']):
                logger.warning("Detected possible authentication error, resetting client...")
                await reset_client()
                if attempt < max_retries - 1:
                    continue

            raise HTTPException(status_code=500, detail=f"Error generating completion: {str(e)}")

    raise HTTPException(status_code=500, detail="Max retries exceeded")


@app.get("/")
async def root():
    return {"status": "online", "message": "Gemini API OneClick is running"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")
