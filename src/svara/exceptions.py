"""Exception hierarchy for the Svara SDK."""

from __future__ import annotations


class SvaraError(Exception):
    """Base class for every error raised by this SDK."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body
        self.request_id = request_id


class APIConnectionError(SvaraError):
    """The request could not reach the API (DNS, TCP, TLS, dropped connection)."""


class APITimeoutError(APIConnectionError):
    """The request timed out."""


class APIStatusError(SvaraError):
    """The API returned a non-2xx status."""


class AuthenticationError(APIStatusError):
    """401 — the API key is missing, malformed, or revoked."""


class PermissionError_(APIStatusError):
    """403 — the key is valid but not allowed to do this."""


class NotFoundError(APIStatusError):
    """404 — voice/resource does not exist."""


class BadRequestError(APIStatusError):
    """400/422 — invalid parameters (e.g. an unsupported response_format)."""


class RateLimitError(APIStatusError):
    """429 — too many concurrent requests / rate limited. Safe to retry with backoff."""


def raise_for_status(status_code: int, body: str, request_id: str | None = None) -> None:
    """Map an HTTP status to the right SvaraError subclass and raise it."""
    msg = f"Svara API error {status_code}: {body}"
    kwargs = dict(status_code=status_code, body=body, request_id=request_id)
    if status_code == 401:
        raise AuthenticationError(msg, **kwargs)
    if status_code == 403:
        raise PermissionError_(msg, **kwargs)
    if status_code == 404:
        raise NotFoundError(msg, **kwargs)
    if status_code in (400, 422):
        raise BadRequestError(msg, **kwargs)
    if status_code == 429:
        raise RateLimitError(msg, **kwargs)
    raise APIStatusError(msg, **kwargs)
