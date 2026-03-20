#!/usr/bin/env python3
"""Gemini API Gateway — smart load balancer with health monitoring.

Runs on host machine. Proxies all /v1/* requests to healthy containers
with automatic failover. Provides status UI and management API.

Port: 9880 (configurable via GATEWAY_PORT env)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import base64
import uuid as _uuid

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

# ── Config ──────────────────────────────────────────────────────────────

ROOT_DIR = Path(__file__).resolve().parent
ENVS_DIR = ROOT_DIR / "envs"
GATEWAY_HTML = ROOT_DIR / "web" / "index.html"


def _read_dotenv() -> dict:
    """Read all key=value pairs from .env file."""
    result = {}
    env_file = ROOT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip()
    return result


_dotenv = _read_dotenv()
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT") or _dotenv.get("GATEWAY_PORT") or "9880")
BASE_PORT = int(os.environ.get("BASE_PORT") or os.environ.get("START_PORT") or _dotenv.get("START_PORT") or "8001")
API_KEY = _dotenv.get("API_KEY", "")
COOKIE_MANAGER_PASSWORD = _dotenv.get("COOKIE_MANAGER_PASSWORD", "")

def _safe_compare(a: str, b: str) -> bool:
    """Timing-safe string comparison."""
    return hmac.compare_digest(a.encode(), b.encode())


# ── Rate Limiter ───────────────────────────────────────────────────
_login_attempts: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 5  # max attempts per window


def _check_rate_limit(ip: str):
    """Raise 429 if IP exceeds login attempt limit."""
    now = time.time()
    attempts = _login_attempts[ip]
    # Clean old entries
    _login_attempts[ip] = [t for t in attempts if now - t < RATE_LIMIT_WINDOW]
    if len(_login_attempts[ip]) >= RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    _login_attempts[ip].append(now)


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _extract_bearer(request: Request) -> str:
    """Extract Bearer token from Authorization header."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""

def verify_auth(request: Request):
    """Dependency: accept either API_KEY or panel password."""
    if not API_KEY and not COOKIE_MANAGER_PASSWORD:
        return
    token = _extract_bearer(request)
    valid_keys = [k for k in (API_KEY, COOKIE_MANAGER_PASSWORD) if k]
    if token and any(_safe_compare(token, k) for k in valid_keys):
        return
    raise HTTPException(status_code=401, detail="未授权")

def verify_panel_auth(request: Request):
    """Dependency: panel/management endpoints, only accept panel password."""
    if not COOKIE_MANAGER_PASSWORD:
        return
    token = _extract_bearer(request)
    if token and _safe_compare(token, COOKIE_MANAGER_PASSWORD):
        return
    raise HTTPException(status_code=401, detail="未授权")

HEALTH_INTERVAL = 30  # seconds between health checks
MAX_RETRIES = 5  # max containers to try per request
ERROR_THRESHOLD = 3  # consecutive errors before auto-disable
MAX_LOG_ENTRIES = 200

# ── Container State ─────────────────────────────────────────────────────

class Container:
    def __init__(self, num: int, port: int):
        self.num = num
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.healthy = False
        self.enabled = True
        self.error_count = 0
        self.last_error = ""
        self.last_check = 0
        self.total_requests = 0
        self.total_errors = 0
        self.health_fail_count = 0  # consecutive health check failures
        self.needs_cookie = False   # True = auth error, restart won't help
        self.cooldown_until = 0  # timestamp: timeout cooldown, skip until then
        self.img_blocked = False    # True = can chat but not generate images, needs fresh cookie
        self.busy = False           # True = currently processing a request

    @property
    def available(self):
        return self.healthy and self.enabled and time.time() > self.cooldown_until and not self.busy

    def to_dict(self):
        return {
            "num": self.num,
            "port": self.port,
            "name": account_names.get(self.num, ""),
            "group": container_groups.get(self.num, ""),
            "healthy": self.healthy,
            "enabled": self.enabled,
            "available": self.available,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "last_check": self.last_check,
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "needs_cookie": self.needs_cookie,
            "img_blocked": self.img_blocked,
            "busy": self.busy,
        }


containers: dict[int, Container] = {}
round_robin_index = 0
logs: deque = deque(maxlen=MAX_LOG_ENTRIES)
account_names: dict[int, str] = {}  # num -> display name, persisted to state/accounts.json

ACCOUNTS_FILE = ROOT_DIR / "state" / "accounts.json"
GATEWAY_STATE_FILE = ROOT_DIR / "state" / "gateway-state.json"
GROUPS_FILE = ROOT_DIR / "state" / "groups.json"
GROUP_DEFS_FILE = ROOT_DIR / "state" / "group-defs.json"

container_groups: dict[int, str] = {}  # num -> group name (e.g. "pro", "was")
group_defs: list[str] = []  # defined group names, created by user
group_round_robin: dict[str, int] = {}  # group -> round robin index
_models_cache: list[dict] = []
_models_cache_time: float = 0
MODELS_CACHE_TTL = 300  # seconds


def load_account_names():
    global account_names
    try:
        if ACCOUNTS_FILE.exists():
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            account_names = {int(k): v for k, v in data.items()}
    except Exception:
        account_names = {}


def save_account_names():
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(json.dumps(account_names, ensure_ascii=False, indent=2), encoding="utf-8")


def load_groups():
    global container_groups, group_defs
    try:
        if GROUPS_FILE.exists():
            data = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
            container_groups = {int(k): v for k, v in data.items() if v}
    except Exception:
        container_groups = {}
    try:
        if GROUP_DEFS_FILE.exists():
            group_defs = json.loads(GROUP_DEFS_FILE.read_text(encoding="utf-8"))
    except Exception:
        group_defs = []
    # Auto-create defs for any existing assignments (migration)
    for g in set(container_groups.values()):
        if g and g not in group_defs:
            group_defs.append(g)
    if group_defs:
        save_group_defs()


