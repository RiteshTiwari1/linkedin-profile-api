"""A closed error taxonomy.

Design rule for this service: the client never sees a bare 500. Every failure
mode LinkedIn can throw at us maps onto one of these codes, so a caller can
branch on `error.code` instead of parsing prose.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """Base for every expected failure. `code` is part of the public contract."""

    code = "INTERNAL_ERROR"
    status_code = 500
    message = "Unexpected server error."
    retryable = False

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details or {}
        self.retry_after = retry_after
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": "error",
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            },
        }
        if self.details:
            body["error"]["details"] = self.details
        if self.retry_after is not None:
            body["error"]["retry_after_seconds"] = self.retry_after
        return body


# --- Caller's fault --------------------------------------------------------


class InvalidProfileUrl(ApiError):
    code = "INVALID_URL"
    status_code = 400
    message = "Not a recognisable LinkedIn profile URL."


class Unauthorized(ApiError):
    code = "UNAUTHORIZED"
    status_code = 401
    message = "Missing or invalid API key. Send it in the X-API-Key header."


class ProfileNotFound(ApiError):
    code = "PROFILE_NOT_FOUND"
    status_code = 404
    message = "No LinkedIn profile exists at that URL."


# --- Visibility ------------------------------------------------------------


class ProfilePrivate(ApiError):
    code = "PROFILE_PRIVATE"
    status_code = 403
    message = (
        "The profile is out of network or restricted; LinkedIn returns little "
        "or nothing for it with these credentials."
    )


# --- Our side / upstream ---------------------------------------------------


class NoSessionsConfigured(ApiError):
    code = "NO_SESSIONS_CONFIGURED"
    status_code = 503
    message = (
        "No LinkedIn session is configured. Set LI_AT and JSESSIONID, or run "
        "with DEMO_MODE=true to serve recorded fixtures."
    )


class SessionExpired(ApiError):
    code = "SESSION_EXPIRED"
    status_code = 503
    message = (
        "The LinkedIn session cookie is no longer valid. Log in again and "
        "refresh LI_AT / JSESSIONID."
    )
    retryable = False


class RateLimited(ApiError):
    code = "RATE_LIMITED"
    status_code = 429
    message = "Local rate limit reached; slowing down to protect the LinkedIn account."
    retryable = True


class UpstreamBlocked(ApiError):
    code = "UPSTREAM_BLOCKED"
    status_code = 429
    message = (
        "LinkedIn refused the request (bot check or throttle). The session has "
        "been put in cooldown."
    )
    retryable = True


class UpstreamError(ApiError):
    code = "UPSTREAM_ERROR"
    status_code = 502
    message = "LinkedIn returned an unexpected response."
    retryable = True


class EndpointRetired(UpstreamError):
    """LinkedIn answered 410 Gone: this endpoint no longer exists.

    Distinct from UpstreamError because it is not a failed *attempt* -- the
    strategy never ran. Treating it as one let a retired endpoint outvote a real
    verdict from a working one, turning "no such profile" into a 502.
    """

    code = "ENDPOINT_RETIRED"
    message = "LinkedIn has retired this endpoint."


class ParseFailed(ApiError):
    code = "PARSE_FAILED"
    status_code = 502
    message = (
        "Fetched the profile but could not map it onto the schema -- LinkedIn "
        "has most likely changed its response shape."
    )
    retryable = False


class FixtureMissing(ApiError):
    code = "FIXTURE_MISSING"
    status_code = 404
    message = "DEMO_MODE is on and no recorded fixture exists for that profile."
