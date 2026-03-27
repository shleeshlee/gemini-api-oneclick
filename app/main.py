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
from typing import Any, Dict, List, Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import AsyncClient
from pydantic import BaseModel

from parsers import (
    build_chat_completion_payload,
    build_chat_reply_text,
    iter_chat_stream_chunks,
    parse_image_generation_result,
    parse_video_generation_result,
)
from worker_events import (
    build_gemini_response_snapshot,
    build_worker_event,
    build_worker_event_headers,
    persist_worker_event,
)
from raw_capture_tracer import RawCaptureTracer
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
runtime_models_cache: list[dict[str, Any]] = []
runtime_models_cache_time = 0.0

RUNTIME_MODELS_CACHE_TTL = 300
RUNTIME_MODELS_EXCLUDE = {
    "gemini-advanced",
    "gemini-apps-while-signed-out",
}
IMAGE_DOWNLOAD_SIZE = os.environ.get("IMAGE_DOWNLOAD_SIZE", "1024").strip() or "1024"

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
                await gemini_client.init(timeout=300, watchdog_timeout=180, auto_refresh=False)
                logger.info("Gemini client initialized successfully.")
            except Exception as e:
                logger.error(f"Failed to initialize Gemini client: {e}")
                gemini_client = None
                raise HTTPException(status_code=503, detail=f"Failed to initialize Gemini client: {str(e)}")

        return gemini_client


async def reset_client():
    """Reset client for error recovery."""
    global gemini_client, runtime_models_cache, runtime_models_cache_time
    async with client_lock:
        gemini_client = None
        runtime_models_cache = []
        runtime_models_cache_time = 0.0
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


def build_model_payload(model_ids: list[str], owned_by: str = "google-gemini-web") -> list[dict[str, Any]]:
    """Build OpenAI-style model list payload entries from model ids."""
    now = int(datetime.now(tz=timezone.utc).timestamp())
    seen = set()
    data = []
    for model_id in model_ids:
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        data.append(
            {
                "id": model_id,
                "object": "model",
                "created": now,
                "owned_by": owned_by,
            }
        )
    return data


def get_enum_models() -> list[dict[str, Any]]:
    """Return the vendored fallback model list."""
    return build_model_payload([m.model_name for m in Model if m.model_name != "unspecified"])


async def get_runtime_models() -> list[dict[str, Any]]:
    """Extract currently exposed Gemini model ids from the live Gemini web app."""
    global runtime_models_cache, runtime_models_cache_time

    now = time.time()
    if runtime_models_cache and (now - runtime_models_cache_time) < RUNTIME_MODELS_CACHE_TTL:
        return runtime_models_cache

    try:
        client = await get_or_create_client()
        if not getattr(client, "client", None):
            raise RuntimeError("Gemini client session is unavailable")

        resp = await client.client.get("https://gemini.google.com/app")
        resp.raise_for_status()

        raw_names = re.findall(r"gemini-[a-z0-9.-]+", resp.text.lower())
        model_ids = []
        seen = set()
        for name in raw_names:
            if name in RUNTIME_MODELS_EXCLUDE or name in seen:
                continue
            seen.add(name)
            model_ids.append(name)

        if model_ids:
            runtime_models_cache = build_model_payload(model_ids)
            runtime_models_cache_time = now
            logger.info("Discovered runtime Gemini models: %s", model_ids)
            return runtime_models_cache
    except Exception as e:
        logger.warning("Falling back to vendored model enum for /v1/models: %s", e)

    runtime_models_cache = get_enum_models()
    runtime_models_cache_time = now
    return runtime_models_cache


def build_custom_model(name: str, base_model: Model) -> dict[str, Any]:
    """Build a custom model dict using a current runtime name with a known-good header."""
    return {
        "model_name": name,
        "model_header": dict(base_model.model_header),
    }


