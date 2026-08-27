"""The Voyager HTTP client.

Implements a strategy chain, because LinkedIn is midway through migrating its
profile page and which endpoints answer depends on the account, the A/B bucket
and the week:

    1. profileView   -- legacy REST. One request, a real domain model, easy to
                        parse. Try it first; when it works nothing else is needed.
    2. dash          -- newer REST projection. Needs a decorationId whose
                        trailing version LinkedIn bumps, so we try several and
                        remember the winner.
    3. graphql       -- what linkedin.com uses today. Needs a persisted-query
                        hash that rotates with every frontend build, so it only
                        runs when a queryId has been supplied or harvested.

Everything routes through `_get`, which is where LinkedIn's genuinely hostile
error reporting gets translated into this project's error taxonomy. Notably
LinkedIn does not send a clean 429: you get HTTP 999, or a 302 to
/checkpoint/challenge, or a 200 with an HTML login page. All three mean "stop".
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import Settings
from ..errors import (
    ApiError,
    ProfileNotFound,
    ProfilePrivate,
    RateLimited,
    SessionExpired,
    UpstreamBlocked,
    UpstreamError,
)
from ..ratelimit import RateLimiter
from . import endpoints as ep
from .session_pool import FailureKind, LinkedInSession, SessionPool

log = logging.getLogger(__name__)

# LinkedIn's own bot-block status. Not in any RFC; it is theirs.
HTTP_LINKEDIN_BLOCKED = 999

# How many same-URL bounces to tolerate before calling it a loop. LinkedIn's
# affinity hop needs exactly one; more than a couple means something is wrong.
_MAX_HOPS = 3

class _Retry(Exception):
    """Internal: LinkedIn bounced us to the same place; retry with new cookies."""


_CHALLENGE_MARKERS = (
    "/checkpoint/challenge",
    "/checkpoint/lg/login",
    "/uas/login",
    "authwall",
    "security verification",
)


class VoyagerClient:
    def __init__(
        self,
        pool: SessionPool,
        limiter: RateLimiter,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._pool = pool
        self._limiter = limiter
        self._settings = settings
        # Injected only by tests, so LinkedIn's error behaviour (999, checkpoint
        # redirects, HTML auth walls) can be exercised without a live account.
        self._transport = transport
        self._working_decoration: str | None = None
        # One client -- and therefore one cookie jar -- per session. Not
        # incidental: LinkedIn re-issues JSESSIONID during its affinity
        # redirect, and the csrf-token header must track the *current* cookie
        # value. A shared jar would cross-contaminate sessions' CSRF tokens.
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._open = False
        self.request_count = 0

    # --- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> VoyagerClient:
        self._open = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
        self._open = False

    def _client_for(self, session: LinkedInSession) -> httpx.AsyncClient:
        """The client bound to this session, created on first use."""
        existing = self._clients.get(session.label)
        if existing is not None:
            return existing
        client = httpx.AsyncClient(
            timeout=self._settings.request_timeout,
            # LinkedIn 301s the first voyager call to the identical URL purely to
            # set `lidc`, its datacenter-affinity cookie. Refusing to follow that
            # reads as a hard failure when nothing is wrong -- so redirects are
            # followed, and the chain is inspected afterwards instead.
            # Redirects are followed manually in `_get`, because the Set-Cookie
            # on each hop is the diagnosis: LinkedIn signals an invalidated
            # session by *deleting* li_at and then bouncing forever. httpx
            # following that silently just turns a clear logout into a loop.
            follow_redirects=False,
            http2=False,
            transport=self._transport,
            cookies=ep.build_cookies(
                session.li_at, session.jsessionid, self._settings.device_cookies
            ),
        )
        self._clients[session.label] = client
        return client

    @staticmethod
    def _csrf_for(client: httpx.AsyncClient, session: LinkedInSession) -> str:
        """The CSRF token, read from the live jar rather than from config.

        LinkedIn hands back a fresh JSESSIONID during the affinity redirect, and
        the csrf-token header must equal whatever the cookie says *now* -- so read
        the jar, falling back to the configured value on the first request.

        LinkedIn also sets JSESSIONID at more than one scope (`.linkedin.com` and
        `www.linkedin.com`), which makes the convenience accessor raise. When
        that happens, prefer the most specific host match.
        """
        current: str | None = None
        try:
            current = client.cookies.get("JSESSIONID")
        except httpx.CookieConflict:
            by_domain = {
                cookie.domain.lstrip("."): cookie.value
                for cookie in client.cookies.jar
                if cookie.name == "JSESSIONID" and cookie.value
            }
            for host in ("www.linkedin.com", "linkedin.com"):
                if host in by_domain:
                    current = by_domain[host]
                    break
            else:
                current = next(iter(by_domain.values()), None)
        return (current or session.jsessionid or "").strip('"')

    async def aclose(self) -> None:
        await self.__aexit__()

    # --- request plumbing --------------------------------------------------

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        """One Voyager GET, with session rotation and error classification.

        Two independent budgets, deliberately not shared:

        * the outer loop gives each configured session one turn -- a failure that
          is the *session's* fault should move to a different session, not retry
          the same one;
        * the inner loop follows LinkedIn's same-URL affinity bounces, which are
          not anyone's fault and must not consume a session's turn.
        """
        if not self._open:
            await self.__aenter__()

        last_error: ApiError | None = None

        for _ in range(max(1, len(self._pool.status()))):
            session = self._pool.acquire()
            client = self._client_for(session)
            hops = 0

            while True:
                allowed, retry_after, reason = self._limiter.check()
                if not allowed:
                    raise RateLimited(
                        f"Local {reason} cap reached ({self._limiter.per_hour}/h, "
                        f"{self._limiter.per_day}/day). This protects the LinkedIn "
                        f"account from a soft block.",
                        retry_after=retry_after,
                    )
                await self._limiter.acquire()

                try:
                    # Cookies come from the jar, not per-request, so whatever
                    # LinkedIn refreshes on a hop is what the next call uses.
                    response = await client.get(
                        ep.BASE + path,
                        params=params,
                        headers=ep.build_headers(
                            self._csrf_for(client, session), self._settings.user_agent
                        ),
                    )
                except httpx.TimeoutException as exc:
                    self._pool.report_failure(session, FailureKind.TRANSIENT, "timeout")
                    last_error = UpstreamError(f"LinkedIn timed out: {exc}")
                    break
                except httpx.HTTPError as exc:
                    self._pool.report_failure(session, FailureKind.TRANSIENT, str(exc)[:80])
                    last_error = UpstreamError(f"Network error talking to LinkedIn: {exc}")
                    break

                self.request_count += 1

                try:
                    return self._interpret(response, session, path, allow_404=allow_404)
                except _Retry:
                    hops += 1
                    if hops <= _MAX_HOPS:
                        continue
                    self._pool.report_failure(session, FailureKind.TRANSIENT, "redirect loop")
                    last_error = UpstreamError(
                        f"LinkedIn bounced the request to itself {_MAX_HOPS} times "
                        f"without ever answering."
                    )
                    break
                except (UpstreamBlocked, SessionExpired, UpstreamError) as exc:
                    # Session-level problem: rotate to the next session.
                    last_error = exc
                    break

        raise last_error or UpstreamError()

    def _interpret(
        self,
        response: httpx.Response,
        session: LinkedInSession,
        path: str,
        *,
        allow_404: bool,
    ) -> dict[str, Any] | None:
        status = response.status_code

        # LinkedIn expires the auth cookies when it has invalidated a session.
        # `Set-Cookie: li_at=delete me; Expires=Thu, 01-Jan-1970` is an explicit
        # logout, and it arrives on a 302 rather than a 401 -- so check for it
        # before anything else, or a dead session presents as a redirect loop.
        if self._is_logout(response):
            self._pool.report_failure(session, FailureKind.EXPIRED, "cookies expired by server")
            raise SessionExpired(
                "LinkedIn invalidated this session: it responded by expiring the "
                "li_at cookie. Log in again in the browser and copy a fresh li_at "
                "and JSESSIONID. Note that logging out of LinkedIn in the browser "
                "kills the token server-side, which is the usual cause."
            )

        if status in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            if any(marker in location.lower() for marker in _CHALLENGE_MARKERS):
                self._pool.report_failure(
                    session, FailureKind.CHALLENGE, f"redirect {location[:60]}"
                )
                raise UpstreamBlocked(
                    "LinkedIn redirected to a security checkpoint. Open LinkedIn "
                    "in a browser with this account and clear the challenge.",
                    details={"location": location[:200]},
                )
            # A bounce to the same URL is LinkedIn's datacenter-affinity hop: it
            # sets `lidc` and expects a retry. The cookies it just set are now in
            # the jar, so retrying is meaningful rather than a loop.
            raise _Retry(location or str(response.url))

        if status == HTTP_LINKEDIN_BLOCKED:
            self._pool.report_failure(session, FailureKind.CHALLENGE, "HTTP 999")
            raise UpstreamBlocked(
                "LinkedIn returned HTTP 999 -- its automated-traffic block. The "
                "session is now in cooldown.",
                retry_after=session.cooldown_remaining or 3600,
            )

        if status == 401:
            self._pool.report_failure(session, FailureKind.EXPIRED, "401")
            raise SessionExpired()

        if status == 403:
            body = response.text[:400].lower()
            if "csrf" in body:
                # csrf-token header did not match the JSESSIONID cookie.
                self._pool.report_failure(session, FailureKind.EXPIRED, "csrf mismatch")
                raise SessionExpired(
                    "LinkedIn rejected the CSRF token. The csrf-token header must "
                    "equal the JSESSIONID cookie value without its quotes."
                )
            self._pool.report_failure(session, FailureKind.CHALLENGE, "403")
            raise UpstreamBlocked("LinkedIn refused the request with 403.")

        if status == 404:
            self._pool.report_success(session)  # the session is fine; the profile is not
            if allow_404:
                return None
            raise ProfileNotFound()

        if status == 400:
            # Our request was malformed -- in practice a decorationId whose
            # version LinkedIn has bumped. The session is blameless, and
            # penalising it here would cool a perfectly good cookie while the
            # candidate list is being walked.
            self._pool.report_success(session)
            raise UpstreamError(f"LinkedIn rejected the request parameters (400): {path}")

        if status == 410:
            # How LinkedIn retires a Voyager endpoint. Observed live on
            # profileView, which is why the strategy chain exists at all. The
            # session is fine, so do not penalise it -- just move on.
            self._pool.report_success(session)
            raise UpstreamError(
                f"LinkedIn has retired this endpoint (410 Gone): {path}. "
                f"Falling through to the next strategy."
            )

        if status == 429:
            self._pool.report_failure(session, FailureKind.RATE_LIMIT, "429")
            raise UpstreamBlocked(
                "LinkedIn rate-limited this session.",
                retry_after=int(response.headers.get("retry-after", 1800)),
            )

        if status >= 500:
            self._pool.report_failure(session, FailureKind.TRANSIENT, f"{status}")
            raise UpstreamError(f"LinkedIn returned {status}.")

        if status != 200:
            self._pool.report_failure(session, FailureKind.TRANSIENT, f"{status}")
            raise UpstreamError(f"LinkedIn returned an unexpected {status}.")

        # 200 but HTML: an auth wall or challenge served with a success status.
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            snippet = response.text[:600].lower()
            if any(marker in snippet for marker in _CHALLENGE_MARKERS):
                self._pool.report_failure(session, FailureKind.CHALLENGE, "html authwall")
                raise UpstreamBlocked("LinkedIn served a login/challenge page instead of JSON.")
            self._pool.report_failure(session, FailureKind.TRANSIENT, f"ct={content_type[:40]}")
            raise UpstreamError(f"Expected JSON from LinkedIn, got {content_type!r}.")

        try:
            payload = response.json()
        except ValueError as exc:
            self._pool.report_failure(session, FailureKind.TRANSIENT, "bad json")
            raise UpstreamError(f"Could not decode LinkedIn's JSON: {exc}") from exc

        self._pool.report_success(session)

        if not isinstance(payload, dict):
            raise UpstreamError("LinkedIn returned a non-object JSON body.")
        return payload

    @staticmethod
    def _is_logout(response: httpx.Response) -> bool:
        """Did LinkedIn expire the auth cookies in this response?

        Detected structurally -- an auth cookie carrying `Max-Age=0` or a 1970
        expiry -- rather than by matching the literal string "delete me", which
        is just what LinkedIn happens to use as the placeholder value today.
        """
        for raw in response.headers.get_list("set-cookie"):
            head, _, attrs = raw.partition(";")
            name = head.split("=", 1)[0].strip().lower()
            if name not in ("li_at", "li_a", "liap"):
                continue
            lowered = attrs.lower()
            if "max-age=0" in lowered.replace(" ", "") or "expires=thu, 01-jan-1970" in lowered:
                return True
        return False

    # --- health ------------------------------------------------------------

    async def probe(self) -> dict[str, Any] | None:
        """Cheapest possible authentication check."""
        return await self._get(ep.ME)

    # --- strategy 1: profileView -------------------------------------------

    async def fetch_profile_view(self, public_id: str) -> dict[str, Any]:
        """Legacy endpoint. Answers 410 Gone as of testing; see the class docs."""
        payload = await self._get(ep.PROFILE_VIEW.format(public_id=public_id))
        if payload is None:
            raise ProfileNotFound()
        if not payload.get("included") and not payload.get("data"):
            raise ProfilePrivate(
                "LinkedIn returned an empty profile -- typically an out-of-network "
                "or restricted profile."
            )
        return payload

    # --- strategy 2: dash --------------------------------------------------

    async def fetch_dash_profile(self, public_id: str) -> dict[str, Any]:
        decorations = (
            (self._working_decoration,) if self._working_decoration else ep.DASH_DECORATIONS
        )
        last: ApiError | None = None
        for decoration in decorations:
            params = {
                "q": "memberIdentity",
                "memberIdentity": public_id,
                "decorationId": decoration,
            }
            try:
                payload = await self._get(ep.DASH_PROFILES, params=params)
            except (UpstreamError, ProfileNotFound) as exc:
                # A wrong decorationId version shows up as a 400/404, not a
                # session problem, so keep walking the candidate list.
                last = exc
                continue
            if payload:
                self._working_decoration = decoration
                log.info("dash decoration in use: %s", decoration)
                return payload
        raise last or UpstreamError("No known dash decorationId was accepted by LinkedIn.")

    # --- section pagination ------------------------------------------------

    async def fetch_collection(
        self, profile_urn: str, collection: str, start: int, count: int
    ) -> dict[str, Any] | None:
        """One page of a per-section dash collection.

        Used to complete sections that the main profile projection caps at 20
        entries. Returns None rather than raising on a section-level failure: a
        partial skills list is worth more to a caller than no profile at all, and
        the omission is reported in `meta.sections_failed`.
        """
        path = ep.DASH_COLLECTION_PATH.format(collection=collection)
        params = {
            "q": "viewee",
            "profileUrn": profile_urn,
            "start": start,
            "count": count,
        }
        try:
            return await self._get(path, params=params, allow_404=True)
        except ApiError as exc:
            log.warning("collection %s start=%d failed: %s", collection, start, exc.code)
            return None

    # --- contact info ------------------------------------------------------

    async def fetch_contact_info(self, public_id: str) -> dict[str, Any] | None:
        try:
            return await self._get(
                ep.CONTACT_INFO.format(public_id=public_id), allow_404=True
            )
        except ApiError as exc:
            log.info("contact info unavailable: %s", exc.code)
            return None
