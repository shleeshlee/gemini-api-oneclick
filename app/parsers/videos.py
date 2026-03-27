from typing import Any, Awaitable, Callable

from .raw_capture import collect_generated_video_urls


VideoDownloader = Callable[[Any], Awaitable[str | None]]


async def parse_video_generation_result(
    videos: list[Any],
    *,
    video_downloader: VideoDownloader,
    raw_capture: dict[str, Any] | None = None,
) -> tuple[list[dict[str, str]], list[str]]:
    result_data: list[dict[str, str]] = []
    for video in videos:
        entry = {
            "url": getattr(video, "url", "") or "",
            "thumbnail_url": getattr(video, "thumbnail_url", "") or "",
        }
        b64 = await video_downloader(video)
        if b64:
            entry["b64_json"] = b64
        result_data.append(entry)
    return result_data, collect_generated_video_urls(raw_capture)
