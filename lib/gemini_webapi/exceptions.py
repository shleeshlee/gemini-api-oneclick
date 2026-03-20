class AuthError(Exception):
    """
    Exception for authentication errors caused by invalid credentials/cookies.
    """

    pass


class APIError(Exception):
    """
    Exception for package-level errors which need to be fixed in the future development (e.g. validation errors).
    """

    pass


class ImageGenerationError(APIError):
    """
    Exception for generated image parsing errors.
    """

    pass


class GeminiError(Exception):
    """
    Exception for errors returned from Gemini server which are not handled by the package.
    """

    pass


class ImageGenerationBlocked(GeminiError):
    """
    Exception for when image generation is blocked due to authentication or regional restrictions.
    This typically manifests as "Are you signed in?" or "image creation isn't available" messages.
    """

    pass


class RequestTimeoutError(GeminiError):
    """
    Exception for request timeouts.
    """

    pass


class UsageLimitExceeded(GeminiError):
    """
    Exception for model usage limit exceeded errors.
    """

    pass


class ModelInvalid(GeminiError):
    """
    Exception for invalid model header string errors.
    """

    pass


class TemporarilyBlocked(GeminiError):
    """
    Exception for 429 Too Many Requests when IP is temporarily blocked.
    """

    pass


class RateLimitExceeded(GeminiError):
    """
    Exception for when Gemini rejects a request due to too many requests.
    This is indicated by responses like "I couldn't do that because I'm getting a lot of requests right now."
    """

    pass


class ImageModelMismatch(GeminiError):
    """
    Exception for when the requested image model (Pro) was not used.
    This happens when use_pro=True but Gemini loads the non-Pro model instead.
    """

    pass
