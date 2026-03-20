import os
import time
from pathlib import Path

from curl_cffi import CurlHttpVersion
from curl_cffi.requests import AsyncSession, Cookies

from ..constants import Endpoint, Headers
from ..exceptions import AuthError


def _get_secure_1psid(cookies: dict | Cookies) -> str | None:
    """Safely extract __Secure-1PSID from cookies, preferring .google.com domain."""
    if isinstance(cookies, Cookies):
        return cookies.get("__Secure-1PSID", domain=".google.com") or cookies.get("__Secure-1PSID")
    return cookies.get("__Secure-1PSID")


def _get_cache_dir() -> Path:
    """Get the cache directory for cookie storage."""
    gemini_cookie_path = os.getenv("GEMINI_COOKIE_PATH")
    if gemini_cookie_path:
        return Path(gemini_cookie_path)
    return Path(__file__).parent / "temp"


def _is_cache_fresh(cache_file: Path, max_age_seconds: int = 60) -> bool:
    """Check if cache file exists and was modified within max_age_seconds."""
    if not cache_file.is_file():
        return False
    return time.time() - cache_file.stat().st_mtime <= max_age_seconds


async def rotate_1psidts(cookies: dict | Cookies, proxy: str | None = None) -> tuple[str | None, Cookies | None]:
    """
    Refresh the __Secure-1PSIDTS cookie and store the refreshed cookie value in cache file.

    Parameters
    ----------
    cookies : `dict | curl_cffi.requests.Cookies`
        Cookies to be used in the request.
    proxy: `str`, optional
        Proxy URL.

    Returns
    -------
    `tuple[str | None, curl_cffi.requests.Cookies | None]`
        New value of the __Secure-1PSIDTS cookie and the full updated cookies jar.

    Raises
    ------
    `gemini_webapi.AuthError`
        If request failed with 401 Unauthorized.
    `curl_cffi.requests.errors.RequestsError`
        If request failed with other status codes.
    """
    cache_dir = _get_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    secure_1psid = _get_secure_1psid(cookies)
    if not secure_1psid:
        return None, None

    cache_file = cache_dir / f".cached_1psidts_{secure_1psid}.txt"

    # Return cached value if fresh (avoids 429 Too Many Requests)
    if _is_cache_fresh(cache_file, max_age_seconds=60):
        return cache_file.read_text(), None

    # Request new cookie rotation
    async with AsyncSession(
        proxy=proxy,
        impersonate="chrome",
        http_version=CurlHttpVersion.V2_0,
    ) as client:
        client.headers.update(Headers.ROTATE_COOKIES.value)
        if isinstance(cookies, dict):
            client.cookies = Cookies(cookies)
        else:
            client.cookies = cookies

        response = await client.post(
            url=Endpoint.ROTATE_COOKIES,
            data='[000,"-0000000000000000000"]',
        )

        if response.status_code == 401:
            raise AuthError("Cookie rotation failed with 401 Unauthorized")
        response.raise_for_status()

        new_1psidts = response.cookies.get("__Secure-1PSIDTS")
        if new_1psidts:
            cache_file.write_text(new_1psidts)
            cache_file.chmod(0o600)  # Restrict cookie cache to owner read/write only
            return new_1psidts, response.cookies

        return None, response.cookies
