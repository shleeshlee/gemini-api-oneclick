# -------------------- Gemini API OneClick — worker.py --------------------
# Single-process multi-slot worker.  Replaces N Docker containers with one
# FastAPI application that manages N GeminiClient instances ("slots").
#
# Each slot behaves identically to a single main.py container:
#   GET  /slot/{num}/health
#   GET  /slot/{num}/v1/models
#   POST /slot/{num}/v1/chat/completions
#   POST /slot/{num}/v1/images/generations
#   POST /slot/{num}/v1/videos/generations
#   POST /slot/{num}/v1/music/generations
#   POST /slot/{num}/v1/research
#
# Management:
#   POST /worker/reload-slot/{num}   — re-read env, reinit client
#   GET  /worker/status              — all slots summary

import asyncio
import base64
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
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from httpx import AsyncClient
from pydantic import BaseModel

from parsers import (
    build_chat_completion_payload,
    build_chat_reply_text,
    collect_generated_image_urls,
    collect_generated_video_urls,
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

from slot import Slot, parse_env_file

# ⚠️ DO NOT REMOVE — auto_refresh kills cookies permanently.
async def _noop_auto_refresh(self, *a, **kw): pass
GeminiClient.start_auto_refresh = _noop_auto_refresh

# ── Config ───────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
set_log_level("INFO")

ROOT_DIR = Path(__file__).resolve().parent.parent
ENVS_DIR = Path(os.environ.get("ENVS_DIR", "")) if os.environ.get("ENVS_DIR") else ROOT_DIR / "envs"
PROXY = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or None
API_KEY = os.environ.get("API_KEY", "")
IMAGE_DOWNLOAD_SIZE = os.environ.get("IMAGE_DOWNLOAD_SIZE", "0").strip() or "0"
RUNTIME_MODELS_CACHE_TTL = 300
RUNTIME_MODELS_EXCLUDE = {"gemini-advanced", "gemini-apps-while-signed-out"}

# ── Slot registry ────────────────────────────────────────────────────

slots: dict[int, Slot] = {}

# Shared model cache (safe — model list is the same across accounts)
_models_cache: list[dict[str, Any]] = []
_models_cache_time: float = 0.0

# Per-slot log buffer
from collections import deque
_slot_logs: dict[int, deque] = {}
_SLOT_LOG_MAX = 200

def slot_log(num: int, msg: str):
    """Append a timestamped log entry for a slot."""
    if num not in _slot_logs:
        _slot_logs[num] = deque(maxlen=_SLOT_LOG_MAX)
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    _slot_logs[num].append(f"{ts} {msg}")


# ── Startup ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    """Discover slots from envs/ and init them with staggered delays."""
    for env_file in sorted(ENVS_DIR.glob("account*.env")):
        m = re.match(r"account(\d+)\.env$", env_file.name)
        if m:
            num = int(m.group(1))
            slots[num] = Slot.from_env_file(num, env_file)
    logger.info("Discovered %d slots from %s", len(slots), ENVS_DIR)

    async def init_one(slot: Slot):
        delay = random.randint(5, 60)
        logger.info("Slot %d: waiting %ds before init", slot.num, delay)
        await asyncio.sleep(delay)
        for attempt in range(1, 4):
            try:
                await slot.init_client(proxy=PROXY)
                break
            except Exception as e:
                logger.error("Slot %d: init attempt %d/3 failed: %s", slot.num, attempt, e)
                if attempt < 3:
                    await asyncio.sleep(15 * attempt)

    # Init slots in background so the server starts accepting requests immediately
    asyncio.create_task(_init_all_slots())
    logger.info("Slot init started in background")
    yield


async def _init_all_slots():
    async def init_one(slot: Slot):
        delay = random.randint(5, 60)
        logger.info("Slot %d: waiting %ds before init", slot.num, delay)
        await asyncio.sleep(delay)
        for attempt in range(1, 4):
            try:
                await slot.init_client(proxy=PROXY)
                break
            except Exception as e:
                logger.error("Slot %d: init attempt %d/3 failed: %s", slot.num, attempt, e)
                if attempt < 3:
                    await asyncio.sleep(15 * attempt)

    await asyncio.gather(*(init_one(s) for s in slots.values()))
    logger.info("All slots initialised")


app = FastAPI(title="Gemini API OneClick Worker", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Shared helpers ───────────────────────────────────────────────────

def _get_slot(num: int) -> Slot:
    if num not in slots:
        raise HTTPException(status_code=404, detail=f"Slot {num} not found")
    return slots[num]


async def _get_client(slot: Slot) -> GeminiClient:
    """Ensure the slot has an initialised client, return it."""
    if slot.client is None:
        async with slot.lock:
            if slot.client is None:
                await slot.init_client(proxy=PROXY)
    if slot.client is None:
        raise HTTPException(status_code=503, detail=f"Slot {slot.num}: client not available")
    return slot.client


def _verify_api_key(authorization: str = Header(None)):
    if not API_KEY:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    try:
        scheme, token = authorization.split(None, 1)
        if scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authentication scheme")
        if token != API_KEY:
            raise HTTPException(status_code=401, detail="Invalid API key")
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid authorization format")
    return token


# ── Model resolution (adapted from main.py, accepts client param) ───

def build_model_payload(model_ids: list[str], owned_by: str = "google-gemini-web") -> list[dict[str, Any]]:
    now = int(datetime.now(tz=timezone.utc).timestamp())
    seen: set[str] = set()
    data = []
    for model_id in model_ids:
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        data.append({"id": model_id, "object": "model", "created": now, "owned_by": owned_by})
    return data


def get_enum_models() -> list[dict[str, Any]]:
    return build_model_payload([m.model_name for m in Model if m.model_name != "unspecified"])


def _build_model_name(m) -> str:
    desc = getattr(m, "description", "") or ""
    display = getattr(m, "display_name", "") or ""
    ver_match = re.search(r"(\d+(?:\.\d+)?)\s+(Pro|Flash|Thinking)", desc, re.IGNORECASE)
    if ver_match:
        ver, family = ver_match.group(1), ver_match.group(2).lower()
        return f"gemini-{ver}-{family}"
    display_lower = display.lower()
    family_map = {
        "fast": "flash", "thinking": "flash-thinking", "pro": "pro",
        "快速": "flash", "快捷": "flash", "思考": "flash-thinking", "思考型": "flash-thinking",
    }
    family = family_map.get(display_lower)
    if not family:
        return getattr(m, "model_id", "") or ""
    return f"gemini-3-{family}"


async def get_runtime_models(client: GeminiClient | None) -> list[dict[str, Any]]:
    global _models_cache, _models_cache_time
    now = time.time()
    if _models_cache and (now - _models_cache_time) < RUNTIME_MODELS_CACHE_TTL:
        return _models_cache
    if client:
        try:
            registry = getattr(client, "_model_registry", None)
            if registry:
                model_ids = [_build_model_name(m) for m in registry.values()]
                model_ids = [mid for mid in model_ids if mid and mid not in RUNTIME_MODELS_EXCLUDE]
                if model_ids:
                    _models_cache = build_model_payload(model_ids)
                    _models_cache_time = now
                    return _models_cache
        except Exception as e:
            logger.warning("Falling back to vendored models: %s", e)
    _models_cache = get_enum_models()
    _models_cache_time = now
    return _models_cache


def build_custom_model(name: str, base_model: Model) -> dict[str, Any]:
    return {"model_name": name, "model_header": dict(base_model.model_header)}


def infer_model_alias(openai_model_name: str) -> dict[str, Any] | None:
    name_lower = openai_model_name.lower()
    if "thinking" in name_lower:
        return build_custom_model(openai_model_name, Model.G_3_0_FLASH_THINKING)
    if "flash" in name_lower:
        return build_custom_model(openai_model_name, Model.G_3_0_FLASH)
    if "pro" in name_lower:
        return build_custom_model(openai_model_name, Model.G_3_1_PRO)
    return None


def extract_header_token(model_header: dict[str, str]) -> str:
    header_value = model_header.get("x-goog-ext-525001261-jspb", "")
    match = re.search(r'"([0-9a-f]{16})"', header_value)
    return match.group(1) if match else ""


def classify_model_family(model_name: str) -> str:
    model_name = model_name.lower()
    if "thinking" in model_name:
        return "thinking"
    if "flash" in model_name:
        return "flash"
    if "pro" in model_name:
        return "pro"
    return "unknown"


def describe_model(model: Model | dict[str, Any]) -> dict[str, str]:
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


def resolve_model_for_chat(openai_model_name: str, client: GeminiClient | None = None):
    name_lower = openai_model_name.lower()
    if client:
        registry = getattr(client, "_model_registry", None)
        if registry:
            for m in registry.values():
                if name_lower in (m.display_name.lower(), m.model_name.lower()):
                    trace = {"requested_model": openai_model_name, "resolution": "registry-exact"}
                    trace.update(describe_model(m))
                    return m, trace
            req_keywords = {kw for kw in ("flash", "pro", "thinking") if kw in name_lower}
            if req_keywords:
                for m in registry.values():
                    model_names = f"{m.display_name} {m.model_name}".lower()
                    model_keywords = {kw for kw in ("flash", "pro", "thinking") if kw in model_names}
                    if req_keywords == model_keywords:
                        trace = {"requested_model": openai_model_name, "resolution": "registry-keyword"}
                        trace.update(describe_model(m))
                        return m, trace
    return _resolve_from_vendored(openai_model_name)


def resolve_model_for_media(openai_model_name: str):
    trace = {"requested_model": openai_model_name, "resolution": "media-basic"}
    trace.update(describe_model(Model.BASIC_FLASH))
    return Model.BASIC_FLASH, trace


def _resolve_from_vendored(openai_model_name: str):
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
    return {
        "X-OneClick-Requested-Model": trace.get("requested_model", ""),
        "X-OneClick-Resolved-Model": trace.get("resolved_model", ""),
        "X-OneClick-Header-Family": trace.get("header_family", ""),
        "X-OneClick-Header-Token": trace.get("header_token", ""),
        "X-OneClick-Model-Resolution": trace.get("resolution", ""),
        "X-OneClick-Endpoint": endpoint,
    }


# ── Shared utilities ─────────────────────────────────────────────────

def correct_markdown(md_text: str) -> str:
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


def log_worker_event(event, payload: dict[str, Any] | None = None):
    persisted = persist_worker_event(event, payload=payload)
    logger.info("Worker event: %s", persisted.model_dump_json())
    return persisted


# ── Pydantic models ──────────────────────────────────────────────────

class Message(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]
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


class ImageGenerationRequest(BaseModel):
    prompt: str
    model: Optional[str] = "gemini-3-flash"
    n: Optional[int] = 1
    size: Optional[str] = "1024x1024"
    quality: Optional[str] = "standard"
    style: Optional[str] = None
    negative_prompt: Optional[str] = None
    response_format: Optional[str] = "b64_json"
    image: Optional[str] = None
    media_type: Optional[str] = "image"
    session_id: Optional[str] = None
    use_pro: Optional[bool] = False


class VideoGenerationRequest(BaseModel):
    prompt: str
    model: Optional[str] = "gemini-3-flash"
    image: Optional[str] = None
    media_type: Optional[str] = "image"


class MusicGenerationRequest(BaseModel):
    prompt: str
    model: Optional[str] = "gemini-3-flash"


# ── Conversation prep ────────────────────────────────────────────────

def prepare_conversation(messages: list) -> tuple[str, list[str]]:
    conversation = ""
    temp_files: list[str] = []
    for msg in messages:
        content = msg.content
        role = msg.role
        if isinstance(content, str):
            prefix = {"system": "System", "user": "Human", "assistant": "Assistant"}.get(role, role)
            conversation += f"{prefix}: {content}\n\n"
        else:
            prefix = {"system": "System", "user": "Human", "assistant": "Assistant"}.get(role, role)
            conversation += f"{prefix}: "
            for item in content:
                if isinstance(item, dict):
                    item_type = item.get("type", "")
                    if item_type == "text":
                        conversation += item.get("text", "")
                    elif item_type == "image_url":
                        image_url_data = item.get("image_url", {})
                        image_url = image_url_data.get("url", "") if isinstance(image_url_data, dict) else ""
                        if image_url.startswith("data:"):
                            try:
                                mime = image_url.split(";")[0].split(":")[1] if ":" in image_url else ""
                                if "video" in mime:
                                    suffix = ".mp4"
                                elif "audio" in mime:
                                    subtype = mime.split("/", 1)[1] if "/" in mime else "mp3"
                                    # 常见 OpenAI-style audio/mpeg 实际是 mp3；wav/ogg/flac/m4a 原样；
                                    # 其他少见格式按 subtype 当扩展名（Gemini 网页端按文件嗅探识别内容）
                                    suffix = ".mp3" if subtype in ("mpeg", "mp3") else f".{subtype or 'mp3'}"
                                else:
                                    suffix = ".png"
                                base64_data = image_url.split(",")[1]
                                image_data = base64.b64decode(base64_data)
                                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                                    tmp.write(image_data)
                                    temp_files.append(tmp.name)
                            except Exception as e:
                                logger.error("Error processing base64 media: %s", e)
            conversation += "\n\n"
    conversation += "Assistant: "
    return conversation, temp_files


# ── Image / video download ───────────────────────────────────────────

_DOWNLOAD_RETRIES = 3
_RECOVERABLE_MEDIA_ERRORS = (
    "stream interrupted or truncated",
    "response stalled",
    "no response from gemini within",
    "timed out while waiting for gemini",
)


def _is_recoverable_media_error(exc: Exception) -> bool:
    return any(token in str(exc).lower() for token in _RECOVERABLE_MEDIA_ERRORS)


async def _retry_media_request(label: str, factory, retries: int = 2):
    """Retry a media generation request on transient errors.

    factory(tracer, attempt) -> result.
    Returns (result, tracer). On failure, attaches the last tracer to the exception.
    """
    last_tracer: RawCaptureTracer | None = None
    for attempt in range(1, retries + 1):
        tracer = RawCaptureTracer()
        last_tracer = tracer
        try:
            result = await factory(tracer, attempt)
            return result, tracer
        except Exception as exc:
            if attempt >= retries or not _is_recoverable_media_error(exc):
                exc._last_tracer = last_tracer  # type: ignore[attr-defined]
                raise
            logger.warning("%s transient failure (attempt %d/%d): %s", label, attempt, retries, exc)
            await asyncio.sleep(min(attempt, 2))
    raise RuntimeError(f"{label} failed")


async def download_image_as_base64(image, cookies=None) -> str | None:
    try:
        url = image.url
        req_cookies = cookies
        if isinstance(image, GeneratedImage):
            url = url + f"=s{IMAGE_DOWNLOAD_SIZE}"
            raw_cookies = image.cookies
            if raw_cookies and not isinstance(raw_cookies, dict):
                try:
                    req_cookies = dict(raw_cookies.items())
                except Exception:
                    req_cookies = {k: v for k, v in raw_cookies.items()} if hasattr(raw_cookies, 'items') else raw_cookies
            else:
                req_cookies = raw_cookies
        for attempt in range(1, _DOWNLOAD_RETRIES + 1):
            async with AsyncClient(http2=True, follow_redirects=True, cookies=req_cookies, timeout=90.0) as http_client:
                resp = await http_client.get(url)
                if resp.status_code == 200:
                    return base64.b64encode(resp.content).decode("utf-8")
                logger.warning("Failed to download image (attempt %d/%d): %d %s",
                               attempt, _DOWNLOAD_RETRIES, resp.status_code, url[:80])
                if resp.status_code in {401, 403, 404}:
                    return None
            if attempt < _DOWNLOAD_RETRIES:
                await asyncio.sleep(attempt)
    except Exception as e:
        logger.error("Error downloading image: %s", e)
        return None


async def download_video_as_base64(video: GeneratedVideo) -> str | None:
    try:
        url = video.url
        req_cookies = {}
        if video.cookies:
            if isinstance(video.cookies, dict):
                req_cookies = video.cookies
            elif hasattr(video.cookies, "jar"):
                req_cookies = {c.name: c.value for c in video.cookies.jar}
        if "usercontent.google.com" in url and "authuser" not in url:
            url += f"&authuser={video.account_index}" if "?" in url else f"?authuser={video.account_index}"
        for attempt in range(1, _DOWNLOAD_RETRIES + 1):
            async with AsyncClient(http2=True, follow_redirects=True, cookies=req_cookies, timeout=120.0) as http_client:
                resp = await http_client.get(url)
                if resp.status_code == 200:
                    return base64.b64encode(resp.content).decode("utf-8")
                logger.warning("Failed to download video (attempt %d/%d): %d %s",
                               attempt, _DOWNLOAD_RETRIES, resp.status_code, url[:80])
                if resp.status_code in {401, 403, 404}:
                    return None
            if attempt < _DOWNLOAD_RETRIES:
                await asyncio.sleep(attempt)
    except Exception as e:
        logger.error("Error downloading video: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════════
#  Slot endpoints — /slot/{num}/...
# ═══════════════════════════════════════════════════════════════════════

@app.get("/slot/{num}/health")
async def slot_health(num: int):
    slot = _get_slot(num)
    return slot.health_response()


@app.get("/slot/{num}/v1/models")
async def slot_models(num: int):
    slot = _get_slot(num)
    return {"object": "list", "data": await get_runtime_models(slot.client)}


@app.post("/slot/{num}/v1/chat/completions")
async def slot_chat_completion(
    num: int,
    request: ChatCompletionRequest,
    response: Response,
    api_key: str = Depends(_verify_api_key),
):
    slot = _get_slot(num)
    trace_headers: dict[str, str] = {}
    try:
        client = await _get_client(slot)
        conversation, temp_files = prepare_conversation(request.messages)
        logger.info("Slot %d chat: %s...", num, conversation[:200])
        slot_log(num, f"Chat: {conversation[:80]}")

        model, model_trace = resolve_model_for_chat(request.model, client)
        trace_headers = build_model_trace_headers(model_trace, "chat")

        tracer = RawCaptureTracer()
        completion_id = f"chatcmpl-{uuid.uuid4()}"
        created_time = int(time.time())

        if request.stream:
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
                                "id": completion_id, "object": "chat.completion.chunk",
                                "created": created_time, "model": request.model,
                                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(role_chunk)}\n\n"
                            first = False
                        chunk = {
                            "id": completion_id, "object": "chat.completion.chunk",
                            "created": created_time, "model": request.model,
                            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                    if full_thoughts and not full_text:
                        close_chunk = {
                            "id": completion_id, "object": "chat.completion.chunk",
                            "created": created_time, "model": request.model,
                            "choices": [{"index": 0, "delta": {"content": "</think>"}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(close_chunk)}\n\n"

                    if last_output and hasattr(last_output, 'images') and last_output.images:
                        for idx, img in enumerate(last_output.images):
                            b64 = await download_image_as_base64(img)
                            if b64:
                                img_chunk = {
                                    "id": completion_id, "object": "chat.completion.chunk",
                                    "created": created_time, "model": request.model,
                                    "choices": [{"index": 0, "delta": {"content": f"\n\n![generated_image_{idx}](data:image/png;base64,{b64})"}, "finish_reason": None}],
                                }
                                yield f"data: {json.dumps(img_chunk)}\n\n"

                except Exception as e:
                    logger.error("Slot %d stream error: %s", num, e, exc_info=True)
                    slot.report_error(e)
                    slot_log(num, f"Error: {str(e)[:80]}")
                    if first:
                        role_chunk = {
                            "id": completion_id, "object": "chat.completion.chunk",
                            "created": created_time, "model": request.model,
                            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(role_chunk)}\n\n"
                    err_chunk = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created_time, "model": request.model,
                        "choices": [{"index": 0, "delta": {"content": f"\n\n[Error: {str(e)[:200]}]"}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(err_chunk)}\n\n"
                finally:
                    for tf in temp_files:
                        try:
                            os.unlink(tf)
                        except Exception:
                            pass
                    done_chunk = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created_time, "model": request.model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    yield f"data: {json.dumps(done_chunk)}\n\n"
                    yield "data: [DONE]\n\n"

            return StreamingResponse(stream_relay(), media_type="text/event-stream", headers=trace_headers)

        else:
            full_text = ""
            full_thoughts = ""
            last_output = None
            stream_gen = client.generate_content_stream(
                conversation, files=temp_files if temp_files else None, model=model,
            )
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

            if last_output and hasattr(last_output, 'images') and last_output.images:
                for idx, img in enumerate(last_output.images):
                    b64 = await download_image_as_base64(img)
                    if b64:
                        reply_text += f"\n\n![generated_image_{idx}](data:image/png;base64,{b64})"

            reply_text = reply_text.replace("&lt;", "<").replace("\\<", "<").replace("\\_", "_").replace("\\>", ">")
            reply_text = reply_text.replace("\\#", "#").replace("\\!", "!").replace("\\|", "|")
            reply_text = re.sub(r"```\s*\n(<[a-zA-Z][\s\S]*?)\n```", r"\1", reply_text)
            reply_text = correct_markdown(reply_text)
            if not reply_text.strip():
                reply_text = "Empty response from Gemini."

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
        logger.error("Slot %d chat error: %s", num, e, exc_info=True)
        slot.report_error(e)
        slot_log(num, f"Error: {str(e)[:80]}")
        if any(kw in error_msg for kw in ['429', 'rate limit', 'resource exhausted', 'quota']):
            raise HTTPException(status_code=429, detail=f"Rate limited: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.post("/slot/{num}/v1/images/generations")
async def slot_image_generation(
    num: int,
    request: ImageGenerationRequest,
    response: Response,
    api_key: str = Depends(_verify_api_key),
):
    slot = _get_slot(num)
    temp_files: list[str] = []
    trace_headers: dict[str, str] = {}
    session_id = request.session_id
    chat = None
    prompt = request.prompt
    tracer: RawCaptureTracer | None = None
    try:
        client = await _get_client(slot)
        slot.cleanup_expired_sessions()
        logger.info("Slot %d image: '%s' has_image=%s session=%s use_pro=%s",
                     num, request.prompt[:200], request.image is not None, request.session_id, request.use_pro)
        slot_log(num, f"Image: {request.prompt[:60]}")

        model = None
        if request.model:
            model, model_trace = resolve_model_for_media(request.model)
            trace_headers = build_model_trace_headers(model_trace, "image")

        if session_id and session_id in slot.edit_sessions:
            # Existing session: single attempt only — retrying would double-send
            # the edit instruction into the same conversation, corrupting state.
            # Recovery via raw_capture still works in the except block.
            tracer = RawCaptureTracer()
            chat, _ = slot.edit_sessions[session_id]
            slot.edit_sessions[session_id] = (chat, time.time())
            logger.info("Continuing edit session %s, use_pro=%s", session_id, request.use_pro)
            gemini_response = await chat.send_message(prompt, tracer=tracer, use_pro=request.use_pro)
        else:
            kwargs: dict[str, Any] = {}
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
                except Exception as e:
                    raise HTTPException(status_code=400, detail=f"Invalid base64 image: {str(e)}")

                async def _edit_factory(attempt_tracer, _attempt):
                    new_chat = client.start_chat()
                    if model:
                        new_chat.model = model
                    resp = await new_chat.send_message(prompt, files=temp_files, tracer=attempt_tracer, use_pro=request.use_pro)
                    return resp, new_chat

                (gemini_response, chat), tracer = await _retry_media_request("image edit", _edit_factory)
                session_id = str(uuid.uuid4())[:12]
                slot.edit_sessions[session_id] = (chat, time.time())
            else:
                async def _gen_factory(attempt_tracer, _attempt):
                    new_chat = client.start_chat()
                    if model:
                        new_chat.model = model
                    resp = await new_chat.send_message(prompt, tracer=attempt_tracer, use_pro=request.use_pro)
                    return resp, new_chat

                (gemini_response, chat), tracer = await _retry_media_request("image generation", _gen_factory)
                session_id = str(uuid.uuid4())[:12]
                slot.edit_sessions[session_id] = (chat, time.time())

        images = gemini_response.images
        if not images and not request.image and not request.session_id:
            gemini_response, tracer = await _retry_media_request(
                "image fallback",
                lambda t, _a: chat.send_message(f"Create a picture: {request.prompt}", tracer=t),
            )
            images = gemini_response.images

        if not images:
            raise HTTPException(
                status_code=422,
                detail=f"Gemini did not generate images. Response: {gemini_response.text[:300] if gemini_response.text else 'empty'}",
            )

        result_data, raw_image_urls = await parse_image_generation_result(
            images, limit=request.n, image_downloader=download_image_as_base64,
            raw_capture=tracer.get_snapshot() if tracer else None,
        )

        if not result_data and raw_image_urls:
            result_data = [{"url": url} for url in raw_image_urls[:request.n]]
        elif not result_data:
            raise HTTPException(status_code=500, detail="Images found but all downloads failed")

        worker_event = build_worker_event(
            "image", trace_headers, gemini_response,
            chat_id=getattr(chat, "cid", "") if chat else "", session_id=session_id or "",
        )
        worker_event = log_worker_event(worker_event, payload={
            "request": {
                "model": request.model, "prompt": prompt, "count": request.n,
                "size": request.size, "quality": request.quality,
                "has_input_media": bool(request.image), "media_type": request.media_type,
                "session_id": session_id,
            },
            "response": build_gemini_response_snapshot(gemini_response),
            "raw_capture": tracer.get_snapshot() if tracer else None,
        })
        trace_headers.update(build_worker_event_headers(worker_event))

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
        if tracer is None:
            tracer = getattr(e, "_last_tracer", None)
        raw_capture = tracer.get_snapshot() if tracer else None
        recovered_urls = collect_generated_image_urls(raw_capture)
        if recovered_urls:
            logger.warning("Slot %d recovering %d image URLs from raw capture after: %s", num, len(recovered_urls), e)
            result = {
                "created": int(time.time()),
                "data": [{"url": url} for url in recovered_urls[:request.n]],
                "final_prompt": prompt,
                "raw_image_urls": recovered_urls[:4],
            }
            if session_id:
                result["session_id"] = session_id
            response.headers.update(trace_headers)
            return result
        logger.error("Slot %d image error: %s", num, e, exc_info=True)
        error_event = build_worker_event(
            "image", trace_headers, None,
            chat_id=getattr(chat, "cid", "") if chat else "", session_id=session_id or "",
            raw_response_preview=str(e), error_summary=str(e),
        )
        log_worker_event(error_event, payload={
            "request": {
                "model": request.model, "prompt": prompt, "count": request.n,
                "size": request.size, "quality": request.quality,
                "has_input_media": bool(request.image), "media_type": request.media_type,
                "session_id": session_id,
            },
            "error": str(e),
            "raw_capture": raw_capture,
        })
        if session_id and session_id in slot.edit_sessions and not request.session_id:
            del slot.edit_sessions[session_id]
        slot.report_error(e)
        slot_log(num, f"Image error: {str(e)[:80]}")
        raise HTTPException(status_code=500, detail=f"Image generation failed: {str(e)}")
    finally:
        for tf in temp_files:
            try:
                os.unlink(tf)
            except Exception:
                pass


@app.post("/slot/{num}/v1/videos/generations")
async def slot_video_generation(
    num: int,
    request: VideoGenerationRequest,
    response: Response,
    api_key: str = Depends(_verify_api_key),
):
    slot = _get_slot(num)
    temp_files: list[str] = []
    trace_headers: dict[str, str] = {}
    tracer: RawCaptureTracer | None = None
    try:
        client = await _get_client(slot)
        has_image = request.image is not None
        logger.info("Slot %d video: '%s' has_image=%s", num, request.prompt[:200], has_image)
        slot_log(num, f"Video: {request.prompt[:60]}")

        model = None
        if request.model:
            model, model_trace = resolve_model_for_media(request.model)
            trace_headers = build_model_trace_headers(model_trace, "video")
        kwargs: dict[str, Any] = {}
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
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid base64 image: {str(e)}")

        gemini_response, tracer = await _retry_media_request(
            "video generation",
            lambda t, _a: client.generate_content(request.prompt, tracer=t, **kwargs),
        )

        if not gemini_response.videos:
            raise HTTPException(
                status_code=422,
                detail=f"Gemini did not generate video. Response: {gemini_response.text[:300] if gemini_response.text else 'empty'}"
            )

        result_data, raw_video_urls = await parse_video_generation_result(
            gemini_response.videos, video_downloader=download_video_as_base64,
            raw_capture=tracer.get_snapshot() if tracer else None,
        )
        worker_event = build_worker_event("video", trace_headers, gemini_response)
        worker_event = log_worker_event(worker_event, payload={
            "request": {
                "model": request.model, "prompt": request.prompt,
                "has_input_media": bool(request.image), "media_type": request.media_type,
            },
            "response": build_gemini_response_snapshot(gemini_response),
            "raw_capture": tracer.get_snapshot() if tracer else None,
        })
        trace_headers.update(build_worker_event_headers(worker_event))

        result = {"created": int(time.time()), "data": result_data, "text": gemini_response.text or ""}
        if raw_video_urls:
            result["raw_video_urls"] = raw_video_urls[:4]
        response.headers.update(trace_headers)
        return result

    except HTTPException:
        raise
    except Exception as e:
        if tracer is None:
            tracer = getattr(e, "_last_tracer", None)
        raw_capture = tracer.get_snapshot() if tracer else None
        recovered_urls = collect_generated_video_urls(raw_capture)
        if recovered_urls:
            logger.warning("Slot %d recovering %d video URLs from raw capture after: %s", num, len(recovered_urls), e)
            result = {
                "created": int(time.time()),
                "data": [{"url": url, "thumbnail_url": ""} for url in recovered_urls],
                "text": "",
                "raw_video_urls": recovered_urls[:4],
            }
            response.headers.update(trace_headers)
            return result
        logger.error("Slot %d video error: %s", num, e, exc_info=True)
        error_event = build_worker_event(
            "video", trace_headers, None,
            raw_response_preview=str(e), error_summary=str(e),
        )
        log_worker_event(error_event, payload={
            "request": {
                "model": request.model, "prompt": request.prompt,
                "has_input_media": bool(request.image), "media_type": request.media_type,
            },
            "error": str(e),
            "raw_capture": raw_capture,
        })
        slot.report_error(e)
        if any(kw in str(e).lower() for kw in ['rate limit', '429', 'quota', "can't generate more videos"]):
            raise HTTPException(status_code=429, detail=f"Video rate limited: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Video generation failed: {str(e)}")
    finally:
        for tf in temp_files:
            try:
                os.unlink(tf)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════
#  Music endpoint — /slot/{num}/v1/music/generations
# ═══════════════════════════════════════════════════════════════════════

async def download_audio_as_base64(url: str, cookies: dict | None = None, account_index: int = 0) -> str | None:
    try:
        req_cookies = cookies or {}
        if "usercontent.google.com" in url and "authuser" not in url:
            url += f"&authuser={account_index}" if "?" in url else f"?authuser={account_index}"
        for attempt in range(1, _DOWNLOAD_RETRIES + 1):
            async with AsyncClient(http2=True, follow_redirects=True, cookies=req_cookies, timeout=120.0) as http_client:
                resp = await http_client.get(url)
                if resp.status_code == 200:
                    return base64.b64encode(resp.content).decode("utf-8")
                logger.warning("Failed to download audio (attempt %d/%d): %d %s",
                               attempt, _DOWNLOAD_RETRIES, resp.status_code, url[:80])
                if resp.status_code in {401, 403, 404}:
                    return None
            if attempt < _DOWNLOAD_RETRIES:
                await asyncio.sleep(attempt)
    except Exception as e:
        logger.error("Error downloading audio: %s", e)
        return None


@app.post("/slot/{num}/v1/music/generations")
async def slot_music_generation(
    num: int,
    request: MusicGenerationRequest,
    response: Response,
    api_key: str = Depends(_verify_api_key),
):
    slot = _get_slot(num)
    trace_headers: dict[str, str] = {}
    tracer: RawCaptureTracer | None = None
    try:
        client = await _get_client(slot)
        logger.info("Slot %d music: '%s'", num, request.prompt[:200])
        slot_log(num, f"Music: {request.prompt[:60]}")

        model = None
        if request.model:
            model, model_trace = resolve_model_for_media(request.model)
            trace_headers = build_model_trace_headers(model_trace, "music")

        tracer = RawCaptureTracer()
        # Music generation can take long — extend watchdog to avoid zombie detection
        orig_watchdog = client.watchdog_timeout
        client.watchdog_timeout = 300
        try:
            gemini_response = await client.generate_content(request.prompt, tracer=tracer, model=model)
        finally:
            client.watchdog_timeout = orig_watchdog

        if not gemini_response.media:
            raise HTTPException(
                status_code=422,
                detail=f"Gemini did not generate music. Response: {gemini_response.text[:300] if gemini_response.text else 'empty'}"
            )

        media_item = gemini_response.media[0]
        result_data = []

        # Download MP3
        mp3_b64 = None
        if media_item.mp3_url:
            req_cookies = {}
            if media_item.cookies:
                if isinstance(media_item.cookies, dict):
                    req_cookies = media_item.cookies
                elif hasattr(media_item.cookies, "jar"):
                    req_cookies = {c.name: c.value for c in media_item.cookies.jar}
            mp3_b64 = await download_audio_as_base64(media_item.mp3_url, req_cookies, media_item.account_index)

        # Download MP4 video
        mp4_b64 = None
        if media_item.url:
            mp4_b64 = await download_video_as_base64(media_item)

        entry: dict[str, Any] = {}
        if mp3_b64:
            entry["audio_b64"] = mp3_b64
            entry["audio_url"] = media_item.mp3_url
        if mp4_b64:
            entry["video_b64"] = mp4_b64
            entry["video_url"] = media_item.url
        if media_item.mp3_thumbnail:
            entry["audio_thumbnail"] = media_item.mp3_thumbnail
        if media_item.thumbnail_url:
            entry["video_thumbnail"] = media_item.thumbnail_url
        if not mp3_b64 and not mp4_b64:
            raise HTTPException(status_code=500, detail="Music generated but all downloads failed")
        result_data.append(entry)

        worker_event = build_worker_event("music", trace_headers, gemini_response)
        worker_event = log_worker_event(worker_event, payload={
            "request": {"model": request.model, "prompt": request.prompt},
            "response": build_gemini_response_snapshot(gemini_response),
            "raw_capture": tracer.get_snapshot() if tracer else None,
        })
        trace_headers.update(build_worker_event_headers(worker_event))

        result = {
            "created": int(time.time()),
            "data": result_data,
            "text": gemini_response.text or "",
        }
        raw_urls = []
        if media_item.mp3_url:
            raw_urls.append(media_item.mp3_url)
        if media_item.url:
            raw_urls.append(media_item.url)
        if raw_urls:
            result["raw_urls"] = raw_urls

        response.headers.update(trace_headers)
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Slot %d music error: %s", num, e, exc_info=True)
        error_event = build_worker_event(
            "music", trace_headers, None,
            raw_response_preview=str(e), error_summary=str(e),
        )
        log_worker_event(error_event, payload={
            "request": {"model": request.model, "prompt": request.prompt},
            "error": str(e),
            "raw_capture": tracer.get_snapshot() if tracer else None,
        })
        slot.report_error(e)
        if any(kw in str(e).lower() for kw in ['rate limit', '429', 'quota']):
            raise HTTPException(status_code=429, detail=f"Music rate limited: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Music generation failed: {str(e)}")


# ═══════════════════════════════════════════════════════════════════════
#  Deep Research endpoint — /slot/{num}/v1/research
# ═══════════════════════════════════════════════════════════════════════

class DeepResearchRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    poll_interval: float = 10.0
    timeout: float = 900.0


# In-memory store for active/completed research tasks
_research_tasks: Dict[str, dict] = {}


async def _run_research_background(slot_num: int, client, prompt: str, poll_interval: float, timeout: float, task_id: str):
    """Background coroutine that runs deep research and updates the task store."""
    task = _research_tasks[task_id]
    try:
        def on_status(status):
            entry = {
                "state": status.state,
                "title": status.title,
                "notes": status.notes[:5] if status.notes else [],
                "done": status.done,
                "raw_state": status.raw_state,
            }
            task["statuses"].append(entry)
            task["last_state"] = status.state
            notes_preview = "; ".join(status.notes[:2]) if status.notes else ""
            logger.info("Slot %d research [%s] status: %s notes=[%s]", slot_num, task_id[:8], status.state, notes_preview[:100])

        result = await client.deep_research(
            prompt=prompt,
            poll_interval=poll_interval,
            timeout=timeout,
            on_status=on_status,
        )

        task["done"] = result.done
        task["text"] = result.text
        task["plan"] = {
            "research_id": result.plan.research_id,
            "title": result.plan.title,
            "query": result.plan.query,
            "steps": result.plan.steps,
            "eta_text": result.plan.eta_text,
        }
        # Extract sources from the final output
        sources = []
        if result.final_output and result.final_output.candidates:
            for c in result.final_output.candidates:
                if c.sources:
                    sources = c.sources
                    break
        task["sources"] = sources
        task["finished"] = True
        task["finished_at"] = int(time.time())
        logger.info("Slot %d research [%s] finished. done=%s text_len=%d sources=%d", slot_num, task_id[:8], result.done, len(result.text), len(sources))

    except Exception as e:
        task["finished"] = True
        task["finished_at"] = int(time.time())
        task["error"] = str(e)
        logger.error("Slot %d research [%s] error: %s", slot_num, task_id[:8], e)


@app.post("/slot/{num}/v1/research")
async def slot_deep_research(
    num: int,
    request: DeepResearchRequest,
    response: Response,
    api_key: str = Depends(_verify_api_key),
):
    """Start a deep research task. Returns immediately with a task_id for polling."""
    slot = _get_slot(num)
    try:
        client = await _get_client(slot)
        logger.info("Slot %d deep research: '%s'", num, request.prompt[:200])
        slot_log(num, f"Research: {request.prompt[:60]}")

        task_id = str(uuid.uuid4())
        _research_tasks[task_id] = {
            "task_id": task_id,
            "slot": num,
            "prompt": request.prompt[:200],
            "created_at": int(time.time()),
            "finished": False,
            "finished_at": None,
            "done": False,
            "text": "",
            "plan": None,
            "statuses": [],
            "last_state": "starting",
            "sources": [],
            "error": None,
        }

        # Launch research in background
        asyncio.create_task(
            _run_research_background(num, client, request.prompt, request.poll_interval, request.timeout, task_id)
        )

        return {
            "task_id": task_id,
            "status": "started",
            "message": "Deep research started. Poll GET /v1/research/{task_id} for progress.",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Slot %d research start error: %s", num, e, exc_info=True)
        slot.report_error(e)
        if "not eligible" in str(e).lower():
            raise HTTPException(status_code=403, detail=f"Account not eligible for deep research: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Deep research failed to start: {str(e)}")


@app.get("/v1/research/{task_id}")
async def get_research_status(
    task_id: str,
    api_key: str = Depends(_verify_api_key),
):
    """Poll a deep research task for progress and results."""
    task = _research_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Research task {task_id} not found")

    return {
        "task_id": task["task_id"],
        "slot": task["slot"],
        "prompt": task["prompt"],
        "created_at": task["created_at"],
        "finished": task["finished"],
        "finished_at": task["finished_at"],
        "done": task["done"],
        "last_state": task["last_state"],
        "plan": task["plan"],
        "statuses_count": len(task["statuses"]),
        "statuses": task["statuses"][-5:],
        "text": task["text"],
        "sources": task.get("sources", []),
        "error": task["error"],
    }


@app.get("/v1/research")
async def list_research_tasks(
    api_key: str = Depends(_verify_api_key),
):
    """List all research tasks."""
    tasks = []
    for t in _research_tasks.values():
        tasks.append({
            "task_id": t["task_id"],
            "slot": t["slot"],
            "prompt": t["prompt"],
            "created_at": t["created_at"],
            "finished": t["finished"],
            "done": t["done"],
            "last_state": t["last_state"],
            "text_length": len(t["text"]),
            "error": t["error"],
        })
    return {"tasks": tasks}


# ═══════════════════════════════════════════════════════════════════════
#  Worker management endpoints — /worker/...
# ═══════════════════════════════════════════════════════════════════════

@app.post("/worker/reload-slot/{num}")
async def worker_reload_slot(num: int):
    """Re-read env file and reinitialise the slot's GeminiClient."""
    slot = _get_slot(num)
    try:
        await slot.reload_from_env(ENVS_DIR)
        return {"ok": True, "message": f"Slot {num} reloaded", "health": slot.health_response()}
    except Exception as e:
        logger.error("Slot %d reload failed: %s", num, e)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "message": f"Slot {num} reload failed: {str(e)[:200]}"},
        )


@app.get("/worker/status")
async def worker_status():
    """Summary of all slots."""
    slot_list = []
    for num in sorted(slots):
        s = slots[num]
        h = s.health_response()
        h["num"] = num
        h["has_credentials"] = bool(s.psid)
        slot_list.append(h)
    available = sum(1 for s in slots.values() if s.client is not None and not s.state["initializing"])
    return {
        "total": len(slots),
        "available": available,
        "slots": slot_list,
    }


@app.post("/worker/deploy-slot/{num}")
async def worker_deploy_slot(num: int, request: Request):
    """Update cookie credentials and reinitialise the slot."""
    body = await request.json()
    psid = body.get("psid", "").strip()
    psidts = body.get("psidts", "").strip()
    if not psid or not psidts:
        raise HTTPException(status_code=400, detail="psid and psidts required")

    # Write env file
    env_file = ENVS_DIR / f"account{num}.env"
    lines = []
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0].strip()
            if key not in ("SECURE_1PSID", "SECURE_1PSIDTS"):
                lines.append(line)
    lines.append(f"SECURE_1PSID={psid}")
    lines.append(f"SECURE_1PSIDTS={psidts}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Reload or create slot
    if num in slots:
        await slots[num].reload(psid=psid, psidts=psidts)
    else:
        slots[num] = Slot(num=num, psid=psid, psidts=psidts)
        await slots[num].init_client(proxy=PROXY)

    slot_log(num, "Cookie deployed, slot reloaded")
    return {"ok": True, "message": f"Slot {num} deployed", "health": slots[num].health_response()}


@app.get("/worker/slot-logs/{num}")
async def worker_slot_logs(num: int, tail: int = 60):
    """Return recent log entries for a slot."""
    logs = list(_slot_logs.get(num, []))
    if tail > 0:
        logs = logs[-tail:]
    return {"ok": True, "lines": logs}


@app.post("/worker/test-slot/{num}")
async def worker_test_slot(num: int):
    """Send a minimal chat request to verify the slot works."""
    slot = _get_slot(num)
    if not slot.client or slot.state.get("initializing"):
        raise HTTPException(status_code=503, detail=f"Slot {num} not ready")

    try:
        models = await get_runtime_models(slot.client)
        model_name = None
        for m in models:
            mid = m.get("id", "")
            if "flash" in mid and "image" not in mid and "veo" not in mid:
                model_name = mid
                break
        if not model_name and models:
            model_name = models[0].get("id", "")
        if not model_name:
            raise HTTPException(status_code=500, detail="No models available")

        chat = slot.client.start_chat()
        response = await chat.send_message("hi")
        reply = response.text[:100] if response and response.text else "(empty)"
        # Strip think tags
        reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
        slot_log(num, f"Test OK: {reply[:50]}")
        return {"ok": True, "reply": reply, "model": model_name, "status": 200}
    except HTTPException:
        raise
    except Exception as e:
        slot_log(num, f"Test failed: {str(e)[:80]}")
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)[:200]})


@app.delete("/worker/slot/{num}")
async def worker_delete_slot(num: int):
    """Remove a slot: close client, delete env file, drop from registry."""
    if num not in slots:
        raise HTTPException(status_code=404, detail=f"Slot {num} not found")

    slot = slots.pop(num)
    if slot.client:
        try:
            await slot.client.close()
        except Exception:
            pass
    slot.edit_sessions.clear()

    env_file = ENVS_DIR / f"account{num}.env"
    if env_file.exists():
        env_file.unlink()

    _slot_logs.pop(num, None)
    slot_log(num, "Slot deleted")
    return {"ok": True, "message": f"Slot {num} deleted"}


@app.get("/")
async def root():
    return {"status": "online", "message": "Gemini API OneClick Worker", "slots": len(slots)}


# Local dev:  PYTHONPATH=./lib:./app uvicorn worker:app --app-dir app --port 7860
