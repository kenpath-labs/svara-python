"""Exception hierarchy for the Svara SDK."""

from __future__ import annotations

import email.utils
import json
import time
from typing import Any, Dict, Mapping, NoReturn, Optional, Tuple

#: A server that asks us to wait longer than this is not worth waiting for —
#: the caller's own timeout will have fired first. Matches the OpenAI SDK.
MAX_RETRY_AFTER_SECONDS = 120.0


class SvaraError(Exception):
    """Base class for every error raised by this SDK."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        #: The raw response body, for logging. ``message`` is the readable form.
        self.body = body
        self.request_id = request_id
        #: Seconds the server asked us to wait, parsed from ``Retry-After``.
        self.retry_after = retry_after
        #: The server's machine-readable status — ``invalid_api_key``,
        #: ``rate_limit_exceeded``, ``too_many_concurrent_requests``,
        #: ``insufficient_quota`` … — when the response carried one. The
        #: vocabulary matches OpenAI's and ElevenLabs', see
        #: https://docs.kenpathlabs.com/rate-limits.
        self.code = code


class MissingAPIKeyError(SvaraError, ValueError):
    """No API key was passed and ``SVARA_API_KEY`` is not set.

    Inherits :class:`ValueError` as well as :class:`SvaraError`: this used to be
    a bare ``ValueError``, and code in the wild catches it that way. Callers who
    would rather have one ``except SvaraError`` around all SDK failures now get
    that too, without anything breaking.
    """


class InvalidRequestError(SvaraError, ValueError):
    """The request was rejected before it was sent — text too long, speed out
    of range, an unsupported sample rate. The server would have answered 422;
    failing locally saves the round trip and names the argument."""


class APIConnectionError(SvaraError):
    """The request could not reach the API (DNS, TCP, TLS, dropped connection)."""


class APITimeoutError(APIConnectionError):
    """The request timed out."""


class StreamInterruptedError(APIConnectionError):
    """The server closed a stream before it finished sending the audio.

    Distinct from a plain connection error because the request was accepted and
    was producing output: some audio may already have been yielded. Raised
    rather than returned quietly, because the alternative — a stream that ends
    early and reports success — reaches the end user as unexplained silence.
    """

    def __init__(self, message: str, *, frames: int = 0, close_code: int | None = None,
                 **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        #: How many audio frames were delivered before the stream died.
        self.frames = frames
        #: The WebSocket close code, when the peer sent one. 1000 is clean,
        #: 1006 means the connection vanished with no close frame, 1011 is the
        #: server reporting its own failure.
        self.close_code = close_code


class APIStatusError(SvaraError):
    """The API returned a non-2xx status."""


class AuthenticationError(APIStatusError):
    """401 — the API key is missing, malformed, or revoked."""


class PermissionError_(APIStatusError):
    """403 — the key is valid but not allowed to do this."""


#: The OpenAI SDK's name for the same error. ``PermissionError_`` carries a
#: trailing underscore only to avoid shadowing Python's builtin.
PermissionDeniedError = PermissionError_


class NotFoundError(APIStatusError):
    """404 — voice/resource does not exist."""


class BadRequestError(APIStatusError):
    """400/422 — invalid parameters (e.g. an unsupported response_format)."""


class UnprocessableEntityError(BadRequestError):
    """422 — a field failed validation; the message names it. A
    :class:`BadRequestError`, so handlers written for 0.1 still catch it."""


class ConflictError(APIStatusError):
    """409 — the resource already exists (a dictionary with that name)."""


class RateLimitError(APIStatusError):
    """429 — too many requests this minute, or every concurrency slot is busy.
    Safe to retry with backoff; the SDK already does, honouring ``Retry-After``."""


class QuotaExceededError(RateLimitError):
    """429 with ``insufficient_quota`` — the monthly character budget is spent.

    Still a :class:`RateLimitError`, so existing handlers catch it, but the SDK
    does **not** retry it: nothing changes until the month rolls over or the
    plan does, and a retry loop here is a retry storm.
    """


class InternalServerError(APIStatusError):
    """5xx — the server failed. Retried automatically, up to ``max_retries``."""


def parse_retry_after(headers: Optional[Mapping[str, str]]) -> Optional[float]:
    """Seconds to wait, from ``Retry-After`` — or ``None`` if unusable.

    Handles the three forms seen in the wild: the non-standard ``retry-after-ms``
    (preferred, it is more precise than whole seconds), a numeric
    ``Retry-After``, and an HTTP-date ``Retry-After``. A server that tells us
    when to come back knows more than our backoff curve does, so this takes
    precedence over it.
    """
    if not headers:
        return None
    ms = headers.get("retry-after-ms")
    if ms is not None:
        try:
            return float(ms) / 1000.0
        except (TypeError, ValueError):
            pass
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    try:
        parsed = email.utils.parsedate_tz(raw)
        if parsed is None:
            return None
        when = email.utils.mktime_tz(parsed)
        return max(0.0, when - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def parse_error_body(body: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """``(code, message)`` from an error response body, whatever its shape.

    The API answers in three shapes, all under ``detail``:

    * ``{"detail": {"status": "invalid_api_key", "message": "…"}}`` — the
      gateway's own errors (auth, rate limits, quota). ``status`` is the code.
    * ``{"detail": "voice 'x' not found …"}`` — a plain string.
    * ``{"detail": [{"loc": ["body", "speed"], "msg": "…"}, …]}`` — pydantic
      validation on a 422, one entry per bad field.

    OpenAI's ``{"error": {"message", "code"}}`` is handled too, since the
    gateway speaks that dialect on some paths. Anything unparseable comes back
    as ``(None, None)`` and the caller falls back to the raw body.
    """
    if not body:
        return None, None
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    detail = data.get("detail", data.get("error"))
    if isinstance(detail, dict):
        code = detail.get("status") or detail.get("code") or detail.get("type")
        msg = detail.get("message") or detail.get("msg")
        return (str(code) if code else None), (str(msg) if msg else None)
    if isinstance(detail, str):
        return None, detail
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            loc = item.get("loc") or []
            field = ".".join(str(x) for x in loc if x not in ("body", "query"))
            msg = item.get("msg") or ""
            parts.append(f"{field}: {msg}" if field else msg)
        return ("validation_error" if parts else None), ("; ".join(parts) or None)
    return None, None


def raise_for_status(
    status_code: int,
    body: str,
    request_id: str | None = None,
    headers: Optional[Mapping[str, str]] = None,
) -> NoReturn:
    """Map an HTTP status to the right SvaraError subclass and raise it."""
    code, detail = parse_error_body(body)
    if detail:
        msg = f"Svara API error {status_code}" + (f" ({code})" if code else "") + f": {detail}"
    else:
        msg = f"Svara API error {status_code}: {body}"
    kwargs: Dict[str, Any] = dict(
        status_code=status_code,
        body=body,
        request_id=request_id,
        retry_after=parse_retry_after(headers),
        code=code,
    )
    if status_code == 401:
        raise AuthenticationError(msg, **kwargs)
    if status_code == 403:
        raise PermissionError_(msg, **kwargs)
    if status_code == 404:
        raise NotFoundError(msg, **kwargs)
    if status_code == 422:
        raise UnprocessableEntityError(msg, **kwargs)
    if status_code == 400:
        raise BadRequestError(msg, **kwargs)
    if status_code == 409:
        raise ConflictError(msg, **kwargs)
    if status_code == 429:
        if code == "insufficient_quota":
            raise QuotaExceededError(msg, **kwargs)
        raise RateLimitError(msg, **kwargs)
    if status_code >= 500:
        raise InternalServerError(msg, **kwargs)
    raise APIStatusError(msg, **kwargs)
