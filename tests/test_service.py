"""Service-level behaviour: caching, degradation, and the strategy chain."""

import httpx
import pytest

from app.errors import ProfileNotFound, UpstreamBlocked
from app.linkedin.client import VoyagerClient
from app.service import ProfileService


async def build_service(settings, handler) -> ProfileService:
    service = ProfileService(settings)
    service._client = await VoyagerClient(
        service.pool, service.limiter, settings, transport=httpx.MockTransport(handler)
    ).__aenter__()
    return service


def json_response(payload):
    return httpx.Response(200, headers={"content-type": "application/json"}, json=payload)


async def test_fresh_cache_never_touches_linkedin(live_settings, synthetic_payload):
    live_settings.cache_enabled = True
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return json_response(synthetic_payload)

    service = await build_service(live_settings, handler)
    try:
        first = await service.get_profile("https://www.linkedin.com/in/priya-raghavan-synthetic")
        assert first.meta.source == "live"
        assert first.meta.upstream_requests == 1

        second = await service.get_profile("https://www.linkedin.com/in/priya-raghavan-synthetic")
        assert second.meta.source == "cache"
        assert second.meta.upstream_requests == 0
        assert second.profile.full_name == "Priya Raghavan"
        assert len(calls) == 1, "the cached request must not have reached LinkedIn"
    finally:
        await service.shutdown()


async def test_refresh_forces_a_live_fetch(live_settings, synthetic_payload):
    live_settings.cache_enabled = True
    calls = []

    def handler(request):
        calls.append(1)
        return json_response(synthetic_payload)

    service = await build_service(live_settings, handler)
    try:
        await service.get_profile("priya-raghavan-synthetic")
        again = await service.get_profile("priya-raghavan-synthetic", refresh=True)
        assert again.meta.source == "live"
        assert len(calls) == 2
    finally:
        await service.shutdown()


async def test_stale_cache_is_served_when_linkedin_blocks_us(live_settings, synthetic_payload):
    """The availability guarantee: degraded and labelled beats a 502."""
    live_settings.cache_enabled = True
    state = {"blocked": False}

    def handler(request):
        if state["blocked"]:
            return httpx.Response(999, text="blocked")
        return json_response(synthetic_payload)

    service = await build_service(live_settings, handler)
    try:
        await service.get_profile("priya-raghavan-synthetic")
        # Age the entry past its TTL, then block upstream.
        service.settings.cache_ttl_seconds = 0
        state["blocked"] = True

        response = await service.get_profile("priya-raghavan-synthetic")
        assert response.meta.source == "stale-cache"
        assert response.meta.stale is True
        assert response.meta.partial is True
        assert response.profile.full_name == "Priya Raghavan"
    finally:
        await service.shutdown()


async def test_block_with_no_cache_surfaces_the_error(live_settings):
    service = await build_service(live_settings, lambda r: httpx.Response(999, text="blocked"))
    try:
        with pytest.raises(UpstreamBlocked):
            await service.get_profile("never-fetched-before")
    finally:
        await service.shutdown()


async def test_dash_is_tried_first_and_profile_view_is_not_touched(live_settings):
    """profileView answers 410 Gone, so trying it first wasted one upstream
    request on every single fetch -- doubling the cost against a budget of about
    a hundred a day. dash goes first.
    """
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return json_response(
            {
                "data": {},
                "included": [
                    {
                        "entityUrn": "urn:li:fsd_profile:X",
                        "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                        "firstName": "Dash",
                        "lastName": "First",
                        "headline": "h",
                        "publicIdentifier": "dash-first",
                    }
                ],
            }
        )

    service = await build_service(live_settings, handler)
    try:
        response = await service.get_profile("dash-first")
        assert response.meta.strategy == "dash"
        assert response.meta.upstream_requests == 1, "one request, not two"
        assert not any("profileView" in path for path in seen)
    finally:
        await service.shutdown()


async def test_profile_view_still_serves_as_a_fallback(live_settings, synthetic_payload):
    """Kept for the day LinkedIn changes dash: it costs nothing until then."""

    def handler(request):
        if "identity/dash/profiles" in request.url.path:
            return httpx.Response(400)
        if "profileView" in request.url.path:
            return json_response(synthetic_payload)
        return httpx.Response(404)

    service = await build_service(live_settings, handler)
    try:
        response = await service.get_profile("priya-raghavan-synthetic")
        assert response.meta.strategy == "profile_view"
        assert response.profile.full_name == "Priya Raghavan"
        assert len(response.profile.skills) == 15
    finally:
        await service.shutdown()


