"""Orchestration: URL in, ProfileResponse out.

The order of operations here *is* the availability strategy:

    normalise URL
      -> fresh cache?         return immediately, LinkedIn untouched
      -> DEMO_MODE?           serve a recorded fixture
      -> live fetch           strategy chain, rate-limited, session-rotated
           on failure         fall back to a stale cache entry if we have one
      -> parse, cache, return

The fallback line is the one that matters. LinkedIn will block this service
sooner or later; when it does, a 30-day-old copy labelled `stale: true` is a
better answer than a 502, and pretending otherwise would be dishonest about
what the caller is holding.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from .cache import ProfileCache
from .config import Settings
from .errors import (
    ApiError,
    FixtureMissing,
    NoSessionsConfigured,
    ParseFailed,
    ProfileNotFound,
    RateLimited,
    SessionExpired,
    UpstreamBlocked,
    UpstreamError,
)
from .fixtures import FixtureStore
from .linkedin import endpoints as ep
from .linkedin.client import VoyagerClient
from .linkedin.normalize import resolve
from .linkedin.session_pool import SessionPool
from .models import Meta, Profile, ProfileResponse
from .parsers import dash as dash_parser
from .parsers import profile_view as pv_parser
from .ratelimit import RateLimiter
from .url import extract_public_identifier

log = logging.getLogger(__name__)

# Errors where trying another strategy is pointless and actively harmful --
# each attempt spends another unit of the daily LinkedIn budget.
_ABORT_CHAIN = (RateLimited, UpstreamBlocked, SessionExpired, NoSessionsConfigured)


class ProfileService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool = SessionPool(settings.sessions)
        self.limiter = RateLimiter(
            per_hour=settings.max_profiles_per_hour,
            per_day=settings.max_profiles_per_day,
            min_delay=settings.min_delay_seconds,
            max_delay=settings.max_delay_seconds,
        )
        self.cache = ProfileCache(
            settings.cache_path,
            ttl_seconds=settings.cache_ttl_seconds,
            stale_max_seconds=settings.cache_stale_max_seconds,
        )
        self.fixtures = FixtureStore(settings.fixtures_dir)
        self._client: VoyagerClient | None = None

    # --- lifecycle ---------------------------------------------------------

    async def startup(self) -> None:
        # Data minimisation on boot: entries past the stale window can never be
        # served again, so holding other people's profile data is pure exposure.
        purged = self.cache.purge_expired()
        if purged:
            log.info(
                "purged %d cache entr%s past the stale window",
                purged,
                "y" if purged == 1 else "ies",
            )

        if self.pool.configured and not self.settings.demo_mode:
            self._client = await VoyagerClient(self.pool, self.limiter, self.settings).__aenter__()
            log.info("voyager client ready with %d session(s)", len(self.pool.status()))
        else:
            reason = "DEMO_MODE" if self.settings.demo_mode else "no sessions configured"
            log.warning("running without a LinkedIn client (%s); fixtures only", reason)
        log.info("fixtures loaded: %s", self.fixtures.available() or "none")

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self.cache.close()

    # --- public API --------------------------------------------------------

    async def get_profile(
        self,
        url: str,
        *,
        refresh: bool = False,
        include_contact_info: bool = False,
    ) -> ProfileResponse:
        started = time.perf_counter()
        public_id = extract_public_identifier(url)

        caching = self.settings.cache_enabled
        cached = None if (refresh or not caching) else self.cache.get(public_id)
        if cached and cached.is_fresh(self.settings.cache_ttl_seconds):
            return self._from_cache(cached, public_id, started, stale=False)

        if self.settings.demo_mode:
            return self._from_fixture(public_id, started)

        if not self.pool.configured:
            # No credentials: a fixture is better than a 503 if we have one.
            if self.fixtures.get(public_id):
                return self._from_fixture(public_id, started)
            raise NoSessionsConfigured()

        try:
            return await self._fetch_live(
                public_id, started, include_contact_info=include_contact_info
            )
        except ApiError as exc:
            if cached and cached.is_usable(self.settings.cache_stale_max_seconds):
                log.warning(
                    "live fetch failed (%s); serving stale cache aged %ds",
                    exc.code, cached.age_seconds,
                )
                return self._from_cache(cached, public_id, started, stale=True)
            raise

    def status(self) -> dict[str, Any]:
        return {
            "sessions": self.pool.status(),
            "rate_limit": self.limiter.status(),
            "cache": self.cache.stats(),
            "fixtures": self.fixtures.available(),
            "demo_mode": self.settings.demo_mode,
        }

    async def probe(self) -> bool:
        """Is the LinkedIn session actually alive? Costs one upstream request."""
        if self._client is None:
            return False
        try:
            return bool(await self._client.probe())
        except ApiError:
            return False

    # --- live path ---------------------------------------------------------

    async def _fetch_live(
        self,
        public_id: str,
        started: float,
        *,
        include_contact_info: bool,
    ) -> ProfileResponse:
        assert self._client is not None
        client = self._client
        before = client.request_count

        # dash first: profileView answers 410 Gone, so trying it first spent one
        # upstream request per profile on a guaranteed failure -- doubling the
        # cost of every fetch against a budget of roughly 100 a day.
        strategies = [
            ("dash", client.fetch_dash_profile, self._parse_dash),
            ("profile_view", client.fetch_profile_view, self._parse_profile_view),
        ]

        failures: dict[str, str] = {}
        profile: Profile | None = None
        used: str | None = None
        raw: dict[str, Any] | None = None

        for name, fetch, parse in strategies:
            try:
                raw = await fetch(public_id)
                profile = parse(raw, public_id)
            except _ABORT_CHAIN:
                raise
            except ApiError as exc:
                # A 404 here can mean "endpoint retired for this account" just
                # as easily as "no such profile", so keep walking the chain and
                # only conclude NOT_FOUND if every strategy agrees.
                failures[name] = exc.code
                continue
            except Exception as exc:  # parser bug -- never leak a traceback
                log.exception("parser %s crashed", name)
                failures[name] = f"PARSER_ERROR: {type(exc).__name__}"
                continue
            used = name
            break

        if profile is None or used is None or raw is None:
            detail = {"attempted": failures}
            # Only strategies that actually ran get a vote on existence.
            if failures and all(code == "PROFILE_NOT_FOUND" for code in failures.values()):
                raise ProfileNotFound(details=detail)
            if "PROFILE_PRIVATE" in failures.values():
                from .errors import ProfilePrivate

                raise ProfilePrivate(details=detail)
            raise UpstreamError(
                "Every fetch strategy failed for this profile.", details=detail
            )

        sections_failed: list[str] = []
        sections_truncated = self._truncated_sections(raw, used)

        if sections_truncated and used == "dash":
            completed, sections_truncated = await self._complete_sections(
                profile, raw, sections_truncated
            )
            for section in completed:
                log.info("completed section %s by pagination", section)

        if include_contact_info:
            try:
                contact_raw = await client.fetch_contact_info(public_id)
                profile.contact_info = pv_parser.parse_contact_info(
                    resolve(contact_raw) if contact_raw else None
                )
            except ApiError as exc:
                sections_failed.append("contact_info")
                log.info("contact info failed: %s", exc.code)

        upstream = client.request_count - before
        if self.settings.cache_enabled:
            self.cache.set(public_id, profile.model_dump(mode="json"), used)

        return ProfileResponse(
            meta=Meta(
                source="live",
                strategy=used,  # type: ignore[arg-type]
                fetched_at=datetime.now(UTC),
                age_seconds=0,
                stale=False,
                partial=bool(sections_failed),
                sections_failed=sections_failed,
                sections_truncated=sections_truncated,
                upstream_requests=upstream,
                duration_ms=_elapsed(started),
            ),
            profile=profile,
        )

    @staticmethod
    def _truncated_sections(raw: dict[str, Any], strategy: str) -> list[str]:
        """Which sections LinkedIn cut short, read off its own paging metadata.

        Guessing here would be worse than saying nothing: reporting a section as
        truncated when `paging.total` says it holds two entries tells the caller
        to spend requests re-fetching data it already has, and reporting an empty
        section as truncated is indistinguishable from one that was withheld.
        """
        truncated: list[str] = []
        if strategy != "profile_view":
            for attribute, (retrieved, total) in dash_parser.collection_paging(raw).items():
                if total > retrieved:
                    truncated.append(attribute)
            return truncated
        for entity in raw.get("included") or []:
            if not isinstance(entity, dict):
                continue
            paging = entity.get("paging")
            etype = entity.get("$type", "")
            if not isinstance(paging, dict) or not isinstance(etype, str):
                continue
            total, count = paging.get("total"), paging.get("count")
            if isinstance(total, int) and isinstance(count, int) and total > count:
                name = etype.rsplit(".", 1)[-1].replace("View", "").lower()
                truncated.append(name)
        return truncated

    async def _complete_sections(
        self,
        profile: Profile,
        raw: dict[str, Any],
        truncated: list[str],
    ) -> tuple[list[str], list[str]]:
        """Page in the rest of any section LinkedIn cut off at 20 entries.

        Returns (completed, still_truncated). The brief asks for *most of the
        information on the profile page*; returning 20 of someone's 47 skills
        does not honestly meet that, so the pages are fetched -- but under a hard
        request ceiling, because each one spends the same scarce budget as a
        whole profile fetch. Whatever cannot be completed within the ceiling
        stays reported as truncated rather than silently trimmed.
        """
        assert self._client is not None
        profile_urn = profile.urn
        if not profile_urn:
            return [], truncated

        paging = dash_parser.collection_paging(raw)
        completed: list[str] = []
        still: list[str] = []
        budget = self.settings.max_pagination_requests

        # Longest gap first: with a limited budget, spend it where the most data
        # is missing.
        ordered = sorted(
            truncated,
            key=lambda a: (paging.get(a, (0, 0))[1] - paging.get(a, (0, 0))[0]),
            reverse=True,
        )

        for attribute in ordered:
            collection = ep.DASH_COLLECTIONS.get(attribute)
            retrieved, total = paging.get(attribute, (0, 0))
            if not collection or total <= retrieved:
                continue
            if budget <= 0:
                still.append(attribute)
                continue

            items = list(getattr(profile, attribute) or [])
            start = len(items)
            while start < total and budget > 0:
                budget -= 1
                payload = await self._client.fetch_collection(
                    profile_urn, collection, start, ep.COLLECTION_PAGE_SIZE
                )
                if payload is None:
                    sections_failed = f"{attribute} (page at {start})"
                    log.warning("pagination stopped: %s", sections_failed)
                    break
                page = dash_parser.parse_collection(payload, attribute)
                if not page:
                    break
                items.extend(page)
                start += len(page)

            setattr(profile, attribute, items)
            (completed if len(items) >= total else still).append(attribute)

        return completed, still

    # --- parsing -----------------------------------------------------------

    @staticmethod
    def _parse_profile_view(raw: dict[str, Any], public_id: str) -> Profile:
        profile = pv_parser.parse(resolve(raw), public_id)
        if not profile.full_name and not profile.headline:
            raise ParseFailed("profileView returned no identifiable profile fields.")
        return profile

    @staticmethod
    def _parse_dash(raw: dict[str, Any], public_id: str) -> Profile:
        profile = dash_parser.parse(raw, public_id)
        if not profile.full_name and not profile.headline:
            raise ParseFailed("dash/GraphQL payload contained no identifiable profile fields.")
        return profile

    # --- cache / fixture paths --------------------------------------------

    def _from_cache(self, entry, public_id: str, started: float, *, stale: bool) -> ProfileResponse:
        profile = Profile.model_validate(entry.payload)
        return ProfileResponse(
            meta=Meta(
                source="stale-cache" if stale else "cache",
                strategy=entry.strategy,  # type: ignore[arg-type]
                fetched_at=datetime.fromtimestamp(entry.fetched_at, tz=UTC),
                age_seconds=entry.age_seconds,
                stale=stale,
                partial=stale,
                sections_failed=["*upstream unavailable, cached copy served*"] if stale else [],
                upstream_requests=0,
                duration_ms=_elapsed(started),
            ),
            profile=profile,
        )

    def _from_fixture(self, public_id: str, started: float) -> ProfileResponse:
        fixture = self.fixtures.get(public_id)
        if fixture is None:
            raise FixtureMissing(
                f"No fixture for '{public_id}'. Available: "
                f"{', '.join(self.fixtures.available()) or 'none'}.",
                details={"available": self.fixtures.available()},
            )
        if fixture.strategy == "profile_view":
            profile = pv_parser.parse(resolve(fixture.payload), public_id)
        else:
            profile = dash_parser.parse(fixture.payload, public_id)
        if fixture.contact_info:
            profile.contact_info = pv_parser.parse_contact_info(resolve(fixture.contact_info))
        return ProfileResponse(
            meta=Meta(
                source="fixture",
                strategy="fixture",
                fetched_at=datetime.now(UTC),
                stale=False,
                partial=False,
                upstream_requests=0,
                duration_ms=_elapsed(started),
            ),
            profile=profile,
        )


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
