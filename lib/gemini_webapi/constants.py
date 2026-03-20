from enum import Enum, IntEnum, StrEnum


class Endpoint(StrEnum):
    GOOGLE = "https://www.google.com"
    INIT = "https://gemini.google.com/app"
    GENERATE = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
    ROTATE_COOKIES = "https://accounts.google.com/RotateCookies"
    UPLOAD = "https://content-push.googleapis.com/upload"  # Legacy, use get_upload_url() instead
    BATCH_EXEC = "https://gemini.google.com/_/BardChatUi/data/batchexecute"

    @staticmethod
    def _get_account_prefix(account_index: int) -> str:
        """Get the account path prefix for URLs (e.g., '/u/2' or '')."""
        return f"/u/{account_index}" if account_index > 0 else ""

    @staticmethod
    def get_init_url(account_index: int = 0) -> str:
        """
        Get the initialization URL for a specific Google account.

        Parameters
        ----------
        account_index : int
            Google account index (0-based). When multiple Google accounts are signed in,
            this corresponds to the /u/{index}/ path in Google URLs.
            - 0 = first account (default, equivalent to /u/0/ or just /app)
            - 1 = second account (/u/1/app)
            - 2 = third account (/u/2/app)
            etc.

        Returns
        -------
        str
            The full URL to initialize the Gemini client for the specified account.
        """
        prefix = Endpoint._get_account_prefix(account_index)
        return f"https://gemini.google.com{prefix}/app"

    @staticmethod
    def get_generate_url(account_index: int = 0) -> str:
        """
        Get the generate/streaming URL for a specific Google account.

        Parameters
        ----------
        account_index : int
            Google account index (0-based).

        Returns
        -------
        str
            The full URL for the StreamGenerate endpoint for the specified account.
        """
        prefix = Endpoint._get_account_prefix(account_index)
        return f"https://gemini.google.com{prefix}/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"

    @staticmethod
    def get_batch_exec_url(account_index: int = 0) -> str:
        """
        Get the batch execute URL for a specific Google account.

        Parameters
        ----------
        account_index : int
            Google account index (0-based).

        Returns
        -------
        str
            The full URL for the batchexecute endpoint for the specified account.
        """
        prefix = Endpoint._get_account_prefix(account_index)
        return f"https://gemini.google.com{prefix}/_/BardChatUi/data/batchexecute"

    @staticmethod
    def get_source_path(account_index: int = 0) -> str:
        """
        Get the source-path parameter value for a specific Google account.

        Parameters
        ----------
        account_index : int
            Google account index (0-based).

        Returns
        -------
        str
            The source-path value (e.g., '/app' or '/u/2/app').
        """
        prefix = Endpoint._get_account_prefix(account_index)
        return f"{prefix}/app"

    @staticmethod
    def get_upload_url(account_index: int = 0) -> str:
        """
        Get the file upload URL for a specific Google account.

        Uses push.clients6.google.com with authuser parameter for multi-account support.

        Parameters
        ----------
        account_index : int
            Google account index (0-based).

        Returns
        -------
        str
            The full URL for file uploads with authuser parameter.
        """
        return f"https://push.clients6.google.com/upload/?authuser={account_index}"


class GRPC(StrEnum):
    """
    Google RPC ids used in Gemini API.
    """

    # Chat methods
    LIST_CHATS = "MaZiqc"
    READ_CHAT = "hNvQHb"
    DELETE_CHAT = "GzXR5e"

    # Gem methods
    LIST_GEMS = "CNgdBe"
    CREATE_GEM = "oMH3Zd"
    UPDATE_GEM = "kHv0Vd"
    DELETE_GEM = "UXcSJb"

    # Activity methods
    BARD_ACTIVITY = "ESY5D"


class Headers(Enum):
    GEMINI = {
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "X-Same-Domain": "1",
    }
    ROTATE_COOKIES = {
        "Content-Type": "application/json",
    }
    # Note: UPLOAD headers are now defined in upload_file.py for the resumable upload protocol


class Model(Enum):
    UNSPECIFIED = ("unspecified", {}, False)
    G_3_0_PRO = (
        "gemini-3.0-pro",
        {"x-goog-ext-525001261-jspb": '[1,null,null,null,"9d8ca3786ebdfbea",null,null,0,[4],null,null,1]'},
        False,
    )
    G_3_0_FLASH = (
        "gemini-3.0-flash",
        {"x-goog-ext-525001261-jspb": '[1,null,null,null,"fbb127bbb056c959",null,null,0,[4],null,null,1]'},
        False,
    )
    G_3_0_FLASH_THINKING = (
        "gemini-3.0-flash-thinking",
        {"x-goog-ext-525001261-jspb": '[1,null,null,null,"5bf011840784117a",null,null,0,[4],null,null,1]'},
        False,
    )

    def __init__(self, name, header, advanced_only):
        self.model_name = name
        self.model_header = header
        self.advanced_only = advanced_only

    @classmethod
    def from_name(cls, name: str):
        for model in cls:
            if model.model_name == name:
                return model

        raise ValueError(f"Unknown model name: {name}. Available models: {', '.join([model.model_name for model in cls])}")

    @classmethod
    def from_dict(cls, model_dict: dict):
        if "model_name" not in model_dict or "model_header" not in model_dict:
            raise ValueError("When passing a custom model as a dictionary, 'model_name' and 'model_header' keys must be provided.")

        if not isinstance(model_dict["model_header"], dict):
            raise ValueError("When passing a custom model as a dictionary, 'model_header' must be a dictionary containing valid header strings.")

        custom_model = cls.UNSPECIFIED
        custom_model.model_name = model_dict["model_name"]
        custom_model.model_header = model_dict["model_header"]
        return custom_model


class ErrorCode(IntEnum):
    """
    Known error codes returned from server.
    """

    TEMPORARY_ERROR_1013 = 1013  # Randomly raised when generating with certain models, but disappears soon after
    USAGE_LIMIT_EXCEEDED = 1037
    MODEL_INCONSISTENT = 1050
    MODEL_HEADER_INVALID = 1052
    IP_TEMPORARILY_BLOCKED = 1060
