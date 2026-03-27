"""Tracer protocol for observing GeminiClient request lifecycle.

Implementations receive callbacks at each stage of a request.
The tracer is passed per-call (not per-client) to ensure concurrent request isolation.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# Header whitelist for on_response_meta — only these are passed to tracer.
# Prevents leaking set-cookie, authorization, and other sensitive headers.
_SAFE_HEADER_PREFIXES = ("content-type", "x-goog-", "date", "server")
_BLOCKED_HEADERS = frozenset({"set-cookie", "authorization", "cookie", "www-authenticate", "proxy-authorization"})


def sanitize_headers(raw_headers: dict[str, str] | Any) -> dict[str, str]:
    """Filter response headers to a safe whitelist before passing to tracer."""
    if not isinstance(raw_headers, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in raw_headers.items():
        lower_key = key.lower()
        if lower_key in _BLOCKED_HEADERS:
            continue
        if any(lower_key.startswith(prefix) for prefix in _SAFE_HEADER_PREFIXES):
            result[key] = value
    return result


@runtime_checkable
class Tracer(Protocol):
    """Protocol for observing GeminiClient._generate() lifecycle.

    Each method corresponds to a stage in the request pipeline.
    Implementations should be lightweight — callbacks run inline on the request path.
    """

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
        """Called once before the first HTTP request (before polling loop)."""
        ...

    def on_response_meta(
        self,
        *,
        status_code: int,
        headers: dict[str, str],
        poll_iteration: int,
    ) -> None:
        """Called after each HTTP response is received (may be called multiple times during polling)."""
        ...

    def on_stream_frame(
        self,
        *,
        part: list[Any],
        part_json: list[Any] | None,
        flags: dict[str, Any],
    ) -> None:
        """Called for each parsed stream frame.

        part: the raw frame structure (list, not str/bytes — this is post parse_response_by_frame)
        part_json: inner JSON parsed from part[2], or None if parse failed
        flags: snapshot of current _StreamFlags as dict
        """
        ...

    def on_request_end(
        self,
        *,
        status: str,
        error: str | None,
        final_flags: dict[str, Any] | None,
        chat_metadata_after: list[Any] | None,
        poll_iterations: int,
    ) -> None:
        """Called once when the request completes (success or failure).

        status: one of "ok", "http_error", "stalled", "incomplete", "timeout",
                "request_error", "api_error", "parse_error"
        """
        ...

    def get_snapshot(self) -> dict[str, Any] | None:
        """Return the accumulated capture data for this request."""
        ...