async def test_unanimous_404_means_not_found(live_settings):
    service = await build_service(live_settings, lambda r: httpx.Response(404))
    try:
        with pytest.raises(ProfileNotFound):
            await service.get_profile("definitely-nobody")
    finally:
        await service.shutdown()


async def test_truncation_is_read_off_linkedins_own_paging(live_settings):
    payload = {
        "data": {"*profile": "urn:p", "*skillView": "urn:sv"},
        "included": [
            {"entityUrn": "urn:p", "$type": "x.Profile", "firstName": "A", "lastName": "B"},
            {
                "entityUrn": "urn:sv",
                "$type": "com.linkedin.voyager.identity.profile.SkillView",
                "*elements": ["urn:s1"],
                "paging": {"count": 1, "start": 0, "total": 47},
            },
            {"entityUrn": "urn:s1", "$type": "x.Skill", "name": "Python"},
        ],
    }

    def handler(request):
        if "identity/dash/profiles" in request.url.path:
            return httpx.Response(400)
        return json_response(payload)

    service = await build_service(live_settings, handler)
    try:
        response = await service.get_profile("someone")
        assert response.meta.strategy == "profile_view"
        assert "skill" in response.meta.sections_truncated
    finally:
        await service.shutdown()


async def test_parser_crash_is_contained(live_settings, monkeypatch):
    """A parser bug must cost the request, never the process."""
    from app.parsers import profile_view as pv

    def boom(*args, **kwargs):
        raise ValueError("simulated parser bug")

    monkeypatch.setattr(pv, "parse", boom)
    service = await build_service(
        live_settings, lambda r: json_response({"data": {}, "included": [{"a": 1}]})
    )
    try:
        with pytest.raises(Exception) as exc:
            await service.get_profile("someone")
        assert "PARSER_ERROR" in str(exc.value.details) or exc.value.code
    finally:
        await service.shutdown()


async def test_demo_mode_never_calls_linkedin(demo_settings):
    service = ProfileService(demo_settings)
    await service.startup()
    try:
        response = await service.get_profile("priya-raghavan-synthetic")
        assert response.meta.source == "fixture"
        assert response.meta.upstream_requests == 0
        assert response.profile.full_name == "Priya Raghavan"
    finally:
        await service.shutdown()


async def test_dash_truncation_comes_from_linkedins_own_paging(live_settings):
    """`paging.total` is the only honest source for "was this cut short?".

    Observed live: a sparse profile reports total 0 for most sections. Reporting
    those as truncated would be indistinguishable from data being withheld, and
    would send the caller off to spend requests re-fetching nothing.
    """

    def collection(urn, elements, total):
        return {
            "entityUrn": urn,
            "$type": "com.linkedin.restli.common.CollectionResponse",
            "*elements": elements,
            "paging": {"start": 0, "count": 20, "total": total},
        }

    payload = {
        "data": {},
        "included": [
            {
                "entityUrn": "urn:li:fsd_profile:X",
                "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                "firstName": "Sparse",
                "lastName": "Profile",
                "headline": "h",
                "publicIdentifier": "sparse",
                "*profileSkills": "urn:c:skills",
                "*profileLanguages": "urn:c:langs",
                "*profileEducations": "urn:c:edu",
            },
            # 3 of 47 skills present -> genuinely truncated.
            collection("urn:c:skills", ["urn:s:1", "urn:s:2", "urn:s:3"], 47),
            # 0 of 0 languages -> empty, NOT truncated.
            collection("urn:c:langs", [], 0),
            # 1 of 1 education -> complete.
            collection("urn:c:edu", ["urn:e:1"], 1),
            {"entityUrn": "urn:s:1", "$type": "x.profile.Skill", "name": "Python"},
            {"entityUrn": "urn:s:2", "$type": "x.profile.Skill", "name": "Go"},
            {"entityUrn": "urn:s:3", "$type": "x.profile.Skill", "name": "SQL"},
            {"entityUrn": "urn:e:1", "$type": "x.profile.Education", "schoolName": "A School"},
        ],
    }

    service = await build_service(live_settings, lambda r: json_response(payload))
    try:
        response = await service.get_profile("sparse")
        assert response.meta.sections_truncated == ["skills"]
        assert len(response.profile.skills) == 3
        assert len(response.profile.education) == 1
        assert response.profile.languages == []
    finally:
        await service.shutdown()


