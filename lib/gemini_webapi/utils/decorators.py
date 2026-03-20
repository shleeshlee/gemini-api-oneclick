import asyncio
import functools
import inspect
from collections.abc import AsyncGenerator, Callable
from typing import Any, TypeVar

from ..exceptions import APIError

DELAY_FACTOR = 5
T = TypeVar("T")


async def _ensure_client_running(client: Any, func_name: str) -> None:
    """Ensure the client is initialized and running."""
    if client._running:
        return

    await client.init(
        timeout=client.timeout,
        auto_close=client.auto_close,
        close_delay=client.close_delay,
        auto_refresh=client.auto_refresh,
        refresh_interval=client.refresh_interval,
        verbose=client.verbose,
        watchdog_timeout=client.watchdog_timeout,
    )

    if not client._running:
        raise APIError(f"Invalid function call: GeminiClient.{func_name}. Client initialization failed.")


def _calculate_retry_delay(retry_max: int, current_retry: int) -> float:
    """Calculate exponential backoff delay for retries."""
    return (retry_max - current_retry + 1) * DELAY_FACTOR


def running(retry: int = 0) -> Callable:
    """
    Decorator to check if GeminiClient is running before making a request.
    Supports both regular async functions and async generators.

    Parameters
    ----------
    retry: `int`, optional
        Max number of retries when `gemini_webapi.APIError` is raised.
    """

    def decorator(func: Callable) -> Callable:
        if inspect.isasyncgenfunction(func):
            return _wrap_async_generator(func, retry)
        return _wrap_async_function(func, retry)

    return decorator


def _wrap_async_generator(func: Callable, retry_max: int) -> Callable:
    """Wrap an async generator function with retry logic."""

    @functools.wraps(func)
    async def wrapper(client: Any, *args: Any, current_retry: int | None = None, **kwargs: Any) -> AsyncGenerator:
        retries_remaining = retry_max if current_retry is None else current_retry

        try:
            await _ensure_client_running(client, func.__name__)
            async for item in func(client, *args, **kwargs):
                yield item
        except APIError:
            if retries_remaining <= 0:
                raise

            delay = _calculate_retry_delay(retry_max, retries_remaining)
            await asyncio.sleep(delay)

            async for item in wrapper(client, *args, current_retry=retries_remaining - 1, **kwargs):
                yield item

    return wrapper


def _wrap_async_function(func: Callable, retry_max: int) -> Callable:
    """Wrap a regular async function with retry logic."""

    @functools.wraps(func)
    async def wrapper(client: Any, *args: Any, current_retry: int | None = None, **kwargs: Any) -> Any:
        retries_remaining = retry_max if current_retry is None else current_retry

        try:
            await _ensure_client_running(client, func.__name__)
            return await func(client, *args, **kwargs)
        except APIError:
            if retries_remaining <= 0:
                raise

            delay = _calculate_retry_delay(retry_max, retries_remaining)
            await asyncio.sleep(delay)
            return await wrapper(client, *args, current_retry=retries_remaining - 1, **kwargs)

    return wrapper
