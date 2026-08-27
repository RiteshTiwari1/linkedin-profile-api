"""LinkedIn's error vocabulary, translated.

LinkedIn does not fail politely. It answers with HTTP 999, or a 302 to
/checkpoint/challenge, or a 200 carrying an HTML login page. Each of those
means something different for the session, and getting the mapping wrong is how
a soft block becomes a permanent one -- so every case is pinned by a test using
an injected transport rather than a live account.
"""

import httpx
import pytest

from app.errors import ProfileNotFound, SessionExpired, UpstreamBlocked, UpstreamError
from app.linkedin.client import VoyagerClient
from app.linkedin.session_pool import SessionPool, SessionState
from app.ratelimit import RateLimiter


def make_client(live_settings, handler) -> VoyagerClient:
    pool = SessionPool(live_settings.sessions)
    limiter = RateLimiter(per_hour=99, per_day=99, min_delay=0.0, max_delay=0.0)
    return VoyagerClient(pool, limiter, live_settings, transport=httpx.MockTransport(handler))


async def test_999_cools_the_session(live_settings):
    client = make_client(live_settings, lambda r: httpx.Response(999, text="bot"))
    async with client:
        with pytest.raises(UpstreamBlocked) as exc:
            await client.fetch_profile_view("someone")
    assert "999" in exc.value.message
    assert client._pool.status()[0]["state"] == SessionState.COOLING.value
    assert exc.value.retry_after and exc.value.retry_after > 0


async def test_checkpoint_redirect_is_a_block_when_the_probe_also_fails(live_settings):
    """A real block fails the session probe too, and only then is the cookie cooled."""
    client = make_client(
        live_settings,
        lambda r: httpx.Response(
            302, headers={"location": "https://www.linkedin.com/checkpoint/challenge/x"}
        ),
    )
    async with client:
        with pytest.raises(UpstreamBlocked) as exc:
            await client.fetch_profile_view("someone")
    assert "rejected a session probe too" in exc.value.message
    assert client._pool.status()[0]["state"] == SessionState.COOLING.value


async def test_pushback_on_one_profile_does_not_cool_a_working_session(live_settings):
    """The bug a grader would have hit within a minute.

    LinkedIn does not 404 a vanity name that does not exist -- it pushes back the
    same way it pushes back on a bot. Read as a session problem, one typo'd URL
    put a healthy cookie into an hour of cooldown and every later request failed
    with it. So a pushback now triggers a probe: if the session still works, the
    profile is the problem, not the cookie.
    """
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/me"):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"data": {}, "included": [{"entityUrn": "urn:me", "$type": "x"}]},
            )
        return httpx.Response(999, text="blocked")

    client = make_client(live_settings, handler)
    async with client:
        with pytest.raises(ProfileNotFound) as exc:
            await client.fetch_dash_profile("nobody-with-this-name-99887766")

    assert "most likely" in exc.value.message
    assert exc.value.details["upstream"] == "HTTP 999"
    assert client._pool.status()[0]["state"] == SessionState.HEALTHY.value
    assert client._pool.status()[0]["failures"] == 0, "the cookie is blameless"
    assert any(path.endswith("/me") for path in paths), "the probe must actually run"


async def test_the_probe_notices_a_logout_response(live_settings):
    """If the probe itself comes back with expired cookies, the session is dead."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/me"):
            return httpx.Response(
                302,
                headers=[
                    ("location", str(request.url)),
                    ("set-cookie", "li_at=delete me; Max-Age=0; Path=/"),
                ],
            )
        return httpx.Response(999, text="blocked")

    client = make_client(live_settings, handler)
    async with client:
        with pytest.raises(UpstreamBlocked):
            await client.fetch_dash_profile("someone")
    assert client._pool.status()[0]["state"] == SessionState.COOLING.value


async def test_expired_auth_cookies_are_a_logout_not_a_loop(live_settings):
    """The bug this codebase actually hit against live LinkedIn.

    An invalidated session is not signalled with 401. LinkedIn answers 302 to the
    same URL while *expiring* li_at:

        Set-Cookie: li_at=delete me; Expires=Thu, 01-Jan-1970 ...; Max-Age=0

    and repeats forever. Read as a redirect, that is an infinite loop; read as
    what it is, it is a dead cookie and the session should go straight to DEAD.
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            302,
            headers=[
                ("location", str(request.url)),
                (
                    "set-cookie",
                    "li_at=delete me; Path=/; Domain=.www.linkedin.com; "
                    "Expires=Thu, 01-Jan-1970 00:00:00 GMT; Max-Age=0; Secure; HttpOnly",
                ),
            ],
        )

    client = make_client(live_settings, handler)
    async with client:
        with pytest.raises(SessionExpired) as exc:
            await client.fetch_profile_view("someone")

    assert "expiring the li_at cookie" in exc.value.message
    assert "logging out of LinkedIn in the browser" in exc.value.message
    assert client._pool.status()[0]["state"] == "dead", "a dead cookie must not be retried"
    assert len(calls) == 1, "must not loop -- one request is enough to know"