async def test_retired_endpoint_410_falls_through_without_blaming_the_session(live_settings):
    """profileView returns 410 Gone on live LinkedIn -- the endpoint is retired.

    That is not the session's fault, so it must not count as a failure against
    it, or a healthy cookie would be cooled off by an endpoint that no longer
    exists.
    """

    def handler(request):
        if "profileView" in request.url.path:
            return httpx.Response(410)
        return json_response(
            {
                "data": {},
                "included": [
                    {
                        "entityUrn": "urn:li:fsd_profile:X",
                        "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                        "firstName": "Still",
                        "lastName": "Works",
                        "headline": "h",
                        "publicIdentifier": "still-works",
                    }
                ],
            }
        )

    service = await build_service(live_settings, handler)
    try:
        response = await service.get_profile("still-works")
        assert response.meta.strategy == "dash"
        assert response.profile.full_name == "Still Works"
        assert service.pool.status()[0]["state"] == "healthy"
        assert service.pool.status()[0]["failures"] == 0
    finally:
        await service.shutdown()


def _skill_page(start: int, count: int, total: int, prefix: str = "skill"):
    """A dash per-section collection response, as LinkedIn returns one."""
    urns = [f"urn:s:{i}" for i in range(start, min(start + count, total))]
    return {
        "data": {
            "$type": "com.linkedin.restli.common.CollectionResponse",
            "*elements": urns,
            "paging": {"start": start, "count": count, "total": total},
        },
        "included": [
            {"entityUrn": u, "$type": "x.profile.Skill", "name": f"{prefix}-{u.rsplit(':', 1)[-1]}"}
            for u in urns
        ],
    }


def _profile_with_skills(retrieved: int, total: int):
    """A dash profile whose skills collection is capped at `retrieved` of `total`."""
    urns = [f"urn:s:{i}" for i in range(retrieved)]
    return {
        "data": {},
        "included": [
            {
                "entityUrn": "urn:li:fsd_profile:PAGED",
                "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
                "firstName": "Well",
                "lastName": "Filled",
                "headline": "h",
                "publicIdentifier": "well-filled",
                "*profileSkills": "urn:c:skills",
            },
            {
                "entityUrn": "urn:c:skills",
                "$type": "com.linkedin.restli.common.CollectionResponse",
                "*elements": urns,
                "paging": {"start": 0, "count": 20, "total": total},
            },
            *(
                {
                    "entityUrn": u,
                    "$type": "x.profile.Skill",
                    "name": f"skill-{u.rsplit(':', 1)[-1]}",
                }
                for u in urns
            ),
        ],
    }


async def test_truncated_sections_are_paged_in(live_settings):
    """20 of someone's 47 skills is not "most of the information on the page"."""
    calls = []

    def handler(request):
        if "identity/dash/profileSkills" in request.url.path:
            start = int(request.url.params.get("start", 0))
            count = int(request.url.params.get("count", 20))
            calls.append(start)
            return json_response(_skill_page(start, count, 47))
        if "identity/dash/profiles" in request.url.path:
            return json_response(_profile_with_skills(20, 47))
        return httpx.Response(404)

    service = await build_service(live_settings, handler)
    try:
        response = await service.get_profile("well-filled")
        assert len(response.profile.skills) == 47, "every skill must be present"
        assert response.meta.sections_truncated == [], "nothing left truncated"
        assert calls == [20, 40], "two pages, starting where the profile left off"
        # 1 profile + 2 pages
        assert response.meta.upstream_requests == 3
        names = [s.name for s in response.profile.skills]
        assert len(set(names)) == 47, "no duplicates across page boundaries"
    finally:
        await service.shutdown()


async def test_pagination_respects_its_request_budget(live_settings):
    """A profile with 400 skills must not quietly eat the day's allowance."""
    live_settings.max_pagination_requests = 2
    pages = []

    def handler(request):
        if "identity/dash/profileSkills" in request.url.path:
            start = int(request.url.params.get("start", 0))
            pages.append(start)
            return json_response(_skill_page(start, 20, 400))
        if "identity/dash/profiles" in request.url.path:
            return json_response(_profile_with_skills(20, 400))
        return httpx.Response(404)

    service = await build_service(live_settings, handler)
    try:
        response = await service.get_profile("well-filled")
        assert len(pages) == 2, "budget honoured"
        assert len(response.profile.skills) == 60, "20 embedded + 2 pages of 20"
        # Still incomplete, and says so rather than pretending.
        assert response.meta.sections_truncated == ["skills"]
    finally:
        await service.shutdown()


