# -------------------- Slot: per-account GeminiClient lifecycle --------------------
# Each Slot is a "virtual container" — holds one GeminiClient, its state, and sessions.

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SESSION_TTL = 600   # 10 minutes
_MAX_SESSIONS = 50   # per slot
_TLS_RESTART_THRESHOLD = 3


def model_label(model) -> str:
    """Prefer Google's canonical version label, never the token or enum alias."""
    description = getattr(model, "description", "") or ""
    display = getattr(model, "display_name", "") or ""
    return description if re.search(r"\d+(?:\.\d+)*\s+\w", description) else display


def model_version(label: str) -> tuple[int, ...]:
    match = re.search(r"\d+(?:\.\d+)*", label)
    return tuple(map(int, match.group().split("."))) if match else (0,)


def label_level(label: str) -> str | None:
    name = label.lower()
    if "pro" in name:
        return "pro"
    if "lite" in name:
        return "flash-lite"
    if "flash" in name:
        return "flash"
    if "auto" in name:
        return None
    name = re.sub(r"\b\d+(?:\.\d+)*\b|\bgemini\b", "", name)
    return re.sub(r"[^\w]+", "-", name).strip("-") or None


def request_level(name: str, known_levels=()) -> str:
    name = name.lower()
    if "-gemini-" in name:
        name = "gemini-" + name.rsplit("-gemini-", 1)[1]
    for level in sorted(set(known_levels) - {"pro", "flash", "flash-lite"}, key=lambda x: (-len(x), x)):
        if level in name:
            return level
    if "lite" in name:
        return "flash-lite"
    if "pro" in name:
        return "pro"
    return "flash"


def label_model_id(label: str) -> str:
    slug = re.sub(r"[^\w.]+", "-", label.lower()).strip("-")
    return slug if slug.startswith("gemini-") else f"gemini-{slug}"


def logged_in(client) -> bool:
    status = getattr(client, "account_status", None)
    return client is not None and getattr(status, "name", "") == "AVAILABLE"


def client_capacity(client) -> int:
    return max((m.capacity for m in (getattr(client, "_model_registry", None) or {}).values()), default=0)


def registry_levels(client) -> dict[str, Any]:
    levels = {}
    if not logged_in(client):
        return levels
    for model in (getattr(client, "_model_registry", None) or {}).values():
        level = label_level(model_label(model))
        if level and model.is_available and (level not in levels or model_version(model_label(model)) > model_version(model_label(levels[level]))):
            levels[level] = model
    return levels


def best_flash_model(clients):
    candidates = [registry_levels(client)["flash"] for client in clients
                  if client_capacity(client) >= 2 and "flash" in registry_levels(client)]
    if candidates:
        return max(candidates, key=lambda m: model_version(model_label(m)))
    from gemini_webapi.constants import Model, MODEL_HEADER_KEY
    # The vendored table is the only fallback. Derive capacity from its header.
    candidates = [m for m in Model if label_level(m.model_name) == "flash"]
    return max(candidates, key=lambda m: max(
        (v for v in json.loads(m.model_header[MODEL_HEADER_KEY])[11:] if isinstance(v, int)), default=0))


def actual_model(output) -> str:
    value = getattr(output, "actual_model", None)
    return value if isinstance(value, str) and value else "unknown"


async def truth_stream_response(stream, headers):
    """Forward SSE immediately; the actual model is carried by each chunk."""
    from fastapi.responses import StreamingResponse
    stream_headers = {key: value for key, value in headers.items()
                      if key.lower() != "x-oneclick-actual-model"}
    return StreamingResponse(stream, media_type="text/event-stream", headers=stream_headers)


