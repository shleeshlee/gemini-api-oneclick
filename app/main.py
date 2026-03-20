# -------------------- Gemini API OneClick — main.py --------------------
# Features:
# 1. Random startup delay (5-60s) - avoid simultaneous init triggering risk control
# 2. Auto-reconnect - retry on cookie expiry
# 3. Streaming response - SSE with 10-char chunks
# 4. Image support - base64 decode + upload
# 5. Thinking output - <think> tags from response.thoughts
# 6. Markdown correction - strip Google search link wrappers
# 7. Image generation - download generated images, return as base64 in content

import asyncio
import base64
import io
import json
import logging
import os
import random
import re
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import AsyncClient
from pydantic import BaseModel

from gemini_webapi import GeminiClient, set_log_level
from gemini_webapi.constants import Model
from gemini_webapi.types.image import GeneratedImage
from gemini_webapi.types.video import GeneratedVideo

# ⚠️ DO NOT REMOVE — auto_refresh kills cookies permanently.
# gemini_webapi's RotateCookies sends 401 and invalidates all cookies.
# This monkey-patch disables it regardless of init() parameters or library defaults.
# See: https://github.com/shleeshlee/gemini-api-oneclick/issues/XX
async def _noop_auto_refresh(self, *a, **kw): pass
GeminiClient.start_auto_refresh = _noop_auto_refresh

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
set_log_level("INFO")

# Global client and lock
gemini_client = None
client_lock = asyncio.Lock()

# Authentication credentials
SECURE_1PSID = os.environ.get("SECURE_1PSID", "")
SECURE_1PSIDTS = os.environ.get("SECURE_1PSIDTS", "")
API_KEY = os.environ.get("API_KEY", "")

# Startup debug
logger.info("----------- COOKIE DEBUG -----------")
logger.info(f"SECURE_1PSID: {'SET' if SECURE_1PSID else 'EMPTY'} (len={len(SECURE_1PSID)})")
logger.info(f"SECURE_1PSIDTS: {'SET' if SECURE_1PSIDTS else 'EMPTY'} (len={len(SECURE_1PSIDTS)})")
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
                proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or None
                logger.info(f"Initializing Gemini client... proxy={proxy}")
                gemini_client = GeminiClient(SECURE_1PSID, SECURE_1PSIDTS, proxy=proxy)
                await gemini_client.init(timeout=150, watchdog_timeout=60, auto_refresh=False)
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


@asynccontextmanager
async def lifespan(app):
    """Warm up client on startup with random delay to avoid simultaneous logins."""
    if SECURE_1PSID and SECURE_1PSIDTS:
        delay = random.randint(5, 60)
        logger.info(f"Waiting {delay}s before initializing (staggered startup)...")
        await asyncio.sleep(delay)
        try:
            await get_or_create_client()
            logger.info("Gemini client is warmed up and ready.")
        except Exception as e:
            logger.error(f"Failed to initialize Gemini client during startup: {e}")
    else:
        logger.error("Credentials (SECURE_1PSID, SECURE_1PSIDTS) are not set.")
    yield


app = FastAPI(title="Gemini API OneClick", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


async def verify_api_key(authorization: str = Header(None)):
    """Verify API Key."""
    if not API_KEY:
        logger.warning("API key validation skipped - no API_KEY set")
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    try:
        scheme, token = authorization.split(None, 1)
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
        if m.model_name != "unspecified"
    ]
    return {"object": "list", "data": data}


def map_model_name(openai_model_name: str) -> Model:
    """Map OpenAI model name to Gemini Model enum."""
    name_lower = openai_model_name.lower()

    # Exact match first
    for m in Model:
        model_name = m.model_name if hasattr(m, "model_name") else str(m)
        if name_lower == model_name.lower():
            return m

    # Substring match fallback
    for m in Model:
        model_name = m.model_name if hasattr(m, "model_name") else str(m)
        if name_lower in model_name.lower():
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


async def download_image_as_base64(image, cookies=None) -> str | None:
    """Download an image and return as raw base64 string (no data: prefix).

    For GeneratedImage: appends =s2048 for full size, uses its cookies.
    For WebImage: downloads directly.
    """
    try:
        url = image.url
        req_cookies = cookies

        if isinstance(image, GeneratedImage):
            url = url + "=s2048"  # full size instead of 512x512 preview
            req_cookies = image.cookies

        async with AsyncClient(
            http2=True, follow_redirects=True, cookies=req_cookies, timeout=30.0
        ) as http_client:
            resp = await http_client.get(url)
            if resp.status_code == 200:
                return base64.b64encode(resp.content).decode("utf-8")
            else:
                logger.warning(f"Failed to download image: {resp.status_code} {url[:80]}")
                return None
    except Exception as e:
        logger.error(f"Error downloading image: {e}")
        return None


