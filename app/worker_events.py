import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class WorkerMediaHints(BaseModel):
    image_count: int = 0
    video_count: int = 0
    has_thoughts: bool = False
    sample_image_urls: list[str] = Field(default_factory=list)
    sample_video_urls: list[str] = Field(default_factory=list)


class WorkerRawEvent(BaseModel):
    event_id: str
    timestamp: int
    endpoint: str
    requested_model: str = ""
    resolved_model: str = ""
    header_family: str = ""
    header_token: str = ""
    model_resolution: str = ""
    chat_id: str = ""
    session_id: str = ""
    raw_response_ref: str = ""
    raw_response_preview: str = ""
    media_hints: WorkerMediaHints = Field(default_factory=WorkerMediaHints)
    error_summary: str = ""


STATE_DIR = Path(os.environ.get("ONECLICK_STATE_DIR", "/app/state")) / "worker-events"


def _sample_media_urls(items: list[Any], limit: int = 2) -> list[str]:
    urls: list[str] = []
    for item in items[:limit]:
        url = getattr(item, "url", "") or ""
        if url:
            urls.append(url[:160])
    return urls


def build_worker_event(
    endpoint: str,
    trace_headers: dict[str, str] | None = None,
    gemini_response: Any | None = None,
    *,
    chat_id: str = "",
    session_id: str = "",
    raw_response_ref: str = "",
    raw_response_preview: str = "",
    error_summary: str = "",
) -> WorkerRawEvent:
    trace_headers = trace_headers or {}
    images = list(getattr(gemini_response, "images", []) or [])
    videos = list(getattr(gemini_response, "videos", []) or [])
    thoughts = getattr(gemini_response, "thoughts", None)

    if not raw_response_preview and gemini_response is not None:
        preview_parts: list[str] = []
        text = getattr(gemini_response, "text", None)
        if text:
            preview_parts.append(text[:240])
        if thoughts:
            preview_parts.append(f"<think>{str(thoughts)[:120]}</think>")
        raw_response_preview = "\n".join(preview_parts)[:400]

    return WorkerRawEvent(
        event_id=f"wrk-{uuid.uuid4().hex[:12]}",
        timestamp=int(time.time()),
        endpoint=endpoint,
        requested_model=trace_headers.get("X-OneClick-Requested-Model", ""),
        resolved_model=trace_headers.get("X-OneClick-Resolved-Model", ""),
        header_family=trace_headers.get("X-OneClick-Header-Family", ""),
        header_token=trace_headers.get("X-OneClick-Header-Token", ""),
        model_resolution=trace_headers.get("X-OneClick-Model-Resolution", ""),
        chat_id=chat_id,
        session_id=session_id,
        raw_response_ref=raw_response_ref,
        raw_response_preview=raw_response_preview,
        media_hints=WorkerMediaHints(
            image_count=len(images),
            video_count=len(videos),
            has_thoughts=bool(thoughts),
            sample_image_urls=_sample_media_urls(images),
            sample_video_urls=_sample_media_urls(videos),
        ),
        error_summary=error_summary,
    )


def build_worker_event_headers(event: WorkerRawEvent) -> dict[str, str]:
    return {
        "X-OneClick-Worker-Event-ID": event.event_id,
    }


def build_gemini_response_snapshot(gemini_response: Any | None) -> dict[str, Any]:
    if gemini_response is None:
        return {}

    candidates: list[dict[str, Any]] = []
    for candidate in getattr(gemini_response, "candidates", []) or []:
        candidates.append(
            {
                "rcid": getattr(candidate, "rcid", ""),
                "text": getattr(candidate, "text", ""),
                "thoughts": getattr(candidate, "thoughts", None),
                "web_images": [
                    {
                        "url": getattr(image, "url", ""),
                        "title": getattr(image, "title", ""),
                        "alt": getattr(image, "alt", ""),
                    }
                    for image in (getattr(candidate, "web_images", None) or [])
                ],
                "generated_images": [
                    {
                        "url": getattr(image, "url", ""),
                        "title": getattr(image, "title", ""),
                        "alt": getattr(image, "alt", ""),
                        "account_index": getattr(image, "account_index", 0),
                    }
                    for image in (getattr(candidate, "generated_images", None) or [])
                ],
                "generated_videos": [
                    {
                        "url": getattr(video, "url", ""),
                        "thumbnail_url": getattr(video, "thumbnail_url", ""),
                        "title": getattr(video, "title", ""),
                        "account_index": getattr(video, "account_index", 0),
                    }
                    for video in (getattr(candidate, "generated_videos", None) or [])
                ],
            }
        )

    return {
        "metadata": list(getattr(gemini_response, "metadata", []) or []),
        "chosen": getattr(gemini_response, "chosen", 0),
        "text": getattr(gemini_response, "text", ""),
        "thoughts": getattr(gemini_response, "thoughts", None),
        "rcid": getattr(gemini_response, "rcid", ""),
        "candidates": candidates,
    }


def persist_worker_event(
    event: WorkerRawEvent,
    *,
    payload: dict[str, Any] | None = None,
) -> WorkerRawEvent:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    event_file = STATE_DIR / f"{event.event_id}.event.json"
    payload_file = STATE_DIR / f"{event.event_id}.payload.json"

    updated_event = event
    if payload is not None:
        payload_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        updated_event = event.model_copy(update={"raw_response_ref": str(payload_file)})

    event_file.write_text(updated_event.model_dump_json(indent=2), encoding="utf-8")
    return updated_event
