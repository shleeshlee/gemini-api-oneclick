import asyncio
import codecs
import io
import os
import random
import re
import time
from asyncio import Task
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import orjson as json
from curl_cffi import CurlHttpVersion
from curl_cffi.requests import AsyncSession, Cookies, Response
from curl_cffi.requests.errors import RequestsError

from .components import GemMixin, ResearchMixin
from .tracer import Tracer, sanitize_headers
from .constants import (
    AccountStatus,
    GRPC,
    Endpoint,
    ErrorCode,
    Headers,
    MODEL_HEADER_KEY,
    Model,
)
from .exceptions import (
    APIError,
    AuthError,
    GeminiError,
    ImageGenerationBlocked,
    ImageModelMismatch,
    ModelInvalid,
    RateLimitExceeded,
    RequestTimeoutError,
    TemporarilyBlocked,
    UsageLimitExceeded,
)
from .types import (
    AvailableModel,
    Candidate,
    DeepResearchPlan,
    Gem,
    GeneratedImage,
    GeneratedVideo,
    ModelOutput,
    RPCData,
    WebImage,
)
from .types.video import GeneratedMedia
from .utils import (
    extract_deep_research_plan,
    extract_json_from_response,
    get_access_token,
    get_delta_by_fp_len,
    get_nested_value,
    logger,
    parse_file_name,
    parse_response_by_frame,
    rotate_1psidts,
    running,
    upload_file,
)


@dataclass(slots=True)
class _StreamingState:
    """Internal mutable state for tracking streaming progress across retries."""

    last_texts: dict[str, str] = field(default_factory=dict)
    last_thoughts: dict[str, str] = field(default_factory=dict)
    last_progress_time: float = field(default_factory=time.time)


@dataclass(slots=True)
class _StreamFlags:
    """Mutable flags tracking stream processing state."""

    is_thinking: bool = False
    is_queueing: bool = False
    has_candidates: bool = False
    is_completed: bool = False
    is_final_chunk: bool = False
    detected_image_model: str | None = None  # "pro" or "standard" when detected
    deep_research: bool = False


def _raise_for_error_code(error_code: int, model_name: str) -> None:
    """Raise appropriate exception for API error codes."""
    match error_code:
        case ErrorCode.USAGE_LIMIT_EXCEEDED:
            raise UsageLimitExceeded(
                f"Usage limit exceeded for model '{model_name}'. Please wait a few minutes, switch to a different model (e.g., Gemini Flash), or check your account limits on gemini.google.com."
            )
        case ErrorCode.MODEL_INCONSISTENT:
            raise ModelInvalid("The specified model is inconsistent with the conversation history. Please ensure you are using the same 'model' parameter throughout the entire ChatSession.")
        case ErrorCode.MODEL_HEADER_INVALID:
            raise ModelInvalid(
                f"The model '{model_name}' is currently unavailable or the request structure is outdated. "
                "Please update 'gemini_webapi' to the latest version or report this on GitHub if the problem persists."
            )
        case ErrorCode.IP_TEMPORARILY_BLOCKED:
            raise TemporarilyBlocked("Your IP address has been temporarily flagged or blocked by Google. Please try using a proxy, a different network, or wait for a while before retrying.")
        case ErrorCode.TEMPORARY_ERROR_1013:
            raise APIError("Gemini encountered a temporary error (1013). Retrying...")
        case _:
            raise APIError(f"Failed to generate contents (stream). Unknown API error code: {error_code}. This might be a temporary Google service issue.")


def _collect_all_urls(data: Any, result: dict[str, list[str]]) -> None:
    """Recursively scan nested data and classify all URLs by type."""
    if isinstance(data, str):
        if not data.startswith("http"):
            return
        if "usercontent.google.com" in data and ("download" in data or "video" in data.lower()):
            result["video"].append(data)
        elif data.startswith("https://lh3.googleusercontent.com/"):
            result["image"].append(data)
        elif data.startswith("https://encrypted-tbn") or data.startswith("https://www.google.com/imgres"):
            result["web_image"].append(data)
    elif isinstance(data, list):
        for item in data:
            _collect_all_urls(item, result)
    elif isinstance(data, dict):
        for v in data.values():
            _collect_all_urls(v, result)


def _parse_all_media(
    candidate_data: list[Any],
    proxy: str | None,
    cookies: Cookies,
    account_index: int = 0,
    session_kwargs: dict | None = None,
) -> tuple[list[WebImage], list[GeneratedImage], list[GeneratedVideo]]:
    """Extract all media from candidate data by recursively scanning for URLs."""
    sk = session_kwargs or {}

    # Collect every URL from the entire candidate_data
    url_map: dict[str, list[str]] = {"image": [], "video": [], "web_image": []}
    _collect_all_urls(candidate_data, url_map)

    # Deduplicate
    seen: set[str] = set()

    # Web images
    web_images: list[WebImage] = []
    for url in url_map["web_image"]:
        if url not in seen:
            seen.add(url)
            web_images.append(WebImage(url=url, title="", alt="", proxy=proxy, session_kwargs=sk))

    # Generated images (lh3.googleusercontent.com)
    generated_images: list[GeneratedImage] = []
    for url in url_map["image"]:
        if url not in seen:
            seen.add(url)
            generated_images.append(
                GeneratedImage(
                    url=url, title="[Generated Image]", alt="",
                    proxy=proxy, cookies=cookies, account_index=account_index, session_kwargs=sk,
                )
            )

    # Generated videos (contribution.usercontent.google.com)
    generated_videos: list[GeneratedVideo] = []
    for url in url_map["video"]:
        if url not in seen:
            seen.add(url)
            # Unescape if needed
            clean_url = url.encode().decode("unicode_escape") if "\\u" in url else url
            # Try to find a thumbnail from the image URLs
            thumb = url_map["image"][len(generated_videos)] if len(generated_videos) < len(url_map["image"]) else ""
            generated_videos.append(
                GeneratedVideo(
                    url=clean_url, thumbnail_url=thumb,
                    title=f"[Generated Video {len(generated_videos) + 1}]",
                    proxy=proxy, cookies=cookies, account_index=account_index, session_kwargs=sk,
                )
            )

    return web_images, generated_images, generated_videos


# Patterns indicating rate limiting by Gemini
_RATE_LIMIT_PATTERNS = [
    r"I couldn't do that because I'm getting a lot of requests right now",
    r"I'm getting a lot of requests right now",
    r"Please try again later",
]

_IMAGE_GEN_BLOCKED_PATTERNS = [
    r"Are you signed in\?.*(?:search for images|can't.*create)",
    r"can't seem to create any.*for you right now",
    r"image creation isn't available in your location",
    r"I can search for images, but can't.*create",
]

# Video generation pending patterns
_VIDEO_GEN_PENDING_PATTERNS = [
    r"http://googleusercontent\.com/video_gen_chip/\d+",
    r"正在生成视频",
    r"视频已准备就绪",
    r"generating.*video",
    r"video.*ready",
    r"I'm generating your video",
]

# Video generation rate limit / blocked patterns
_VIDEO_GEN_BLOCKED_PATTERNS = [
    r"can't generate more videos for you today",
    r"come back tomorrow",
    r"video generation isn't available",
    r"unable to generate.*video",
    r"无法.*生成.*视频",
]

# Video polling configuration
_VIDEO_POLL_INTERVAL = 5.0  # seconds between polls
_VIDEO_POLL_MAX_ATTEMPTS = 60  # max attempts (5 minutes total)

# Image model detection patterns
# "Loading Nano Banana Pro..." or "Loading Nano Banana 2..." = Pro model
# "Loading Nano Banana..." (without Pro/2) = Standard model
_IMAGE_MODEL_PRO_PATTERN = r"loading\s+nano\s+banana\s+(pro|2)"
_IMAGE_MODEL_STANDARD_PATTERN = r"loading\s+nano\s+banana\s*\.{3}"


def _detect_image_model(text: str) -> str | None:
    """Detect which image generation model is being used from the loading message.

    Returns:
        "pro" if Pro model detected, "standard" if standard model detected, None if not detected
    """
    # Check for Pro first (more specific)
    if re.search(_IMAGE_MODEL_PRO_PATTERN, text, re.IGNORECASE):
        return "pro"
    # Then check for standard (ends with ... without Pro/2)
    if re.search(_IMAGE_MODEL_STANDARD_PATTERN, text, re.IGNORECASE):
        return "standard"
    return None


def _is_video_generation_pending(text: str) -> bool:
    """Check if the response indicates video generation is in progress (not blocked/rate-limited)."""
    # First check if it's blocked/rate-limited
    for pattern in _VIDEO_GEN_BLOCKED_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return False
    # Check any pending pattern
    for pattern in _VIDEO_GEN_PENDING_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _check_rate_limit_response(text: str) -> None:
    """Check if the response text indicates a rate limit and raise exception if so."""
    for pattern in _RATE_LIMIT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            raise RateLimitExceeded(f"Gemini is rate limiting requests. Response: {text[:200]}... Please wait a moment before trying again.")


