class AppError(Exception):
    """Base class for this package's errors."""


class MissingApiKeyError(AppError):
    """SERVICE_API_KEY is not set."""
