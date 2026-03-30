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
from gemini_webapi.constants import AccountStatus, Model
from gemini_webapi.exceptions import ImageGenerationBlocked
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

# Container real state — reported to gateway, never self-managed
_container_state: dict[str, Any] = {
    "auth_status": "unknown",      # AVAILABLE / UNAUTHENTICATED / LOCATION_REJECTED / ...
    "last_error": "",              # most recent request error
    "last_error_type": "",         # cookie_expired / image_blocked / rate_limit / tls_error / ...
    "needs_restart": False,        # True = TLS/connection repeatedly broken
    "initializing": True,          # True during startup init
    "_tls_fail_count": 0,          # consecutive TLS failures → triggers needs_restart
}

RUNTIME_MODELS_CACHE_TTL = 300
RUNTIME_MODELS_EXCLUDE = {
    "gemini-advanced",
    "gemini-apps-while-signed-out",
}
IMAGE_DOWNLOAD_SIZE = os.environ.get("IMAGE_DOWNLOAD_SIZE", "1024").strip() or "1024"

# TLS failure threshold before requesting restart
_TLS_RESTART_THRESHOLD = 3

# Authentication credentials
SECURE_1PSID = os.environ.get("SECURE_1PSID", "")
SECURE_1PSIDTS = os.environ.get("SECURE_1PSIDTS", "")
API_KEY = os.environ.get("API_KEY", "")

# Startup debug
logger.info("----------- COOKIE DEBUG -----------")
logger.info(f"SECURE_1PSID: {'SET' if SECURE_1PSID else 'EMPTY'} (len={len(SECURE_1PSID)})")
logger.info(f"SECURE_1PSIDTS: {'SET' if SECURE_1PSIDTS else 'EMPTY'} (len={len(SECURE_1PSIDTS)})")
logger.info("------------------------------------")


def _report_error(e: Exception) -> None:
    """Classify error and update _container_state. Container only reports, never self-manages."""
    from gemini_webapi.exceptions import (
        AuthError, RateLimitExceeded, UsageLimitExceeded,
        ImageGenerationBlocked, TemporarilyBlocked,
    )
    error_str = str(e)
    error_lower = error_str.lower()

    if isinstance(e, ImageGenerationBlocked):
        _container_state["last_error_type"] = "image_blocked"
    elif isinstance(e, (AuthError,)):
        _container_state["last_error_type"] = "cookie_expired"
    elif isinstance(e, UsageLimitExceeded):
        _container_state["last_error_type"] = "usage_limit"
    elif isinstance(e, RateLimitExceeded):
        _container_state["last_error_type"] = "rate_limit"
    elif isinstance(e, TemporarilyBlocked):
        _container_state["last_error_type"] = "temporarily_blocked"
    elif "tls" in error_lower or "ssl" in error_lower or "curl: (35)" in error_lower:
        _container_state["last_error_type"] = "tls_error"
        _container_state["_tls_fail_count"] += 1
        if _container_state["_tls_fail_count"] >= _TLS_RESTART_THRESHOLD:
            _container_state["needs_restart"] = True
    elif any(kw in error_lower for kw in ["can't generate more videos", "video generation isn't available"]):
        _container_state["last_error_type"] = "video_quota"
    elif any(kw in error_lower for kw in ['401', '403', 'cookie', 'expired']):
        _container_state["last_error_type"] = "cookie_expired"
    elif '429' in error_lower or 'rate limit' in error_lower:
        _container_state["last_error_type"] = "rate_limit"
    else:
        _container_state["last_error_type"] = "unknown"

    _container_state["last_error"] = error_str[:200]

    # Reset TLS counter on non-TLS errors (connection recovered)
    if _container_state["last_error_type"] != "tls_error":
        _container_state["_tls_fail_count"] = 0

    logger.info(f"Error reported: type={_container_state['last_error_type']} msg={error_str[:100]}")