def parse_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines from an env file."""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip()
    return data


@dataclass
class Slot:
    num: int
    psid: str = ""
    psidts: str = ""
    client: GeminiClient | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _cookie_lock: Any = field(default_factory=threading.Lock, repr=False)
    state: dict[str, Any] = field(default_factory=lambda: {
        "auth_status": "unknown",
        "last_error": "",
        "last_error_type": "",
        "needs_restart": False,
        "initializing": True,
        "_tls_fail_count": 0,
    })
    edit_sessions: dict[str, Any] = field(default_factory=dict)
    pro_verified: bool = False
    pro_denied: bool = False
    last_actual_model: str = "unknown"
    actual_labels: dict[str, str] = field(default_factory=dict)
    model_events: list[dict[str, Any]] = field(default_factory=list)
    env_path: Path | None = None
    _last_cookie_write: float | None = None
    _pro_probe_done: bool = False

    # ── Factory ──────────────────────────────────────────────────────

    @classmethod
    def from_env_file(cls, num: int, env_path: Path) -> "Slot":
        data = parse_env_file(env_path)
        return cls(
            num=num,
            psid=data.get("SECURE_1PSID", ""),
            psidts=data.get("SECURE_1PSIDTS", ""),
            env_path=env_path,
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    async def init_client(self, proxy: str | None = None) -> None:
        """Create and initialise the GeminiClient for this slot."""
        from gemini_webapi import GeminiClient
        from gemini_webapi.constants import AccountStatus
        self.pro_verified = False
        self.pro_denied = False
        self._pro_probe_done = False
        self.actual_labels.clear()
        self.last_actual_model = "unknown"
        if not self.psid or not self.psidts:
            self.state["auth_status"] = "no_credentials"
            self.state["initializing"] = False
            logger.warning("Slot %d: no credentials, skipping init", self.num)
            return

        try:
            self.state["initializing"] = True
            self.client = GeminiClient(self.psid, self.psidts, proxy=proxy)
            await self.client.init(timeout=300, watchdog_timeout=180, auto_refresh=False)

            status = getattr(self.client, "account_status", None)
            self.state["auth_status"] = status.name if status else "unknown"
            if status and status != AccountStatus.AVAILABLE:
                logger.warning("Slot %d: auth status %s", self.num, status.name)

            await self.probe_free_pro()

            self.state["initializing"] = False
            self.state["needs_restart"] = False
            self.state["_tls_fail_count"] = 0
            self.state["last_error"] = ""
            self.state["last_error_type"] = ""
            logger.info("Slot %d: client initialised", self.num)

        except Exception as e:
            logger.error("Slot %d: init failed: %s", self.num, e)
            self.client = None
            self.state["initializing"] = False
            error_str = str(e).lower()
            if "tls" in error_str or "ssl" in error_str or "curl: (35)" in error_str:
                self.state["_tls_fail_count"] += 1
                self.state["last_error_type"] = "tls_error"
                if self.state["_tls_fail_count"] >= _TLS_RESTART_THRESHOLD:
                    self.state["needs_restart"] = True
            else:
                self.state["last_error_type"] = "init_failed"
            self.state["last_error"] = str(e)[:200]
            raise

    async def reload(self, psid: str | None = None, psidts: str | None = None) -> None:
        """Destroy current client and reinitialise with (optionally new) credentials."""
        if psid is not None:
            self.psid = psid
        if psidts is not None:
            self.psidts = psidts
        self.client = None
        self.edit_sessions.clear()
        self.state.update({
            "auth_status": "unknown",
            "last_error": "",
            "last_error_type": "",
            "needs_restart": False,
            "_tls_fail_count": 0,
            "initializing": True,
        })
        proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or None
        await self.init_client(proxy=proxy)

    async def reload_from_env(self, envs_dir: Path) -> None:
        """Re-read the env file and reload the client."""
        env_path = envs_dir / f"account{self.num}.env"
        self.env_path = env_path
        data = parse_env_file(env_path)
        await self.reload(
            psid=data.get("SECURE_1PSID", ""),
            psidts=data.get("SECURE_1PSIDTS", ""),
        )

    # ── Health ───────────────────────────────────────────────────────

    def detect_tier(self) -> dict[str, Any]:
        from gemini_webapi.constants import AccountStatus
        if not self.client:
            return {"capacity": 0, "label": "unknown"}
        registry = getattr(self.client, "_model_registry", None)
        if not registry:
            return {"capacity": 0, "label": "unknown"}
        cap = max((m.capacity for m in registry.values()), default=0)
        label = {4: "plus", 3: "ultra", 2: "pro", 1: "free"}.get(cap, "unknown")
        result: dict[str, Any] = {"capacity": cap, "label": label, "models": len(registry)}
        status = getattr(self.client, "account_status", None)
        if status and status != AccountStatus.AVAILABLE:
            result["account_status"] = status.name
        return result

    def health_response(self) -> dict[str, Any]:
        """Return the same JSON shape that main.py /health returns."""
        auth = self.state["auth_status"]
        client_ready = self.client is not None

        if self.state["initializing"]:
            status = "initializing"
        elif not client_ready:
            status = "no_client"
        else:
            status = "healthy"

        return {
            "status": status,
            "client_ready": client_ready,
            "auth_status": auth,
            "tier": self.detect_tier(),
            "needs_restart": self.state["needs_restart"],
            "last_error": self.state["last_error"],
            "last_error_type": self.state["last_error_type"],
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "model_levels": list(self.level_models()),
            "last_actual_model": self.last_actual_model,
            "model_events": self.model_events[-100:],
        }

    def level_models(self, clients=()) -> dict[str, Any]:
        levels = registry_levels(self.client)
        if not logged_in(self.client):
            return {}
        if client_capacity(self.client) == 1:
            return {"flash": best_flash_model(clients), **(
                {"pro": levels["pro"]} if self.pro_verified and "pro" in levels else {})}
        if self.pro_denied:
            levels.pop("pro", None)
        return levels if client_capacity(self.client) >= 2 else {}

    def record_model_event(self, message: str) -> None:
        self.model_events.append({"message": message, "at": time.time(), "seq": len(self.model_events) + 1})
        logger.info("slot %d %s", self.num, message)

    async def probe_free_pro(self) -> None:
        if self._pro_probe_done or client_capacity(self.client) != 1:
            return
        pro = registry_levels(self.client).get("pro")
        if pro is None:
            return
        self._pro_probe_done = True
        try:
            output = await self.client.generate_content("Hi", model=pro)
            label = actual_model(output)
            self.pro_verified = "pro" in label.lower()
            self.observe_actual("pro", label)
            await asyncio.to_thread(self.writeback_cookies)
        except Exception:
            self.pro_verified = False
            self.record_model_event("该号 Pro 探测失败，未进 pro 档")

    def observe_actual(self, level: str, label: str) -> None:
        self.last_actual_model = label
        if label != "unknown":
            self.actual_labels[level] = label
        if level == "pro" and "pro" not in label.lower():
            self.pro_verified = False
            self.pro_denied = True
            self.record_model_event(f"该号 Pro 不可用，实际给 {label}")

    def writeback_cookies(self, envs_dir: Path | None = None) -> bool:
        """Serialize file writes independently, without waiting on chat or refresh."""
        if not self._cookie_lock.acquire(blocking=False):
            return False
        try:
            return self._writeback_cookies(envs_dir)
        finally:
            self._cookie_lock.release()

    def _writeback_cookies(self, envs_dir: Path | None = None) -> bool:
        """Persist the current session jar only; no RPC, init or rotation."""
        now = time.monotonic()
        if self._last_cookie_write is not None and now - self._last_cookie_write < 600:
            return False
        path = self.env_path or (envs_dir / f"account{self.num}.env" if envs_dir else None)
        if not self.client or path is None:
            return False
        temporary = None
        try:
            cookies = {}
            for source in (getattr(self.client, "cookies", None),
                           getattr(getattr(self.client, "client", None), "cookies", None)):
                if source is not None:
                    jar = getattr(source, "jar", None)
                    cookies.update({c.name: c.value for c in jar} if jar is not None else dict(source))
            values = {"SECURE_1PSID": cookies.get("__Secure-1PSID"),
                      "SECURE_1PSIDTS": cookies.get("__Secure-1PSIDTS")}
            if not all(isinstance(v, str) and v and "\n" not in v and "\r" not in v for v in values.values()):
                return False
            with path.open(encoding="utf-8", newline="") as source:
                original = source.read()
            current = parse_env_file(path)
            if all(current.get(key) == value for key, value in values.items()):
                return False
            found = set()
            lines = []
            for line in original.splitlines(keepends=True):
                key = line.split("=", 1)[0].strip() if "=" in line else ""
                if key in values:
                    found.add(key)
                    ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                    line = f"{key}={values[key]}{ending}"
                lines.append(line)
            # A partial env file is not rewritten into an incomplete credential pair.
            if found != set(values):
                return False
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
                temporary = output.name
                os.chmod(temporary, path.stat().st_mode & 0o777)
                output.write("".join(lines))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            self._last_cookie_write = now
            self.psid, self.psidts = values["SECURE_1PSID"], values["SECURE_1PSIDTS"]
            logger.info("slot %d cookie 已写回", self.num)
            return True
        except Exception:
            logger.warning("slot %d cookie 写回失败", self.num)
            return False
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    # ── Error reporting ──────────────────────────────────────────────

    def report_error(self, e: Exception) -> None:
        """Classify error and update this slot's state."""
        from gemini_webapi.exceptions import (
            AuthError, RateLimitExceeded, UsageLimitExceeded,
            ImageGenerationBlocked, TemporarilyBlocked,
        )
        error_str = str(e)
        error_lower = error_str.lower()

        if isinstance(e, ImageGenerationBlocked):
            self.state["last_error_type"] = "image_blocked"
        elif isinstance(e, AuthError):
            self.state["last_error_type"] = "cookie_expired"
        elif isinstance(e, UsageLimitExceeded):
            self.state["last_error_type"] = "usage_limit"
        elif isinstance(e, RateLimitExceeded):
            self.state["last_error_type"] = "rate_limit"
        elif isinstance(e, TemporarilyBlocked):
            self.state["last_error_type"] = "temporarily_blocked"
        elif "tls" in error_lower or "ssl" in error_lower or "curl: (35)" in error_lower:
            self.state["last_error_type"] = "tls_error"
            self.state["_tls_fail_count"] += 1
            if self.state["_tls_fail_count"] >= _TLS_RESTART_THRESHOLD:
                self.state["needs_restart"] = True
        elif any(kw in error_lower for kw in ["can't generate more videos", "video generation isn't available"]):
            self.state["last_error_type"] = "video_quota"
        elif any(kw in error_lower for kw in ['401', '403', 'cookie', 'expired']):
            self.state["last_error_type"] = "cookie_expired"
        elif '429' in error_lower or 'rate limit' in error_lower:
            self.state["last_error_type"] = "rate_limit"
        else:
            self.state["last_error_type"] = "unknown"

        self.state["last_error"] = error_str[:200]

        if self.state["last_error_type"] != "tls_error":
            self.state["_tls_fail_count"] = 0

        logger.info("Slot %d error: type=%s msg=%s", self.num, self.state['last_error_type'], error_str[:100])

    # ── Edit sessions ────────────────────────────────────────────────

    def cleanup_expired_sessions(self) -> None:
        now = time.time()
        expired = [sid for sid, (_, ts) in self.edit_sessions.items() if now - ts > _SESSION_TTL]
        for sid in expired:
            del self.edit_sessions[sid]
        if len(self.edit_sessions) > _MAX_SESSIONS:
            by_age = sorted(self.edit_sessions.items(), key=lambda x: x[1][1])
            for sid, _ in by_age[:len(self.edit_sessions) - _MAX_SESSIONS]:
                del self.edit_sessions[sid]