async def test_pagination_failure_keeps_what_it_got(live_settings):
    """A failed page must not discard the entries already collected."""

    def handler(request):
        if "identity/dash/profileSkills" in request.url.path:
            start = int(request.url.params.get("start", 0))
            if start >= 40:
                return httpx.Response(500)
            return json_response(_skill_page(start, 20, 47))
        if "identity/dash/profiles" in request.url.path:
            return json_response(_profile_with_skills(20, 47))
        return httpx.Response(404)

    service = await build_service(live_settings, handler)
    try:
        response = await service.get_profile("well-filled")
        assert len(response.profile.skills) == 40, "the good pages survive"
        assert response.meta.sections_truncated == ["skills"]
    finally:
        await service.shutdown()


async def test_complete_sections_are_not_re_fetched(live_settings, synthetic_payload):
    """Nothing is spent on a section LinkedIn already returned in full."""
    collection_calls = []

    def handler(request):
        if "/identity/dash/profile" in request.url.path and "profiles" not in request.url.path:
            collection_calls.append(request.url.path)
            return json_response(_skill_page(0, 20, 0))
        if "identity/dash/profiles" in request.url.path:
            return json_response(_profile_with_skills(3, 3))
        return httpx.Response(404)

    service = await build_service(live_settings, handler)
    try:
        response = await service.get_profile("well-filled")
        assert len(response.profile.skills) == 3
        assert collection_calls == [], "no pagination requests for a complete section"
        assert response.meta.upstream_requests == 1
    finally:
        await service.shutdown()


async def test_caching_is_off_by_default():
    """Out of the box the service stores no personal data.

    Caching is a deliberate opt-in rather than a default, because the brief asks
    for a URL-in/JSON-out API and caching means holding the profile of everyone
    looked up through it.
    """
    from app.config import Settings

    assert Settings(_env_file=None).cache_enabled is False


async def test_caching_can_be_switched_off_entirely(live_settings, synthetic_payload):
    """A no-store deployment must be one flag, not a code change.

    Caching exists to protect the LinkedIn account, but it does so by keeping
    other people's personal data on disk. Anyone who would rather pay the
    rate-limit cost than hold the data should be able to say so.
    """
    live_settings.cache_enabled = False
    calls = []

    def handler(request):
        calls.append(1)
        if "identity/dash/profiles" in request.url.path:
            return httpx.Response(400)
        return json_response(synthetic_payload)

    service = await build_service(live_settings, handler)
    try:
        first = await service.get_profile("priya-raghavan-synthetic")
        second = await service.get_profile("priya-raghavan-synthetic")
        assert first.meta.source == "live"
        assert second.meta.source == "live", "must not be served from cache"
        assert service.cache.stats()["entries"] == 0, "nothing written to disk"
        assert len(calls) > 2, "each request really went upstream"
    finally:
        await service.shutdown()


async def test_retention_defaults_are_short():
    """Pinned deliberately: an earlier 24h/30d default optimised availability
    using third parties' personal data as the currency."""
    from app.config import Settings

    s = Settings(_env_file=None)
    assert s.cache_ttl_seconds <= 3_600, "fresh window should be an hour or less"
    assert s.cache_stale_max_seconds <= 86_400, "data must not be retained for weeks"


async def test_a_retired_endpoint_cannot_outvote_a_real_verdict(live_settings):
    """The bug the deployed instance showed: a typo'd URL returned 502, not 404.

    dash correctly concluded "no such profile", then profileView answered 410
    Gone -- and that retired-endpoint error was counted as a failed attempt,
    which made the votes non-unanimous and produced UPSTREAM_ERROR. A strategy
    that never ran gets no vote.
    """

    def handler(request):
        if "profileView" in request.url.path:
            return httpx.Response(410)
        if request.url.path.endswith("/me"):
            # The session probe: cookie is fine, so the profile is the problem.
            return json_response({"data": {}, "included": [{"entityUrn": "urn:me", "$type": "x"}]})
        return httpx.Response(999, text="blocked")

    service = await build_service(live_settings, handler)
    try:
        with pytest.raises(ProfileNotFound) as exc:
            await service.get_profile("definitely-nobody-99887766")
        assert exc.value.status_code == 404
        assert exc.value.details["skipped"]["profile_view"] == "ENDPOINT_RETIRED"
        # And the cookie is untouched by any of it.
        assert service.pool.status()[0]["state"] == "healthy"
        assert service.pool.status()[0]["failures"] == 0
    finally:
        await service.shutdown()