async def get_or_create_client():
    """Get or create Gemini client. Reports real auth status to _container_state."""
    global gemini_client

    async with client_lock:
        if gemini_client is None:
            if not SECURE_1PSID or not SECURE_1PSIDTS:
                _container_state["auth_status"] = "no_credentials"
                raise HTTPException(status_code=503, detail="Gemini credentials not configured")

            try:
                _container_state["initializing"] = True
                proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or None
                logger.info(f"Initializing Gemini client... proxy={proxy}")
                gemini_client = GeminiClient(SECURE_1PSID, SECURE_1PSIDTS, proxy=proxy)
                await gemini_client.init(timeout=300, watchdog_timeout=180, auto_refresh=False)

                # Read real auth status from Google
                status = getattr(gemini_client, "account_status", None)
                if status:
                    _container_state["auth_status"] = status.name
                else:
                    _container_state["auth_status"] = "unknown"

                if status and status != AccountStatus.AVAILABLE:
                    logger.warning(f"Auth status: {status.name} — client initialized but account not fully available")

                _container_state["initializing"] = False
                _container_state["needs_restart"] = False
                _container_state["_tls_fail_count"] = 0
                _container_state["last_error"] = ""
                _container_state["last_error_type"] = ""
                logger.info("Gemini client initialized successfully.")
            except Exception as e:
                logger.error(f"Failed to initialize Gemini client: {e}")
                gemini_client = None
                _container_state["initializing"] = False
                error_str = str(e).lower()
                if "tls" in error_str or "ssl" in error_str or "curl: (35)" in error_str:
                    _container_state["_tls_fail_count"] += 1
                    _container_state["last_error_type"] = "tls_error"
                    if _container_state["_tls_fail_count"] >= 3:
                        _container_state["needs_restart"] = True
                else:
                    _container_state["last_error_type"] = "init_failed"
                _container_state["last_error"] = str(e)[:200]
                raise HTTPException(status_code=503, detail=f"Failed to initialize Gemini client: {str(e)}")

        return gemini_client


@asynccontextmanager
async def lifespan(app):
    """Warm up client on startup with random delay to avoid simultaneous logins."""
    if SECURE_1PSID and SECURE_1PSIDTS:
        delay = random.randint(5, 60)
        logger.info(f"Waiting {delay}s before initializing (staggered startup)...")
        await asyncio.sleep(delay)
        for attempt in range(1, 4):
            try:
                await get_or_create_client()
                logger.info("Gemini client is warmed up and ready.")
                break
            except Exception as e:
                logger.error(f"Init attempt {attempt}/3 failed: {e}")
                if attempt < 3:
                    wait = 15 * attempt
                    logger.info(f"Retrying in {wait}s...")
                    await asyncio.sleep(wait)
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


def _build_model_name(m) -> str:
    """Build a model name from registry data. Extracts version from description."""
    desc = getattr(m, "description", "") or ""
    display = getattr(m, "display_name", "") or ""
    # Try to extract version+family from description, e.g. "3.1 Pro" or "3 Flash"
    ver_match = re.search(r"(\d+(?:\.\d+)?)\s+(Pro|Flash|Thinking)", desc, re.IGNORECASE)
    if ver_match:
        ver, family = ver_match.group(1), ver_match.group(2).lower()
        return f"gemini-{ver}-{family}"
    # Fall back: display_name is "Fast"/"Thinking"/"Pro"
    # Models without version in desc self-report as generation 3 (e.g. "Gemini 3 Flash")
    display_lower = display.lower()
    family_map = {
        "fast": "flash", "thinking": "flash-thinking", "pro": "pro",
        "快速": "flash", "快捷": "flash", "思考": "flash-thinking", "思考型": "flash-thinking",
    }
    family = family_map.get(display_lower)
    if not family:
        return getattr(m, "model_id", "") or ""
    if family:
        return f"gemini-3-{family}"
    return getattr(m, "model_id", "") or ""


async def get_runtime_models() -> list[dict[str, Any]]:
    """Return models from the live model registry, falling back to vendored enum."""
    global runtime_models_cache, runtime_models_cache_time

    now = time.time()
    if runtime_models_cache and (now - runtime_models_cache_time) < RUNTIME_MODELS_CACHE_TTL:
        return runtime_models_cache

    try:
        client = await get_or_create_client()
        registry = getattr(client, "_model_registry", None)
        if registry:
            model_ids = []
            for m in registry.values():
                name = _build_model_name(m)
                if name:
                    model_ids.append(name)
            if model_ids:
                runtime_models_cache = build_model_payload(model_ids)
                runtime_models_cache_time = now
                logger.info("Models from registry: %s", model_ids)
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
        return build_custom_model(openai_model_name, Model.G_3_1_PRO)
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


