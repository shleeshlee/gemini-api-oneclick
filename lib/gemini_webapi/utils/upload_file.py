import io
import random
from pathlib import Path

from curl_cffi import CurlHttpVersion
from curl_cffi.requests import AsyncSession
from pydantic import ConfigDict, validate_call

from ..constants import Endpoint
from .logger import logger


def _generate_random_name(extension: str = ".txt") -> str:
    """
    Generate a random filename using a large integer for better performance.
    """

    return f"input_{random.randint(1000000, 9999999)}{extension}"


# Headers for the resumable upload protocol
_UPLOAD_START_HEADERS = {
    "Origin": "https://gemini.google.com",
    "Referer": "https://gemini.google.com/",
    "Push-ID": "feeds/mcudyrk2a4khkz",
    "X-Goog-Upload-Command": "start",
    "X-Goog-Upload-Protocol": "resumable",
    "X-Tenant-ID": "bard-storage",
}

_UPLOAD_FINALIZE_HEADERS = {
    "Origin": "https://gemini.google.com",
    "Referer": "https://gemini.google.com/",
    "Push-ID": "feeds/mcudyrk2a4khkz",
    "X-Goog-Upload-Command": "upload, finalize",
    "X-Goog-Upload-Offset": "0",
    "X-Tenant-ID": "bard-storage",
}


@validate_call(config=ConfigDict(arbitrary_types_allowed=True))
async def upload_file(
    file: str | Path | bytes | io.BytesIO,
    proxy: str | None = None,
    filename: str | None = None,
    session: AsyncSession | None = None,
    account_index: int = 0,
) -> str:
    """
    Upload a file to Google's server using the resumable upload protocol and return its identifier.

    The upload uses a two-step process:
    1. Initiate upload with `x-goog-upload-command: start` to get an upload URL
    2. Send file data with `x-goog-upload-command: upload, finalize` to complete

    Parameters
    ----------
    file : `str` | `Path` | `bytes` | `io.BytesIO`
        Path to the file or file content to be uploaded.
    proxy: `str`, optional
        Proxy URL.
    filename: `str`, optional
        Name of the file to be uploaded. Required if file is bytes or BytesIO.
    session: `AsyncSession`, optional
        Existing session to use for the request.
    account_index: `int`, optional
        Google account index for multi-account support. Defaults to 0.

    Returns
    -------
    `str`
        Identifier of the uploaded file.
        E.g. "/contrib_service/ttl_1d/1709764705i7wdlyx3mdzndme3a767pluckv4flj"

    Raises
    ------
    `curl_cffi.requests.errors.RequestsError`
        If the upload request failed.
    """

    if isinstance(file, (str, Path)):
        file_path = Path(file)
        if not file_path.is_file():
            raise ValueError(f"{file_path} is not a valid file.")
        if not filename:
            filename = file_path.name
        file_content = file_path.read_bytes()
    elif isinstance(file, io.BytesIO):
        file_content = file.getvalue()
        if not filename:
            filename = _generate_random_name()
    elif isinstance(file, bytes):
        file_content = file
        if not filename:
            filename = _generate_random_name()
    else:
        raise ValueError(f"Unsupported file type: {type(file)}")

    upload_url = Endpoint.get_upload_url(account_index)
    logger.debug(f"Initiating resumable upload to: {upload_url}")

    # Prepare headers for step 1 (initiate upload)
    start_headers = {
        **_UPLOAD_START_HEADERS,
        "X-Goog-Upload-Header-Content-Length": str(len(file_content)),
    }

    if session is not None:
        return await _upload_with_session(session, upload_url, file_content, start_headers, filename)

    async with AsyncSession(
        proxy=proxy,
        allow_redirects=True,
        impersonate="chrome",
        http_version=CurlHttpVersion.V2_0,
    ) as client:
        return await _upload_with_session(client, upload_url, file_content, start_headers, filename)


async def _upload_with_session(
    session: AsyncSession,
    upload_url: str,
    file_content: bytes,
    start_headers: dict,
    filename: str,
) -> str:
    """
    Perform the two-step resumable upload using the provided session.
    """
    # Step 1: Initiate upload - body contains the filename
    init_body = f"File name: {filename}"
    response = await session.post(
        url=upload_url,
        headers=start_headers,
        data=init_body,
    )
    logger.debug(f"Upload initiation response: {response.status_code}")
    response.raise_for_status()

    # Extract the upload URL from response headers
    resumable_url = response.headers.get("x-goog-upload-url")
    if not resumable_url:
        raise ValueError(f"Failed to get upload URL from response headers: {dict(response.headers)}")

    logger.debug(f"Got resumable upload URL: {resumable_url[:100]}...")

    # Step 2: Upload the actual file data
    response = await session.post(
        url=resumable_url,
        headers=_UPLOAD_FINALIZE_HEADERS,
        data=file_content,
    )
    logger.debug(f"Upload finalize response: {response.status_code} - {response.text[:200] if response.text else 'empty'}")
    response.raise_for_status()

    return response.text


def parse_file_name(file: str | Path | bytes | io.BytesIO) -> str:
    """
    Parse the file name from the given path or generate a random one for in-memory data.

    Parameters
    ----------
    file : `str` | `Path` | `bytes` | `io.BytesIO`
        Path to the file or file content.

    Returns
    -------
    `str`
        File name with extension.
    """

    if isinstance(file, (str, Path)):
        file = Path(file)
        if not file.is_file():
            raise ValueError(f"{file} is not a valid file.")
        return file.name

    return _generate_random_name()
