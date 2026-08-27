"""Cache, rate limiter and session pool."""

import time

import pytest

from app.cache import ProfileCache
from app.errors import SessionExpired, UpstreamBlocked
from app.linkedin.session_pool import FailureKind, SessionPool, SessionState
from app.ratelimit import RateLimiter

# --- cache -----------------------------------------------------------------


def test_cache_round_trip_is_case_insensitive():
    cache = ProfileCache(":memory:", ttl_seconds=60, stale_max_seconds=600)
    cache.set("Ada", {"full_name": "Ada"}, "profile_view")
    entry = cache.get("ada")
    assert entry.payload["full_name"] == "Ada"
    assert entry.strategy == "profile_view"
    assert entry.is_fresh(60)


def test_cache_stale_window():
    cache = ProfileCache(":memory:", ttl_seconds=0, stale_max_seconds=600)
    cache.set("ada", {"n": 1}, "dash")
    entry = cache.get("ada")
    assert not entry.is_fresh(0)
    assert entry.is_usable(600)
    assert not entry.is_usable(0)


def test_cache_delete_and_stats():
    cache = ProfileCache(":memory:", ttl_seconds=60, stale_max_seconds=600)
    cache.set("a", {"n": 1}, "x")
    cache.set("b", {"n": 2}, "x")
    assert cache.stats()["entries"] == 2
    assert cache.delete("a") == 1
    assert cache.list_identifiers() == ["b"]


# --- rate limiter ----------------------------------------------------------


async def test_hourly_cap_blocks_with_retry_after():
    limiter = RateLimiter(per_hour=2, per_day=99, min_delay=0.0, max_delay=0.0)
    for _ in range(2):
        assert limiter.check()[0]
        await limiter.acquire()
    allowed, retry_after, reason = limiter.check()
    assert not allowed and reason == "hourly" and retry_after > 0


async def test_daily_cap_blocks():
    limiter = RateLimiter(per_hour=99, per_day=1, min_delay=0.0, max_delay=0.0)
    await limiter.acquire()
    allowed, _, reason = limiter.check()
    assert not allowed and reason == "daily"


async def test_spacing_is_enforced_and_randomised():
    limiter = RateLimiter(per_hour=99, per_day=99, min_delay=0.05, max_delay=0.08)
    await limiter.acquire()
    start = time.perf_counter()
    await limiter.acquire()
    elapsed = time.perf_counter() - start
    assert 0.04 < elapsed < 0.2, "requests must be spaced, not fired back to back"


# --- session pool ----------------------------------------------------------


def test_round_robin_spreads_load():
    pool = SessionPool([("a" * 20, "ajax:1"), ("b" * 20, "ajax:2")])
    labels = {pool.acquire().label for _ in range(4)}
    assert labels == {"session-1", "session-2"}


def test_cooling_sessions_are_skipped_then_recovered():
    pool = SessionPool([("a" * 20, "ajax:1"), ("b" * 20, "ajax:2")])
    first = pool.acquire()
    pool.report_failure(first, FailureKind.RATE_LIMIT)
    assert first.state is SessionState.COOLING
    for _ in range(5):
        assert pool.acquire().label != first.label

    first.cooldown_until = time.time() - 1  # simulate the timer expiring
    assert any(pool.acquire().label == first.label for _ in range(5))


def test_expired_sessions_are_never_retried():
    pool = SessionPool([("a" * 20, "ajax:1")])
    pool.report_failure(pool.acquire(), FailureKind.EXPIRED)
    with pytest.raises(SessionExpired):
        pool.acquire()


def test_all_cooling_reports_the_soonest_retry():
    pool = SessionPool([("a" * 20, "ajax:1"), ("b" * 20, "ajax:2")])
    for _ in range(2):
        pool.report_failure(pool.acquire(), FailureKind.CHALLENGE)
    with pytest.raises(UpstreamBlocked) as exc:
        pool.acquire()
    assert exc.value.retry_after > 0
    assert exc.value.details["sessions_cooling"] == 2


def test_transient_failures_are_tolerated_before_cooling():
    pool = SessionPool([("a" * 20, "ajax:1")])
    session = pool.acquire()
    pool.report_failure(session, FailureKind.TRANSIENT)
    assert session.state is SessionState.HEALTHY
    pool.report_failure(session, FailureKind.TRANSIENT)
    pool.report_failure(session, FailureKind.TRANSIENT)
    assert session.state is SessionState.COOLING


def test_success_resets_the_failure_streak():
    pool = SessionPool([("a" * 20, "ajax:1")])
    session = pool.acquire()
    pool.report_failure(session, FailureKind.TRANSIENT)
    pool.report_success(session)
    pool.report_failure(session, FailureKind.TRANSIENT)
    assert session.state is SessionState.HEALTHY


def test_cookies_are_never_exposed_in_status():
    pool = SessionPool([("AQEDsupersecretcookie", "ajax:1")])
    rendered = str(pool.status())
    assert "supersecret" not in rendered
    assert "AQEDsu" in rendered  # a short fingerprint is fine for debugging


def test_empty_pool_is_explicit():
    from app.errors import NoSessionsConfigured

    pool = SessionPool([])
    assert not pool.configured
    with pytest.raises(NoSessionsConfigured):
        pool.acquire()