def resolve_model_for_chat(openai_model_name: str) -> tuple[Model | dict[str, Any], dict[str, str]]:
    """Resolve model for chat — uses live registry (account's real tier) first."""
    name_lower = openai_model_name.lower()

    # Step 1: Try live model registry (has real tokens for this account's tier)
    if gemini_client:
        registry = getattr(gemini_client, "_model_registry", None)
        if registry:
            for m in registry.values():
                if name_lower in (m.display_name.lower(), m.model_name.lower()):
                    trace = {"requested_model": openai_model_name, "resolution": "registry-exact"}
                    trace.update(describe_model(m))
                    return m, trace
            # Keyword match: all family keywords in the request must appear in the model
            req_keywords = {kw for kw in ("flash", "pro", "thinking") if kw in name_lower}
            if req_keywords:
                for m in registry.values():
                    model_names = f"{m.display_name} {m.model_name}".lower()
                    model_keywords = {kw for kw in ("flash", "pro", "thinking") if kw in model_names}
                    if req_keywords == model_keywords:
                        trace = {"requested_model": openai_model_name, "resolution": "registry-keyword"}
                        trace.update(describe_model(m))
                        return m, trace

    # Step 2: Fall back to vendored enum
    return _resolve_from_vendored(openai_model_name)


def resolve_model_for_media(openai_model_name: str) -> tuple[Model | dict[str, Any], dict[str, str]]:
    """Resolve model for image/video generation — always uses BASIC_FLASH (capacity 1).

    Why: Google's media generation (Nano Banana 2 / Veo) runs on the Flash Image
    channel regardless of account tier. Pro/Plus tokens route to search instead of
    ImageFX. The tier-based access control (free 20/day, pro 100/day, video pro-only)
    is enforced server-side by account, not by token.
    """
    trace = {"requested_model": openai_model_name, "resolution": "media-basic"}
    trace.update(describe_model(Model.BASIC_FLASH))
    return Model.BASIC_FLASH, trace


def _resolve_from_vendored(openai_model_name: str) -> tuple[Model | dict[str, Any], dict[str, str]]:
    """Shared fallback: resolve from vendored Model enum."""
    name_lower = openai_model_name.lower()

    for m in Model:
        model_name = m.model_name if hasattr(m, "model_name") else str(m)
        if name_lower == model_name.lower():
            trace = {"requested_model": openai_model_name, "resolution": "exact"}
            trace.update(describe_model(m))
            return m, trace

    alias_model = infer_model_alias(openai_model_name)
    if alias_model:
        trace = {"requested_model": openai_model_name, "resolution": "alias"}
        trace.update(describe_model(alias_model))
        return alias_model, trace

    logger.warning("Unknown model '%s', using BASIC_FLASH default", openai_model_name)
    trace = {"requested_model": openai_model_name, "resolution": "default"}
    trace.update(describe_model(Model.BASIC_FLASH))
    return Model.BASIC_FLASH, trace


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


def _detect_tier() -> dict:
    """Derive account tier from the live model registry."""
    if not gemini_client:
        return {"capacity": 0, "label": "unknown"}
    registry = getattr(gemini_client, "_model_registry", None)
    if not registry:
        return {"capacity": 0, "label": "unknown"}
    cap = max((m.capacity for m in registry.values()), default=0)
    label = {4: "plus", 3: "ultra", 2: "pro", 1: "free"}.get(cap, "unknown")
    status = getattr(gemini_client, "account_status", None)
    result = {"capacity": cap, "label": label, "models": len(registry)}
    if status and status != AccountStatus.AVAILABLE:
        result["account_status"] = status.name
    return result


@app.get("/health")
async def health_check():
    """Health check — reports real container state, no self-management."""
    auth = _container_state["auth_status"]
    client_ready = gemini_client is not None

    if _container_state["initializing"]:
        status = "initializing"
    elif not client_ready:
        status = "no_client"
    elif client_ready:
        # Client initialized = functional. auth_status is informational,
        # not a health gate. Free accounts often report UNAUTHENTICATED
        # but work fine for chat and image generation.
        status = "healthy"
    else:
        status = "degraded"

    return {
        "status": status,
        "client_ready": client_ready,
        "auth_status": auth,
        "tier": _detect_tier(),
        "needs_restart": _container_state["needs_restart"],
        "last_error": _container_state["last_error"],
        "last_error_type": _container_state["last_error_type"],
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }


@app.get("/v1/models")
async def list_models():
    """Return model list from live Gemini page data, falling back to vendored constants."""
    return {"object": "list", "data": await get_runtime_models()}


def map_model_name(openai_model_name: str) -> Model | dict[str, Any]:
    """Map OpenAI model name to a vendored enum or a custom runtime-compatible model dict."""
    model, _ = resolve_model_for_chat(openai_model_name)
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
            http2=True, follow_redirects=True, cookies=req_cookies, timeout=45.0,
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
    """Handle chat completion requests — transparent stream relay, no retries."""
    trace_headers: dict[str, str] = {}

    try:
        client = await get_or_create_client()

        conversation, temp_files = prepare_conversation(request.messages)
        logger.info(f"Prepared conversation: {conversation[:200]}...")
        logger.info(f"Temp files: {temp_files}")

        model, model_trace = resolve_model_for_chat(request.model)
        trace_headers = build_model_trace_headers(model_trace, "chat")
        logger.info("Using model trace: %s", model_trace)

        tracer = RawCaptureTracer()
        logger.info("Sending request to Gemini...")

        completion_id = f"chatcmpl-{uuid.uuid4()}"
        created_time = int(time.time())

        if request.stream:
            # ── True streaming: relay Gemini deltas as they arrive ──
            async def stream_relay():
                full_text = ""
                full_thoughts = ""
                last_output = None
                first = True
                try:
                    stream_gen = client.generate_content_stream(
                        conversation,
                        files=temp_files if temp_files else None,
                        model=model,
                    )
                    async for output in stream_gen:
                        last_output = output
                        text_delta = output.text_delta or ""
                        thoughts_delta = output.thoughts_delta or ""

                        # Build content delta
                        content = ""
                        if thoughts_delta:
                            if not full_thoughts:
                                content += "<think>"
                            content += thoughts_delta
                            full_thoughts += thoughts_delta
                        if text_delta:
                            if full_thoughts and not full_text:
                                content += "</think>"
                            content += text_delta
                            full_text += text_delta

                        if not content:
                            continue

                        if first:
                            role_chunk = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created_time,
                                "model": request.model,
                                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(role_chunk)}\n\n"
                            first = False

                        chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": request.model,
                            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                    # Close thinking tag if still open
                    if full_thoughts and not full_text:
                        close_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": request.model,
                            "choices": [{"index": 0, "delta": {"content": "</think>"}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(close_chunk)}\n\n"

                    # Download and embed images from the last output
                    if last_output and hasattr(last_output, 'images') and last_output.images:
                        for idx, img in enumerate(last_output.images):
                            b64 = await download_image_as_base64(img)
                            if b64:
                                img_chunk = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": request.model,
                                    "choices": [{"index": 0, "delta": {"content": f"\n\n![generated_image_{idx}](data:image/png;base64,{b64})"}, "finish_reason": None}],
                                }
                                yield f"data: {json.dumps(img_chunk)}\n\n"

                except Exception as e:
                    logger.error(f"Stream error: {e}", exc_info=True)
                    _report_error(e)
                    # Send error as final content chunk so caller sees it
                    if first:
                        role_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": request.model,
                            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(role_chunk)}\n\n"
                    err_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": request.model,
                        "choices": [{"index": 0, "delta": {"content": f"\n\n[Error: {str(e)[:200]}]"}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(err_chunk)}\n\n"
                finally:
                    # Clean up temp files
                    for tf in temp_files:
                        try:
                            os.unlink(tf)
                        except Exception:
                            pass

                    # Finish SSE stream
                    done_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": request.model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    yield f"data: {json.dumps(done_chunk)}\n\n"
                    yield "data: [DONE]\n\n"

            return StreamingResponse(stream_relay(), media_type="text/event-stream", headers=trace_headers)

        else:
            # ── Non-streaming: consume stream, return complete JSON ──
            full_text = ""
            full_thoughts = ""
            last_output = None

            if temp_files:
                stream_gen = client.generate_content_stream(conversation, files=temp_files, model=model)
            else:
                stream_gen = client.generate_content_stream(conversation, model=model)

            async for output in stream_gen:
                full_text += output.text_delta or ""
                full_thoughts += output.thoughts_delta or ""
                last_output = output

            for tf in temp_files:
                try:
                    os.unlink(tf)
                except Exception:
                    pass

            reply_text = ""
            if full_thoughts:
                reply_text += f"<think>{full_thoughts}</think>"
            reply_text += full_text

            # Download images if present
            if last_output and hasattr(last_output, 'images') and last_output.images:
                for idx, img in enumerate(last_output.images):
                    b64 = await download_image_as_base64(img)
                    if b64:
                        reply_text += f"\n\n![generated_image_{idx}](data:image/png;base64,{b64})"

            # Normalize escaped markdown chars
            reply_text = reply_text.replace("&lt;", "<").replace("\\<", "<").replace("\\_", "_").replace("\\>", ">")
            reply_text = reply_text.replace("\\#", "#").replace("\\!", "!").replace("\\|", "|")
            reply_text = re.sub(r"```\s*\n(<[a-zA-Z][\s\S]*?)\n```", r"\1", reply_text)
            reply_text = correct_markdown(reply_text)
            if not reply_text.strip():
                reply_text = "Empty response from Gemini."

            logger.info(f"Response: {reply_text[:200]}...")

            result = build_chat_completion_payload(
                completion_id=completion_id,
                created_time=created_time,
                model=request.model,
                reply_text=reply_text,
                conversation=conversation,
            )
            response.headers.update(trace_headers)
            return result

    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e).lower()
        logger.error(f"Error in chat completion: {e}", exc_info=True)

        _report_error(e)
        if any(kw in error_msg for kw in ['429', 'rate limit', 'resource exhausted', 'quota']):
            raise HTTPException(status_code=429, detail=f"Rate limited: {str(e)}")

        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.get("/")
