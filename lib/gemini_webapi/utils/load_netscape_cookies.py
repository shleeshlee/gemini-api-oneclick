"""
Netscape Cookie File Parser

Loads cookies from Netscape/Mozilla format cookie files (cookies.txt).
This format is commonly exported by browser extensions and cookie tools.

Format specification:
Each line contains 7 tab-separated fields:
domain, domain_specified, path, secure, expiration, name, value
"""

from pathlib import Path

from curl_cffi.requests import Cookies


def load_netscape_cookies(
    file_path: str | Path,
    domain_filter: str | None = None,
) -> Cookies:
    """
    Load cookies from a Netscape/Mozilla format cookie file.

    Cookies are set with their proper domains to ensure they are sent
    correctly to the right endpoints.

    Parameters
    ----------
    file_path : str | Path
        Path to the cookie file.
    domain_filter : str, optional
        If provided, only load cookies from domains containing this string
        (e.g., "google" to match .google.com, accounts.google.com, etc.).

    Returns
    -------
    Cookies
        curl_cffi Cookies object containing the loaded cookies with proper domains.

    Raises
    ------
    FileNotFoundError
        If the cookie file does not exist.

    Examples
    --------
    >>> cookies = load_netscape_cookies("cookies.txt")
    >>> cookies = load_netscape_cookies("google_cookies.txt", domain_filter="google")
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"Cookie file not found: {file_path}")

    content = file_path.read_text(encoding="utf-8", errors="ignore")
    return parse_netscape_cookies(content, domain_filter=domain_filter)


def parse_netscape_cookies(
    content: str,
    domain_filter: str | None = None,
) -> Cookies:
    """
    Parse cookies from Netscape/Mozilla format string content.

    Cookies are set with their proper domains to ensure they are sent
    correctly to the right endpoints.

    Parameters
    ----------
    content : str
        The cookie file content.
    domain_filter : str, optional
        If provided, only load cookies from domains containing this string.

    Returns
    -------
    Cookies
        curl_cffi Cookies object containing the parsed cookies with proper domains.
    """
    lines = content.splitlines()
    cookies = Cookies()

    for line in lines:
        # Skip header lines and comments
        if line.startswith("# Netscape") or line.startswith("# This is a generated"):
            continue

        # Skip comments and empty lines
        if line.startswith("#") or not line.strip():
            continue

        parts = line.strip().split("\t")

        # Netscape cookie format must have exactly 7 fields:
        # domain, domain_specified, path, secure, expiration, name, value
        if len(parts) != 7:
            continue

        # Parse cookie fields
        domain = parts[0]
        path = parts[2]
        name = parts[5]
        value = parts[6]

        # Apply domain filter if specified (case-insensitive substring match)
        if domain_filter and domain_filter.lower() not in domain.lower():
            continue

        # Set cookie with proper domain to ensure it's sent to correct endpoints
        cookies.set(name, value, domain=domain, path=path)

    return cookies


def load_netscape_cookies_full(
    file_path: str | Path,
    domain_filter: str | None = None,
) -> list[dict]:
    """
    Load cookies from a Netscape/Mozilla format cookie file with full metadata.

    Returns a list of cookie dicts with domain, path, secure, name, value.
    This is useful when you need complete cookie information.

    Parameters
    ----------
    file_path : str | Path
        Path to the cookie file.
    domain_filter : str, optional
        If provided, only load cookies from domains containing this string.

    Returns
    -------
    list[dict]
        List of cookie dicts with full metadata (domain, path, secure, name, value).

    Examples
    --------
    >>> cookies = load_netscape_cookies_full("cookies.txt", domain_filter="google")
    >>> for c in cookies:
    ...     print(f"{c['name']}: {c['domain']}")
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"Cookie file not found: {file_path}")

    content = file_path.read_text(encoding="utf-8", errors="ignore")
    cookies = []

    for line in content.splitlines():
        # Skip header lines and comments
        if line.startswith("#") or not line.strip():
            continue

        parts = line.strip().split("\t")

        # Netscape format has 7 fields
        if len(parts) != 7:
            continue

        domain = parts[0]
        path = parts[2]
        secure = parts[3].upper() == "TRUE"
        name = parts[5]
        value = parts[6]

        # Apply domain filter
        if domain_filter and domain_filter.lower() not in domain.lower():
            continue

        cookies.append(
            {
                "domain": domain,
                "path": path,
                "secure": secure,
                "name": name,
                "value": value,
            }
        )

    return cookies


def load_netscape_cookies_as_dict(
    file_path: str | Path,
    domain_filter: str | None = None,
) -> dict[str, str]:
    """
    Load cookies from a Netscape/Mozilla format cookie file as a dictionary.

    This is a convenience function that returns cookies as a simple dict.
    Note: This loses domain/path info, so cookies are set on .google.com by default.

    Parameters
    ----------
    file_path : str | Path
        Path to the cookie file.
    domain_filter : str, optional
        If provided, only load cookies from domains containing this string.

    Returns
    -------
    dict[str, str]
        Dictionary mapping cookie names to values.

    Examples
    --------
    >>> cookies = load_netscape_cookies_as_dict("cookies.txt", domain_filter="google")
    >>> client = GeminiClient(cookies.get("__Secure-1PSID"), cookies.get("__Secure-1PSIDTS"))
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"Cookie file not found: {file_path}")

    content = file_path.read_text(encoding="utf-8", errors="ignore")
    result = {}

    for line in content.splitlines():
        # Skip header lines and comments
        if line.startswith("#") or not line.strip():
            continue

        parts = line.strip().split("\t")

        # Netscape format has 7 fields
        if len(parts) != 7:
            continue

        domain = parts[0]
        name = parts[5]
        value = parts[6]

        # Apply domain filter
        if domain_filter and domain_filter.lower() not in domain.lower():
            continue

        result[name] = value

    return result