# --- credential isolation --------------------------------------------------


def test_settings_can_be_fully_isolated_from_dotenv():
    """The suite must never pick up a real cookie from the developer's .env.

    LI_AT/JSESSIONID are additive with LINKEDIN_SESSIONS, so without
    `_env_file=None` a live session would silently join the pool and tests could
    make real LinkedIn requests. This pins the escape hatch every fixture uses.
    """
    from app.config import Settings

    isolated = Settings(_env_file=None, li_at="", jsessionid="", linkedin_sessions="")
    assert isolated.sessions == []


def test_the_two_credential_forms_are_additive_and_deduplicated():
    from app.config import Settings

    both = Settings(
        _env_file=None,
        li_at="AQEDone",
        jsessionid='"ajax:111"',
        linkedin_sessions="AQEDtwo|ajax:222,AQEDone|ajax:111",
    )
    # Quotes stripped, pool built, and the duplicate li_at dropped so one
    # account is not charged twice against its own rate limit.
    assert both.sessions == [("AQEDone", "ajax:111"), ("AQEDtwo", "ajax:222")]


# --- browser cookie header -------------------------------------------------


def test_full_browser_cookie_header_is_parsed():
    """Sending li_at without bcookie/bscookie gets the session invalidated.

    LinkedIn treats a member token arriving without its device-identity cookies
    as lifted from someone else's browser and expires it. So the whole `cookie:`
    header from a real request is the supported input.
    """
    from app.config import Settings

    header = (
        'bcookie="v=2&abc-123"; bscookie="v=1&2024xyz"; li_gc=MTsyMTsxNzA; '
        'lidc="b=OB01:s=O:r=O:a=O"; JSESSIONID="ajax:5555555555555555555"; '
        "li_at=AQEDfromBrowser; lang=v=2&lang=en-us; li_theme=light"
    )
    s = Settings(_env_file=None, linkedin_cookie=header, li_at="", jsessionid="",
                 linkedin_sessions="")

    assert s.sessions == [("AQEDfromBrowser", "ajax:5555555555555555555")]

    device = s.device_cookies
    assert "li_at" not in device, "credentials must not leak into device cookies"
    assert "JSESSIONID" not in device
    for name in ("bcookie", "bscookie", "lidc", "li_gc", "lang", "li_theme"):
        assert name in device, f"{name} must survive parsing"
    assert device["bcookie"] == '"v=2&abc-123"', "quoted values keep their quotes"


def test_cookie_header_tolerates_messy_input():
    from app.linkedin.endpoints import parse_cookie_header

    assert parse_cookie_header("") == {}
    assert parse_cookie_header("  a=1 ;; b=2;  ") == {"a": "1", "b": "2"}
    assert parse_cookie_header("novalue; a=1") == {"a": "1"}
    # A value containing '=' must stay intact.
    assert parse_cookie_header("t=v=2&lang=en") == {"t": "v=2&lang=en"}


def test_device_cookies_are_merged_under_the_credentials():
    from app.linkedin.endpoints import build_cookies

    jar = build_cookies("AQEDreal", "ajax:111", {"bcookie": '"v=2"', "li_at": "AQEDstale"})
    assert jar["li_at"] == "AQEDreal", "the session's li_at must win over the header's"
    assert jar["JSESSIONID"] == '"ajax:111"', "JSESSIONID is stored quoted"
    assert jar["bcookie"] == '"v=2"'


# --- data minimisation -----------------------------------------------------


def test_expired_entries_are_deleted_not_just_ignored():
    """This cache holds other people's personal data.

    An entry past the stale window can never be served again, so keeping it
    serves no purpose and only widens what a disk or a backup would expose.
    """
    import time as _time

    cache = ProfileCache(":memory:", ttl_seconds=0, stale_max_seconds=1)
    cache.set("someone", {"full_name": "Someone Real"}, "dash")
    assert cache.stats()["entries"] == 1

    _time.sleep(1.1)
    assert cache.purge_expired() == 1
    assert cache.get("someone") is None
    assert cache.stats()["entries"] == 0, "the row must be gone from disk, not merely stale"


def test_entries_inside_the_stale_window_survive_a_purge():
    cache = ProfileCache(":memory:", ttl_seconds=0, stale_max_seconds=600)
    cache.set("someone", {"n": 1}, "dash")
    assert cache.purge_expired() == 0
    assert cache.get("someone") is not None, "stale-but-servable data is the fallback path"


def test_writing_purges_opportunistically():
    """No background job: a write is a natural moment to drop dead rows."""
    import time as _time

    cache = ProfileCache(":memory:", ttl_seconds=0, stale_max_seconds=1)
    cache.set("old-person", {"n": 1}, "dash")
    _time.sleep(1.1)
    cache.set("new-person", {"n": 2}, "dash")
    assert cache.list_identifiers() == ["new-person"]


def test_purge_all_forgets_everything():
    cache = ProfileCache(":memory:", ttl_seconds=60, stale_max_seconds=600)
    for name in ("a", "b", "c"):
        cache.set(name, {"n": 1}, "dash")
    assert cache.purge_all() == 3
    assert cache.list_identifiers() == []