def _check_image_gen_blocked(text: str) -> None:
    """Check if the response indicates image generation is blocked due to auth or regional restrictions."""
    for pattern in _IMAGE_GEN_BLOCKED_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            raise ImageGenerationBlocked(
                f"Image generation is blocked for this account. This may be due to:\n"
                f"  - Regional restrictions (image generation not available in your location)\n"
                f"  - Authentication issues (cookies may be invalid or expired)\n"
                f"  - Account restrictions\n"
                f"Response: {text[:300]}..."
            )


def _extract_candidate_text(candidate_data: list[Any]) -> str:
    """Extract and clean text from candidate data."""
    text = get_nested_value(candidate_data, [1, 0], "")
    if re.match(r"^http://googleusercontent\.com/card_content/\d+", text):
        text = get_nested_value(candidate_data, [22, 0]) or text
    # Cleanup googleusercontent artifacts
    return re.sub(r"http://googleusercontent\.com/\w+/\d+\n*", "", text)


def _extract_research_sources(refs_data: Any) -> list[dict]:
    """Extract source URLs and titles from Deep Research immersive [30][0][5] data."""
    if not isinstance(refs_data, list):
        return []

    sources = []
    seen_urls: set[str] = set()

    for section in refs_data:
        if not isinstance(section, dict):
            continue
        citations = section.get("44", [])
        if not isinstance(citations, list):
            continue
        for cite_group in citations:
            if not isinstance(cite_group, list) or len(cite_group) < 2:
                continue
            cite_nums = get_nested_value(cite_group, [0, 0], "")
            entries = cite_group[1] if isinstance(cite_group[1], list) else []
            for entry in entries:
                if not isinstance(entry, list):
                    continue
                source_data = get_nested_value(entry, [3, 0])
                if not isinstance(source_data, list) or len(source_data) < 3:
                    continue
                url = source_data[1] if isinstance(source_data[1], str) else ""
                title = source_data[2] if isinstance(source_data[2], str) else ""
                # Skip favicon URLs and duplicates
                if url and "gstatic.com/faviconV2" not in url and url not in seen_urls:
                    seen_urls.add(url)
                    sources.append({
                        "citation": cite_nums,
                        "url": url,
                        "title": title,
                    })

    return sources


