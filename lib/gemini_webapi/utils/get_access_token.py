import asyncio
import os
import re
from asyncio import Task
from pathlib import Path

from curl_cffi import CurlHttpVersion
from curl_cffi.requests import AsyncSession, Cookies, Response

from ..constants import Endpoint, Headers
from ..exceptions import AuthError
from .logger import logger


async def send_request(
    cookies: dict | Cookies,
    proxy: str | None = None,
    account_index: int = 0,
    session: AsyncSession | None = None,
) -> tuple[Response | None, Cookies]:
    """Send http request with provided cookies using provided session or creating a new one."""
    if session is not None:
        # Use provided session - just update cookies
        if isinstance(cookies, dict):
            session.cookies = Cookies(cookies)
        else:
            session.cookies = cookies

        init_url = Endpoint.get_init_url(account_index)
        response = await session.get(init_url)
        response.raise_for_status()

        # Check if redirected to consent page - means cookies are expired/invalid
        final_url = str(response.url)
        if "consent.google.com" in final_url:
            raise AuthError(
                f"Redirected to Google consent page. This typically means your cookies are expired or invalid "
                f"for account index {account_index}. Please update your __Secure-1PSID and __Secure-1PSIDTS cookies."
            )

        return response, session.cookies

    # Create a new temporary session (fallback for standalone usage)
    async with AsyncSession(
        proxy=proxy,
        allow_redirects=True,
        impersonate="chrome",
        http_version=CurlHttpVersion.V2_0,
    ) as client:
        client.headers.update(Headers.GEMINI.value)
        if isinstance(cookies, dict):
            client.cookies = Cookies(cookies)
        else:
            client.cookies = cookies

        init_url = Endpoint.get_init_url(account_index)
        response = await client.get(init_url)
        response.raise_for_status()

        # Check if redirected to consent page - means cookies are expired/invalid
        final_url = str(response.url)
        if "consent.google.com" in final_url:
            raise AuthError(
                f"Redirected to Google consent page. This typically means your cookies are expired or invalid "
                f"for account index {account_index}. Please update your __Secure-1PSID and __Secure-1PSIDTS cookies."
            )

        return response, client.cookies


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


def _create_cookie_jar_with_base(extra_cookies: Cookies, base_cookies: dict | Cookies) -> Cookies:
    """Create a new cookie jar merging extra and base cookies."""
    jar = Cookies(extra_cookies)
    jar.update(base_cookies)
    return jar


def _add_base_cookie_task(
    tasks: list[Task],
    base_cookies: dict | Cookies,
    extra_cookies: Cookies,
    proxy: str | None,
    verbose: bool,
    account_index: int = 0,
    session: AsyncSession | None = None,
) -> None:
    """Add task for base cookies if both required cookies are present."""
    has_psid = "__Secure-1PSID" in base_cookies
    has_psidts = "__Secure-1PSIDTS" in base_cookies

    if has_psid and has_psidts:
        jar = _create_cookie_jar_with_base(extra_cookies, base_cookies)
        tasks.append(Task(send_request(jar, proxy=proxy, account_index=account_index, session=session)))
    elif verbose:
        logger.debug("Skipping loading base cookies. Either __Secure-1PSID or __Secure-1PSIDTS is not provided.")


def _add_cached_cookie_tasks(
    tasks: list[Task],
    base_cookies: dict | Cookies,
    extra_cookies: Cookies,
    cache_dir: Path,
    secure_1psid: str | None,
    proxy: str | None,
    verbose: bool,
    account_index: int = 0,
    session: AsyncSession | None = None,
) -> None:
    """Add tasks for cached cookie files."""
    if secure_1psid:
        _add_single_cached_cookie_task(tasks, base_cookies, extra_cookies, cache_dir, secure_1psid, proxy, verbose, account_index, session=session)
    else:
        _add_all_cached_cookie_tasks(tasks, extra_cookies, cache_dir, proxy, verbose, account_index, session=session)


def _add_single_cached_cookie_task(
    tasks: list[Task],
    base_cookies: dict | Cookies,
    extra_cookies: Cookies,
    cache_dir: Path,
    secure_1psid: str,
    proxy: str | None,
    verbose: bool,
    account_index: int = 0,
    session: AsyncSession | None = None,
) -> None:
    """Add task for a specific cached cookie file matching the provided PSID."""
    cache_file = cache_dir / f".cached_1psidts_{secure_1psid}.txt"

    if not cache_file.is_file():
        if verbose:
            logger.debug("Skipping loading cached cookies. Cache file not found.")
        return

    cached_1psidts = cache_file.read_text()
    if not cached_1psidts:
        if verbose:
            logger.debug("Skipping loading cached cookies. Cache file is empty.")
        return

    jar = _create_cookie_jar_with_base(extra_cookies, base_cookies)
    jar.set("__Secure-1PSIDTS", cached_1psidts, domain=".google.com")
    tasks.append(Task(send_request(jar, proxy=proxy, account_index=account_index, session=session)))


