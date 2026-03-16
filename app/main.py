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
                await gemini_client.init(timeout=600, watchdog_timeout=120, auto_refresh=False)
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


SIZE_TO_ASPECT = {
    "1280x720": "16:9 widescreen landscape",
    "720x1280": "9:16 tall portrait",
    "1792x1024": "3:2 landscape",
    "1024x1792": "2:3 portrait",
    "1024x1024": "",
}

# Style descriptions — Gemini's API may not trigger the web app's hidden style templates,
# so we provide detailed descriptions as guidance. Style name + description for best results.
STYLE_PROMPTS = {
    # Gemini official image styles
    "Monochrome": "Monochrome style, black and white with dramatic contrast, deep shadows and bright highlights, film noir aesthetic",
    "Color Block": "Color Block style, bold flat areas of saturated color, graphic design inspired, strong geometric shapes",
    "Runway": "Fashion runway style, high-fashion editorial look, dramatic poses, luxury aesthetic, magazine quality",
    "Screen Print": "Screen print style, Andy Warhol inspired, halftone dots, limited color palette, pop art aesthetic",
    "Colorful": "Extremely colorful and vibrant, rainbow palette, maximum saturation, joyful and energetic",
    "Gothic Clay": "Gothic claymation style, stop-motion clay figures, dark and eerie, Tim Burton inspired, textured surfaces",
    "Explosive": "Explosive action style, dramatic impact, debris and particles, high-energy dynamic composition, Michael Bay aesthetic",
    "Salon": "Salon portrait style, elegant and refined, soft glamour lighting, classic beauty photography",
    "Sketch": "Detailed pencil sketch on paper, graphite shading, fine crosshatch linework, hand-drawn feel",
    "Cinematic": "Cinematic style, movie still aesthetic, Rembrandt lighting, dramatic composition, anamorphic lens feel, film grain",
    "Steampunk": "Steampunk style, Victorian-era machinery, brass gears and pipes, industrial revolution meets fantasy",
    "Sunrise": "Golden sunrise style, warm golden hour light, long shadows, atmospheric haze, serene and hopeful mood",
    "Myth Fighter": "Epic mythological warrior style, ancient Greek/Norse aesthetic, dramatic battle poses, ornate armor, heroic composition",
    "Surreal": "Surrealist style, Salvador Dali inspired, impossible geometry, dreamlike distortions, melting forms",
    "Dark": "Dark moody style, deep shadows, minimal lighting, noir atmosphere, mysterious and brooding",
    "Enamel Pin": "Enamel pin style, flat vector illustration, bold outlines, limited colors, cute collectible aesthetic",
    "Cyborg": "Cyborg style, human-machine hybrid, visible circuitry and metal parts, bioluminescent elements, sci-fi realism",
    "Soft Portrait": "Soft portrait style, gentle diffused lighting, shallow depth of field, warm skin tones, intimate and dreamy",
    "Retro Cartoon": "1930s retro cartoon style, rubber hose animation, black and white with halftone, Fleischer Studios inspired",
    "Oil Painting": "Oil painting style, rich impasto brushstrokes, Rembrandt-style golden lighting, classical composition, museum quality, visible canvas texture",
    # Extra common styles
    "Anime": "Anime style, vibrant colors, clean cel-shading lineart, expressive eyes, Japanese animation aesthetic",
    "Photorealistic": "Photorealistic, ultra detailed like a DSLR photograph, natural lighting, sharp focus, 85mm lens",
    "Watercolor": "Watercolor painting, soft translucent washes, visible paper texture, gentle color bleeding, delicate brushwork",
    "Pixel Art": "Pixel art style, retro 16-bit video game aesthetic, clean pixel boundaries, limited palette, nostalgic",
    "Kawaii": "Kawaii style, adorable chibi proportions, pastel colors, round soft shapes, sparkles and hearts",
    "Ghibli": "Studio Ghibli animation style, lush hand-painted nature, warm soft lighting, whimsical and magical atmosphere",
    # Gemini official video styles
    "Civilization": "Ancient civilization epic style, grand architecture, marble and gold, historical drama aesthetic",
    "Metallic": "Metallic chrome style, reflective surfaces, liquid metal, futuristic industrial aesthetic",
    "Memo": "Memo style, playful and expressive, close-up character study, natural and candid feel",
    "Glam": "Glamorous style, sparkle and shine, luxury fashion, dramatic beauty lighting, editorial elegance",
    "Crochet": "Crochet knitted style, soft yarn textures, handcrafted warmth, cozy stop-motion aesthetic",
    "Cyberpunk": "Cyberpunk style, neon-lit streets, holographic signs, rain reflections, futuristic dystopia",
    "Video Game": "Retro video game style, pixel art animation, 8-bit/16-bit aesthetic, arcade feel",
    "Cosmos": "Cosmic space style, nebulae and stars, infinite depth, astronomical wonder, sci-fi grandeur",
    "Action Hero": "Action hero blockbuster style, intense close-ups, dramatic slow motion, gritty and cinematic",
    "Stardust": "Stardust fairy tale style, magical sparkles, enchanted garden, soft dreamy atmosphere, romantic fantasy",
    "Jellytoon": "Jellytoon style, 3D animated character, soft rounded forms, vibrant Pixar-like aesthetic, cute and expressive",
    "Racetrack": "Racetrack style, miniature tilt-shift effect, toy-like world, bright saturated colors, playful perspective",
    "ASMR Apple": "ASMR macro style, extreme close-up detail, satisfying textures, crisp focus, sensory-rich",
    "Red Carpet": "Red carpet documentary style, paparazzi flash, celebrity glamour, dramatic entrances",
    "Popcorn": "Popcorn fun style, playful stop-motion, whimsical food art, creative and surprising compositions",
}

