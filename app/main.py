"""FastAPI application.

Interactive documentation is served at /docs -- generated from the Pydantic
models, so it cannot drift from the actual response shape.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings, get_settings
from .errors import ApiError, Unauthorized
from .models import ProfileResponse
from .service import ProfileService

logging.basicConfig(
    level=get_settings().log_level.upper(),
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
log = logging.getLogger("linkedin_profile_api")

DESCRIPTION = """
Turns a LinkedIn profile URL into structured JSON.

Works by talking to LinkedIn's own internal **Voyager** API -- the private
backend that linkedin.com's frontend uses -- authenticated with a real member
session cookie. There is no public LinkedIn API for this data.

### Quick start

    GET /v1/profile?url=https://www.linkedin.com/in/some-person

### Notes that matter

* **Rate limits are real.** LinkedIn soft-blocks a member account at roughly
  80-150 profile views per day. This service throttles itself well below that
  and caches aggressively; a cache hit never touches LinkedIn.
* **Nothing returns a bare 500.** Every failure carries a stable
  `error.code` -- see the error table in the README.
* **Degradation is reported, not hidden.** Check `meta.partial`, `meta.stale`,
  `meta.sections_failed` and `meta.sections_truncated` before trusting a
  response to be complete and current.
"""

TAGS = [
    {"name": "profile", "description": "Fetch profile data."},
    {"name": "ops", "description": "Health, session state, cache and rate-limit telemetry."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    service = ProfileService(settings)
    await service.startup()
    app.state.service = service
    try:
        yield
    finally:
        await service.shutdown()


app = FastAPI(
    title="LinkedIn Profile API",
    version=__version__,
    description=DESCRIPTION,
    openapi_tags=TAGS,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def get_service(request: Request) -> ProfileService:
    return request.app.state.service


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> None:
    """No-op unless REQUIRE_API_KEY is on -- keeps the public demo curl-able."""
    if not settings.require_api_key:
        return
    if not x_api_key or x_api_key not in settings.allowed_api_keys:
        raise Unauthorized()


# ---------------------------------------------------------------------------
# Error handling: one place, so no endpoint can leak a traceback.
# ---------------------------------------------------------------------------


@app.exception_handler(ApiError)
async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    headers = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    log.info("%s %s -> %s", request.method, request.url.path, exc.code)
    return JSONResponse(exc.to_payload(), status_code=exc.status_code, headers=headers)


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        {
            "status": "error",
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Unexpected server error.",
                "retryable": True,
            },
        },
        status_code=500,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/health", tags=["ops"], summary="Liveness")
async def health(service: ProfileService = Depends(get_service)) -> dict:
    status = service.status()
    healthy_sessions = sum(1 for s in status["sessions"] if s["state"] == "healthy")
    return {
        "status": "ok",
        "version": __version__,
        "demo_mode": status["demo_mode"],
        "sessions_configured": len(status["sessions"]),
        "sessions_healthy": healthy_sessions,
        "cache_entries": status["cache"]["entries"],
        "fixtures": len(status["fixtures"]),
    }


@app.get("/v1/status", tags=["ops"], summary="Session, cache and rate-limit detail")
async def status(
    _: None = Depends(require_api_key),
    service: ProfileService = Depends(get_service),
) -> dict:
    """Operational telemetry. Cookies are redacted; only a fingerprint is shown."""
    return {"status": "ok", "version": __version__, **service.status()}


@app.get(
    "/v1/session/check",
    tags=["ops"],
    summary="Verify the LinkedIn cookie is still valid (costs 1 upstream request)",
)
async def session_check(
    _: None = Depends(require_api_key),
    service: ProfileService = Depends(get_service),
) -> dict:
    alive = await service.probe()
    return {
        "status": "ok",
        "session_valid": alive,
        "detail": (
            "LinkedIn accepted the session."
            if alive
            else "LinkedIn rejected the session -- refresh li_at and JSESSIONID."
        ),
    }


@app.get(
    "/v1/profile",
    tags=["profile"],
    response_model=ProfileResponse,
    response_model_exclude_none=False,
    summary="Fetch one profile",
)
async def get_profile(
    url: str = Query(
        ...,
        description="A LinkedIn profile URL. Bare vanity names and /pub/ and "
                    "/mwlite/ forms are accepted too.",
        examples=["https://www.linkedin.com/in/satyanadella"],
    ),
    refresh: bool = Query(
        False, description="Bypass the cache and force a live fetch. Spends rate-limit budget."
    ),
    contact_info: bool = Query(
        False,
        description="Also fetch contact info. Only ever populated for 1st-degree "
                    "connections; costs one extra upstream request.",
    ),
    _: None = Depends(require_api_key),
    service: ProfileService = Depends(get_service),
) -> ProfileResponse:
    return await service.get_profile(url, refresh=refresh, include_contact_info=contact_info)


class BatchRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=25)
    refresh: bool = False


@app.post(
    "/v1/profiles",
    tags=["profile"],
    summary="Fetch several profiles (sequential, rate-limit aware)",
)
async def get_profiles(
    body: BatchRequest,
    _: None = Depends(require_api_key),
    service: ProfileService = Depends(get_service),
) -> dict:
    """Batch fetch.

    Deliberately **sequential**, not concurrent. Firing ten parallel requests
    at LinkedIn from one session is the fastest way to get that session
    challenged, so batching here saves the caller round-trips -- it does not
    remove the spacing between upstream calls. Per-URL failures are reported
    inline rather than failing the whole batch, and the batch stops early if
    the rate limiter or LinkedIn shuts us down, since every further attempt
    would fail identically.
    """
    results: list[dict] = []
    halted: str | None = None

    for url in body.urls:
        if halted:
            results.append(
                {
                    "url": url,
                    "status": "skipped",
                    "error": {"code": halted, "message": "Batch halted by an earlier failure.",
                              "retryable": True},
                }
            )
            continue
        try:
            response = await service.get_profile(url, refresh=body.refresh)
            results.append({"url": url, **response.model_dump(mode="json")})
        except ApiError as exc:
            results.append({"url": url, **exc.to_payload()})
            if exc.code in ("RATE_LIMITED", "UPSTREAM_BLOCKED", "SESSION_EXPIRED",
                            "NO_SESSIONS_CONFIGURED"):
                halted = exc.code
        except Exception:
            log.exception("batch item failed: %s", url)
            results.append(
                {
                    "url": url,
                    "status": "error",
                    "error": {"code": "INTERNAL_ERROR", "message": "Unexpected error.",
                              "retryable": True},
                }
            )

    succeeded = sum(1 for r in results if r.get("status") == "ok")
    return {
        "status": "ok",
        "requested": len(body.urls),
        "succeeded": succeeded,
        "failed": len(body.urls) - succeeded,
        "halted_by": halted,
        "results": results,
    }


@app.delete("/v1/cache", tags=["ops"], summary="Forget every cached profile")
async def purge_cache(
    _: None = Depends(require_api_key),
    service: ProfileService = Depends(get_service),
) -> dict:
    """Drop the whole cache.

    This service stores the personal data of everyone looked up through it, so
    "forget all of it" needs to be one call rather than a database chore.
    """
    removed = service.cache.purge_all()
    return {"status": "ok", "entries_removed": removed}


@app.delete("/v1/cache/{public_identifier}", tags=["ops"], summary="Evict one cached profile")
async def evict(
    public_identifier: str,
    _: None = Depends(require_api_key),
    service: ProfileService = Depends(get_service),
) -> dict:
    removed = service.cache.delete(public_identifier)
    return {"status": "ok", "public_identifier": public_identifier, "entries_removed": removed}