class GeminiClient(GemMixin, ResearchMixin):
    """
    Async client interface for gemini.google.com using curl_cffi.

    Parameters
    ----------
    secure_1psid: `str`, optional
        __Secure-1PSID cookie value.
    secure_1psidts: `str`, optional
        __Secure-1PSIDTS cookie value, some Google accounts don't require this value, provide only if it's in the cookie list.
    proxy: `str`, optional
        Proxy URL.
    account_index: `int`, optional
        Google account index to use when multiple accounts are signed in.
        Corresponds to the /u/{index}/ path in Google URLs (e.g., /u/0/, /u/1/, /u/2/).
        Defaults to 0 (first account).
    kwargs: `dict`, optional
        Additional arguments which will be passed to the http client.
        Refer to `curl_cffi.requests.AsyncSession` for more information.
    """

    __slots__ = [
        "_gems",  # From GemMixin
        "_lock",
        "_model_registry",
        "_reqid",
        "_running",
        "access_token",
        "account_status",
        "account_index",
        "auto_close",
        "auto_refresh",
        "build_label",
        "client",
        "close_delay",
        "close_task",
        "cookies",
        "kwargs",
        "proxy",
        "refresh_interval",
        "refresh_task",
        "session_id",
        "session_kwargs",
        "timeout",
        "verbose",
        "watchdog_timeout",
    ]

    def __init__(
        self,
        secure_1psid: str | None = None,
        secure_1psidts: str | None = None,
        proxy: str | None = None,
        account_index: int = 0,
        **kwargs,
    ):
        super().__init__()
        self.cookies = Cookies()
        self.proxy = proxy
        self.account_index = account_index
        self._running: bool = False
        self.client: AsyncSession | None = None
        self.access_token: str | None = None
        self.build_label: str | None = None
        self.session_id: str | None = None
        self.timeout: float = 300
        self.auto_close: bool = False
        self.close_delay: float = 300
        self.close_task: Task | None = None
        self.auto_refresh: bool = True
        self.refresh_interval: float = 540
        self.refresh_task: Task | None = None
        self.verbose: bool = True
        self.watchdog_timeout: float = 60  # ≤ DELAY_FACTOR × retry × (retry + 1) / 2
        self._lock = asyncio.Lock()
        self._model_registry: dict[str, AvailableModel] = {}
        self._reqid: int = random.randint(10000, 99999)
        self.account_status: AccountStatus = AccountStatus.AVAILABLE
        self.kwargs = kwargs

        if secure_1psid:
            self.cookies.set("__Secure-1PSID", secure_1psid, domain=".google.com")
            if secure_1psidts:
                self.cookies.set("__Secure-1PSIDTS", secure_1psidts, domain=".google.com")

    async def init(
        self,
        timeout: float = 300,
        auto_close: bool = False,
        close_delay: float = 300,
        auto_refresh: bool = True,
        refresh_interval: float = 540,
        verbose: bool = True,
        watchdog_timeout: float = 60,  # ≤ DELAY_FACTOR × retry × (retry + 1) / 2
    ) -> None:
        """
        Get SNlM0e value as access token. Without this token posting will fail with 400 bad request.

        Parameters
        ----------
        timeout: `float`, optional
            Request timeout of the client in seconds. Used to limit the max waiting time when sending a request.
        auto_close: `bool`, optional
            If `True`, the client will close connections and clear resource usage after a certain period
            of inactivity. Useful for always-on services.
        close_delay: `float`, optional
            Time to wait before auto-closing the client in seconds. Effective only if `auto_close` is `True`.
        auto_refresh: `bool`, optional
            If `True`, will schedule a task to automatically refresh cookies and access token in the background.
        refresh_interval: `float`, optional
            Time interval for background cookie and access token refresh in seconds. Effective only if `auto_refresh` is `True`.
        verbose: `bool`, optional
            If `True`, will print more infomation in logs.
        watchdog_timeout: `float`, optional
            Timeout in seconds for shadow retry watchdog. If no data receives from stream but connection is active,
            client will retry automatically after this duration.
        """

        async with self._lock:
            if self._running:
                return

            try:
                self.verbose = verbose
                self.watchdog_timeout = watchdog_timeout

                # Store kwargs for use by Image/Video save methods
                self.session_kwargs = dict(self.kwargs)

                # Create session first and reuse it for get_access_token
                self.client = AsyncSession(
                    timeout=timeout,
                    proxy=self.proxy,
                    allow_redirects=True,
                    http_version=CurlHttpVersion.V2_0,
                    impersonate=self.kwargs.pop("impersonate", "chrome"),
                    **self.kwargs,
                )
                self.client.headers.update(Headers.GEMINI.value)

                access_token, build_label, session_id, valid_cookies = await get_access_token(
                    base_cookies=self.cookies,
                    proxy=self.proxy,
                    verbose=self.verbose,
                    account_index=self.account_index,
                    session=self.client,
                )

                self.client.cookies = valid_cookies
                self.access_token = access_token
                self.cookies = valid_cookies
                self.build_label = build_label
                self.session_id = session_id
                self._running = True
                self._reqid = random.randint(10000, 99999)

                self.timeout = timeout
                self.auto_close = auto_close
                self.close_delay = close_delay
                if self.auto_close:
                    await self.reset_close_task()

                self.auto_refresh = auto_refresh
                self.refresh_interval = refresh_interval

                if self.refresh_task:
                    self.refresh_task.cancel()
                    self.refresh_task = None

                if self.auto_refresh:
                    self.refresh_task = asyncio.create_task(self.start_auto_refresh())

                await self._init_rpc()

                if self.verbose:
                    logger.success("Gemini client initialized successfully.")
            except Exception:
                await self.close()
                raise

    async def close(self, delay: float = 0) -> None:
        """
        Close the client after a certain period of inactivity, or call manually to close immediately.

        Parameters
        ----------
        delay: `float`, optional
            Time to wait before closing the client in seconds.
        """

        if delay:
            await asyncio.sleep(delay)

        self._running = False

        if self.close_task:
            self.close_task.cancel()
            self.close_task = None

        if self.refresh_task:
            self.refresh_task.cancel()
            self.refresh_task = None

        if self.client:
            await self.client.close()

    async def reset_close_task(self) -> None:
        """
        Reset the timer for closing the client when a new request is made.
        """

        if self.close_task:
            self.close_task.cancel()
            self.close_task = None

        self.close_task = asyncio.create_task(self.close(self.close_delay))

    async def start_auto_refresh(self) -> None:
        """
        Start the background task to automatically refresh cookies.
        """
        if self.refresh_interval < 60:
            self.refresh_interval = 60

        while self._running:
            await asyncio.sleep(self.refresh_interval)

            if not self._running:
                break

            try:
                async with self._lock:
                    # Refresh all cookies in the background to keep the session alive.
                    new_1psidts, rotated_cookies = await rotate_1psidts(self.cookies, self.proxy)
                    if rotated_cookies:
                        self.cookies.update(rotated_cookies)
                        if self.client:
                            self.client.cookies.update(rotated_cookies)

                    if new_1psidts:
                        if rotated_cookies:
                            logger.debug("Cookies refreshed (network update).")
                        else:
                            logger.debug("Cookies are up to date (cached).")
                    else:
                        logger.warning("Rotation response did not contain a new __Secure-1PSIDTS. Session might expire soon if this persists.")
            except asyncio.CancelledError:
                raise
            except AuthError:
                logger.warning("AuthError: Failed to refresh cookies. Retrying in next interval.")
            except Exception as e:
                logger.warning(f"Unexpected error while refreshing cookies: {e}")

    async def _init_rpc(self) -> None:
        """
        Warm up the session and populate dynamic model metadata when possible.
        """

        steps = (
            ("user status", self._fetch_user_status),
            ("bard settings", self._send_bard_settings),
            ("bard activity", self._send_bard_activity),
        )
        for label, func in steps:
            try:
                await func()
            except Exception as exc:
                logger.warning(f"Failed to initialize {label}: {exc}")

    async def _fetch_user_status(self) -> None:
        """
        Fetch account status and derive dynamic models from Gemini RPC metadata.
        """

        response = await self._batch_execute(
            [
                RPCData(
                    rpcid=GRPC.GET_USER_STATUS,
                    payload="[]",
                )
            ]
        )

        response_json = extract_json_from_response(response.text)

        for part in response_json:
            part_body_str = get_nested_value(part, [2])
            if not part_body_str:
                continue

            try:
                part_body = json.loads(part_body_str)
            except json.JSONDecodeError:
                continue

            status_code = get_nested_value(part_body, [14])
            self.account_status = AccountStatus.from_status_code(status_code)

            if self.account_status == AccountStatus.AVAILABLE:
                logger.info(
                    f"Account status: {self.account_status.name} - {self.account_status.description}"
                )
            else:
                logger.warning(
                    f"Account status: {self.account_status.name} - {self.account_status.description}"
                )
                if self.account_status in {
                    AccountStatus.LOCATION_REJECTED,
                    AccountStatus.ACCOUNT_REJECTED,
                    AccountStatus.ACCESS_TEMPORARILY_UNAVAILABLE,
                    AccountStatus.ACCOUNT_REJECTED_BY_GUARDIAN,
                    AccountStatus.GUARDIAN_APPROVAL_REQUIRED,
                }:
                    logger.warning(
                        f"Hard block detected ({self.account_status.name}). Skipping model discovery."
                    )
                    return

            models_list = get_nested_value(part_body, [15])
            if not isinstance(models_list, list):
                continue

            tier_flags = get_nested_value(part_body, [16], [])
            capability_flags = get_nested_value(part_body, [17], [])
            tier_flags = tier_flags if isinstance(tier_flags, list) else []
            capability_flags = capability_flags if isinstance(capability_flags, list) else []
            capacity, capacity_field = AvailableModel.compute_capacity(
                tier_flags, capability_flags
            )
            id_name_mapping = AvailableModel.build_model_id_name_mapping()

            registry: dict[str, AvailableModel] = {}
            for model_data in models_list:
                if not isinstance(model_data, list):
                    continue

                model_id = get_nested_value(model_data, [0], "")
                display_name = get_nested_value(model_data, [1], "")
                description = get_nested_value(model_data, [2], "")
                # Extended description at index 12 often has version info (e.g. "with 3.1 Pro")
                ext_desc = get_nested_value(model_data, [12], "")
                if isinstance(ext_desc, str) and ext_desc:
                    description = ext_desc
                if not model_id or not display_name:
                    continue

                is_model_available = True
                if (
                    self.account_status == AccountStatus.UNAUTHENTICATED
                    and model_id != Model.BASIC_FLASH.model_id
                ):
                    is_model_available = False

                registry[model_id] = AvailableModel(
                    model_id=model_id,
                    model_name=id_name_mapping.get(model_id, ""),
                    display_name=display_name,
                    description=description,
                    capacity=capacity,
                    capacity_field=capacity_field,
                    is_available=is_model_available,
                )

            if registry:
                self._model_registry = registry
            return

    async def _send_bard_settings(self) -> None:
        """
        Send required setup activity to Gemini.
        """

        await self._batch_execute(
            [
                RPCData(
                    rpcid=GRPC.BARD_SETTINGS,
                    payload='[[["adaptive_device_responses_enabled","advanced_mode_theme_override_triggered","advanced_zs_upsell_dismissal_count","advanced_zs_upsell_last_dismissed","ai_transparency_notice_dismissed","audio_overview_discovery_dismissal_count","audio_overview_discovery_last_dismissed","bard_in_chrome_link_sharing_enabled","bard_sticky_mode_disabled_count","canvas_create_discovery_tooltip_seen_count","combined_files_button_tag_seen_count","indigo_banner_explicit_dismissal_count","indigo_banner_impression_count","indigo_banner_last_seen_sec","current_popup_id","deep_research_has_seen_file_upload_tooltip","deep_research_model_update_disclaimer_display_count","default_bot_id","disabled_discovery_card_feature_ids","disabled_model_discovery_tooltip_feature_ids","disabled_mode_disclaimers","disabled_new_model_badge_mode_ids","disabled_settings_discovery_tooltip_feature_ids","disablement_disclaimer_last_dismissed_sec","disable_advanced_beta_dialog","disable_advanced_beta_non_en_banner","disable_advanced_resubscribe_ui","disable_at_mentions_discovery_tooltip","disable_autorun_fact_check_u18","disable_bot_create_tips_card","disable_bot_docs_in_gems_disclaimer","disable_bot_onboarding_dialog","disable_bot_save_reminder_tips_card","disable_bot_send_prompt_tips_card","disable_bot_shared_in_drive_disclaimer","disable_bot_try_create_tips_card","disable_colab_tooltip","disable_collapsed_tool_menu_tooltip","disable_continue_discovery_tooltip","disable_debug_info_moved_tooltip_v2","disable_enterprise_mode_dialog","disable_export_python_tooltip","disable_extensions_discovery_dialog","disable_extension_one_time_badge","disable_fact_check_tooltip_v2","disable_free_file_upload_tips_card","disable_generated_image_download_dialog","disable_get_app_banner","disable_get_app_desktop_dialog","disable_googler_in_enterprise_mode","disable_human_review_disclosure","disable_ice_open_vega_editor_tooltip","disable_image_upload_tooltip","disable_legal_concern_tooltip","disable_llm_history_import_disclaimer","disable_location_popup","disable_memory_discovery","disable_memory_extraction_discovery","disable_new_conversation_dialog","disable_onboarding_experience","disable_personal_context_tooltip","disable_photos_upload_disclaimer","disable_power_up_intro_tooltip","disable_scheduled_actions_mobile_notification_snackbar","disable_storybook_listen_button_tooltip","disable_streaming_settings_tooltip","disable_take_control_disclaimer","disable_teens_only_english_language_dialog","disable_tier1_rebranding_tooltip","disable_try_advanced_mode_dialog","enable_advanced_beta_mode","enable_advanced_mode","enable_googler_in_enterprise_mode","enable_memory","enable_memory_extraction","enable_personal_context","enable_personal_context_gemini","enable_personal_context_gemini_using_photos","enable_personal_context_gemini_using_workspace","enable_personal_context_search","enable_personal_context_youtube","enable_token_streaming","enforce_default_to_fast_version","mayo_discovery_banner_dismissal_count","mayo_discovery_banner_last_dismissed_sec","gempix_discovery_banner_dismissal_count","gempix_discovery_banner_last_dismissed","get_app_banner_ack_count","get_app_banner_seen_count","get_app_mobile_dialog_ack_count","guided_learning_banner_dismissal_count","guided_learning_banner_last_dismissed","has_accepted_agent_mode_fre_disclaimer","has_received_streaming_response","has_seen_agent_mode_tooltip","has_seen_bespoke_tooltip","has_seen_deepthink_mustard_tooltip","has_seen_deepthink_v2_tooltip","has_seen_deep_think_tooltip","has_seen_first_youtube_video_disclaimer","has_seen_ggo_tooltip","has_seen_image_grams_discovery_banner","has_seen_image_preview_in_input_area_tooltip","has_seen_kallo_discovery_banner","has_seen_kallo_tooltip","has_seen_model_picker_in_input_area_tooltip","has_seen_model_tooltip_in_input_area_for_gempix","has_seen_redo_with_gempix2_tooltip","has_seen_veograms_discovery_banner","has_seen_video_generation_discovery_banner","is_imported_chats_panel_open_by_default","jumpstart_onboarding_dismissal_count","last_dismissed_deep_research_implicit_invite","last_dismissed_discovery_feature_implicit_invites","last_dismissed_immersives_canvas_implicit_invite","last_dismissed_immersive_share_disclaimer_sec","last_dismissed_strike_timestamp_sec","last_dismissed_zs_student_aip_banner_sec","last_get_app_banner_ack_timestamp_sec","last_get_app_mobile_dialog_ack_timestamp_sec","last_human_review_disclosure_ack","last_selected_mode_id_in_embedded","last_selected_mode_id_on_web","last_two_up_activation_timestamp_sec","last_winter_olympics_interaction_timestamp_sec","memory_extracted_greeting_name","mini_gemini_tos_closed","mode_switcher_soft_badge_disabled_ids","mode_switcher_soft_badge_seen_count","personalization_first_party_onboarding_cross_surface_clicked","personalization_first_party_onboarding_cross_surface_seen_count","personalization_one_p_discovery_card_seen_count","personalization_one_p_discovery_last_consented","personalization_zero_state_card_last_interacted","personalization_zero_state_card_seen_count","popup_zs_visits_cooldown","require_reconsent_setting_for_personalization_banner_seen_count","show_debug_info","side_nav_open_by_default","student_verification_dismissal_count","student_verification_last_dismissed","task_viewer_cc_banner_dismissed_count","task_viewer_cc_banner_dismissed_time_sec","tool_menu_new_badge_disabled_ids","tool_menu_new_badge_impression_counts","tool_menu_soft_badge_disabled_ids","tool_menu_soft_badge_impression_counts","upload_disclaimer_last_consent_time_sec","viewed_student_aip_upsell_campaign_ids","voice_language","voice_name","web_and_app_activity_enabled","wellbeing_nudge_notice_last_dismissed_sec","zs_student_aip_banner_dismissal_count"]]]',
                )
            ]
        )

    async def _send_bard_activity(self) -> None:
        """
        Send warmup RPC calls before querying.
        """

        await self._batch_execute(
            [
                RPCData(
                    rpcid=GRPC.BARD_SETTINGS,
                    payload='[[["bard_activity_enabled"]]]',
                )
            ]
        )

    def list_models(self) -> list[AvailableModel] | None:
        """
        Return dynamically discovered models for the current account.
        """

        return list(self._model_registry.values()) if self._model_registry else None

    def _resolve_model_by_name(self, name: str) -> Model | AvailableModel:
        """
        Resolve a user-facing model name to a dynamic model first, then enum fallback.
        """

        if name in self._model_registry:
            return self._model_registry[name]

        for model in self._model_registry.values():
            if name in {model.model_name, model.display_name, model.model_id}:
                return model

        return Model.from_name(name)

    def _resolve_enum_model(self, model: Model) -> Model | AvailableModel:
        """
        Upgrade enum models to their dynamic registry entry when possible.
        """

        if model is Model.UNSPECIFIED:
            return model

        header_value = model.model_header.get(MODEL_HEADER_KEY, "")
        if not header_value:
            return model

        try:
            parsed = json.loads(header_value)
        except json.JSONDecodeError:
            return model

        model_id = get_nested_value(parsed, [4], "")
        if model_id and model_id in self._model_registry:
            return self._model_registry[model_id]

        return model

    async def generate_content(
        self,
        prompt: str,
        files: list[str | Path | bytes | io.BytesIO] | None = None,
        model: Model | AvailableModel | str | dict = Model.UNSPECIFIED,
        gem: Gem | str | None = None,
        chat: Optional["ChatSession"] = None,
        use_pro: bool = False,
        deep_research: bool = False,
        tracer: Tracer | None = None,
        **kwargs,
    ) -> ModelOutput:
        """
        Generates contents with prompt.

        Parameters
        ----------
        prompt: `str`
            Prompt provided by user.
        files: `list[str | Path]`, optional
            List of file paths to be attached.
        model: `Model | str | dict`, optional
            Specify the model to use for generation.
            Pass either a `gemini_webapi.constants.Model` enum or a model name string to use predefined models.
            Pass a dictionary to use custom model header strings ("model_name" and "model_header" keys must be provided).
        gem: `Gem | str`, optional
            Specify a gem to use as system prompt for the chat session.
            Pass either a `gemini_webapi.types.Gem` object or a gem id string.
        chat: `ChatSession`, optional
            Chat data to retrieve conversation history. If None, will automatically generate a new chat id when sending post request.
        use_pro: `bool`, optional
            If True, use Nano Banana Pro for image generation. This enables enhanced image generation
            capabilities. Default is False.
        tracer: `Tracer`, optional
            Per-request tracer for observing request lifecycle. Must be a fresh instance per call.
        kwargs: `dict`, optional
            Additional arguments which will be passed to the post request.
            Refer to `curl_cffi.requests.AsyncSession.request` for more information.

        Returns
        -------
        :class:`ModelOutput`
            Output data from gemini.google.com, use `ModelOutput.text` to get the default text reply, `ModelOutput.images` to get a list
            of images in the default reply, `ModelOutput.candidates` to get a list of all answer candidates in the output.

        Raises
        ------
        `AssertionError`
            If prompt is empty.
        `gemini_webapi.TimeoutError`
            If request timed out.
        `gemini_webapi.GeminiError`
            If no reply candidate found in response.
        `gemini_webapi.APIError`
            - If request failed with status code other than 200.
            - If response structure is invalid and failed to parse.
        """

        if self.auto_close:
            await self.reset_close_task()

        if not (isinstance(chat, ChatSession) and chat.cid):
            self._reqid = random.randint(10000, 99999)

        file_data = None
        if files:
            await self._send_bard_activity()

            uploaded_urls = await asyncio.gather(*(upload_file(file, self.proxy, session=self.client, account_index=self.account_index) for file in files))
            file_data = [[[url, 1, None, "image/jpeg"], parse_file_name(file)] for url, file in zip(uploaded_urls, files, strict=True)]

        try:
            await self._send_bard_activity()

            streaming_state = _StreamingState()
            output: ModelOutput | None = None
            accumulated_media: list[GeneratedMedia] = []
            async for output in self._generate(  # noqa: B007 - output used after loop
                prompt=prompt,
                req_file_data=file_data,
                model=model,
                gem=gem,
                chat=chat,
                streaming_state=streaming_state,
                use_pro=use_pro,
                deep_research=deep_research,
                tracer=tracer,
                **kwargs,
            ):
                # Music/audio data appears in intermediate frames, not the final one.
                # Accumulate across frames so we don't lose it.
                if output and output.candidates:
                    for c in output.candidates:
                        if c.generated_media:
                            accumulated_media.extend(c.generated_media)

            if output is None:
                raise GeminiError("Failed to generate contents. No output data found in response.")

            # Merge accumulated music/audio media into final output
            if accumulated_media and output.candidates:
                final_media = output.candidates[output.chosen].generated_media
                seen_urls = {m.mp3_url for m in final_media if m.mp3_url}
                for m in accumulated_media:
                    if m.mp3_url not in seen_urls:
                        final_media.append(m)
                        seen_urls.add(m.mp3_url)

            # Check if video generation is pending and poll for completion
            if output.candidates:
                for candidate in output.candidates:
                    if candidate.text and _is_video_generation_pending(candidate.text):
                        # Get conversation ID from chat metadata
                        cid = None
                        if isinstance(chat, ChatSession) and chat.metadata:
                            cid = chat.metadata[0] if chat.metadata else None
                        elif output.metadata:
                            cid = output.metadata[0] if output.metadata else None

                        if cid:
                            logger.debug(f"Video generation pending, starting poll for cid={cid}")
                            polled_videos = await self._poll_video_generation(cid, verbose=self.verbose)
                            if polled_videos:
                                # Add polled videos to the candidate
                                candidate.generated_videos.extend(polled_videos)
                                # Clean up the pending message from text
                                candidate.text = re.sub(r"I'm generating your video.*?video is ready\.\n?", "", candidate.text, flags=re.IGNORECASE | re.DOTALL)
                                candidate.text = re.sub(r"http://googleusercontent\.com/video_gen_chip/\d+\n?", "", candidate.text)
                        break  # Only poll once per response

            if isinstance(chat, ChatSession):
                output.metadata = chat.metadata
                chat.last_output = output

            return output

        finally:
            if files:
                for file in files:
                    if isinstance(file, io.BytesIO):
                        file.close()

    async def generate_content_stream(
        self,
        prompt: str,
        files: list[str | Path | bytes | io.BytesIO] | None = None,
        model: Model | AvailableModel | str | dict = Model.UNSPECIFIED,
        gem: Gem | str | None = None,
        chat: Optional["ChatSession"] = None,
        use_pro: bool = False,
        tracer: Tracer | None = None,
        **kwargs,
    ) -> AsyncGenerator[ModelOutput, None]:
        """
        Generates contents with prompt in streaming mode.

        This method sends a request to Gemini and yields partial responses as they arrive.
        It automatically calculates the text delta (new characters) to provide a smooth
        streaming experience. It also continuously updates chat metadata and candidate IDs.

        Parameters
        ----------
        prompt: `str`
            Prompt provided by user.
        files: `list[str | Path | bytes | io.BytesIO]`, optional
            List of file paths or byte streams to be attached.
        model: `Model | str | dict`, optional
            Specify the model to use for generation.
        gem: `Gem | str`, optional
            Specify a gem to use as system prompt for the chat session.
        chat: `ChatSession`, optional
            Chat data to retrieve conversation history.
        use_pro: `bool`, optional
            If True, use Nano Banana Pro for image generation. Default is False.
        tracer: `Tracer`, optional
            Per-request tracer for observing request lifecycle.
        kwargs: `dict`, optional
            Additional arguments passed to `curl_cffi.requests.AsyncSession.request`.

        Yields
        ------
        :class:`ModelOutput`
            Partial output data. The `text` attribute contains only the NEW characters
            received since the last yield.

        Raises
        ------
        `gemini_webapi.APIError`
            If the request fails or response structure is invalid.
        `gemini_webapi.TimeoutError`
            If the stream request times out.
        """

        if self.auto_close:
            await self.reset_close_task()

        if not (isinstance(chat, ChatSession) and chat.cid):
            self._reqid = random.randint(10000, 99999)

        file_data = None
        if files:
            await self._send_bard_activity()

            uploaded_urls = await asyncio.gather(*(upload_file(file, self.proxy, session=self.client, account_index=self.account_index) for file in files))
            file_data = [[[url, 1, None, "image/jpeg"], parse_file_name(file)] for url, file in zip(uploaded_urls, files, strict=True)]

        try:
            await self._send_bard_activity()

            streaming_state = _StreamingState()
            output = None
            async for output in self._generate(
                prompt=prompt,
                req_file_data=file_data,
                model=model,
                gem=gem,
                chat=chat,
                streaming_state=streaming_state,
                use_pro=use_pro,
                tracer=tracer,
                **kwargs,
            ):
                yield output

            # Check if video generation is pending and poll for completion
            if output and output.candidates:
                for candidate in output.candidates:
                    if candidate.text and _is_video_generation_pending(candidate.text):
                        # Get conversation ID from chat metadata
                        cid = None
                        if isinstance(chat, ChatSession) and chat.metadata:
                            cid = chat.metadata[0] if chat.metadata else None
                        elif output.metadata:
                            cid = output.metadata[0] if output.metadata else None

                        if cid:
                            logger.debug(f"Video generation pending (stream), starting poll for cid={cid}")
                            polled_videos = await self._poll_video_generation(cid, verbose=self.verbose)
                            if polled_videos:
                                # Add polled videos to the candidate
                                candidate.generated_videos.extend(polled_videos)
                                # Clean up the pending message from text
                                candidate.text = re.sub(r"I'm generating your video.*?video is ready\.\n?", "", candidate.text, flags=re.IGNORECASE | re.DOTALL)
                                candidate.text = re.sub(r"http://googleusercontent\.com/video_gen_chip/\d+\n?", "", candidate.text)
                                # Yield final output with videos
                                yield output
                        break  # Only poll once per response

            if output and isinstance(chat, ChatSession):
                output.metadata = chat.metadata
                chat.last_output = output

        finally:
            if files:
                for file in files:
                    if isinstance(file, io.BytesIO):
                        file.close()

    @running(retry=0)
    async def _generate(
        self,
        prompt: str,
        req_file_data: list[list] | None = None,
        model: Model | AvailableModel | str | dict = Model.UNSPECIFIED,
        gem: Gem | str | None = None,
        chat: Optional["ChatSession"] = None,
        streaming_state: _StreamingState | None = None,
        use_pro: bool = False,
        deep_research: bool = False,
        tracer: Tracer | None = None,
        **kwargs,
    ) -> AsyncGenerator[ModelOutput, None]:
        """
        Internal method which actually sends content generation requests.
        """

        assert prompt, "Prompt cannot be empty."

        # Pop tracer from kwargs so it doesn't get passed to curl_cffi's request()
        kwargs.pop("tracer", None)

        if isinstance(model, str):
            model = self._resolve_model_by_name(model)
        elif isinstance(model, dict):
            model = Model.from_dict(model)
        elif isinstance(model, Model):
            model = self._resolve_enum_model(model)
        elif not isinstance(model, AvailableModel):
            raise TypeError(
                f"'model' must be a `gemini_webapi.constants.Model`, `AvailableModel`, string, or dictionary; got `{type(model).__name__}`"
            )

        _reqid = self._reqid
        self._reqid += 100000

        gem_id = gem.id if isinstance(gem, Gem) else gem

        try:
            message_content: list[Any] = [
                prompt,
                0,
                None,
                req_file_data,
                None,
                None,
                0,
            ]

            # Add "use pro" flag for image regeneration with Nano Banana Pro
            if use_pro:
                # Extend message_content to include the pro flag at index 9
                message_content.extend([None, None, [None, None, None, None, None, None, [None, [1]]]])

            params: dict[str, Any] = {"_reqid": _reqid, "rt": "c"}
            if self.build_label:
                params["bl"] = self.build_label
            if self.session_id:
                params["f.sid"] = self.session_id

            inner_req_list: list[Any] = [None] * 73  # Extend to support index 32+
            inner_req_list[0] = message_content
            inner_req_list[2] = chat.metadata if chat else ["", "", "", None, None, None, None, None, None, ""]
            if deep_research:
                import secrets
                import uuid
                inner_req_list[3] = "!" + secrets.token_urlsafe(2600)
                inner_req_list[4] = uuid.uuid4().hex
            inner_req_list[7] = 1  # Enable Snapshot Streaming
            if gem_id:
                inner_req_list[19] = gem_id
            if use_pro:
                inner_req_list[32] = 1  # Enable pro regeneration
            if deep_research:
                inner_req_list[49] = 1
                inner_req_list[54] = [[[[[1]]]]]
                inner_req_list[55] = [[1]]

            request_data = {
                "at": self.access_token,
                "f.req": json.dumps(
                    [
                        None,
                        json.dumps(inner_req_list).decode("utf-8"),
                    ]
                ).decode("utf-8"),
            }

            model_name = getattr(model, "model_name", str(model))
            _poll_iterations = 0

            if tracer:
                tracer.on_request_start(
                    prompt=prompt,
                    model_name=model_name,
                    params=params,
                    request_data_preview=str(request_data.get("f.req", ""))[:300],
                    chat_metadata=list(chat.metadata) if isinstance(chat, ChatSession) else [],
                    use_pro=use_pro,
                    file_count=len(req_file_data) if req_file_data else 0,
                )

            _empty_retries = 0
            _MAX_EMPTY_RETRIES = 2  # retry up to 2 times on empty stream

            while True:  # polling loop for queueing/thinking retries
                _poll_iterations += 1
                response = await self.client.request(
                    "POST",
                    Endpoint.get_generate_url(self.account_index),
                    params=params,
                    headers=model.model_header,
                    data=request_data,
                    stream=True,
                    **kwargs,
                )

                if tracer:
                    tracer.on_response_meta(
                        status_code=response.status_code,
                        headers=sanitize_headers(dict(response.headers) if hasattr(response, "headers") else {}),
                        poll_iteration=_poll_iterations,
                    )

                if response.status_code != 200:
                    if tracer:
                        tracer.on_request_end(status="http_error", error=f"status={response.status_code}", final_flags=None, chat_metadata_after=None, poll_iterations=_poll_iterations)
                    await self.close()
                    raise APIError(f"Failed to generate contents. Status: {response.status_code}")

                if self.client:
                    self.cookies.update(self.client.cookies)

                buffer = ""
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

                # Track last seen content. streaming_state allows persistence across retries.
                if streaming_state is None:
                    streaming_state = _StreamingState()

                last_texts = streaming_state.last_texts
                last_thoughts = streaming_state.last_thoughts
                last_progress_time = time.time()  # reset per polling iteration to avoid false stall detection
                streaming_state.last_progress_time = last_progress_time
                flags = _StreamFlags(deep_research=deep_research)
                _FIRST_CHUNK_TIMEOUT = 30  # seconds to wait for first chunk

                # Wrap aiter_content with first-chunk timeout detection
                content_iter = response.aiter_content().__aiter__()
                _got_first_chunk = False
                while True:
                    try:
                        if not _got_first_chunk:
                            chunk = await asyncio.wait_for(content_iter.__anext__(), timeout=_FIRST_CHUNK_TIMEOUT)
                            _got_first_chunk = True
                        else:
                            chunk = await content_iter.__anext__()
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        logger.warning(f"No data received from Gemini within {_FIRST_CHUNK_TIMEOUT}s (zombie connection)")
                        raise APIError(f"No response from Gemini within {_FIRST_CHUNK_TIMEOUT}s")

                    # — original loop body below —
                    buffer += decoder.decode(chunk, final=False)
                    if buffer.startswith(")]}'"):
                        buffer = buffer[4:].lstrip()
                    parsed_parts, buffer = parse_response_by_frame(buffer)

                    got_update = False
                    for part in parsed_parts:
                        result = await self._process_stream_part(part, model, chat, last_texts, last_thoughts, flags, use_pro, tracer=tracer)
                        if result:
                            yield result
                            got_update = True
                            # If video generation is pending, break out of stream immediately
                            # so the caller can start polling instead of waiting for timeout
                            if result.candidates and any(
                                c.text and _is_video_generation_pending(c.text) for c in result.candidates
                            ):
                                logger.info("Video generation pending detected, breaking stream to start polling.")
                                return

                    if got_update or flags.is_thinking:
                        last_progress_time = time.time()
                        streaming_state.last_progress_time = last_progress_time
                        continue

                    stall_threshold = min(self.timeout, self.watchdog_timeout)
                    if (time.time() - last_progress_time) > stall_threshold:
                        logger.warning(f"Response stalled (active connection but no progress for {stall_threshold}s). Queueing={flags.is_queueing}. Retrying...")
                        if tracer:
                            tracer.on_request_end(status="stalled", error="Response stalled (zombie stream).", final_flags=None, chat_metadata_after=None, poll_iterations=_poll_iterations)
                        await self.close()
                        raise APIError("Response stalled (zombie stream).")

                # Final flush
                buffer += decoder.decode(b"", final=True)
                if buffer:
                    parsed_parts, _ = parse_response_by_frame(buffer)
                    for part in parsed_parts:
                        result = await self._process_stream_part(part, model, chat, last_texts, last_thoughts, flags, use_pro, tracer=tracer)
                        if result:
                            yield result

                _flags_dict = {
                    "is_completed": flags.is_completed,
                    "is_final_chunk": flags.is_final_chunk,
                    "is_thinking": flags.is_thinking,
                    "is_queueing": flags.is_queueing,
                }

                if not (flags.is_completed or flags.is_final_chunk or flags.has_candidates):
                    _empty_retries += 1
                    if _empty_retries <= _MAX_EMPTY_RETRIES:
                        logger.info(f"Empty stream (attempt {_empty_retries}/{_MAX_EMPTY_RETRIES}), retrying in 2s...")
                        await asyncio.sleep(2)
                        continue  # retry within polling loop
                    logger.warning(f"Stream ended with no content after {_empty_retries} attempts (flags={_flags_dict})")
                    if tracer:
                        tracer.on_request_end(status="empty", error="Stream ended with no content.", final_flags=_flags_dict, chat_metadata_after=None, poll_iterations=_poll_iterations)
                elif tracer:
                    tracer.on_request_end(
                        status="ok",
                        error=None,
                        final_flags=_flags_dict,
                        chat_metadata_after=list(chat.metadata) if isinstance(chat, ChatSession) else [],
                        poll_iterations=_poll_iterations,
                    )
                break  # stream completed, exit polling loop

        except RequestsError as exc:
            if "timeout" in str(exc).lower():
                if tracer:
                    tracer.on_request_end(status="timeout", error=str(exc), final_flags=None, chat_metadata_after=None, poll_iterations=locals().get("_poll_iterations", 0))
                raise RequestTimeoutError(
                    "The request timed out while waiting for Gemini to respond. This often happens with very long prompts "
                    "or complex file analysis. Try increasing the 'timeout' value when initializing GeminiClient."
                ) from exc
            if tracer:
                tracer.on_request_end(status="request_error", error=str(exc), final_flags=None, chat_metadata_after=None, poll_iterations=locals().get("_poll_iterations", 0))
            raise
        except (GeminiError, APIError):
            if tracer:
                tracer.on_request_end(status="api_error", error="GeminiError/APIError raised", final_flags=None, chat_metadata_after=None, poll_iterations=locals().get("_poll_iterations", 0))
            raise
        except Exception as e:
            if tracer:
                tracer.on_request_end(status="parse_error", error=str(e), final_flags=None, chat_metadata_after=None, poll_iterations=locals().get("_poll_iterations", 0))
            logger.debug(f"{type(e).__name__}: {e}; Unexpected response or parsing error. Response: {locals().get('response', 'N/A')}")
            raise APIError(f"Failed to parse response body: {e}") from e

    async def _process_stream_part(
        self,
        part: list[Any],
        model: Model,
        chat: Optional["ChatSession"],
        last_texts: dict[str, str],
        last_thoughts: dict[str, str],
        flags: _StreamFlags,
        use_pro: bool = False,
        tracer: Tracer | None = None,
    ) -> ModelOutput | None:
        """Process a single stream part and return ModelOutput if candidates found."""
        # Check for fatal error codes
        error_code = get_nested_value(part, [5, 2, 0, 1, 0])
        if error_code:
            await self.close()
            _raise_for_error_code(error_code, model.model_name)

        # Detect thinking state and image model
        if "data_analysis_tool" in str(part):
            flags.is_thinking = True
            if not flags.has_candidates:
                logger.debug("Model is active (thinking/analyzing)...")

            # Check for image model in data_analysis_tool message
            # The message is at path [6][1][2] in the inner JSON or can be found in raw part
            if flags.detected_image_model is None:
                part_str = str(part)
                detected = _detect_image_model(part_str)
                if detected:
                    flags.detected_image_model = detected
                    logger.debug(f"Detected image model: {detected}")

        # Check for queueing status
        status = get_nested_value(part, [5])
        if isinstance(status, list) and status:
            flags.is_queueing = True
            if not flags.has_candidates:
                logger.debug("Model is in a waiting state (queueing)...")

        inner_json_str = get_nested_value(part, [2])
        if not inner_json_str:
            if tracer:
                tracer.on_stream_frame(part=part, part_json=None, flags={"is_thinking": flags.is_thinking, "is_queueing": flags.is_queueing, "has_candidates": flags.has_candidates, "is_completed": flags.is_completed, "is_final_chunk": flags.is_final_chunk, "detected_image_model": flags.detected_image_model})
            return None

        try:
            part_json = json.loads(inner_json_str)
        except json.JSONDecodeError:
            if tracer:
                tracer.on_stream_frame(part=part, part_json=None, flags={"is_thinking": flags.is_thinking, "is_queueing": flags.is_queueing, "has_candidates": flags.has_candidates, "is_completed": flags.is_completed, "is_final_chunk": flags.is_final_chunk, "detected_image_model": flags.detected_image_model})
            return None

        if tracer:
            tracer.on_stream_frame(part=part, part_json=part_json, flags={"is_thinking": flags.is_thinking, "is_queueing": flags.is_queueing, "has_candidates": flags.has_candidates, "is_completed": flags.is_completed, "is_final_chunk": flags.is_final_chunk, "detected_image_model": flags.detected_image_model})

        # Update chat metadata
        m_data = get_nested_value(part_json, [1])
        if m_data and isinstance(chat, ChatSession):
            chat.metadata = m_data

        # Check for completion
        context_str = get_nested_value(part_json, [25])
        if isinstance(context_str, str):
            flags.is_completed = True
            flags.is_thinking = False
            flags.is_queueing = False
            if isinstance(chat, ChatSession):
                chat.metadata = [None] * 9 + [context_str]

        # Process candidates
        candidates_list = get_nested_value(part_json, [4], [])
        if not candidates_list:
            return None

        output_candidates = self._parse_candidates(candidates_list, chat, last_texts, last_thoughts, flags)

        if not output_candidates:
            return None

        # Check for image model mismatch (user requested Pro but got standard)
        if use_pro and flags.detected_image_model == "standard":
            raise ImageModelMismatch(
                "Requested Nano Banana Pro (use_pro=True) but Gemini loaded standard Nano Banana instead. "
                "This may happen due to rate limiting or account restrictions. Try again later or use use_pro=False."
            )

        flags.is_thinking = False
        flags.is_queueing = False
        return ModelOutput(
            metadata=get_nested_value(part_json, [1], []),
            candidates=output_candidates,
        )

    def _parse_candidates(
        self,
        candidates_list: list[Any],
        chat: Optional["ChatSession"],
        last_texts: dict[str, str],
        last_thoughts: dict[str, str],
        flags: _StreamFlags,
    ) -> list[Candidate]:
        """Parse candidate data into Candidate objects."""
        output_candidates = []

        for i, candidate_data in enumerate(candidates_list):
            rcid = get_nested_value(candidate_data, [0])
            if not rcid:
                continue

            if isinstance(chat, ChatSession):
                chat.rcid = rcid

            candidate = self._parse_single_candidate(candidate_data, i, rcid, last_texts, last_thoughts, flags)
            if candidate:
                output_candidates.append(candidate)

        return output_candidates

    def _parse_single_candidate(
        self,
        candidate_data: list[Any],
        index: int,
        rcid: str,
        last_texts: dict[str, str],
        last_thoughts: dict[str, str],
        flags: _StreamFlags,
    ) -> Candidate | None:
        """Parse a single candidate from the response."""
        # Debug dump disabled by default — set GEMINI_DEBUG_DUMP=1 to enable
        if os.environ.get("GEMINI_DEBUG_DUMP"):
            try:
                dump_dir = Path("/tmp/gemini_debug")
                dump_dir.mkdir(exist_ok=True)
                dump_file = dump_dir / f"candidate_{index}_{rcid}_{int(time.time()*1000)}.json"
                dump_file.write_bytes(json.dumps(candidate_data, default=str, option=json.OPT_INDENT_2))
                logger.info(f"[RAW_CANDIDATE] dumped to {dump_file}")
            except Exception as e:
                logger.warning(f"[RAW_CANDIDATE] dump failed: {e}")

        text = _extract_candidate_text(candidate_data)

        # Check for rate limiting responses
        _check_rate_limit_response(text)

        # Check for image generation blocked responses
        _check_image_gen_blocked(text)

        thoughts = get_nested_value(candidate_data, [37, 0, 0]) or ""

        web_images, generated_images, generated_videos = _parse_all_media(
            candidate_data, self.proxy, self.cookies, self.account_index, self.session_kwargs
        )

        # Parse music/audio data — try [12][0][87] first, fallback to [12][86]
        generated_media: list[GeneratedMedia] = []
        media_data = get_nested_value(candidate_data, [12, 0, 87], None)
        if media_data is None:
            media_data = get_nested_value(candidate_data, [12, 86], None)
        if media_data:
            mp3_url = ""
            mp3_thumb = ""
            mp3_list = get_nested_value(media_data, [0, 1, 7], [])
            if isinstance(mp3_list, list) and len(mp3_list) >= 2:
                mp3_thumb = mp3_list[0] or ""
                mp3_url = mp3_list[1] or ""

            mp4_url = ""
            mp4_thumb = ""
            mp4_list = get_nested_value(media_data, [1, 1, 7], [])
            if isinstance(mp4_list, list) and len(mp4_list) >= 2:
                mp4_thumb = mp4_list[0] or ""
                mp4_url = mp4_list[1] or ""

            if mp3_url or mp4_url:
                generated_media.append(
                    GeneratedMedia(
                        url=mp4_url or "",
                        thumbnail_url=mp4_thumb,
                        mp3_url=mp3_url,
                        mp3_thumbnail=mp3_thumb,
                        cookies=self.cookies,
                        proxy=self.proxy,
                        account_index=self.account_index,
                        session_kwargs=self.session_kwargs,
                    )
                )

        # Determine if this frame represents the final state
        flags.is_final_chunk = isinstance(get_nested_value(candidate_data, [2]), list) or get_nested_value(candidate_data, [8, 0], 1) == 2

        # Calculate deltas
        last_sent_text = last_texts.get(rcid) or last_texts.get(f"idx_{index}", "")
        text_delta, new_full_text = get_delta_by_fp_len(text, last_sent_text, is_final=flags.is_final_chunk)

        last_sent_thought = last_thoughts.get(rcid) or last_thoughts.get(f"idx_{index}", "")
        if thoughts:
            thoughts_delta, new_full_thought = get_delta_by_fp_len(thoughts, last_sent_thought, is_final=flags.is_final_chunk)
        else:
            thoughts_delta = ""
            new_full_thought = ""

        # Extract deep research plan if in deep research mode
        deep_research_plan = None
        if flags.deep_research:
            plan_data = extract_deep_research_plan(
                candidate_data,
                fallback_text=text,
            )
            if plan_data:
                deep_research_plan = DeepResearchPlan(**plan_data)
            else:
                # Plan extraction failed but there might still be a research_id
                # (e.g., in the confirmation response where "57" has a different structure)
                from .utils.research import _extract_research_id
                rid = _extract_research_id(candidate_data)
                if rid:
                    deep_research_plan = DeepResearchPlan(research_id=rid, response_text=text)

        if text_delta or thoughts_delta or web_images or generated_images or generated_videos or generated_media or deep_research_plan:
            flags.has_candidates = True

        # Update state
        last_texts[rcid] = last_texts[f"idx_{index}"] = new_full_text
        last_thoughts[rcid] = last_thoughts[f"idx_{index}"] = new_full_thought

        return Candidate(
            rcid=rcid,
            text=text,
            text_delta=text_delta,
            thoughts=thoughts or None,
            thoughts_delta=thoughts_delta,
            web_images=web_images,
            generated_images=generated_images,
            generated_videos=generated_videos,
            generated_media=generated_media,
            deep_research_plan=deep_research_plan,
        )

    def start_chat(self, **kwargs) -> "ChatSession":
        """
        Returns a `ChatSession` object attached to this client.

        Parameters
        ----------
        kwargs: `dict`, optional
            Additional arguments which will be passed to the chat session.
            Refer to `gemini_webapi.ChatSession` for more information.

        Returns
        -------
        :class:`ChatSession`
            Empty chat session object for retrieving conversation history.
        """

        return ChatSession(geminiclient=self, **kwargs)

    async def delete_chat(self, cid: str) -> None:
        """
        Delete a specific conversation by chat id.

        Parameters
        ----------
        cid: `str`
            The ID of the chat requiring deletion (e.g. "c_...").
        """

        await self._batch_execute(
            [
                RPCData(
                    rpcid=GRPC.DELETE_CHAT,
                    payload=json.dumps([cid]).decode("utf-8"),
                ),
            ]
        )

    @running(retry=2)
    async def _batch_execute(self, payloads: list[RPCData], close_on_error: bool = True, **kwargs) -> Response:
        """
        Execute a batch of requests to Gemini API.

        Parameters
        ----------
        payloads: `list[RPCData]`
            List of `gemini_webapi.types.RPCData` objects to be executed.
        kwargs: `dict`, optional
            Additional arguments which will be passed to the post request.
            Refer to `curl_cffi.requests.AsyncSession.request` for more information.

        Returns
        -------
        :class:`curl_cffi.requests.Response`
            Response object containing the result of the batch execution.
        """

        _reqid = self._reqid
        self._reqid += 100000

        try:
            params: dict[str, Any] = {
                "rpcids": ",".join([p.rpcid for p in payloads]),
                "_reqid": _reqid,
                "rt": "c",
                "source-path": Endpoint.get_source_path(self.account_index),
            }
            if self.build_label:
                params["bl"] = self.build_label
            if self.session_id:
                params["f.sid"] = self.session_id

            response = await self.client.post(
                Endpoint.get_batch_exec_url(self.account_index),
                params=params,
                data={
                    "at": self.access_token,
                    "f.req": json.dumps([[payload.serialize() for payload in payloads]]).decode("utf-8"),
                },
                **kwargs,
            )
        except RequestsError as exc:
            if "timeout" in str(exc).lower():
                raise RequestTimeoutError(
                    "The request timed out while waiting for Gemini to respond. This often happens with very long prompts "
                    "or complex file analysis. Try increasing the 'timeout' value when initializing GeminiClient."
                ) from exc
            raise

        if response.status_code != 200:
            if close_on_error:
                await self.close()
            raise APIError(f"Batch execution failed with status code {response.status_code}")

        if self.client:
            self.cookies.update(self.client.cookies)

        return response

    async def _poll_video_generation(self, cid: str, verbose: bool = False) -> list[GeneratedVideo]:
        """
        Poll for video generation completion using READ_CHAT RPC.

        Parameters
        ----------
        cid : str
            Conversation ID (e.g., "c_27f6ab77b809248f")
        verbose : bool
            Whether to log polling progress

        Returns
        -------
        list[GeneratedVideo]
            List of generated videos when ready, empty list if polling times out
        """
        for attempt in range(_VIDEO_POLL_MAX_ATTEMPTS):
            if verbose:
                logger.debug(f"Polling for video generation (attempt {attempt + 1}/{_VIDEO_POLL_MAX_ATTEMPTS})...")

            await asyncio.sleep(_VIDEO_POLL_INTERVAL)

            try:
                # Call READ_CHAT RPC to check video status
                # Payload format: ["cid", 10, null, 1, [1], [4], null, 1]
                payload = json.dumps([cid, 10, None, 1, [1], [4], None, 1]).decode("utf-8")
                response = await self._batch_execute([RPCData(rpcid=GRPC.READ_CHAT, payload=payload)])

                # Parse the response
                response_text = response.text
                if not response_text:
                    continue

                # Skip the ")]}'" prefix
                if response_text.startswith(")]}'"):
                    response_text = response_text[5:]

                # Parse frames from response
                parsed_parts, _ = parse_response_by_frame(response_text)

                for part in parsed_parts:
                    # Look for READ_CHAT response
                    if get_nested_value(part, [1]) != GRPC.READ_CHAT:
                        continue

                    inner_json_str = get_nested_value(part, [2])
                    if not inner_json_str:
                        continue

                    try:
                        part_json = json.loads(inner_json_str)
                    except json.JSONDecodeError:
                        continue

                    # Check if video is ready by looking for "Your video is ready"
                    # The response structure: [[[[cid, rid], null, [...], [[[rcid, [text], ...]]]...]]]
                    candidates = get_nested_value(part_json, [0, 0, 3], [])
                    for candidate_data in candidates:
                        text = get_nested_value(candidate_data, [1, 0], "")
                        # Check for video ready in any language
                        if any(kw in text.lower() for kw in ["video is ready", "视频已准备就绪", "视频已准备好"]):
                            if verbose:
                                logger.debug("Video generation completed!")
                            _, _, videos = _parse_all_media(candidate_data, self.proxy, self.cookies, self.account_index, self.session_kwargs)
                            if videos:
                                return videos

                        # Also check: not pending anymore but has video URLs = ready
                        if not _is_video_generation_pending(text):
                            _, _, videos = _parse_all_media(candidate_data, self.proxy, self.cookies, self.account_index, self.session_kwargs)
                            if videos:
                                return videos
                        else:
                            if verbose:
                                logger.debug("Video still generating...")
                            break

            except Exception as e:
                if verbose:
                    logger.debug(f"Polling error: {e}")
                continue

        logger.warning(f"Video generation polling timed out after {_VIDEO_POLL_MAX_ATTEMPTS * _VIDEO_POLL_INTERVAL} seconds")
        return []

    async def fetch_latest_chat_response(self, cid: str) -> ModelOutput | None:
        """
        Fetch the latest model response from an existing chat by reading chat history.
        Also handles Deep Research immersive results stored at candidate[0][30][0][4].

        Parameters
        ----------
        cid : str
            Conversation ID

        Returns
        -------
        ModelOutput | None
            The latest model response, or None if not found.
        """
        try:
            payload = json.dumps([cid, 10, None, 1, [1], [4], None, 1]).decode("utf-8")
            response = await self._batch_execute(
                [RPCData(rpcid=GRPC.READ_CHAT, payload=payload)],
                close_on_error=False,
            )

            response_text = response.text
            if not response_text:
                return None

            if response_text.startswith(")]}'"):
                response_text = response_text[5:]

            parsed_parts, _ = parse_response_by_frame(response_text)

            for part in parsed_parts:
                if get_nested_value(part, [1]) != GRPC.READ_CHAT:
                    continue

                inner_json_str = get_nested_value(part, [2])
                if not inner_json_str:
                    continue

                try:
                    part_json = json.loads(inner_json_str)
                except json.JSONDecodeError:
                    continue

                candidates_data = get_nested_value(part_json, [0, 0, 3], [])
                if not candidates_data:
                    continue

                output_candidates = []
                for candidate_data in candidates_data:
                    # READ_CHAT wraps candidates in an extra layer:
                    # candidate_data[0] = [rcid, [text], ...metadata...]
                    inner = candidate_data
                    if isinstance(get_nested_value(candidate_data, [0]), list):
                        inner = candidate_data[0]

                    rcid = get_nested_value(inner, [0])
                    if not isinstance(rcid, str):
                        continue

                    # Standard text path
                    text = get_nested_value(inner, [1, 0], "")

                    # Deep Research immersive result
                    immersive_text = get_nested_value(inner, [30, 0, 4])
                    sources = []
                    if isinstance(immersive_text, str) and len(immersive_text) > len(text or ""):
                        text = immersive_text
                        # Extract sources from [30][0][5]
                        sources = _extract_research_sources(get_nested_value(inner, [30, 0, 5]))

                    if text:
                        output_candidates.append(
                            Candidate(rcid=rcid, text=text, sources=sources)
                        )

                if output_candidates:
                    metadata_raw = get_nested_value(part_json, [0, 0, 0], [])
                    return ModelOutput(
                        metadata=metadata_raw if isinstance(metadata_raw, list) else [cid],
                        candidates=output_candidates,
                    )

        except Exception as e:
            logger.debug(f"fetch_latest_chat_response({cid!r}) failed: {type(e).__name__}: {e}")

        return None


