"""Configuration, loaded from environment / .env. No secret ever lives in code."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Hard ceilings on how long other people's profile data may be held. Deliberately
# not configurable upward: a value left behind in a hosting dashboard must not be
# able to extend retention past what the service promises.
MAX_CACHE_TTL_SECONDS = 3_600        # 1 hour served as fresh
MAX_CACHE_RETENTION_SECONDS = 86_400  # 24 hours, then deleted

# A real, current desktop Chrome UA. LinkedIn rejects obviously-scripted agents.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LinkedIn sessions -------------------------------------------------
    # Two ways to supply credentials, in precedence order:
    #
    #   LI_AT + JSESSIONID    one account, one cookie per variable. Easiest to
    #                         paste straight out of DevTools, so this is the
    #                         documented path in .env and in hosting dashboards.
    #   LINKEDIN_SESSIONS     "li_at|jsessionid" pairs, comma-separated. The
    #                         only way to configure a pool of several accounts.
    #   LINKEDIN_COOKIE       the entire `cookie:` header copied from a real
    #                         browser request. Strongly preferred: it carries
    #                         bcookie/bscookie, LinkedIn's device-identity pair,
    #                         without which LinkedIn invalidates the session.
    linkedin_cookie: str = ""
    li_at: str = ""
    jsessionid: str = ""
    linkedin_sessions: str = ""

    # --- API access control ------------------------------------------------
    api_keys: str = ""
    require_api_key: bool = False

    # --- Rate limiting -----------------------------------------------------
    max_profiles_per_hour: int = 12
    max_profiles_per_day: int = 80
    min_delay_seconds: float = 3.0
    max_delay_seconds: float = 9.0

    # --- Cache -------------------------------------------------------------
    # Retention is a privacy decision, not a tuning knob. This cache holds other
    # people's personal data, so the window is the shortest that still does its
    # two jobs -- making repeat requests free, and having something to serve when
    # LinkedIn blocks the session. An earlier 24h/30d default optimised
    # availability using third parties' data as the currency.
    cache_ttl_seconds: int = 3_600        # 1h  -- served as fresh
    cache_stale_max_seconds: int = 86_400  # 24h -- last-resort fallback, then deleted
    # Off by default. The brief asks for a URL-in/JSON-out API and nothing more,
    # so the out-of-the-box behaviour stores no personal data at all. Turn it on
    # for a deployment you are demoing: it makes repeat requests free and gives
    # the service something to serve when LinkedIn kills the session, which is
    # the only thing standing between a blocked session and a 502.
    cache_enabled: bool = False
    cache_path: str = "data/cache.sqlite3"

    # --- Section pagination ------------------------------------------------
    # LinkedIn caps profile collections at 20 entries, so a well-filled profile
    # needs extra requests to complete. Each page is one unit of the daily
    # budget, so the ceiling is explicit rather than unbounded: a profile with
    # 200 skills must not silently consume a tenth of the day's allowance.
    max_pagination_requests: int = 8

    # --- Behaviour ---------------------------------------------------------
    demo_mode: bool = False
    fixtures_dir: str = "fixtures/raw"
    request_timeout: float = 25.0
    log_level: str = "INFO"
    user_agent: str = DEFAULT_USER_AGENT

    @field_validator("cache_ttl_seconds")
    @classmethod
    def _cap_ttl(cls, value: int) -> int:
        return min(value, MAX_CACHE_TTL_SECONDS)

    @field_validator("cache_stale_max_seconds")
    @classmethod
    def _cap_retention(cls, value: int) -> int:
        """Clamp retention rather than trusting the environment.

        This cache holds the personal data of everyone looked up through the
        service, so how long that is kept is a privacy decision, not a tuning
        knob -- and a stale value left in a hosting dashboard should not be able
        to quietly extend it to a month. Configurable downward, capped upward.
        """
        return min(value, MAX_CACHE_RETENTION_SECONDS)

    # --- Derived -----------------------------------------------------------
    @property
    def sessions(self) -> list[tuple[str, str]]:
        """Every configured session as [(li_at, jsessionid), ...].

        LI_AT/JSESSIONID and LINKEDIN_SESSIONS are additive, so a single-account
        .env and a multi-account deployment can share one code path. Duplicates
        are dropped: the same cookie twice would mean one account being charged
        double against its own rate limit.
        """
        out: list[tuple[str, str]] = []

        browser = self.browser_cookies
        if browser.get("li_at"):
            out.append((browser["li_at"], browser.get("JSESSIONID", "").strip('"')))

        if self.li_at.strip():
            out.append((self.li_at.strip(), self.jsessionid.strip().strip('"')))
        for raw in self.linkedin_sessions.split(","):
            raw = raw.strip()
            if not raw:
                continue
            if "|" not in raw:
                # Tolerate a bare li_at; JSESSIONID is then unknown and the
                # client will bootstrap one on first request.
                out.append((raw, ""))
                continue
            li_at, jsessionid = raw.split("|", 1)
            out.append((li_at.strip(), jsessionid.strip().strip('"')))

        seen: set[str] = set()
        unique: list[tuple[str, str]] = []
        for li_at, jsessionid in out:
            if li_at in seen:
                continue
            seen.add(li_at)
            unique.append((li_at, jsessionid))
        return unique

    @property
    def browser_cookies(self) -> dict[str, str]:
        """Every cookie from LINKEDIN_COOKIE, as copied out of the browser."""
        from .linkedin.endpoints import parse_cookie_header

        return parse_cookie_header(self.linkedin_cookie) if self.linkedin_cookie else {}

    @property
    def device_cookies(self) -> dict[str, str]:
        """The non-credential cookies to send alongside li_at.

        li_at and JSESSIONID are excluded because they are per-session and come
        from the session pool; everything else identifies the browser and is
        shared across sessions.
        """
        return {
            name: value
            for name, value in self.browser_cookies.items()
            if name not in ("li_at", "JSESSIONID")
        }

    @property
    def allowed_api_keys(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