QUALITY_PROMPTS = {
    "hd": "Make it extremely detailed and high quality, with 4K resolution clarity and sharp focus throughout.",
}


def build_image_prompt(request: ImageGenerationRequest) -> str:
    """Build natural language prompt optimized for Gemini's ImageFX engine."""
    parts = ["Generate an image:"]

    # User prompt first — the core intent
    parts.append(request.prompt)

    # Style — use detailed description if available, fall back to style name
    if request.style:
        desc = STYLE_PROMPTS.get(request.style, f"{request.style} style")
        parts.append(desc)

    # Quality enhancement
    if request.quality and request.quality in QUALITY_PROMPTS:
        parts.append(QUALITY_PROMPTS[request.quality])

    # Aspect ratio as natural description
    aspect_desc = SIZE_TO_ASPECT.get(request.size or "", "")
    if aspect_desc:
        parts.append(f"The image should be in {aspect_desc} format.")

    # Negative prompt as natural instruction
    if request.negative_prompt:
        parts.append(f"Important: do not include {request.negative_prompt} in the image.")

    return " ".join(parts)


@app.post("/v1/images/generations")
async def create_image(request: ImageGenerationRequest, api_key: str = Depends(verify_api_key)):
    """DALL-E compatible image generation endpoint using Gemini ImageFX."""
    try:
        client = await get_or_create_client()
        logger.info(f"Image generation request: '{request.prompt[:100]}' style={request.style} quality={request.quality} size={request.size}")

        model = map_model_name(request.model) if request.model else None
        prompt = build_image_prompt(request)
        logger.info(f"Final prompt: '{prompt[:200]}' model={model}")

        kwargs = {}
        if model:
            kwargs["model"] = model
        response = await client.generate_content(prompt, **kwargs)

        # Log what we got back for debugging
        logger.info(f"Response text: '{response.text[:200] if response.text else 'None'}'")
        logger.info(f"Response images: {response.images}")
        logger.info(f"Generated images: {response.candidates[response.chosen].generated_images}")
        logger.info(f"Web images: {response.candidates[response.chosen].web_images}")

        images = response.images
        if not images:
            # Try alternate prompt format
            logger.info("No images with first prompt, trying alternate format...")
            response = await client.generate_content(
                f"Create a picture: {request.prompt}"
            )
            images = response.images
            logger.info(f"Alternate attempt images: {images}")

        if not images:
            return {
                "created": int(time.time()),
                "data": [],
                "error": {
                    "message": f"Gemini did not generate images. Response: {response.text[:300] if response.text else 'empty'}",
                    "type": "no_images",
                }
            }

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

        logger.info(f"Image generation complete: {len(result_data)} image(s)")
        return {"created": int(time.time()), "data": result_data}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Image generation error: {e}", exc_info=True)
        error_msg = str(e).lower()
        if any(kw in error_msg for kw in ['auth', 'cookie', 'expired', '401', '403']):
            await reset_client()
        raise HTTPException(status_code=500, detail=f"Image generation failed: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")