class ChatSession:
    """
    Chat data to retrieve conversation history. Only if all 3 ids are provided will the conversation history be retrieved.

    Parameters
    ----------
    geminiclient: `GeminiClient`
        Async client interface for gemini.google.com.
    metadata: `list[str]`, optional
        List of chat metadata `[cid, rid, rcid]`, can be shorter than 3 elements, like `[cid, rid]` or `[cid]` only.
    cid: `str`, optional
        Chat id, if provided together with metadata, will override the first value in it.
    rid: `str`, optional
        Reply id, if provided together with metadata, will override the second value in it.
    rcid: `str`, optional
        Reply candidate id, if provided together with metadata, will override the third value in it.
    model: `Model | str | dict`, optional
        Specify the model to use for generation.
        Pass either a `gemini_webapi.constants.Model` enum or a model name string to use predefined models.
        Pass a dictionary to use custom model header strings ("model_name" and "model_header" keys must be provided).
    gem: `Gem | str`, optional
        Specify a gem to use as system prompt for the chat session.
        Pass either a `gemini_webapi.types.Gem` object or a gem id string.
    """

    __slots__ = [
        "__metadata",
        "gem",
        "geminiclient",
        "last_output",
        "model",
    ]

    def __init__(
        self,
        geminiclient: GeminiClient,
        metadata: list[str | None] | None = None,
        cid: str | None = None,  # chat id
        rid: str | None = None,  # reply id
        rcid: str | None = None,  # reply candidate id
        model: Model | str | dict = Model.UNSPECIFIED,
        gem: Gem | str | None = None,
    ):
        self.__metadata: list[str | None] = [
            "",
            "",
            "",
            None,
            None,
            None,
            None,
            None,
            None,
            "",
        ]
        self.geminiclient: GeminiClient = geminiclient
        self.last_output: ModelOutput | None = None
        self.model: Model | str | dict = model
        self.gem: Gem | str | None = gem

        if metadata:
            self.metadata = metadata
        if cid:
            self.cid = cid
        if rid:
            self.rid = rid
        if rcid:
            self.rcid = rcid

    def __str__(self):
        return f"ChatSession(cid='{self.cid}', rid='{self.rid}', rcid='{self.rcid}')"

    __repr__ = __str__

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        # update conversation history when last output is updated
        if name == "last_output" and isinstance(value, ModelOutput):
            self.metadata = value.metadata
            self.rcid = value.rcid

    async def send_message(
        self,
        prompt: str,
        files: list[str | Path] | None = None,
        deep_research: bool = False,
        **kwargs,
    ) -> ModelOutput:
        """
        Generates contents with prompt.
        Use as a shortcut for `GeminiClient.generate_content(prompt, image, self)`.

        Parameters
        ----------
        prompt: `str`
            Prompt provided by user.
        files: `list[str | Path]`, optional
            List of file paths to be attached.
        deep_research: `bool`, optional
            If True, enable deep research mode.
        kwargs: `dict`, optional
            Additional arguments which will be passed to the post request.
            Refer to `curl_cffi.requests.AsyncSession.request` for more information.

        Returns
        -------
        :class:`ModelOutput`
            Output data from gemini.google.com, use `ModelOutput.text` to get the default text reply, `ModelOutput.images` to get a list
            of images in the default reply, `ModelOutput.candidates` to get a list of all answer candidates in the output.

        Raises
        ------
        `AssertionError`
            If prompt is empty.
        `gemini_webapi.TimeoutError`
            If request timed out.
        `gemini_webapi.GeminiError`
            If no reply candidate found in response.
        `gemini_webapi.APIError`
            - If request failed with status code other than 200.
            - If response structure is invalid and failed to parse.
        """

        return await self.geminiclient.generate_content(
            prompt=prompt,
            files=files,
            model=self.model,
            gem=self.gem,
            chat=self,
            deep_research=deep_research,
            **kwargs,
        )

    async def send_message_stream(
        self,
        prompt: str,
        files: list[str | Path] | None = None,
        **kwargs,
    ) -> AsyncGenerator[ModelOutput, None]:
        """
        Generates contents with prompt in streaming mode within this chat session.

        This is a shortcut for `GeminiClient.generate_content_stream(prompt, files, self)`.
        The session's metadata and conversation history are automatically managed.

        Parameters
        ----------
        prompt: `str`
            Prompt provided by user.
        files: `list[str | Path]`, optional
            List of file paths to be attached.
        kwargs: `dict`, optional
            Additional arguments passed to the streaming request.

        Yields
        ------
        :class:`ModelOutput`
            Partial output data containing text deltas.
        """

        async for output in self.geminiclient.generate_content_stream(
            prompt=prompt,
            files=files,
            model=self.model,
            gem=self.gem,
            chat=self,
            **kwargs,
        ):
            yield output

    def choose_candidate(self, index: int) -> ModelOutput:
        """
        Choose a candidate from the last `ModelOutput` to control the ongoing conversation flow.

        Parameters
        ----------
        index: `int`
            Index of the candidate to choose, starting from 0.

        Returns
        -------
        :class:`ModelOutput`
            Output data of the chosen candidate.

        Raises
        ------
        `ValueError`
            If no previous output data found in this chat session, or if index exceeds the number of candidates in last model output.
        """

        if not self.last_output:
            raise ValueError("No previous output data found in this chat session.")

        if index >= len(self.last_output.candidates):
            raise ValueError(f"Index {index} exceeds the number of candidates in last model output.")

        self.last_output.chosen = index
        self.rcid = self.last_output.rcid
        return self.last_output

    @property
    def metadata(self):
        return self.__metadata

    @metadata.setter
    def metadata(self, value: list[str]):
        if not isinstance(value, list):
            return

        # Update only non-None elements to preserve existing CID/RID/RCID/Context
        for i, val in enumerate(value):
            if i < 10 and val is not None:
                self.__metadata[i] = val

    @property
    def cid(self):
        return self.__metadata[0]

    @cid.setter
    def cid(self, value: str):
        self.__metadata[0] = value

    @property
    def rcid(self):
        return self.__metadata[2]

    @rcid.setter
    def rcid(self, value: str):
        self.__metadata[2] = value

    @property
    def rid(self):
        return self.__metadata[1]

    @rid.setter
    def rid(self, value: str):
        self.__metadata[1] = value
