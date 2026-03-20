import re
from datetime import datetime
from pathlib import Path

from curl_cffi import CurlHttpVersion
from curl_cffi.requests import AsyncSession, Cookies
from pydantic import BaseModel, ConfigDict, field_validator

from ..utils import logger


class HTTPError(Exception):
    """HTTP error for compatibility."""

    pass


class Image(BaseModel):
    """
    A single image object returned from Gemini.

    Parameters
    ----------
    url: `str`
        URL of the image.
    title: `str`, optional
        Title of the image, by default is "[Image]".
    alt: `str`, optional
        Optional description of the image.
    proxy: `str`, optional
        Proxy used when saving image.
    session_kwargs: `dict`, optional
        Additional kwargs to pass to the HTTP session (e.g., verify=False).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str
    title: str = "[Image]"
    alt: str = ""
    proxy: str | None = None
    session_kwargs: dict = {}

    def __str__(self):
        return f"Image(title='{self.title}', alt='{self.alt}', url='{(len(self.url) <= 20 and self.url) or self.url[:8] + '...' + self.url[-12:]}')"

    async def save(
        self,
        path: str = "temp",
        filename: str | None = None,
        cookies: dict | Cookies | None = None,
        verbose: bool = False,
        skip_invalid_filename: bool = False,
    ) -> str | None:
        """
        Save the image to disk.

        Parameters
        ----------
        path: `str`, optional
            Path to save the image, by default will save to "./temp".
        filename: `str`, optional
            File name to save the image, by default will use the original file name from the URL.
        cookies: `dict`, optional
            Cookies used for requesting the content of the image.
        verbose : `bool`, optional
            If True, will print the path of the saved file or warning for invalid file name, by default False.
        skip_invalid_filename: `bool`, optional
            If True, will only save the image if the file name and extension are valid, by default False.

        Returns
        -------
        `str | None`
            Absolute path of the saved image if successful, None if filename is invalid and `skip_invalid_filename` is True.

        Raises
        ------
        `HTTPError`
            If the network request failed.
        """

        filename = filename or self.url.split("/")[-1].split("?")[0]
        match = re.search(r"^(.*\.\w+)", filename)
        if match:
            filename = match.group()
        else:
            if verbose:
                logger.warning(f"Invalid filename: {filename}")
            if skip_invalid_filename:
                return None

        # Use image-specific headers that match browser behavior
        headers = {
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Origin": "https://gemini.google.com",
            "Referer": "https://gemini.google.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
        }
        download_url = self.url

        # Copy cookies with additional domains for Google image CDN
        # Cookies are needed for the intermediate redirect to lh3.google.com
        download_cookies = Cookies()
        if cookies:
            if isinstance(cookies, dict):
                # Dict cookies - set for google.com (covers lh3.google.com)
                for name, value in cookies.items():
                    download_cookies.set(name, value, domain=".google.com")
            elif hasattr(cookies, "jar"):
                # Cookies object with jar attribute
                for cookie in cookies.jar:
                    if cookie.value is not None:
                        download_cookies.set(cookie.name, cookie.value, domain=cookie.domain)
            else:
                # Assume it's already a Cookies object, copy it
                download_cookies = cookies

        async with AsyncSession(
            proxy=self.proxy,
            allow_redirects=True,
            impersonate="chrome",
            http_version=CurlHttpVersion.V2_0,
            **self.session_kwargs,
        ) as client:
            client.headers.update(headers)
            client.cookies = download_cookies
            # Google uses text-based redirects where the response body contains the next URL
            # The redirect chain is:
            # 1. lh3.googleusercontent.com/gg/... -> text/plain body with redirect URL (no cookies needed)
            # 2. lh3.google.com/rd-gg/... -> text/plain body with redirect URL (COOKIES NEEDED!)
            # 3. lh3.googleusercontent.com/rd-gg/... -> actual image (no cookies needed)
            current_url = download_url
            max_redirects = 5
            response = None

            for hop in range(max_redirects):
                logger.debug(f"Image download hop {hop + 1}: {current_url[:80]}...")
                response = await client.get(current_url)
                logger.debug(f"  Status: {response.status_code}, Content-Type: {response.headers.get('content-type', 'N/A')}")

                if response.status_code != 200:
                    logger.debug(f"  Non-200 response, body: {response.text[:200] if response.text else 'N/A'}")
                    break

                content_type = response.headers.get("content-type", "")

                # If we got an image, we're done
                if "image" in content_type:
                    logger.debug(f"  Got image, size: {len(response.content)} bytes")
                    break

                # If we got text/plain, the body contains the redirect URL
                if content_type.startswith("text/plain"):
                    new_url = response.text.strip()
                    logger.debug(f"  Text redirect to: {new_url[:80]}...")
                    if new_url.startswith("http"):
                        current_url = new_url
                        continue

                # Unknown content type, stop
                logger.debug("  Unknown content type, stopping")
                break

            if response is not None and response.status_code == 200 and "image" in response.headers.get("content-type", ""):
                dest_path = Path(path)
                dest_path.mkdir(parents=True, exist_ok=True)

                dest = dest_path / filename
                dest.write_bytes(response.content)

                if verbose:
                    logger.info(f"Image saved as {dest.resolve()}")

                return str(dest.resolve())
            else:
                reason = getattr(response, "reason_phrase", None) or getattr(response, "reason", "") or ""
                raise HTTPError(f"Error downloading image: {response.status_code if response else 'No response'} {reason}")


class WebImage(Image):
    """
    Image retrieved from web. Returned when ask Gemini to "SEND an image of [something]".
    """

    pass


class GeneratedImage(Image):
    """
    Image generated by ImageFX, Google's AI image generator. Returned when ask Gemini to "GENERATE an image of [something]".

    Parameters
    ----------
    cookies: `dict | curl_cffi.requests.Cookies`
        Cookies used for requesting the content of the generated image, inherit from GeminiClient object or manually set.
        Should contain valid "__Secure-1PSID" and "__Secure-1PSIDTS" values.
    account_index: `int`, optional
        Google account index for multi-account cookie support, by default 0.
    """

    cookies: dict[str, str] | Cookies
    account_index: int = 0

    @field_validator("cookies", mode="after")
    @classmethod
    def validate_cookies(cls, v: dict[str, str] | Cookies) -> dict[str, str] | Cookies:
        if len(v) == 0:
            raise ValueError("GeneratedImage is designed to be initialized with same cookies as GeminiClient.")
        return v

    # @override
    async def save(
        self,
        path: str = "temp",
        filename: str | None = None,
        cookies: dict | Cookies | None = None,
        verbose: bool = False,
        skip_invalid_filename: bool = False,
        full_size: bool = True,
    ) -> str | None:
        """
        Save the image to disk.

        Parameters
        ----------
        path: `str`, optional
            Path to save the image, by default will save to "./temp".
        filename: `str`, optional
            Filename to save the image, generated images are always in .png format, but file extension will not be included in the URL.
            And since the URL ends with a long hash, by default will use timestamp + end of the hash as the filename.
        cookies: `dict`, optional
            Cookies used for requesting the content of the image. If not provided, will use the cookies from the GeneratedImage instance.
        verbose : `bool`, optional
            If True, will print the path of the saved file or warning for invalid file name, by default False.
        skip_invalid_filename: `bool`, optional
            If True, will only save the image if the file name and extension are valid, by default False.
        full_size: `bool`, optional
            If True, will modify the default preview (512*512) URL to get the full size image, by default True.

        Returns
        -------
        `str | None`
            Absolute path of the saved image if successfully saved.
        """

        # Build URL with size suffix and authuser parameter for multi-account support
        url_suffix = "=s2048" if full_size else ""
        self.url += url_suffix

        # Add authuser parameter for multi-account support
        if self.account_index > 0:
            separator = "&" if "?" in self.url else "?"
            self.url += f"{separator}authuser={self.account_index}"

        return await super().save(
            path=path,
            filename=filename or f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{self.url[-10:]}.png",
            cookies=cookies or self.cookies,
            verbose=verbose,
            skip_invalid_filename=skip_invalid_filename,
        )