@app.post("/v1/chat/completions")
async def create_chat_completion(request: ChatCompletionRequest, api_key: str = Depends(verify_api_key)):
    """Handle chat completion requests with retry and streaming support."""
    max_retries = 3

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

            # If Gemini returned images (edit/generation), embed as base64 markdown
            if hasattr(response, "images") and response.images:
                logger.info(f"Response contains {len(response.images)} image(s), downloading...")
                for idx, img in enumerate(response.images):
                    b64 = await download_image_as_base64(img)
                    if b64:
                        reply_text += f"\n\n![generated_image_{idx}](data:image/png;base64,{b64})"
                        logger.info(f"Image {idx} embedded, base64 length={len(b64)}")
                    else:
                        logger.warning(f"Failed to download response image {idx}")
            reply_text = reply_text.replace("&lt;", "<").replace("\\<", "<").replace("\\_", "_").replace("\\>", ">")
            reply_text = reply_text.replace("\\#", "#").replace("\\!", "!").replace("\\|", "|")
            # Strip code fences wrapping HTML content (Gemini sometimes wraps HTML in ```)
            reply_text = re.sub(r'```\s*\n(<[a-zA-Z][\s\S]*?)\n```', r'\1', reply_text)
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

            if any(keyword in error_msg for keyword in ['429', 'rate limit', 'resource exhausted', 'quota']):
                raise HTTPException(status_code=429, detail=f"Rate limited: {str(e)}")

            # Stream interrupted / truncated — retryable
            if any(keyword in error_msg for keyword in ['stream', 'interrupted', 'truncated', 'incomplete']):
                logger.warning(f"Stream error, retrying... ({attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue

            raise HTTPException(status_code=500, detail=f"Error generating completion: {str(e)}")

    raise HTTPException(status_code=500, detail="Max retries exceeded")


@app.get("/")
async def root():
    return {"status": "online", "message": "Gemini API OneClick is running"}


class ImageGenerationRequest(BaseModel):
    prompt: str
    model: Optional[str] = "gemini-3.0-flash"
    n: Optional[int] = 1
    size: Optional[str] = "1024x1024"
    quality: Optional[str] = "standard"
    style: Optional[str] = None
    negative_prompt: Optional[str] = None
    response_format: Optional[str] = "b64_json"
    image: Optional[str] = None  # base64 encoded image for edit mode (first round)
    session_id: Optional[str] = None  # continue editing in same session


# Edit session store: session_id -> ChatSession
_edit_sessions: dict[str, Any] = {}
_SESSION_TTL = 600  # 10 minutes


def _cleanup_expired_sessions():
    """Remove sessions older than TTL."""
    now = time.time()
    expired = [sid for sid, (_, ts) in _edit_sessions.items() if now - ts > _SESSION_TTL]
    for sid in expired:
        del _edit_sessions[sid]


@app.post("/v1/images/generations")
async def create_image(request: ImageGenerationRequest, api_key: str = Depends(verify_api_key)):
    """DALL-E compatible image generation endpoint using Gemini ImageFX.

    Edit mode:
    - First round: send `image` (base64) + `prompt` → returns `session_id`
    - Subsequent rounds: send `session_id` + `prompt` (no image needed) → continues editing
    """
    temp_files = []
    try:
        client = await get_or_create_client()
        _cleanup_expired_sessions()
        logger.info(f"Image generation: '{request.prompt[:200]}' has_image={request.image is not None} session={request.session_id}")

        model = map_model_name(request.model) if request.model else None
        prompt = request.prompt  # prompt building is done by gateway

        chat = None
        session_id = request.session_id

        # Continue existing edit session
        if session_id and session_id in _edit_sessions:
            chat, _ = _edit_sessions[session_id]
            _edit_sessions[session_id] = (chat, time.time())  # refresh TTL
            logger.info(f"Continuing edit session {session_id}, cid={chat.cid}")
            response = await chat.send_message(prompt)

        else:
            # New request (text-to-image or first round of edit)
            kwargs = {}
            if model:
                kwargs["model"] = model

            if request.image:
                # Edit mode first round: upload image
                try:
                    image_data = base64.b64decode(request.image)
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                        tmp.write(image_data)
                        temp_files.append(tmp.name)
                    kwargs["files"] = temp_files
                    logger.info(f"Edit mode: image decoded, {len(image_data)} bytes -> {temp_files[0]}")
                except Exception as e:
                    logger.error(f"Failed to decode input image: {e}")
                    raise HTTPException(status_code=400, detail=f"Invalid base64 image: {str(e)}")

                # Start a ChatSession so subsequent edits stay in context
                chat = client.start_chat()
                response = await chat.send_message(prompt, files=temp_files)
                session_id = str(uuid.uuid4())[:12]
                _edit_sessions[session_id] = (chat, time.time())
                logger.info(f"New edit session {session_id} created, cid={chat.cid}")

            else:
                # Pure text-to-image (no session needed)
                response = await client.generate_content(prompt, **kwargs)

        # Log what we got back
        logger.info(f"Response text: '{response.text[:200] if response.text else 'None'}'")
        logger.info(f"Response images: {response.images}")

        images = response.images
        if not images and not chat:
            # Retry only for pure text-to-image
            logger.info("No images with first prompt, trying alternate format...")
            response = await client.generate_content(f"Create a picture: {request.prompt}")
            images = response.images

        if not images:
            raise HTTPException(status_code=422, detail=f"Gemini did not generate images. Response: {response.text[:300] if response.text else 'empty'}")

        result_data = []
        for img in images[:request.n]:
            logger.info(f"Downloading image: {type(img).__name__} url={img.url[:80]}...")
            b64 = await download_image_as_base64(img)
            if b64:
                result_data.append({"b64_json": b64})
                logger.info(f"Image downloaded OK, base64 length={len(b64)}")
            else:
                logger.warning(f"Failed to download image: {img.url[:80]}")

        if not result_data:
            raise HTTPException(status_code=500, detail="Images found but all downloads failed")

        logger.info(f"Image generation complete: {len(result_data)} image(s), session={session_id}")
        result = {"created": int(time.time()), "data": result_data, "final_prompt": prompt}
        if session_id:
            result["session_id"] = session_id
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Image generation error: {e}", exc_info=True)
        error_msg = str(e).lower()
        if any(kw in error_msg for kw in ['auth', 'cookie', 'expired', '401', '403']):
            await reset_client()
        raise HTTPException(status_code=500, detail=f"Image generation failed: {str(e)}")
    finally:
        for tf in temp_files:
            try:
                os.unlink(tf)
            except Exception:
                pass


class VideoGenerationRequest(BaseModel):
    prompt: str
    model: Optional[str] = "gemini-2.0-flash"


async def download_video_as_base64(video: GeneratedVideo) -> str | None:
    """Download a video and return as raw base64 string."""
    try:
        url = video.url
        # Build cookies for download
        req_cookies = {}
        if video.cookies:
            if isinstance(video.cookies, dict):
                req_cookies = video.cookies
            elif hasattr(video.cookies, "jar"):
                req_cookies = {c.name: c.value for c in video.cookies.jar}

        async with AsyncClient(
            http2=True, follow_redirects=True, cookies=req_cookies, timeout=60.0
        ) as http_client:
            # Add authuser for multi-account support
            if "usercontent.google.com" in url and "authuser" not in url:
                url += f"&authuser={video.account_index}" if "?" in url else f"?authuser={video.account_index}"
            resp = await http_client.get(url)
            if resp.status_code == 200:
                return base64.b64encode(resp.content).decode("utf-8")
            else:
                logger.warning(f"Failed to download video: {resp.status_code} {url[:80]}")
                return None
    except Exception as e:
        logger.error(f"Error downloading video: {e}")
        return None


@app.post("/v1/videos/generations")
async def create_video(request: VideoGenerationRequest, api_key: str = Depends(verify_api_key)):
    """Video generation endpoint using Gemini Veo.

    Returns both download URL and base64 data.
    Library handles video polling automatically (up to 5 minutes).
    """
    try:
        client = await get_or_create_client()
        logger.info(f"Video generation: '{request.prompt[:200]}'")

        model = map_model_name(request.model) if request.model else None
        kwargs = {}
        if model:
            kwargs["model"] = model

        response = await client.generate_content(request.prompt, **kwargs)

        logger.info(f"Response text: '{response.text[:200] if response.text else 'None'}'")
        logger.info(f"Response videos: {response.videos}")

        if not response.videos:
            raise HTTPException(
                status_code=422,
                detail=f"Gemini did not generate video. Response: {response.text[:300] if response.text else 'empty'}"
            )

        result_data = []
        for idx, video in enumerate(response.videos):
            logger.info(f"Downloading video {idx}: {video.url[:80]}...")
            entry = {
                "url": video.url,
                "thumbnail_url": video.thumbnail_url or "",
            }
            b64 = await download_video_as_base64(video)
            if b64:
                entry["b64_json"] = b64
                logger.info(f"Video {idx} downloaded OK, base64 length={len(b64)}")
            else:
                logger.warning(f"Video {idx} base64 download failed, URL still available")
            result_data.append(entry)

        logger.info(f"Video generation complete: {len(result_data)} video(s)")
        return {
            "created": int(time.time()),
            "data": result_data,
            "text": response.text or "",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Video generation error: {e}", exc_info=True)
        error_msg = str(e).lower()
        if any(kw in error_msg for kw in ['auth', 'cookie', 'expired', '401', '403']):
            await reset_client()
        if any(kw in error_msg for kw in ['rate limit', '429', 'quota', "can't generate more videos"]):
            raise HTTPException(status_code=429, detail=f"Video rate limited: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Video generation failed: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")