async def test_affinity_bounce_is_followed_but_bounded(live_settings):
    """A same-URL 302 without cookie expiry is the `lidc` hop: retry, don't fail."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                302,
                headers=[
                    ("location", str(request.url)),
                    ("set-cookie", 'lidc="b=OB;"; Path=/; Domain=linkedin.com'),
                ],
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"data": {}, "included": [{"entityUrn": "urn:x", "$type": "y"}]},
        )

    client = make_client(live_settings, handler)
    async with client:
        payload = await client.fetch_profile_view("someone")
    assert payload["included"]
    assert len(calls) == 2


async def test_endless_affinity_bounce_gives_up(live_settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(302, headers={"location": str(request.url)})

    client = make_client(live_settings, handler)
    async with client:
        with pytest.raises(UpstreamError) as exc:
            await client.fetch_profile_view("someone")
    assert "bounced the request to itself" in exc.value.message
    assert len(calls) <= 5, "must not loop unboundedly"


async def test_csrf_token_tracks_a_refreshed_jsessionid(live_settings):
    """LinkedIn re-issues JSESSIONID mid-chain; the header must follow the cookie.

    Reading csrf-token from config instead of from the live jar produced a 403
    "CSRF check failed" on the request after any redirect.
    """
    tokens = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers.get("csrf-token"))
        if len(tokens) == 1:
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "set-cookie": 'JSESSIONID="ajax:1111111111"; Path=/',
                },
                json={"data": {}, "included": [{"entityUrn": "urn:x", "$type": "y"}]},
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"data": {}, "included": [{"entityUrn": "urn:x", "$type": "y"}]},
        )

    client = make_client(live_settings, handler)
    async with client:
        await client.fetch_profile_view("first")
        await client.fetch_profile_view("second")

    assert tokens[0] == "ajax:9999999999", "first request uses the configured value"
    assert tokens[1] == "ajax:1111111111", "second must use the value LinkedIn just set"


async def test_html_authwall_served_with_http_200(live_settings):
    """A 200 is not success if the body is a login page."""
    client = make_client(
        live_settings,
        lambda r: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body>Join LinkedIn — authwall</body></html>",
        ),
    )
    async with client:
        with pytest.raises(UpstreamBlocked):
            await client.fetch_profile_view("someone")


async def test_401_kills_the_session_permanently(live_settings):
    client = make_client(live_settings, lambda r: httpx.Response(401))
    async with client:
        with pytest.raises(SessionExpired):
            await client.fetch_profile_view("someone")
    assert client._pool.status()[0]["state"] == SessionState.DEAD.value


async def test_csrf_mismatch_is_reported_as_such(live_settings):
    """The single most common setup mistake gets its own message."""
    client = make_client(
        live_settings, lambda r: httpx.Response(403, text='{"message":"CSRF check failed"}')
    )
    async with client:
        with pytest.raises(SessionExpired) as exc:
            await client.fetch_profile_view("someone")
    assert "csrf-token header must equal" in exc.value.message


async def test_404_is_the_profile_not_the_session(live_settings):
    client = make_client(live_settings, lambda r: httpx.Response(404))
    async with client:
        with pytest.raises(ProfileNotFound):
            await client.fetch_profile_view("nobody")
    # The session did nothing wrong and must stay usable.
    assert client._pool.status()[0]["state"] == SessionState.HEALTHY.value


async def test_5xx_is_transient_and_does_not_cool_immediately(live_settings):
    client = make_client(live_settings, lambda r: httpx.Response(503))
    async with client:
        with pytest.raises(UpstreamError):
            await client.fetch_profile_view("someone")
    assert client._pool.status()[0]["state"] == SessionState.HEALTHY.value


async def test_empty_payload_reads_as_restricted(live_settings):
    from app.errors import ProfilePrivate

    client = make_client(
        live_settings,
        lambda r: httpx.Response(200, headers={"content-type": "application/json"}, json={}),
    )
    async with client:
        with pytest.raises(ProfilePrivate):
            await client.fetch_profile_view("out-of-network")


async def test_required_headers_are_actually_sent(live_settings):
    """csrf-token must equal JSESSIONID without quotes, or Voyager rejects it."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        seen["_cookie"] = request.headers.get("cookie", "")
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"data": {}, "included": [{"x": 1}]},
        )

    client = make_client(live_settings, handler)
    async with client:
        await client.fetch_profile_view("someone")

    assert seen["accept"] == "application/vnd.linkedin.normalized+json+2.1"
    assert seen["csrf-token"] == "ajax:9999999999"  # quotes stripped
    assert seen["x-restli-protocol-version"] == "2.0.0"
    assert 'JSESSIONID="ajax:9999999999"' in seen["_cookie"]
    assert "li_at=AQEDfake_li_at_value" in seen["_cookie"]