def _add_all_cached_cookie_tasks(
    tasks: list[Task],
    extra_cookies: Cookies,
    cache_dir: Path,
    proxy: str | None,
    verbose: bool,
    account_index: int = 0,
    session: AsyncSession | None = None,
) -> None:
    """Add tasks for all valid cached cookie files."""
    valid_caches = 0

    for cache_file in cache_dir.glob(".cached_1psidts_*.txt"):
        cached_1psidts = cache_file.read_text()
        if not cached_1psidts:
            continue

        jar = Cookies(extra_cookies)
        psid = cache_file.stem[16:]  # Extract PSID from filename
        jar.set("__Secure-1PSID", psid, domain=".google.com")
        jar.set("__Secure-1PSIDTS", cached_1psidts, domain=".google.com")
        tasks.append(Task(send_request(jar, proxy=proxy, account_index=account_index, session=session)))
        valid_caches += 1

    if valid_caches == 0 and verbose:
        logger.debug("Skipping loading cached cookies. Cookies will be cached after successful initialization.")


def _extract_tokens_from_response(response_text: str) -> tuple[str | None, str | None, str | None]:
    """Extract SNlM0e, cfb2h, and FdrFJe tokens from response HTML.

    Google removed SNlM0e from the page around Feb 2025, but cfb2h and FdrFJe
    are still present and sufficient for API calls. Returns empty string for
    SNlM0e if not found, which works correctly with the API.
    """
    snlm0e_match = re.search(r'"SNlM0e":\s*"(.*?)"', response_text)
    cfb2h_match = re.search(r'"cfb2h":\s*"(.*?)"', response_text)
    fdrfje_match = re.search(r'"FdrFJe":\s*"(.*?)"', response_text)

    # Return None tuple if no tokens found at all
    if not (snlm0e_match or cfb2h_match or fdrfje_match):
        return None, None, None

    return (
        snlm0e_match.group(1) if snlm0e_match else "",
        cfb2h_match.group(1) if cfb2h_match else None,
        fdrfje_match.group(1) if fdrfje_match else None,
    )


async def get_access_token(
    base_cookies: dict | Cookies,
    proxy: str | None = None,
    verbose: bool = False,
    account_index: int = 0,
    session: AsyncSession | None = None,
) -> tuple[str, str | None, str | None, Cookies]:
    """
    Send a get request to gemini.google.com for each group of available cookies and return
    the value of "SNlM0e" as access token on the first successful request.

    Possible cookie sources:
    - Base cookies passed to the function.
    - __Secure-1PSID from base cookies with __Secure-1PSIDTS from cache.

    Parameters
    ----------
    base_cookies : `dict | curl_cffi.requests.Cookies`
        Base cookies to be used in the request.
    proxy: `str`, optional
        Proxy URL.
    verbose: `bool`, optional
        If `True`, will print more information in logs.
    account_index: `int`, optional
        Google account index to use when multiple accounts are signed in.
        Corresponds to the /u/{index}/ path in Google URLs (e.g., /u/0/, /u/1/, /u/2/).
        Defaults to 0 (first account).
    session: `AsyncSession`, optional
        Existing session to reuse for requests.

    Returns
    -------
    `tuple[str, str | None, str | None, Cookies]`
        By order: access token; build label; session id; cookies of the successful request.

    Raises
    ------
    `gemini_webapi.AuthError`
        If all requests failed.
    """
    # Fetch initial cookies from google.com
    if session is not None:
        response = await session.get(Endpoint.GOOGLE)
    else:
        async with AsyncSession(
            proxy=proxy,
            allow_redirects=True,
            impersonate="chrome",
            http_version=CurlHttpVersion.V2_0,
        ) as client:
            response = await client.get(Endpoint.GOOGLE)

    extra_cookies = response.cookies if response.status_code == 200 else Cookies()
    cache_dir = _get_cache_dir()
    secure_1psid = _get_secure_1psid(base_cookies)

    # Collect authentication tasks from various sources
    tasks: list[Task] = []
    _add_base_cookie_task(tasks, base_cookies, extra_cookies, proxy, verbose, account_index, session=session)
    _add_cached_cookie_tasks(tasks, base_cookies, extra_cookies, cache_dir, secure_1psid, proxy, verbose, account_index, session=session)

    if not tasks:
        raise AuthError("No valid cookies available for initialization. Please pass __Secure-1PSID and __Secure-1PSIDTS manually.")

    # Try each authentication method until one succeeds
    for i, future in enumerate(asyncio.as_completed(tasks)):
        try:
            response, request_cookies = await future
            snlm0e, cfb2h, fdrfje = _extract_tokens_from_response(response.text)

            # Success if any token is found (SNlM0e was removed by Google around Feb 2025)
            if snlm0e is not None or cfb2h or fdrfje:
                if verbose:
                    logger.debug(f"Init attempt ({i + 1}/{len(tasks)}) succeeded. Initializing client...")
                return snlm0e, cfb2h, fdrfje, request_cookies

            if verbose:
                logger.debug(f"Init attempt ({i + 1}/{len(tasks)}) failed. Cookies invalid.")
        except Exception as e:
            if verbose:
                logger.debug(f"Init attempt ({i + 1}/{len(tasks)}) failed with error: {e}")

    raise AuthError(f"Failed to initialize client. SECURE_1PSIDTS could get expired frequently, please make sure cookie values are up to date. (Failed initialization attempts: {len(tasks)})")