def infer_model_alias(openai_model_name: str) -> Optional[dict[str, Any]]:
    """Map newer runtime model names onto the closest supported vendored header."""
    name_lower = openai_model_name.lower()

    if "thinking" in name_lower:
        return build_custom_model(openai_model_name, Model.G_3_0_FLASH_THINKING)
    if "flash" in name_lower:
        return build_custom_model(openai_model_name, Model.G_3_0_FLASH)
    if "pro" in name_lower:
        return build_custom_model(openai_model_name, Model.G_3_0_PRO)
    return None


def extract_header_token(model_header: dict[str, str]) -> str:
    """Extract the active Google model token from the vendored header payload."""
    header_value = model_header.get("x-goog-ext-525001261-jspb", "")
    match = re.search(r'"([0-9a-f]{16})"', header_value)
    return match.group(1) if match else ""


def classify_model_family(model_name: str) -> str:
    """Collapse model names into the currently supported header families."""
    model_name = model_name.lower()
    if "thinking" in model_name:
        return "thinking"
    if "flash" in model_name:
        return "flash"
    if "pro" in model_name:
        return "pro"
    return "unknown"


def describe_model(model: Model | dict[str, Any]) -> dict[str, str]:
    """Normalize the resolved model into trace-friendly metadata."""
    if isinstance(model, dict):
        model_name = model.get("model_name", "unspecified")
        model_header = model.get("model_header", {})
    else:
        model_name = getattr(model, "model_name", str(model))
        model_header = getattr(model, "model_header", {})
    return {
        "resolved_model": model_name,
        "header_family": classify_model_family(model_name),
        "header_token": extract_header_token(model_header),
    }


def resolve_model_selection(openai_model_name: str) -> tuple[Model | dict[str, Any], dict[str, str]]:
    """Resolve an incoming model name and keep trace metadata for the gateway."""
    name_lower = openai_model_name.lower()

    for m in Model:
        model_name = m.model_name if hasattr(m, "model_name") else str(m)
        if name_lower == model_name.lower():
            trace = {"requested_model": openai_model_name, "resolution": "exact"}
            trace.update(describe_model(m))
            return m, trace

    for m in Model:
        model_name = m.model_name if hasattr(m, "model_name") else str(m)
        if name_lower in model_name.lower():
            trace = {"requested_model": openai_model_name, "resolution": "substring"}
            trace.update(describe_model(m))
            return m, trace

    alias_model = infer_model_alias(openai_model_name)
    if alias_model:
        logger.info("Mapped runtime model alias '%s' to vendored header family", openai_model_name)
        trace = {"requested_model": openai_model_name, "resolution": "alias"}
        trace.update(describe_model(alias_model))
        return alias_model, trace

    logger.warning("Unknown model '%s', using default", openai_model_name)
    for m in Model:
        model_name = m.model_name if hasattr(m, "model_name") else str(m)
        if model_name != "unspecified":
            trace = {"requested_model": openai_model_name, "resolution": "default"}
            trace.update(describe_model(m))
            return m, trace

    fallback = next(iter(Model))
    trace = {"requested_model": openai_model_name, "resolution": "default"}
    trace.update(describe_model(fallback))
    return fallback, trace


def build_model_trace_headers(trace: dict[str, str], endpoint: str) -> dict[str, str]:
    """Expose the model resolution chain to the gateway and admin UI."""
    return {
        "X-OneClick-Requested-Model": trace.get("requested_model", ""),
        "X-OneClick-Resolved-Model": trace.get("resolved_model", ""),
        "X-OneClick-Header-Family": trace.get("header_family", ""),
        "X-OneClick-Header-Token": trace.get("header_token", ""),
        "X-OneClick-Model-Resolution": trace.get("resolution", ""),
        "X-OneClick-Endpoint": endpoint,
    }


def log_worker_event(event, payload: dict[str, Any] | None = None):
    """Persist and emit a structured worker event for later gateway/parser refactors."""
    persisted = persist_worker_event(event, payload=payload)
    logger.info("Worker event: %s", persisted.model_dump_json())
    return persisted


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
    """Return model list from live Gemini page data, falling back to vendored constants."""
    return {"object": "list", "data": await get_runtime_models()}


