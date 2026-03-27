"""Per-request raw capture tracer for OneClick worker containers.

Implements the gemini_webapi.Tracer protocol.
Each instance tracks a single request lifecycle — create a new one per call.
"""

import time
from typing import Any


_MAX_FRAMES = 18


class RawCaptureTracer:
    """Captures structured request/response data for a single Gemini API call."""

    __slots__ = ("_capture",)

    def __init__(self) -> None:
        self._capture: dict[str, Any] | None = None

    def on_request_start(
        self,
        *,
        prompt: str,
        model_name: str,
        params: dict[str, Any],
        request_data_preview: str,
        chat_metadata: list[Any],
        use_pro: bool,
        file_count: int,
    ) -> None:
        self._capture = {
            "started_at": time.time(),
            "prompt_preview": prompt[:200],
            "model_name": model_name,
            "params": {k: str(v)[:80] for k, v in params.items()},
            "request_data_preview": request_data_preview[:300],
            "chat_metadata_before": chat_metadata,
            "use_pro": use_pro,
            "file_count": file_count,
            "frames": [],
            "poll_iterations": 0,
            "status": "in_progress",
        }

    def on_response_meta(
        self,
        *,
        status_code: int,
        headers: dict[str, str],
        poll_iteration: int,
    ) -> None:
        if self._capture is None:
            return
        self._capture["poll_iterations"] = poll_iteration
        self._capture["response_status_code"] = status_code
        self._capture["response_headers"] = headers

    def on_stream_frame(
        self,
        *,
        part: list[Any],
        part_json: list[Any] | None,
        flags: dict[str, Any],
    ) -> None:
        if self._capture is None:
            return
        frames = self._capture["frames"]
        if len(frames) >= _MAX_FRAMES:
            return
        frames.append({
            "part": part,
            "part_json": part_json,
            "flags": dict(flags),
        })

    def on_request_end(
        self,
        *,
        status: str,
        error: str | None,
        final_flags: dict[str, Any] | None,
        chat_metadata_after: list[Any] | None,
        poll_iterations: int,
    ) -> None:
        if self._capture is None:
            return
        self._capture["finished_at"] = time.time()
        self._capture["status"] = status
        self._capture["poll_iterations"] = poll_iterations
        if error:
            self._capture["error"] = error
        if final_flags:
            self._capture["final_flags"] = final_flags
        if chat_metadata_after is not None:
            self._capture["chat_metadata_after"] = chat_metadata_after

    def get_snapshot(self) -> dict[str, Any] | None:
        return self._capture
