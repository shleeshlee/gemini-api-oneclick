#!/usr/bin/env python3
"""Gemini API Gateway — smart load balancer with health monitoring.

Runs on host machine. Proxies all /v1/* requests to healthy containers
with automatic failover. Provides status UI and management API.

Port: 9800 (configurable via GATEWAY_PORT env)
"""

import asyncio
import json
import os
import re
import subprocess
import time
from collections import deque
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
import uvicorn

# ── Config ──────────────────────────────────────────────────────────────

ROOT_DIR = Path(__file__).resolve().parent
ENVS_DIR = ROOT_DIR / "envs"
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "9800"))
GATEWAY_HTML = ROOT_DIR / "web" / "index.html"
BASE_PORT = int(os.environ.get("BASE_PORT", "8001"))

# ── Auth ─────────────────────────────────────────────────────────────
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
API_KEY = _dotenv.get("API_KEY", "")
COOKIE_MANAGER_PASSWORD = _dotenv.get("COOKIE_MANAGER_PASSWORD", "")

def verify_auth(request: Request):
    """Dependency: check key query param, Authorization Bearer header, or cookie-manager password."""
    if not API_KEY and not COOKIE_MANAGER_PASSWORD:
        return  # no key configured, allow all
    valid_keys = {k for k in (API_KEY, COOKIE_MANAGER_PASSWORD) if k}
    # Check query param
    key = request.query_params.get("key", "")
    if key and key in valid_keys:
        return
    # Check Authorization header
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        if token in valid_keys:
            return
    raise HTTPException(status_code=401, detail="未授权：密钥无效")
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

    @property
    def available(self):
        return self.healthy and self.enabled

    def to_dict(self):
        return {
            "num": self.num,
            "port": self.port,
            "name": account_names.get(self.num, ""),
            "healthy": self.healthy,
            "enabled": self.enabled,
            "available": self.available,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "last_check": self.last_check,
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
        }


containers: dict[int, Container] = {}
round_robin_index = 0
logs: deque = deque(maxlen=MAX_LOG_ENTRIES)
account_names: dict[int, str] = {}  # num -> display name, persisted to state/accounts.json

ACCOUNTS_FILE = ROOT_DIR / "state" / "accounts.json"
GATEWAY_STATE_FILE = ROOT_DIR / "state" / "gateway-state.json"


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


def get_next_available() -> Container | None:
    """Round-robin to next available container."""
    global round_robin_index
    nums = sorted(containers.keys())
    if not nums:
        return None

    tried = 0
    while tried < len(nums):
        round_robin_index = (round_robin_index + 1) % len(nums)
        c = containers[nums[round_robin_index]]
        if c.available:
            return c
        tried += 1
    return None


# ── Health Check ────────────────────────────────────────────────────────

async def check_health(c: Container, client: httpx.AsyncClient):
    """Check single container health."""
    try:
        resp = await client.get(f"{c.url}/health", timeout=5.0)
        data = resp.json()
        was_healthy = c.healthy
        c.healthy = resp.status_code == 200 and data.get("client_ready", False)
        c.last_check = time.time()

        if c.healthy:
            c.error_count = 0
            if not was_healthy:
                add_log("info", c.num, "Container recovered — now healthy")
        else:
            reason = "client not ready" if resp.status_code == 200 else f"HTTP {resp.status_code}"
            if was_healthy:
                add_log("warn", c.num, f"Health check failed: {reason}")
    except Exception as e:
        c.healthy = False
        c.last_check = time.time()
        add_log("warn", c.num, f"Health check error: {str(e)[:80]}")


async def health_loop():
    """Background health check loop."""
    await asyncio.sleep(5)  # initial delay
    async with httpx.AsyncClient() as client:
        while True:
            tasks = [check_health(c, client) for c in containers.values()]
            await asyncio.gather(*tasks, return_exceptions=True)

            available = sum(1 for c in containers.values() if c.available)
            total = len(containers)
            add_log("info", None, f"Health check: {available}/{total} available")

            await asyncio.sleep(HEALTH_INTERVAL)


# ── FastAPI App ─────────────────────────────────────────────────────────

app = FastAPI(title="Gemini API Gateway")


@app.on_event("startup")
async def startup():
    load_account_names()
    discover_containers()
    asyncio.create_task(health_loop())


# ── Proxy ───────────────────────────────────────────────────────────────

AUTH_ERRORS = {"auth", "cookie", "expired", "invalid", "401", "403",
               "credentials not configured", "failed to initialize"}