def map_model_name(openai_model_name: str) -> Model | dict[str, Any]:
    """Map OpenAI model name to a vendored enum or a custom runtime-compatible model dict."""
    model, _ = resolve_model_selection(openai_model_name)
    return model


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
                    if image_url.startswith("data:"):
                        try:
                            # Detect suffix from MIME type
                            mime = image_url.split(";")[0].split(":")[1] if ":" in image_url else ""
                            suffix = ".mp4" if "video" in mime else ".png"
                            base64_data = image_url.split(",")[1]
                            image_data = base64.b64decode(base64_data)
                            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                                tmp.write(image_data)
                                temp_files.append(tmp.name)
                        except Exception as e:
                            logger.error(f"Error processing base64 media: {str(e)}")
            conversation += "\n\n"

    conversation += "Assistant: "
    return conversation, temp_files


async def download_image_as_base64(image, cookies=None) -> str | None:
    """Download an image and return as raw base64 string (no data: prefix).

    For GeneratedImage: append an explicit size suffix and use its cookies.
    For WebImage: downloads directly.
    """
    try:
        url = image.url
        req_cookies = cookies

        if isinstance(image, GeneratedImage):
            # 1024 keeps the studio responsive while remaining sharper than the preview.
            url = url + f"=s{IMAGE_DOWNLOAD_SIZE}"
            req_cookies = image.cookies

        async with AsyncClient(
            http2=True, follow_redirects=True, cookies=req_cookies, timeout=45.0
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
async def create_chat_completion(
    request: ChatCompletionRequest,
    response: Response,
    api_key: str = Depends(verify_api_key),
):
    """Handle chat completion requests with retry and streaming support."""
    max_retries = 3
    trace_headers: dict[str, str] = {}
    conversation = ""
    client = None
    tracer: RawCaptureTracer | None = None

    for attempt in range(max_retries):
        try:
            client = await get_or_create_client()

            conversation, temp_files = prepare_conversation(request.messages)
            logger.info(f"Prepared conversation: {conversation[:200]}...")
            logger.info(f"Temp files: {temp_files}")

            model, model_trace = resolve_model_selection(request.model)
            trace_headers = build_model_trace_headers(model_trace, "chat")
            logger.info("Using model trace: %s", model_trace)

            tracer = RawCaptureTracer()
            logger.info("Sending request to Gemini...")
            if temp_files:
                gemini_response = await client.generate_content(conversation, files=temp_files, model=model, tracer=tracer)
            else:
                gemini_response = await client.generate_content(conversation, model=model, tracer=tracer)

            for temp_file in temp_files:
                try:
                    os.unlink(temp_file)
                except Exception as e:
                    logger.warning(f"Failed to delete temp file {temp_file}: {str(e)}")

            reply_text = await build_chat_reply_text(
                gemini_response,
                image_downloader=download_image_as_base64,
                markdown_corrector=correct_markdown,
                raw_capture=tracer.get_snapshot() if tracer else None,
            )
            worker_event = build_worker_event(
                "chat",
                trace_headers,
                gemini_response,
                chat_id=getattr(gemini_response, "cid", "") or "",
            )
            worker_event = log_worker_event(
                worker_event,
                payload={
                    "request": {
                        "model": request.model,
                        "message_count": len(request.messages),
                        "conversation": conversation,
                        "has_temp_files": bool(temp_files),
                    },
                    "response": build_gemini_response_snapshot(gemini_response),
                    "raw_capture": tracer.get_snapshot() if tracer else None,
                },
            )
            trace_headers.update(build_worker_event_headers(worker_event))

            logger.info(f"Response: {reply_text[:200]}...")

            completion_id = f"chatcmpl-{uuid.uuid4()}"
            created_time = int(time.time())

            if request.stream:
                return StreamingResponse(
                    iter_chat_stream_chunks(
                        completion_id=completion_id,
                        created_time=created_time,
                        model=request.model,
                        reply_text=reply_text,
                    ),
                    media_type="text/event-stream",
                    headers=trace_headers,
                )
            else:
                result = build_chat_completion_payload(
                    completion_id=completion_id,
                    created_time=created_time,
                    model=request.model,
                    reply_text=reply_text,
                    conversation=conversation,
                )

                logger.info("Returning response successfully")
                response.headers.update(trace_headers)
                return result

        except HTTPException:
            raise
        except Exception as e:
            error_msg = str(e).lower()
            logger.error(f"Error generating completion (attempt {attempt + 1}/{max_retries}): {str(e)}", exc_info=True)
            error_event = build_worker_event(
                "chat",
                trace_headers,
                None,
                raw_response_preview=str(e),
                error_summary=str(e),
            )
            log_worker_event(
                error_event,
                payload={
                    "request": {
                        "model": request.model,
                        "message_count": len(request.messages),
                        "conversation": conversation,
                        "attempt": attempt + 1,
                    },
                    "error": str(e),
                    "raw_capture": tracer.get_snapshot() if tracer else None,
                },
            )

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
    image: Optional[str] = None  # base64 encoded media for edit mode (first round)
    media_type: Optional[str] = "image"  # "image" or "video"
    session_id: Optional[str] = None  # continue editing in same session


# Edit session store: session_id -> ChatSession
_edit_sessions: dict[str, Any] = {}
_SESSION_TTL = 600  # 10 minutes
_MAX_SESSIONS = 50  # prevent unbounded memory growth


def _cleanup_expired_sessions():
    """Remove sessions older than TTL. If still over limit, evict oldest."""
    now = time.time()
    expired = [sid for sid, (_, ts) in _edit_sessions.items() if now - ts > _SESSION_TTL]
    for sid in expired:
        del _edit_sessions[sid]
    # Hard cap: evict oldest sessions if still over limit
    if len(_edit_sessions) > _MAX_SESSIONS:
        by_age = sorted(_edit_sessions.items(), key=lambda x: x[1][1])
        for sid, _ in by_age[:len(_edit_sessions) - _MAX_SESSIONS]:
            del _edit_sessions[sid]


@app.post("/v1/images/generations")
async def create_image(
    request: ImageGenerationRequest,
    response: Response,
    api_key: str = Depends(verify_api_key),
):
    """DALL-E compatible image generation endpoint using Gemini ImageFX.

    Edit mode:
    - First round: send `image` (base64) + `prompt` → returns `session_id`
    - Subsequent rounds: send `session_id` + `prompt` (no image needed) → continues editing
    """
    temp_files = []
    trace_headers: dict[str, str] = {}
    session_id = request.session_id
    chat = None
    prompt = request.prompt
    client = None
    tracer: RawCaptureTracer | None = None
    try:
        client = await get_or_create_client()
        _cleanup_expired_sessions()
        logger.info(f"Image generation: '{request.prompt[:200]}' has_image={request.image is not None} session={request.session_id}")

        model = None
        if request.model:
            model, model_trace = resolve_model_selection(request.model)
            trace_headers = build_model_trace_headers(model_trace, "image")
        prompt = request.prompt  # prompt building is done by gateway

        tracer = RawCaptureTracer()

        # Continue existing edit session
        if session_id and session_id in _edit_sessions:
            chat, _ = _edit_sessions[session_id]
            _edit_sessions[session_id] = (chat, time.time())  # refresh TTL
            logger.info(f"Continuing edit session {session_id}, cid={chat.cid}")
            gemini_response = await chat.send_message(prompt, tracer=tracer)

        else:
            # New request (text-to-image or first round of edit)
            kwargs = {}
            if model:
                kwargs["model"] = model

            if request.image:
                # Edit mode first round: upload media (image or video)
                try:
                    suffix = ".mp4" if request.media_type == "video" else ".png"
                    image_data = base64.b64decode(request.image)
                    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                        tmp.write(image_data)
                        temp_files.append(tmp.name)
                    kwargs["files"] = temp_files
                    logger.info(f"Edit mode: image decoded, {len(image_data)} bytes -> {temp_files[0]}")
                except Exception as e:
                    logger.error(f"Failed to decode input image: {e}")
                    raise HTTPException(status_code=400, detail=f"Invalid base64 image: {str(e)}")

                # Start a ChatSession so subsequent edits stay in context
                chat = client.start_chat()
                if model:
                    chat.model = model
                gemini_response = await chat.send_message(prompt, files=temp_files, tracer=tracer)
                session_id = str(uuid.uuid4())[:12]
                _edit_sessions[session_id] = (chat, time.time())
                logger.info(f"New edit session {session_id} created, cid={chat.cid}")

            else:
                # Pure text-to-image (no session needed)
                gemini_response = await client.generate_content(prompt, tracer=tracer, **kwargs)

        # Log what we got back
        logger.info(f"Response text: '{gemini_response.text[:200] if gemini_response.text else 'None'}'")
        logger.info(f"Response images: {gemini_response.images}")

        images = gemini_response.images
        if not images and not chat:
            # Retry only for pure text-to-image
            logger.info("No images with first prompt, trying alternate format...")
            tracer = RawCaptureTracer()  # fresh tracer for retry
            retry_kwargs = {}
            if model:
                retry_kwargs["model"] = model
            gemini_response = await client.generate_content(f"Create a picture: {request.prompt}", tracer=tracer, **retry_kwargs)
            images = gemini_response.images

        if not images:
            raise HTTPException(
                status_code=422,
                detail=f"Gemini did not generate images. Response: {gemini_response.text[:300] if gemini_response.text else 'empty'}",
            )

        result_data, raw_image_urls = await parse_image_generation_result(
            images,
            limit=request.n,
            image_downloader=download_image_as_base64,
            raw_capture=tracer.get_snapshot() if tracer else None,
        )

        if not result_data:
            raise HTTPException(status_code=500, detail="Images found but all downloads failed")

        worker_event = build_worker_event(
            "image",
            trace_headers,
            gemini_response,
            chat_id=getattr(chat, "cid", "") if chat else "",
            session_id=session_id or "",
        )
        worker_event = log_worker_event(
            worker_event,
            payload={
                "request": {
                    "model": request.model,
                    "prompt": prompt,
                    "count": request.n,
                    "size": request.size,
                    "quality": request.quality,
                    "has_input_media": bool(request.image),
                    "media_type": request.media_type,
                    "session_id": session_id,
                },
                "response": build_gemini_response_snapshot(gemini_response),
                "raw_capture": tracer.get_snapshot() if tracer else None,
            },
        )
        trace_headers.update(build_worker_event_headers(worker_event))

        logger.info(f"Image generation complete: {len(result_data)} image(s), session={session_id}")
        result = {"created": int(time.time()), "data": result_data, "final_prompt": prompt}
        if session_id:
            result["session_id"] = session_id
        if raw_image_urls:
            result["raw_image_urls"] = raw_image_urls[:4]
        response.headers.update(trace_headers)
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Image generation error: {e}", exc_info=True)
        error_msg = str(e).lower()
        error_event = build_worker_event(
            "image",
            trace_headers,
            None,
            chat_id=getattr(chat, "cid", "") if chat else "",
            session_id=session_id or "",
            raw_response_preview=str(e),
            error_summary=str(e),
        )
        log_worker_event(
            error_event,
            payload={
                "request": {
                    "model": request.model,
                    "prompt": prompt,
                    "count": request.n,
                    "size": request.size,
                    "quality": request.quality,
                    "has_input_media": bool(request.image),
                    "media_type": request.media_type,
                    "session_id": session_id,
                },
                "error": str(e),
                "raw_capture": tracer.get_snapshot() if tracer else None,
            },
        )
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
    image: Optional[str] = None  # base64 encoded media for image/video-to-video
    media_type: Optional[str] = "image"  # "image" or "video"


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
async def create_video(
    request: VideoGenerationRequest,
    response: Response,
    api_key: str = Depends(verify_api_key),
):
    """Video generation endpoint using Gemini Veo.

    Returns both download URL and base64 data.
    Library handles video polling automatically (up to 5 minutes).
    """
    temp_files = []
    trace_headers: dict[str, str] = {}
    client = None
    tracer: RawCaptureTracer | None = None
    try:
        client = await get_or_create_client()
        has_image = request.image is not None
        logger.info(f"Video generation: '{request.prompt[:200]}' has_image={has_image}")

        model = None
        if request.model:
            model, model_trace = resolve_model_selection(request.model)
            trace_headers = build_model_trace_headers(model_trace, "video")
        kwargs = {}
        if model:
            kwargs["model"] = model

        if request.image:
            try:
                suffix = ".mp4" if request.media_type == "video" else ".png"
                image_data = base64.b64decode(request.image)
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(image_data)
                    temp_files.append(tmp.name)
                kwargs["files"] = temp_files
                logger.info(f"Media-to-video: {request.media_type} decoded, {len(image_data)} bytes -> {temp_files[0]}")
            except Exception as e:
                logger.error(f"Failed to decode input image: {e}")
                raise HTTPException(status_code=400, detail=f"Invalid base64 image: {str(e)}")

        tracer = RawCaptureTracer()
        gemini_response = await client.generate_content(request.prompt, tracer=tracer, **kwargs)

        logger.info(f"Response text: '{gemini_response.text[:200] if gemini_response.text else 'None'}'")
        logger.info(f"Response videos: {gemini_response.videos}")

        if not gemini_response.videos:
            raise HTTPException(
                status_code=422,
                detail=f"Gemini did not generate video. Response: {gemini_response.text[:300] if gemini_response.text else 'empty'}"
            )

        result_data, raw_video_urls = await parse_video_generation_result(
            gemini_response.videos,
            video_downloader=download_video_as_base64,
            raw_capture=tracer.get_snapshot() if tracer else None,
        )
        worker_event = build_worker_event("video", trace_headers, gemini_response)
        worker_event = log_worker_event(
            worker_event,
            payload={
                "request": {
                    "model": request.model,
                    "prompt": request.prompt,
                    "has_input_media": bool(request.image),
                    "media_type": request.media_type,
                },
                "response": build_gemini_response_snapshot(gemini_response),
                "raw_capture": tracer.get_snapshot() if tracer else None,
            },
        )
        trace_headers.update(build_worker_event_headers(worker_event))

        logger.info(f"Video generation complete: {len(result_data)} video(s)")
        result = {
            "created": int(time.time()),
            "data": result_data,
            "text": gemini_response.text or "",
        }
        if raw_video_urls:
            result["raw_video_urls"] = raw_video_urls[:4]
        response.headers.update(trace_headers)
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Video generation error: {e}", exc_info=True)
        error_msg = str(e).lower()
        error_event = build_worker_event(
            "video",
            trace_headers,
            None,
            raw_response_preview=str(e),
            error_summary=str(e),
        )
        log_worker_event(
            error_event,
            payload={
                "request": {
                    "model": request.model,
                    "prompt": request.prompt,
                    "has_input_media": bool(request.image),
                    "media_type": request.media_type,
                },
                "error": str(e),
                "raw_capture": tracer.get_snapshot() if tracer else None,
            },
        )
        if any(kw in error_msg for kw in ['auth', 'cookie', 'expired', '401', '403']):
            await reset_client()
        if any(kw in error_msg for kw in ['rate limit', '429', 'quota', "can't generate more videos"]):
            raise HTTPException(status_code=429, detail=f"Video rate limited: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Video generation failed: {str(e)}")
    finally:
        for tf in temp_files:
            try:
                os.unlink(tf)
            except Exception:
                pass


# Local dev:  PYTHONPATH=./lib:./app python3 -m uvicorn main:app --app-dir app
# Container:  uvicorn main:app  (Dockerfile CMD)
