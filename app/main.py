import asyncio
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from gemini_webapi import GeminiClient, set_log_level

set_log_level("INFO")

app = FastAPI(title="Gemini API OneClick")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SECURE_1PSID = os.getenv("SECURE_1PSID", "")
SECURE_1PSIDTS = os.getenv("SECURE_1PSIDTS", "")
API_KEY = os.getenv("API_KEY", "")

_client: Optional[GeminiClient] = None
_client_lock = asyncio.Lock()


class Message(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]


class ChatRequest(BaseModel):
    model: str
    messages: List[Message]
    stream: Optional[bool] = False


def _extract_text(resp: Any) -> str:
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        if "text" in resp:
            return str(resp["text"])
        if "content" in resp:
            return str(resp["content"])
        return str(resp)
    text = getattr(resp, "text", None)
    if text is not None:
        return str(text)
    return str(resp)


def _build_prompt(messages: List[Message]) -> str:
    chunks = []
    for m in messages:
        if isinstance(m.content, str):
            content = m.content
        else:
            content = "\n".join(
                part.get("text", "") for part in m.content if isinstance(part, dict)
            )
        chunks.append(f"{m.role}: {content}")
    return "\n".join(chunks)


async def verify_api_key(authorization: str = Header(default=None)):
    if not API_KEY:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    try:
        scheme, token = authorization.split()
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid Authorization format") from exc
    if scheme.lower() != "bearer" or token != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


async def get_client() -> GeminiClient:
    global _client
    async with _client_lock:
        if _client is not None:
            return _client
        if not SECURE_1PSID or not SECURE_1PSIDTS:
            raise HTTPException(status_code=503, detail="Gemini credentials not configured")
        cli = GeminiClient(SECURE_1PSID, SECURE_1PSIDTS)
        await cli.init(timeout=180)
        _client = cli
        return cli


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "status": "healthy" if (SECURE_1PSID and SECURE_1PSIDTS) else "degraded",
        "credentials_ready": bool(SECURE_1PSID and SECURE_1PSIDTS),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/v1/models")
async def models() -> Dict[str, Any]:
    ts = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": "gemini-2.5-pro", "object": "model", "created": ts, "owned_by": "google"},
            {"id": "gemini-2.5-flash", "object": "model", "created": ts, "owned_by": "google"},
        ],
    }


@app.post("/v1/chat/completions")
async def chat(req: ChatRequest, _: None = Depends(verify_api_key)):
    client = await get_client()
    prompt = _build_prompt(req.messages)

    try:
        if hasattr(client, "chat"):
            resp = await client.chat(model=req.model, prompt=prompt)
        elif hasattr(client, "generate_content"):
            resp = await client.generate_content(prompt=prompt, model=req.model)
        else:
            raise RuntimeError("gemini-webapi client method not found")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}") from exc

    text = _extract_text(resp)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