def save_groups():
    GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    GROUPS_FILE.write_text(
        json.dumps({str(k): v for k, v in container_groups.items()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_group_defs():
    GROUP_DEFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    GROUP_DEFS_FILE.write_text(json.dumps(sorted(group_defs), ensure_ascii=False, indent=2), encoding="utf-8")


def get_all_group_names() -> set[str]:
    return set(group_defs) if group_defs else set()


def load_gateway_state():
    """Load persisted disabled container list."""
    try:
        if GATEWAY_STATE_FILE.exists():
            data = json.loads(GATEWAY_STATE_FILE.read_text(encoding="utf-8"))
            return set(data.get("disabled", []))
    except Exception:
        pass
    return set()


def save_gateway_state():
    """Persist disabled container list."""
    disabled = [c.num for c in containers.values() if not c.enabled]
    GATEWAY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    GATEWAY_STATE_FILE.write_text(
        json.dumps({"disabled": sorted(disabled)}, indent=2),
        encoding="utf-8",
    )


def add_log(level: str, container_num: int | None, message: str):
    logs.appendleft({
        "time": time.strftime("%H:%M:%S"),
        "ts": time.time(),
        "level": level,
        "container": container_num,
        "message": message,
    })


def discover_containers():
    """Discover containers from envs/ directory, apply persisted state."""
    global containers
    disabled_nums = load_gateway_state()
    for env_file in ENVS_DIR.glob("account*.env"):
        m = re.match(r"account(\d+)$", env_file.stem)
        if not m:
            continue
        num = int(m.group(1))
        port = BASE_PORT + num - 1
        if num not in containers:
            containers[num] = Container(num, port)
            if num in disabled_nums:
                containers[num].enabled = False
    add_log("info", None, f"Discovered {len(containers)} containers")


def get_next_available(group: str | None = None, is_image: bool = False) -> Container | None:
    """Round-robin to next available container, optionally filtered by group."""
    if group:
        nums = sorted(n for n, g in container_groups.items()
                       if g == group and n in containers and containers[n].available
                       and (not is_image or not containers[n].img_blocked))
    else:
        nums = sorted(n for n in containers if containers[n].available
                       and (not is_image or not containers[n].img_blocked))

    if not nums:
        return None

    key = group or "__default__"
    if is_image:
        key += ":img"
    idx = group_round_robin.get(key, -1)
    idx = (idx + 1) % len(nums)
    group_round_robin[key] = idx
    return containers[nums[idx]]


# ── Health Check ────────────────────────────────────────────────────────

HEALTH_FAIL_TOLERANCE = 2   # consecutive failures before marking unhealthy
HEALTH_RESTART_THRESHOLD = 3  # consecutive failures to trigger docker restart
_AUTH_KEYWORDS = {"auth", "cookie", "expired", "credentials", "401", "403"}


async def check_health(c: Container, client: httpx.AsyncClient):
    """Check single container health with failure tolerance and auto-restart."""
    try:
        resp = await client.get(f"{c.url}/health", timeout=5.0)
        data = resp.json()
        was_healthy = c.healthy
        ok = resp.status_code == 200 and data.get("client_ready", False)
        c.last_check = time.time()

        if ok:
            c.health_fail_count = 0
            if not c.healthy:
                c.healthy = True
                c.needs_cookie = False
                add_log("info", c.num, "恢复正常")
        else:
            c.health_fail_count += 1
            reason = "client not ready" if resp.status_code == 200 else f"HTTP {resp.status_code}"
            if c.health_fail_count == HEALTH_FAIL_TOLERANCE:
                c.healthy = False
                add_log("warn", c.num, f"健康检查连续{HEALTH_FAIL_TOLERANCE}次失败: {reason}")
            if c.health_fail_count >= HEALTH_RESTART_THRESHOLD:
                await _maybe_restart(c)
    except Exception as e:
        c.health_fail_count += 1
        c.last_check = time.time()
        if c.health_fail_count == HEALTH_FAIL_TOLERANCE:
            c.healthy = False
            add_log("warn", c.num, f"Health check error: {str(e)[:80]}")
        if c.health_fail_count >= HEALTH_RESTART_THRESHOLD:
            await _maybe_restart(c)


async def _maybe_restart(c: Container):
    """Restart container if not an auth problem. Auth issues need new cookies, not restart."""
    cname = f"gemini_api_account_{c.num}"
    # Check recent logs for auth errors
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "logs", "--tail", "30", cname,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        recent = stdout.decode(errors="replace").lower()
        if any(kw in recent for kw in _AUTH_KEYWORDS):
            if not c.needs_cookie:
                c.needs_cookie = True
                add_log("error", c.num, "认证错误，需要更换 Cookie（重启无效）")
            c.health_fail_count = 0  # stop retrying restart
            return
    except Exception:
        pass

    # Not auth — restart
    add_log("warn", c.num, f"连续{c.health_fail_count}次健康检查失败，自动重启容器")
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "restart", cname,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        c.health_fail_count = 0
        c.healthy = False  # wait for next health check to confirm
        add_log("info", c.num, "容器已重启，等待恢复")
    except Exception as e:
        add_log("error", c.num, f"容器重启失败: {str(e)[:80]}")


_last_log_ts: dict[int, str] = {}  # container num -> last seen log timestamp
_LOG_KEYWORDS = {"error", "exception", "failed", "cookie", "expired", "traceback",
                 "credentials", "401", "403", "500", "timeout", "client_ready"}
_LOG_SKIP = {"/health", "health check", "uvicorn running", "started server", "waiting for"}


# count_container_requests removed — proxy tracks total_requests/total_errors in real time


_log_seen: dict[int, set] = {}  # container num -> set of seen log hashes


async def sample_container_logs():
    """Read recent docker logs from all containers, surface new important entries only."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - HEALTH_INTERVAL - 5))
    for c in containers.values():
        cname = f"gemini_api_account_{c.num}"
        try:
            # Only read logs since last check interval
            proc = await asyncio.create_subprocess_exec(
                "docker", "logs", "--since", now_iso, "--timestamps", cname,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            lines = stdout.decode(errors="replace").splitlines()
            if c.num not in _log_seen:
                _log_seen[c.num] = set()
            seen = _log_seen[c.num]
            for line in lines:
                lower = line.lower()
                if not any(kw in lower for kw in _LOG_KEYWORDS):
                    continue
                if any(sk in lower for sk in _LOG_SKIP):
                    continue
                # Deduplicate by content hash
                text = line.split(" ", 1)[-1].strip()[:150] if " " in line else line.strip()[:150]
                if not text:
                    continue
                h = hashlib.md5(text.encode()).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                level = "error" if any(k in lower for k in {"error", "exception", "failed", "traceback"}) else "warn"
                add_log(level, c.num, text)
            # Keep seen set bounded
            if len(seen) > 500:
                _log_seen[c.num] = set()
        except Exception:
            pass


_last_health_available = -1  # track state changes


async def health_loop():
    """Background health check + log aggregation loop."""
    global _last_health_available
    await asyncio.sleep(5)  # initial delay
    async with httpx.AsyncClient() as client:
        while True:
            tasks = [check_health(c, client) for c in containers.values()]
            await asyncio.gather(*tasks, return_exceptions=True)

            available = sum(1 for c in containers.values() if c.available)
            total = len(containers)
            # Only log when availability changes
            if available != _last_health_available:
                add_log("info", None, f"Health check: {available}/{total} available")
                _last_health_available = available

            await sample_container_logs()

            await asyncio.sleep(HEALTH_INTERVAL)


# ── FastAPI App ─────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    load_account_names()
    load_groups()
    discover_containers()
    asyncio.create_task(health_loop())
    yield

app = FastAPI(title="Gemini API Gateway", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Proxy ───────────────────────────────────────────────────────────────

AUTH_ERRORS = {"auth", "cookie", "expired", "invalid", "401", "403",
               "credentials not configured", "failed to initialize"}


def is_auth_error(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in AUTH_ERRORS)


def parse_group_from_model(model: str) -> tuple[str | None, str]:
    """Check if model starts with a known group prefix.

    Returns (group_name, real_model). If no group matches, returns (None, original_model).
    E.g. "pro-gemini-2.0-flash" → ("pro", "gemini-2.0-flash")
    """
    groups = get_all_group_names()
    for g in sorted(groups, key=len, reverse=True):  # longest prefix first
        prefix = g + "-"
        if model.startswith(prefix) and len(model) > len(prefix):
            return g, model[len(prefix):]
    return None, model


async def fetch_base_models() -> list[dict]:
    """Return model list from saved state, or fetch from container."""
    global _models_cache, _models_cache_time
    now = time.time()
    if _models_cache and (now - _models_cache_time) < MODELS_CACHE_TTL:
        return _models_cache

    # Try saved models first
    saved = load_saved_models()
    if saved:
        _models_cache = saved
        _models_cache_time = now
        return _models_cache

    # Fallback: fetch from a healthy container
    for c in containers.values():
        if not c.available:
            continue
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{c.url}/v1/models")
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    _models_cache = data
                    _models_cache_time = now
                    save_models(data)
                    return _models_cache
        except Exception:
            continue
    return _models_cache


@app.get("/v1/models", dependencies=[Depends(verify_auth)])
async def list_models():
    """Return model list with group prefixes, filtering out 'unspecified'."""
    base_models = [m for m in await fetch_base_models() if m.get("id") != "unspecified"]
    groups = get_all_group_names()

    if not groups:
        return {"object": "list", "data": base_models}

    result = []
    for g in sorted(groups):
        for m in base_models:
            result.append({
                "id": f"{g}-{m['id']}",
                "object": "model",
                "created": m.get("created", 0),
                "owned_by": m.get("owned_by", "google"),
            })
    result.extend(base_models)
    return {"object": "list", "data": result}



# ── Image Prompt Building ──────────────────────────────────────────────

SIZE_TO_ASPECT = {
    "1280x720": "16:9 widescreen landscape",
    "720x1280": "9:16 tall portrait",
    "1792x1024": "3:2 landscape",
    "1024x1792": "2:3 portrait",
    "1024x1024": "",
}

STYLE_PROMPTS = {
    "Monochrome": {"prefix": "Monochrome style, black and white with dramatic contrast", "suffix": "deep shadows and bright highlights, film noir aesthetic, highly detailed"},
    "Color Block": {"prefix": "Color Block style, bold flat areas of saturated color, graphic design inspired", "suffix": "strong geometric shapes, clean edges, highly detailed"},
    "Runway": {"prefix": "Fashion runway style, high-fashion editorial look", "suffix": "dramatic poses, luxury aesthetic, magazine quality, highly detailed"},
    "Screen Print": {"prefix": "Screen print style, Andy Warhol inspired, halftone dots", "suffix": "limited color palette, pop art aesthetic, bold graphic quality"},
    "Colorful": {"prefix": "Extremely colorful and vibrant, rainbow palette", "suffix": "maximum saturation, joyful and energetic, highly detailed"},
    "Gothic Clay": {"prefix": "Gothic claymation style, stop-motion clay figures", "suffix": "dark and eerie, Tim Burton inspired, textured surfaces, highly detailed"},
    "Explosive": {"prefix": "Explosive action style, dramatic impact", "suffix": "debris and particles, high-energy dynamic composition, cinematic lighting, highly detailed"},
    "Salon": {"prefix": "Salon portrait style, elegant and refined", "suffix": "soft glamour lighting, classic beauty photography, smooth skin texture, highly detailed"},
    "Sketch": {"prefix": "Detailed pencil sketch on paper, graphite shading", "suffix": "fine crosshatch linework, hand-drawn feel, visible paper texture"},
    "Cinematic": {"prefix": "Cinematic style, movie still aesthetic, dramatic Rembrandt lighting", "suffix": "anamorphic lens feel, volumetric light rays, film grain, atmospheric haze, highly detailed"},
    "Steampunk": {"prefix": "Steampunk style, Victorian-era machinery", "suffix": "brass gears and pipes, industrial revolution meets fantasy, warm amber lighting, highly detailed"},
    "Sunrise": {"prefix": "Golden sunrise style, warm golden hour light", "suffix": "long shadows, atmospheric haze, serene and hopeful mood, highly detailed"},
    "Myth Fighter": {"prefix": "Epic mythological warrior style, ancient Greek/Norse aesthetic", "suffix": "dramatic battle poses, ornate armor, heroic composition, cinematic lighting, highly detailed"},
    "Surreal": {"prefix": "Surrealist style, Salvador Dali inspired", "suffix": "impossible geometry, dreamlike distortions, melting forms, ethereal lighting"},
    "Dark": {"prefix": "Dark moody style, deep shadows, minimal cold lighting", "suffix": "noir atmosphere, misty volumetric haze, subtle rim light on edges, mysterious and brooding, highly detailed"},
    "Enamel Pin": {"prefix": "Enamel pin style, flat vector illustration, bold outlines", "suffix": "limited colors, cute collectible aesthetic, clean graphic quality"},
    "Cyborg": {"prefix": "Cyborg style, human-machine hybrid, visible circuitry", "suffix": "bioluminescent elements, sci-fi realism, cold blue rim lighting, highly detailed"},
    "Soft Portrait": {"prefix": "Soft portrait style, gentle diffused lighting, shallow depth of field", "suffix": "warm skin tones, smooth skin texture, dreamy bokeh highlights, intimate atmosphere, highly detailed"},
    "Retro Cartoon": {"prefix": "1930s retro cartoon style, rubber hose animation", "suffix": "black and white with halftone, Fleischer Studios inspired, playful and nostalgic"},
    "Oil Painting": {"prefix": "Oil painting style, rich impasto brushstrokes, semi-painterly rendering", "suffix": "Rembrandt-style golden lighting, classical composition, visible canvas texture, museum quality, highly detailed"},
    "Anime": {"prefix": "High-quality digital anime illustration with refined lineart and confident varied line weight, elegant anime proportions with large expressive eyes featuring layered catchlights and iris detail", "suffix": "soft cel-shading with smooth gradient blending, refined facial features, graceful slender hands, silky hair with highlight streaks, cinematic lighting, soft shadows"},
    "Photorealistic": {"prefix": "Photorealistic, ultra detailed like a DSLR photograph", "suffix": "natural lighting, sharp focus, 85mm lens, shallow depth of field, highly detailed"},
    "Watercolor": {"prefix": "Detailed watercolor painting, soft translucent washes", "suffix": "visible paper texture, gentle color bleeding, warm tyndall effect lighting, delicate brushwork, highly detailed"},
    "Pixel Art": {"prefix": "Pixel art style, retro 16-bit video game aesthetic", "suffix": "clean pixel boundaries, limited palette, nostalgic, charming"},
    "Kawaii": {"prefix": "Kawaii style, adorable chibi proportions, pastel colors", "suffix": "round soft shapes, sparkles and hearts, cute and expressive"},
    "Ghibli": {"prefix": "High-quality anime illustration with pseudo-painterly rendering and semi-thick brush strokes, refined anime proportions with expressive eyes and soft facial features", "suffix": "vivid color grading with warm natural tones, expressive shadows with soft textured lighting, silky hair with natural highlight streaks, painterly anime background with atmospheric depth, dramatic atmosphere"},
    "Civilization": {"prefix": "Ancient civilization epic style, grand architecture", "suffix": "marble and gold, historical drama aesthetic, cinematic lighting, highly detailed"},
    "Metallic": {"prefix": "Metallic chrome style, reflective surfaces, liquid metal", "suffix": "futuristic industrial aesthetic, cold blue lighting, highly detailed"},
    "Memo": {"prefix": "Memo style, playful and expressive, close-up character study", "suffix": "natural and candid feel, warm soft lighting, highly detailed"},
    "Glam": {"prefix": "Glamorous style, sparkle and shine, luxury fashion", "suffix": "dramatic beauty lighting, editorial elegance, highly detailed"},
    "Crochet": {"prefix": "Crochet knitted style, soft yarn textures", "suffix": "handcrafted warmth, cozy stop-motion aesthetic, soft lighting, highly detailed"},
    "Cyberpunk": {"prefix": "Cyberpunk style, neon-lit streets, holographic signs", "suffix": "rain reflections, futuristic dystopia, volumetric neon lighting, highly detailed"},
    "Video Game": {"prefix": "Retro video game style, pixel art animation", "suffix": "8-bit/16-bit aesthetic, arcade feel, nostalgic"},
    "Cosmos": {"prefix": "Cosmic space style, nebulae and stars, infinite depth", "suffix": "astronomical wonder, sci-fi grandeur, ethereal lighting, highly detailed"},
    "Action Hero": {"prefix": "Action hero blockbuster style, intense close-ups", "suffix": "dramatic slow motion, gritty and cinematic, volumetric lighting, highly detailed"},
    "Stardust": {"prefix": "Stardust fairy tale style, magical sparkles", "suffix": "enchanted garden, soft dreamy atmosphere, romantic fantasy, ethereal glow, highly detailed"},
    "Jellytoon": {"prefix": "Jellytoon style, 3D animated character, soft rounded forms", "suffix": "vibrant Pixar-like aesthetic, cute and expressive, soft studio lighting"},
    "Racetrack": {"prefix": "Racetrack style, miniature tilt-shift effect", "suffix": "toy-like world, bright saturated colors, playful perspective"},
    "ASMR Apple": {"prefix": "ASMR macro style, extreme close-up detail", "suffix": "satisfying textures, crisp focus, sensory-rich, highly detailed"},
    "Red Carpet": {"prefix": "Red carpet documentary style, paparazzi flash", "suffix": "celebrity glamour, dramatic entrances, cinematic, highly detailed"},
    "Popcorn": {"prefix": "Popcorn fun style, playful stop-motion", "suffix": "whimsical food art, creative and surprising compositions"},
    "Otome CG": {"prefix": "High-quality otome game CG illustration with refined lineart and delicate tapered strokes, elegant bishoujo anime proportions with large expressive eyes featuring complex layered irises and bright catchlights, refined soft facial features", "suffix": "soft watercolor wash with warm peach and cool blue cel-shading, silky fine-line hair highlights, soft matte skin rendering, tyndall effect lighting, sentimental atmosphere with shallow depth-of-field, elegant jewelry details"},
    "Fantasy Anime": {"prefix": "High-quality fantasy anime illustration with refined lineart and varied line weights, elegant anime proportions with large detailed eyes featuring layered catchlights, refined facial features, graceful slender gestures", "suffix": "soft watercolor-like coloring with desaturated pastel tones and subtle color bleeding, ethereal diffused lighting with soft bloom effect, floating particles and magical light motes, lush fully-realized fantasy environment with atmospheric depth and dreamy haze"},
    "Shinkai": {"prefix": "High-quality digital anime illustration with crisp refined lineart and delicate tapered ends, thin internal detailing and slightly weighted outer silhouettes, elegant anime proportions with large intensely detailed eyes featuring complex iris patterns and multiple catchlights", "suffix": "saturated palette with vivid cold-warm color grading, smooth digital gradients with soft-edged cel-shading, dramatic cinematic rim lighting with subtle bloom effect, silky hair with sharp high-contrast highlights, luminous transparent sky with detailed cloud layers and sunlight rays piercing through, wistful and nostalgic atmosphere"},
    "Soft Anime": {"prefix": "High-quality digital anime illustration with soft watercolor aesthetic, refined sketchy lineart with varied line weights and delicate tapered strokes blending into soft coloring, elegant bishoujo anime proportions with large expressive eyes featuring complex layered irises and bright catchlights", "suffix": "desaturated pastel palette with gentle wet-on-wet transitions and subtle color bleeding, light cel-shading with warm peach and cool blue shadow accents, soft diffused ambient light with ethereal high-key glow, traditional watercolor paper grain texture, silky fine-line hair highlights, minimalist airy storybook atmosphere with floating particles"},
    "Pixiv Rank": {"prefix": "Top-ranked Pixiv illustration quality, masterful digital painting with professional polish and appeal, refined anime art style with meticulous attention to detail", "suffix": "vibrant saturated colors with expert light-shadow interplay, intricate hair rendering with flowing strands, jewel-like eyes with multiple reflections, dramatic composition, trending on Pixiv daily ranking, highly detailed"},
}

QUALITY_PROMPTS = {
    "hd": "Make it extremely detailed and high quality, with 4K resolution clarity and sharp focus throughout.",
}


def _build_image_prompt(body_json: dict, headers: dict) -> tuple[dict, bytes, dict]:
    """Extract style/quality/size/negative from body, build final prompt, clean body for container."""
    prompt = body_json.get("prompt", "")
    style = body_json.pop("style", None)
    quality = body_json.get("quality")
    size = body_json.get("size", "")
    negative = body_json.pop("negative_prompt", None)

    style_data = STYLE_PROMPTS.get(style) if style else None
    parts_prefix = []
    parts_suffix = []

    if style_data:
        parts_prefix.append(style_data["prefix"])
        parts_suffix.append(style_data["suffix"])

    if quality and quality in QUALITY_PROMPTS:
        parts_suffix.append(QUALITY_PROMPTS[quality])

    aspect_desc = SIZE_TO_ASPECT.get(size or "", "")
    if aspect_desc:
        parts_suffix.append(f"The image should be in {aspect_desc} format.")

    if negative:
        parts_suffix.append(f"Important: do not include {negative} in the image.")

    prefix = ", ".join(parts_prefix) + ", " if parts_prefix else ""
    suffix = ", " + ", ".join(parts_suffix) if parts_suffix else ""
    final_prompt = f"{prefix}{prompt}{suffix}"

    body_json["prompt"] = final_prompt
    new_body = json.dumps(body_json).encode("utf-8")
    headers["content-length"] = str(len(new_body))
    return body_json, new_body, headers


@app.api_route("/v1/{path:path}", methods=["GET", "POST"], dependencies=[Depends(verify_auth)])
async def proxy(request: Request, path: str):
    """Proxy requests to healthy containers with auto-failover and group routing."""
    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    # Replace auth header with API_KEY for container authentication
    if API_KEY:
        headers["authorization"] = f"Bearer {API_KEY}"

    # Parse model and detect group prefix
    target_group = None
    body_json = None
    if body:
        try:
            body_json = json.loads(body)
            model = body_json.get("model", "")
            if model:
                target_group, real_model = parse_group_from_model(model)
                if target_group:
                    # Rewrite model to real name before forwarding
                    body_json["model"] = real_model
                    body = json.dumps(body_json).encode("utf-8")
                    # Update content-length to match new body
                    headers["content-length"] = str(len(body))
                    add_log("info", None, f"Route [{target_group}] {model} → {real_model}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    is_media_req = "images" in path or "videos" in path
    is_image_req = is_media_req  # reuse for img_blocked filtering
    if body_json and "images" in path:
        body_json, body, headers = _build_image_prompt(body_json, headers)

    # Count available containers in target pool
    if target_group:
        pool_available = sum(1 for n, g in container_groups.items()
                             if g == target_group and n in containers and containers[n].available
                             and (not is_image_req or not containers[n].img_blocked))
    else:
        pool_available = sum(1 for c in containers.values() if c.available
                             and (not is_image_req or not c.img_blocked))

    retries = min(MAX_RETRIES, pool_available)
    if retries == 0:
        if is_image_req:
            detail = f"No containers available for image generation" + (f" in group [{target_group}]" if target_group else "") + " (all may be img_blocked)"
        else:
            detail = f"No healthy containers in group [{target_group}]" if target_group else "No healthy containers available"
        raise HTTPException(status_code=503, detail=detail)

    last_error = ""
    for attempt in range(retries):
        c = get_next_available(target_group, is_image=is_image_req)
        if not c:
            break

        target_url = f"{c.url}/v1/{path}"
        c.total_requests += 1
        c.busy = True

        try:
            # 视频 330s（轮询最多300s），图片 100s，聊天 120s
            if "videos" in path:
                read_timeout = 330.0
            elif "images" in path:
                read_timeout = 100.0
            else:
                read_timeout = 120.0
            client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=read_timeout, write=10.0, pool=10.0))
            try:
                req = client.build_request(request.method, target_url, content=body, headers=headers)
                resp = await client.send(req, stream=True)

                if resp.status_code >= 500 or resp.status_code == 422:
                    error_body = (await resp.aread()).decode(errors="replace")[:200]
                    await resp.aclose()
                    await client.aclose()
                    # Detect image generation blocked
                    if is_image_req and resp.status_code == 500:
                        lower_err = error_body.lower()
                        if any(kw in lower_err for kw in ("blocked", "safety", "policy", "restricted", "not available")):
                            c.img_blocked = True
                            add_log("warn", c.num, f"生图被阻止，标记为仅生文: {error_body[:80]}")
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}: {error_body}",
                        request=resp.request, response=resp
                    )
                c.error_count = 0

                async def stream_generator():
                    try:
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                    finally:
                        c.busy = False
                        await resp.aclose()
                        await client.aclose()

                return StreamingResponse(
                    stream_generator(),
                    status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"),
                )
            except Exception:
                await client.aclose()
                raise

        except Exception as e:
            c.busy = False
            error_msg = str(e)[:200]
            c.total_errors += 1
            c.error_count += 1
            c.last_error = error_msg
            last_error = error_msg
            add_log("error", c.num, f"Request failed (attempt {attempt+1}): {error_msg[:100]}")

            # 超时的容器冷却60秒，防止积压
            if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                c.cooldown_until = time.time() + 60
                add_log("warning", c.num, f"Container timed out, cooling down 60s")

            if is_auth_error(error_msg):
                c.healthy = False
                if c.error_count >= ERROR_THRESHOLD:
                    c.enabled = False
                    save_gateway_state()
                    add_log("error", c.num, f"Auto-disabled after {c.error_count} errors")
            continue

    raise HTTPException(status_code=502, detail=f"All containers failed. Last: {last_error}")


# ── Management API ──────────────────────────────────────────────────────

@app.get("/gateway/status", dependencies=[Depends(verify_panel_auth)])
async def gateway_status():
    """Return all container statuses."""
    return {
        "containers": [containers[n].to_dict() for n in sorted(containers.keys())],
        "available": sum(1 for c in containers.values() if c.available),
        "total": len(containers),
        "group_defs": sorted(group_defs),
    }


@app.get("/gateway/logs", dependencies=[Depends(verify_panel_auth)])
async def gateway_logs(limit: int = 50):
    """Return recent gateway logs."""
    return {"logs": list(logs)[:limit]}


@app.post("/gateway/enable/{num}", dependencies=[Depends(verify_panel_auth)])
async def enable_container(num: int):
    """Re-enable a disabled container."""
    if num not in containers:
        raise HTTPException(status_code=404, detail=f"Container {num} not found")
    c = containers[num]
    c.enabled = True
    c.error_count = 0
    save_gateway_state()
    add_log("info", num, "Manually re-enabled")
    return {"ok": True, "message": f"Container {num} re-enabled"}


@app.post("/gateway/disable/{num}", dependencies=[Depends(verify_panel_auth)])
async def disable_container(num: int):
    """Manually disable a container."""
    if num not in containers:
        raise HTTPException(status_code=404, detail=f"Container {num} not found")
    c = containers[num]
    c.enabled = False
    save_gateway_state()
    add_log("info", num, "Manually disabled")
    return {"ok": True, "message": f"Container {num} disabled"}


@app.post("/gateway/name/{num}", dependencies=[Depends(verify_panel_auth)])
async def set_container_name(num: int, request: Request):
    """Set display name for a container (persisted to disk)."""
    if num not in containers:
        raise HTTPException(status_code=404, detail=f"Container {num} not found")
    body = await request.json()
    name = body.get("name", "").strip()
    if name:
        account_names[num] = name
    else:
        account_names.pop(num, None)
    save_account_names()
    return {"ok": True}


@app.post("/gateway/refresh", dependencies=[Depends(verify_panel_auth)])
async def refresh_health():
    """Trigger immediate health check."""
    async with httpx.AsyncClient() as client:
        tasks = [check_health(c, client) for c in containers.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
    available = sum(1 for c in containers.values() if c.available)
    return {"ok": True, "available": available, "total": len(containers)}


@app.post("/gateway/group/{num}", dependencies=[Depends(verify_panel_auth)])
async def set_container_group(num: int, request: Request):
    """Set group for a container. Must be a defined group or empty (= default)."""
    if num not in containers:
        raise HTTPException(status_code=404, detail=f"Container {num} not found")
    body = await request.json()
    group = body.get("group", "").strip().lower()
    if group and group not in group_defs:
        raise HTTPException(status_code=400, detail=f"分组 [{group}] 不存在，请先创建")
    if group:
        container_groups[num] = group
    else:
        container_groups.pop(num, None)
    save_groups()
    add_log("info", num, f"Group set to [{group}]" if group else "Group → 默认")
    return {"ok": True}


@app.post("/gateway/batch-group", dependencies=[Depends(verify_panel_auth)])
async def batch_set_groups(request: Request):
    """Batch assign containers to a group. Body: {"group": "pro", "containers": [1,2,3,...]}"""
    body = await request.json()
    group = body.get("group", "").strip().lower()
    nums = body.get("containers", [])
    if group and group not in group_defs:
        raise HTTPException(status_code=400, detail=f"分组 [{group}] 不存在，请先创建")
    count = 0
    for num in nums:
        num = int(num)
        if num not in containers:
            continue
        if group:
            container_groups[num] = group
        else:
            container_groups.pop(num, None)
        count += 1
    save_groups()
    label = f"[{group}]" if group else "默认"
    add_log("info", None, f"批量分组 → {label}: {count} 个容器")
    return {"ok": True, "count": count}


@app.get("/gateway/groups", dependencies=[Depends(verify_panel_auth)])
async def get_groups():
    """Return defined groups with their container lists."""
    groups: dict[str, list[int]] = {}
    for g in group_defs:
        groups[g] = []
    ungrouped = []
    for num in sorted(containers.keys()):
        g = container_groups.get(num, "")
        if g and g in groups:
            groups[g].append(num)
        else:
            ungrouped.append(num)
    return {"groups": groups, "ungrouped": ungrouped, "defs": sorted(group_defs)}


@app.post("/gateway/create-group", dependencies=[Depends(verify_panel_auth)])
async def create_group(request: Request):
    """Create a new group definition."""
    body = await request.json()
    name = body.get("name", "").strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="分组名不能为空")
    if not re.match(r'^[a-z0-9_-]+$', name):
        raise HTTPException(status_code=400, detail="分组名只能包含小写字母、数字、下划线、横杠")
    if name in group_defs:
        raise HTTPException(status_code=400, detail=f"分组 [{name}] 已存在")
    group_defs.append(name)
    save_group_defs()
    add_log("info", None, f"Created group [{name}]")
    return {"ok": True}


@app.post("/gateway/rename-group", dependencies=[Depends(verify_panel_auth)])
async def rename_group(request: Request):
    """Rename a group definition. Containers follow automatically."""
    body = await request.json()
    old = body.get("old", "").strip().lower()
    new = body.get("new", "").strip().lower()
    if old not in group_defs:
        raise HTTPException(status_code=404, detail=f"分组 [{old}] 不存在")
    if not new:
        raise HTTPException(status_code=400, detail="新名称不能为空")
    if not re.match(r'^[a-z0-9_-]+$', new):
        raise HTTPException(status_code=400, detail="分组名只能包含小写字母、数字、下划线、横杠")
    if new != old and new in group_defs:
        raise HTTPException(status_code=400, detail=f"分组 [{new}] 已存在")
    # Rename in defs
    group_defs[group_defs.index(old)] = new
    # Rename in container assignments
    for num in list(container_groups.keys()):
        if container_groups[num] == old:
            container_groups[num] = new
    save_group_defs()
    save_groups()
    add_log("info", None, f"Group renamed [{old}] → [{new}]")
    return {"ok": True}


@app.post("/gateway/delete-group", dependencies=[Depends(verify_panel_auth)])
async def delete_group(request: Request):
    """Delete a group definition and ungroup its containers."""
    body = await request.json()
    name = body.get("name", "").strip().lower()
    if name not in group_defs:
        raise HTTPException(status_code=404, detail=f"分组 [{name}] 不存在")
    group_defs.remove(name)
    # Ungroup all containers in this group
    removed = [n for n, g in container_groups.items() if g == name]
    for n in removed:
        del container_groups[n]
    save_group_defs()
    save_groups()
    add_log("info", None, f"Deleted group [{name}], {len(removed)} containers → 默认")
    return {"ok": True, "ungrouped": len(removed)}


@app.post("/gateway/deploy/{num}", dependencies=[Depends(verify_panel_auth)])
async def deploy_cookie(num: int, request: Request):
    """Deploy cookies to a container: write env file and recreate container."""
    body = await request.json()
    psid = body.get("psid", "").strip().replace("\n", "").replace("\r", "")
    psidts = body.get("psidts", "").strip().replace("\n", "").replace("\r", "")
    if not psid:
        raise HTTPException(status_code=400, detail="psid 不能为空")

    # Write env file, preserving existing API_KEY
    env_file = ENVS_DIR / f"account{num}.env"
    existing_key = ""
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("API_KEY="):
                existing_key = line.split("=", 1)[1]
                break
    env_file.write_text(
        f"API_KEY={existing_key}\nSECURE_1PSID={psid}\nSECURE_1PSIDTS={psidts}\n",
        encoding="utf-8",
    )
    add_log("info", num, "Cookie 已更新，正在重建容器 ...")

    # Recreate container via docker compose
    compose_file = ROOT_DIR / "docker-compose.accounts.yml"
    service_name = f"gemini-api-{num}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "-f", str(compose_file),
            "up", "-d", "--force-recreate", service_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(ROOT_DIR),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:200]
            add_log("error", num, f"容器重建失败: {err}")
            raise HTTPException(status_code=500, detail=f"容器重建失败: {err}")
    except asyncio.TimeoutError:
        add_log("error", num, "容器重建超时(60s)")
        raise HTTPException(status_code=504, detail="容器重建超时")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="docker compose 命令未找到")

    # Discover new container if it wasn't known, reset health state
    port = BASE_PORT + num - 1
    if num not in containers:
        containers[num] = Container(num, port)
    c = containers[num]
    c.needs_cookie = False
    c.health_fail_count = 0
    c.healthy = False  # wait for health check to confirm

    add_log("info", num, "容器已重建")
    return {"ok": True, "message": f"容器 #{num} Cookie 已部署并重建"}


# ── Container Logs & Test ──────────────────────────────────────────────

@app.get("/gateway/container-log/{num}", dependencies=[Depends(verify_panel_auth)])
async def container_log(num: int, tail: int = 60):
    """Fetch recent docker logs from a specific container."""
    if num not in containers:
        raise HTTPException(status_code=404, detail=f"Container {num} not found")
    cname = f"gemini_api_account_{num}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "logs", "--tail", str(min(tail * 10, 2000)), "--timestamps", cname,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        all_lines = stdout.decode(errors="replace").splitlines()
        # Filter out health check noise so real logs are visible
        lines = [l for l in all_lines if "/health" not in l]
        if not lines:
            lines = all_lines
        lines = lines[-min(tail, 200):]
        return {"ok": True, "lines": lines}
    except asyncio.TimeoutError:
        return {"ok": False, "lines": ["Error: docker logs timed out (10s)"]}
    except Exception as e:
        return {"ok": False, "lines": [f"Error: {str(e)[:200]}"]}


@app.post("/gateway/test/{num}", dependencies=[Depends(verify_panel_auth)])
async def test_container(num: int):
    """Send a minimal test request to a specific container to verify it works."""
    if num not in containers:
        raise HTTPException(status_code=404, detail=f"Container {num} not found")
    c = containers[num]
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # First check health
            health = await client.get(f"{c.url}/health")
            health_data = health.json()
            if not health_data.get("client_ready"):
                return {"ok": False, "error": "Cookie 未就绪或已过期", "detail": health_data}

            # Get first available model from container
            models_resp = await client.get(f"{c.url}/v1/models")
            model_name = "unspecified"
            if models_resp.status_code == 200:
                model_data = models_resp.json().get("data", [])
                # 测试用 flash 模型（快）
                for m in model_data:
                    mid = m.get("id", "")
                    if "flash" in mid.lower():
                        model_name = mid
                        break
                if model_name == "unspecified":
                    for m in model_data:
                        if m.get("id") and m["id"] != "unspecified":
                            model_name = m["id"]
                            break

            # Send a minimal chat request
            resp = await client.post(
                f"{c.url}/v1/chat/completions",
                json={"model": model_name, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                reply = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                # Strip <think>...</think> tags
                reply = re.sub(r'<think>.*?</think>', '', reply, flags=re.DOTALL).strip()
                return {"ok": True, "reply": reply[:80], "model": model_name, "status": resp.status_code}
            else:
                return {"ok": False, "error": resp.text[:200], "model": model_name, "status": resp.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ── Model List Management ──────────────────────────────────────────────

MODELS_FILE = ROOT_DIR / "state" / "models.json"


def load_saved_models() -> list[dict]:
    try:
        if MODELS_FILE.exists():
            return json.loads(MODELS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def save_models(models: list[dict]):
    MODELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODELS_FILE.write_text(json.dumps(models, ensure_ascii=False, indent=2), encoding="utf-8")


@app.post("/gateway/refresh-models", dependencies=[Depends(verify_panel_auth)])
async def refresh_models():
    """Fetch model list from a healthy container and save to state."""
    global _models_cache, _models_cache_time
    for c in containers.values():
        if not c.available:
            continue
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{c.url}/v1/models")
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    save_models(data)
                    _models_cache = data
                    _models_cache_time = time.time()
                    add_log("info", c.num, f"模型列表已刷新: {len(data)} 个模型")
                    return {"ok": True, "models": data, "source": f"container #{c.num}"}
        except Exception:
            continue
    raise HTTPException(status_code=503, detail="无可用容器获取模型列表")


# ── Cookie Manager APIs (merged from cookie-manager.py) ────────────────

# Guard settings keys we allow reading/writing from frontend
_GUARD_KEYS = {
    "GUARD_AUTO_DISABLE",
    "GUARD_DISABLE_KEYWORDS",
    "GUARD_DISABLE_CODES",
    "GUARD_ERROR_THRESHOLD",
}
DOTENV_PATH = ROOT_DIR / ".env"


@app.post("/api/login")
async def api_login(request: Request):
    """Authenticate with cookie-manager password (rate-limited)."""
    ip = _get_client_ip(request)
    _check_rate_limit(ip)
    body = await request.json()
    if not COOKIE_MANAGER_PASSWORD:
        return {"ok": True}
    password = body.get("password", "")
    if not _safe_compare(password, COOKIE_MANAGER_PASSWORD):
        raise HTTPException(status_code=401, detail="unauthorized")
    return {"ok": True}


@app.get("/api/accounts", dependencies=[Depends(verify_panel_auth)])
async def api_accounts():
    """List accounts from envs/ directory with cookie status."""
    accounts_list = []
    numbered = []
    for env_file in ENVS_DIR.glob("account*.env"):
        m = re.match(r"account(\d+)$", env_file.stem)
        if not m:
            continue
        numbered.append((int(m.group(1)), env_file))

    for num, env_file in sorted(numbered, key=lambda x: x[0]):
        try:
            content = env_file.read_text()
            has_cookie = "SECURE_1PSID=" in content
            psid_preview = ""
            for line in content.split("\n"):
                if line.startswith("SECURE_1PSID=") and not line.startswith("SECURE_1PSIDTS"):
                    val = line.split("=", 1)[1]
                    if not val.strip():
                        has_cookie = False
                    psid_preview = val[:20] + "..." if len(val) > 20 else val
        except Exception:
            has_cookie = False
            psid_preview = ""
        accounts_list.append({"num": num, "has_cookie": has_cookie, "psid_preview": psid_preview})
    return {"accounts": accounts_list}


@app.get("/api/guard-settings", dependencies=[Depends(verify_panel_auth)])
async def api_get_guard_settings():
    """Read guard settings from .env."""
    current = _read_dotenv()
    settings = {}
    for key in _GUARD_KEYS:
        settings[key] = current.get(key, "")
    if not settings.get("GUARD_AUTO_DISABLE"):
        settings["GUARD_AUTO_DISABLE"] = "true"
    if not settings.get("GUARD_DISABLE_KEYWORDS"):
        settings["GUARD_DISABLE_KEYWORDS"] = "credentials not configured,failed to initialize"
    if not settings.get("GUARD_ERROR_THRESHOLD"):
        settings["GUARD_ERROR_THRESHOLD"] = "3"
    return {"settings": settings}


@app.post("/api/guard-settings", dependencies=[Depends(verify_panel_auth)])
async def api_set_guard_settings(request: Request):
    """Update guard settings in .env."""
    body = await request.json()
    new_settings = body.get("settings", {})
    if not new_settings:
        raise HTTPException(status_code=400, detail="no settings provided")

    lines = []
    seen_keys = set()
    if DOTENV_PATH.exists():
        for line in DOTENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in _GUARD_KEYS and key in new_settings:
                    lines.append(f"{key}={new_settings[key]}")
                    seen_keys.add(key)
                    continue
            lines.append(line)

    for key in _GUARD_KEYS:
        if key in new_settings and key not in seen_keys:
            lines.append(f"{key}={new_settings[key]}")

    DOTENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": True, "message": "Guard settings saved"}


@app.get("/api/health")
async def api_health():
    return {"status": "ok"}


# ── Image Gallery (persistent storage) ─────────────────────────────────

IMAGES_DIR = ROOT_DIR / "data" / "images"
IMAGES_META = ROOT_DIR / "data" / "images" / "meta.json"


def _load_image_meta() -> list[dict]:
    try:
        if IMAGES_META.exists():
            return json.loads(IMAGES_META.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_image_meta(meta: list[dict]):
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


@app.post("/gateway/images/save", dependencies=[Depends(verify_panel_auth)])
async def save_image(request: Request):
    """Save a base64 image to the project gallery."""
    body = await request.json()
    b64 = body.get("b64", "")
    if not b64:
        raise HTTPException(status_code=400, detail="b64 data required")

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    img_id = _uuid.uuid4().hex[:12]
    filename = f"{img_id}.png"
    filepath = IMAGES_DIR / filename

    try:
        img_data = base64.b64decode(b64)
        filepath.write_bytes(img_data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64: {e}")

    entry = {
        "id": img_id,
        "filename": filename,
        "prompt": body.get("prompt", ""),
        "final_prompt": body.get("final_prompt", ""),
        "style": body.get("style", ""),
        "size": body.get("size", ""),
        "quality": body.get("quality", ""),
        "channel": body.get("channel", ""),
        "created": int(time.time()),
    }

    meta = _load_image_meta()
    meta.insert(0, entry)
    _save_image_meta(meta)

    return {"ok": True, "id": img_id, "filename": filename}


@app.get("/gateway/images", dependencies=[Depends(verify_panel_auth)])
async def list_images(offset: int = 0, limit: int = 50):
    """List saved images with metadata."""
    meta = _load_image_meta()
    total = len(meta)
    items = meta[offset:offset + limit]
    return {"images": items, "total": total}


@app.delete("/gateway/images/{img_id}", dependencies=[Depends(verify_panel_auth)])
async def delete_image(img_id: str):
    """Delete a saved image."""
    meta = _load_image_meta()
    entry = next((m for m in meta if m["id"] == img_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Image not found")

    fn = entry["filename"]
    if "/" in fn or "\\" in fn or ".." in fn:
        raise HTTPException(status_code=400, detail="Invalid filename in metadata")
    filepath = (IMAGES_DIR / fn).resolve()
    if filepath.parent != IMAGES_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid filename in metadata")
    if filepath.exists():
        filepath.unlink()

    meta = [m for m in meta if m["id"] != img_id]
    _save_image_meta(meta)
    return {"ok": True}


@app.get("/data/images/{filename}")
async def serve_image(filename: str):
    """Serve saved image files (path-safe, no auth — images are user-facing)."""
    # Reject path traversal attempts
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    filepath = (IMAGES_DIR / filename).resolve()
    if not filepath.parent == IMAGES_DIR.resolve():
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(filepath, media_type="image/png")


# ── Style Templates ────────────────────────────────────────────────────

STYLES_DIR = ROOT_DIR / "data" / "styles"
STYLES_META = STYLES_DIR / "meta.json"


def _load_styles() -> list[dict]:
    try:
        if STYLES_META.exists():
            return json.loads(STYLES_META.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_styles(styles: list[dict]):
    STYLES_DIR.mkdir(parents=True, exist_ok=True)
    STYLES_META.write_text(json.dumps(styles, ensure_ascii=False, indent=2), encoding="utf-8")


@app.post("/gateway/styles/save", dependencies=[Depends(verify_panel_auth)])
async def save_style(request: Request):
    """Save a style template with multi-dimension descriptors."""
    body = await request.json()
    name = body.get("name", "").strip()
    dimensions = body.get("dimensions", {})
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if not dimensions:
        raise HTTPException(status_code=400, detail="dimensions required")

    style_id = _uuid.uuid4().hex[:8]
    entry = {
        "id": style_id,
        "name": name,
        "dimensions": dimensions,
        "created": int(time.time()),
    }

    styles = _load_styles()
    styles.insert(0, entry)
    _save_styles(styles)
    return {"ok": True, "id": style_id}


@app.get("/gateway/styles", dependencies=[Depends(verify_panel_auth)])
async def list_styles():
    """List saved style templates."""
    return {"styles": _load_styles()}


@app.delete("/gateway/styles/{style_id}", dependencies=[Depends(verify_panel_auth)])
async def delete_style(style_id: str):
    """Delete a style template."""
    styles = _load_styles()
    styles = [s for s in styles if s["id"] != style_id]
    _save_styles(styles)
    return {"ok": True}


# ── Frontend ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    if GATEWAY_HTML.exists():
        return GATEWAY_HTML.read_text(encoding="utf-8")
    return "<h1>Gateway running</h1><p>gateway.html not found</p>"


@app.get("/health")
async def health():
    available = sum(1 for c in containers.values() if c.available)
    return {"status": "ok" if available > 0 else "degraded", "available": available}


# ── Main ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[gateway] Starting on port {GATEWAY_PORT}")
    print(f"[gateway] Managing containers from {ENVS_DIR}")
    uvicorn.run("gateway:app", host="0.0.0.0", port=GATEWAY_PORT, log_level="info")