def is_auth_error(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in AUTH_ERRORS)


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy(request: Request, path: str):
    """Proxy requests to healthy containers with auto-failover."""
    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    is_stream = False

    if body:
        try:
            body_json = json.loads(body)
            is_stream = body_json.get("stream", False)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    retries = min(MAX_RETRIES, sum(1 for c in containers.values() if c.available))
    if retries == 0:
        raise HTTPException(status_code=503, detail="No healthy containers available")

    last_error = ""
    for attempt in range(retries):
        c = get_next_available()
        if not c:
            break

        target_url = f"{c.url}/v1/{path}"
        c.total_requests += 1

        try:
            # Always use streaming proxy to avoid client timeout
            # (Gemini can take 5+ seconds before first byte)
            client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0))
            try:
                req = client.build_request(request.method, target_url, content=body, headers=headers)
                resp = await client.send(req, stream=True)

                if resp.status_code >= 500:
                    error_body = (await resp.aread()).decode(errors="replace")[:200]
                    await resp.aclose()
                    await client.aclose()
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
            error_msg = str(e)[:200]
            c.total_errors += 1
            c.error_count += 1
            c.last_error = error_msg
            last_error = error_msg
            add_log("error", c.num, f"Request failed (attempt {attempt+1}): {error_msg[:100]}")

            if is_auth_error(error_msg):
                c.healthy = False
                if c.error_count >= ERROR_THRESHOLD:
                    c.enabled = False
                    save_gateway_state()
                    add_log("error", c.num, f"Auto-disabled after {c.error_count} errors")
            continue

    raise HTTPException(status_code=502, detail=f"All containers failed. Last: {last_error}")


# ── Management API ──────────────────────────────────────────────────────

@app.get("/gateway/status", dependencies=[Depends(verify_auth)])
async def gateway_status():
    """Return all container statuses."""
    return {
        "containers": [containers[n].to_dict() for n in sorted(containers.keys())],
        "available": sum(1 for c in containers.values() if c.available),
        "total": len(containers),
    }


@app.get("/gateway/logs", dependencies=[Depends(verify_auth)])
async def gateway_logs(limit: int = 50):
    """Return recent gateway logs."""
    return {"logs": list(logs)[:limit]}


@app.post("/gateway/enable/{num}", dependencies=[Depends(verify_auth)])
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


@app.post("/gateway/disable/{num}", dependencies=[Depends(verify_auth)])
async def disable_container(num: int):
    """Manually disable a container."""
    if num not in containers:
        raise HTTPException(status_code=404, detail=f"Container {num} not found")
    c = containers[num]
    c.enabled = False
    save_gateway_state()
    add_log("info", num, "Manually disabled")
    return {"ok": True, "message": f"Container {num} disabled"}


@app.post("/gateway/name/{num}", dependencies=[Depends(verify_auth)])
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


@app.post("/gateway/refresh", dependencies=[Depends(verify_auth)])
async def refresh_health():
    """Trigger immediate health check."""
    async with httpx.AsyncClient() as client:
        tasks = [check_health(c, client) for c in containers.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
    available = sum(1 for c in containers.values() if c.available)
    return {"ok": True, "available": available, "total": len(containers)}


@app.post("/gateway/deploy/{num}", dependencies=[Depends(verify_auth)])
async def deploy_cookie(num: int, request: Request):
    """Deploy cookies to a container: write env file and recreate container."""
    body = await request.json()
    psid = body.get("psid", "").strip()
    psidts = body.get("psidts", "").strip()
    if not psid:
        raise HTTPException(status_code=400, detail="psid 不能为空")

    # Write env file
    env_file = ENVS_DIR / f"account{num}.env"
    env_file.write_text(
        f"API_KEY=\nSECURE_1PSID={psid}\nSECURE_1PSIDTS={psidts}\n",
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
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:200]
            add_log("error", num, f"容器重建失败: {err}")
            raise HTTPException(status_code=500, detail=f"容器重建失败: {err}")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="docker compose 命令未找到")

    # Discover new container if it wasn't known
    port = BASE_PORT + num - 1
    if num not in containers:
        containers[num] = Container(num, port)

    add_log("info", num, "容器已重建")
    return {"ok": True, "message": f"容器 #{num} Cookie 已部署并重建"}


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
    """Authenticate with cookie-manager password."""
    body = await request.json()
    if not COOKIE_MANAGER_PASSWORD:
        return {"ok": True}
    password = body.get("password", "")
    if password != COOKIE_MANAGER_PASSWORD:
        raise HTTPException(status_code=401, detail="unauthorized")
    return {"ok": True}


@app.get("/api/accounts")
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


@app.get("/api/guard-settings")
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


@app.post("/api/guard-settings")
async def api_set_guard_settings(request: Request):
    """Update guard settings in .env."""
    body = await request.json()
    if COOKIE_MANAGER_PASSWORD:
        password = body.get("password", "")
        if password != COOKIE_MANAGER_PASSWORD:
            raise HTTPException(status_code=401, detail="unauthorized")

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
