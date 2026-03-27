import json
import asyncio
import re
from typing import Any, AsyncGenerator, Awaitable, Callable

from .raw_capture import build_snapshot_from_raw_capture


ImageDownloader = Callable[[Any], Awaitable[str | None]]


def _normalize_reply_text(text: str) -> str:
    text = text.replace("&lt;", "<").replace("\\<", "<").replace("\\_", "_").replace("\\>", ">")
    text = text.replace("\\#", "#").replace("\\!", "!").replace("\\|", "|")
    return re.sub(r"```\s*\n(<[a-zA-Z][\s\S]*?)\n```", r"\1", text)


async def build_chat_reply_text(
    gemini_response: Any | None,
    *,
    image_downloader: ImageDownloader,
    markdown_corrector: Callable[[str], str],
    raw_capture: dict[str, Any] | None = None,
) -> str:
    raw_snapshot = build_snapshot_from_raw_capture(raw_capture)
    reply_text = ""
    thoughts = raw_snapshot.get("thoughts")
    text = raw_snapshot.get("text") or ""
    if not text and gemini_response is not None:
        thoughts = getattr(gemini_response, "thoughts", None)
        if getattr(gemini_response, "text", None):
            text = gemini_response.text
        else:
            text = str(gemini_response)

    if thoughts:
        reply_text += f"<think>{thoughts}</think>"
    reply_text += text

    images = getattr(gemini_response, "images", None) or []
    for idx, image in enumerate(images):
        b64 = await image_downloader(image)
        if b64:
            reply_text += f"\n\n![generated_image_{idx}](data:image/png;base64,{b64})"

    reply_text = markdown_corrector(_normalize_reply_text(reply_text))
    if not reply_text.strip():
        return "Empty response from Gemini. Please check if your cookie is still valid."
    return reply_text


async def iter_chat_stream_chunks(
    *,
    completion_id: str,
    created_time: int,
    model: str,
    reply_text: str,
    chunk_size: int = 10,
) -> AsyncGenerator[str, None]:
    first_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created_time,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(first_chunk)}\n\n"

    for i in range(0, len(reply_text), chunk_size):
        chunk = reply_text[i:i + chunk_size]
        data = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created_time,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(data)}\n\n"
        await asyncio.sleep(0.02)

    last_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created_time,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(last_chunk)}\n\n"
    yield "data: [DONE]\n\n"


def build_chat_completion_payload(
    *,
    completion_id: str,
    created_time: int,
    model: str,
    reply_text: str,
    conversation: str,
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created_time,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": reply_text}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": len(conversation) // 4,
            "completion_tokens": len(reply_text) // 4,
            "total_tokens": (len(conversation) + len(reply_text)) // 4,
        },
    }