async def root():
    return {"status": "online", "message": "Gemini API OneClick is running"}


class ImageGenerationRequest(BaseModel):
    prompt: str
    model: Optional[str] = "gemini-3-flash"
    n: Optional[int] = 1
    size: Optional[str] = "1024x1024"
    quality: Optional[str] = "standard"
    style: Optional[str] = None
    negative_prompt: Optional[str] = None
    response_format: Optional[str] = "b64_json"
    image: Optional[str] = None  # base64 encoded media for edit mode (first round)
    media_type: Optional[str] = "image"  # "image" or "video"
    session_id: Optional[str] = None  # continue editing in same session
    use_pro: Optional[bool] = False  # True = Nano Banana Pro (paid accounts, "Redo with Pro")


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
        logger.info(f"Image generation: '{request.prompt[:200]}' has_image={request.image is not None} session={request.session_id} use_pro={request.use_pro}")

        model = None
        if request.model:
            model, model_trace = resolve_model_for_media(request.model)
            trace_headers = build_model_trace_headers(model_trace, "image")
        prompt = request.prompt  # prompt building is done by gateway

        tracer = RawCaptureTracer()

        # Continue existing edit session
        if session_id and session_id in _edit_sessions:
            chat, _ = _edit_sessions[session_id]
            _edit_sessions[session_id] = (chat, time.time())  # refresh TTL
            logger.info(f"Continuing edit session {session_id}, cid={chat.cid}")
            gemini_response = await chat.send_message(prompt, tracer=tracer, use_pro=request.use_pro)

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
                gemini_response = await chat.send_message(prompt, files=temp_files, tracer=tracer, use_pro=request.use_pro)
                session_id = str(uuid.uuid4())[:12]
                _edit_sessions[session_id] = (chat, time.time())
                logger.info(f"New edit session {session_id} created, cid={chat.cid}")

            else:
                # Pure text-to-image (no session needed)
                gemini_response = await client.generate_content(prompt, tracer=tracer, use_pro=request.use_pro, **kwargs)

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
            gemini_response = await client.generate_content(f"Create a picture: {request.prompt}", tracer=tracer, use_pro=request.use_pro, **retry_kwargs)
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
        _report_error(e)
        raise HTTPException(status_code=500, detail=f"Image generation failed: {str(e)}")
    finally:
        for tf in temp_files:
            try:
                os.unlink(tf)
            except Exception:
                pass


class VideoGenerationRequest(BaseModel):
    prompt: str
    model: Optional[str] = "gemini-3-flash"
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
            model, model_trace = resolve_model_for_media(request.model)
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
        _report_error(e)
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