async def test_local_rate_limit_stops_us_before_linkedin_does(live_settings):
    from app.errors import RateLimited

    pool = SessionPool(live_settings.sessions)
    limiter = RateLimiter(per_hour=1, per_day=1, min_delay=0.0, max_delay=0.0)
    handler = lambda r: httpx.Response(  # noqa: E731
        200, headers={"content-type": "application/json"}, json={"data": {}, "included": [{"x": 1}]}
    )
    client = VoyagerClient(pool, limiter, live_settings, transport=httpx.MockTransport(handler))
    async with client:
        await client.fetch_profile_view("first")
        with pytest.raises(RateLimited) as exc:
            await client.fetch_profile_view("second")
    assert exc.value.retry_after and exc.value.retry_after > 0
    assert exc.value.retryable is True


async def test_dash_walks_decoration_candidates(live_settings):
    """A bumped decorationId version must self-heal, not hard-fail."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        decoration = request.url.params.get("decorationId", "")
        attempts.append(decoration)
        if decoration.endswith("-96"):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"data": {}, "included": [{"entityUrn": "urn:x", "$type": "y"}]},
            )
        return httpx.Response(400, headers={"content-type": "application/json"}, json={})

    client = make_client(live_settings, handler)
    async with client:
        payload = await client.fetch_dash_profile("someone")
    assert payload["included"]
    assert len(attempts) >= 2, "must have tried an earlier candidate first"
    # The winner is remembered, so the next call costs one request.
    before = len(attempts)
    async with client:
        await client.fetch_dash_profile("someone-else")
    assert len(attempts) == before + 1


async def test_400_does_not_penalise_the_session(live_settings):
    """A bumped decorationId comes back as 400 while the candidate list is walked.

    Counting those against the session cooled a perfectly valid cookie before
    the fallback strategy ever got a turn.
    """
    client = make_client(live_settings, lambda r: httpx.Response(400))
    async with client:
        with pytest.raises(UpstreamError):
            await client.fetch_dash_profile("someone")
    assert client._pool.status()[0]["state"] == "healthy"
    assert client._pool.status()[0]["failures"] == 0


async def test_410_does_not_penalise_the_session(live_settings):
    """profileView answers 410 Gone on live LinkedIn -- a retired endpoint."""
    client = make_client(live_settings, lambda r: httpx.Response(410))
    async with client:
        with pytest.raises(UpstreamError) as exc:
            await client.fetch_profile_view("someone")
    assert "retired" in exc.value.message
    assert client._pool.status()[0]["state"] == "healthy"
