from typing import Any, Awaitable, Callable

from .raw_capture import collect_generated_image_urls


ImageDownloader = Callable[[Any], Awaitable[str | None]]


async def parse_image_generation_result(
    images: list[Any],
    *,
    limit: int,
    image_downloader: ImageDownloader,
    raw_capture: dict[str, Any] | None = None,
) -> tuple[list[dict[str, str]], list[str]]:
    result_data: list[dict[str, str]] = []
    for image in images[:limit]:
        b64 = await image_downloader(image)
        if b64:
            result_data.append({"b64_json": b64})
    return result_data, collect_generated_image_urls(raw_capture)
