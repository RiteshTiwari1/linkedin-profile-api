"""The reverse-engineered surface of LinkedIn's internal API.

Everything here was found by opening a profile in Chrome with DevTools'
Network tab filtered to XHR. The linkedin.com SPA talks to its own private
backend at `www.linkedin.com/voyager/api/...`; these are the calls the profile
page makes.

Three things make a Voyager request work:

1. `cookie: li_at=...; JSESSIONID="ajax:..."` -- proves you are a logged-in
   member. The li_at cookie is the session; it lasts about a year.
2. `csrf-token: ajax:...` -- must equal the JSESSIONID cookie value with the
   surrounding double quotes removed. Voyager rejects the request otherwise.
   This is the detail that trips up most first attempts.
3. `accept: application/vnd.linkedin.normalized+json+2.1` -- asks for the
   *normalized* representation: `{data, included[]}` with URN cross-references
   instead of a deeply nested tree. See normalize.py.

Plus `x-restli-protocol-version: 2.0.0`, because Voyager speaks Rest.li and
without it the query-parameter encoding is interpreted differently.
"""

from __future__ import annotations

import base64
import json
import uuid

BASE = "https://www.linkedin.com/voyager/api"

# The voyager-web build the real client reports. Observed in a live request;
# LinkedIn bumps it every release. Stale by a few versions is unremarkable,
# absent or obviously fake is not.
CLIENT_VERSION = "1.13.46243"

# --- Endpoints -------------------------------------------------------------

# Cheap authentication probe -- returns the logged-in member. Used for health
# checks so we can distinguish "our cookie died" from "that profile is gone".
ME = "/me"

# The classic profile endpoint. Returns a proper domain model (positionView,
# educationView, skillView, ...) rather than a UI component tree, and is much
# nicer to parse -- but it now answers **410 Gone**, verified live. Kept as a
# fallback rather than deleted: it costs nothing while dash keeps working, and
# it is the natural landing place if LinkedIn ever restores it.
PROFILE_VIEW = "/identity/profiles/{public_id}/profileView"

# Contact info. Only returns emails/phones for 1st-degree connections.
CONTACT_INFO = "/identity/profiles/{public_id}/profileContactInfo"

# The "dash" (Data Schema) generation, and the endpoint that actually works
# today. Needs a decorationId naming the projection you want; the trailing
# number is a version LinkedIn bumps, which is why several candidates are tried
# and the winner is remembered. This is strategy #1.
DASH_PROFILES = "/identity/dash/profiles"

# Per-section dash collections. Each answers
#   /identity/dash/{collection}?q=viewee&profileUrn=...&start=N&count=20
# with a CollectionResponse carrying `paging.total`, and -- usefully -- needs no
# decorationId at all. This is how sections longer than one page are completed:
# the main profile projection caps collections at 20 entries, and without these
# a profile with 47 skills could only ever return 20 of them.
DASH_COLLECTIONS: dict[str, str] = {
    "experience": "profilePositionGroups",
    "education": "profileEducations",
    "skills": "profileSkills",
    "certifications": "profileCertifications",
    "languages": "profileLanguages",
    "projects": "profileProjects",
    "publications": "profilePublications",
    "honors": "profileHonors",
    "courses": "profileCourses",
    "volunteering": "profileVolunteerExperiences",
    "patents": "profilePatents",
    "test_scores": "profileTestScores",
    "organizations": "profileOrganizations",
}

# LinkedIn's own page size for these collections.
COLLECTION_PAGE_SIZE = 20

DASH_COLLECTION_PATH = "/identity/dash/{collection}"

# --- Dash decoration candidates -------------------------------------------
# Tried in order; the first that returns 200 is cached for the process
# lifetime. Cheap self-healing against LinkedIn's version bumps.
DASH_DECORATIONS: tuple[str, ...] = (
    "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-101",
    "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-96",
    "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-83",
    "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-71",
    "com.linkedin.voyager.dash.deco.identity.profile.WebTopCardCore-6",
)

def build_headers(
    jsessionid: str,
    user_agent: str,
    *,
    client_version: str = CLIENT_VERSION,
    timezone: str = "Asia/Calcutta",
    timezone_offset: float = 5.5,
    page_key: str = "d_flagship3_profile_view_base",
    viewport: tuple[int, int] = (2940, 1912),
) -> dict[str, str]:
    """Headers for a Voyager request, mirroring what linkedin.com actually sends.

    The header set is copied from a real logged-in request rather than reduced to
    the minimum that "works", because LinkedIn invalidates sessions whose traffic
    does not look like the browser they were minted in. Two in particular are
    easy to omit and shouldn't be:

    * `csrf-token` must equal the JSESSIONID cookie value with its surrounding
      double quotes removed. This is the load-bearing line; a mismatch is a 403.
    * `x-restli-protocol-version: 2.0.0` -- Voyager speaks Rest.li, and without
      this the query-parameter encoding is read under older rules.

    `x-li-track` is the client's self-description. The real one reports the
    display geometry and timezone, so a request claiming a plausible desktop is
    less anomalous than one omitting the header's richer fields entirely.
    """
    track = {
        "clientVersion": client_version,
        "mpVersion": client_version,
        "osName": "web",
        "timezoneOffset": timezone_offset,
        "timezone": timezone,
        "deviceFormFactor": "DESKTOP",
        "mpName": "voyager-web",
        "displayDensity": 2,
        "displayWidth": viewport[0],
        "displayHeight": viewport[1],
    }
    return {
        "accept": "application/vnd.linkedin.normalized+json+2.1",
        "accept-language": "en-US,en;q=0.9",
        "csrf-token": jsessionid.strip('"'),
        "priority": "u=1, i",
        "referer": "https://www.linkedin.com/feed/",
        "user-agent": user_agent,
        "x-li-lang": "en_US",
        # A per-request tracking id, as the real client sends. Constant values
        # across thousands of requests would themselves be a signal.
        "x-li-page-instance": f"urn:li:page:{page_key};{_tracking_id()}",
        "x-li-track": json.dumps(track, separators=(",", ":")),
        "x-requested-with": "XMLHttpRequest",
        "x-restli-protocol-version": "2.0.0",
        # Client hints. Chrome sends these on every XHR; their absence alongside
        # a Chrome user-agent is contradictory.
        "sec-ch-prefers-color-scheme": "light",
        "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def _tracking_id() -> str:
    """A base64 tracking id in LinkedIn's format: 16 random bytes."""
    return base64.b64encode(uuid.uuid4().bytes).decode("ascii")


# Cookies LinkedIn uses to recognise the *device*, as opposed to the member.
# `bcookie`/`bscookie` are its browser-identity pair. Presenting a valid li_at
# without them looks like a cookie lifted from someone else's browser, and
# LinkedIn responds by invalidating the session -- which is exactly what
# happened during development when only li_at and JSESSIONID were sent.
DEVICE_COOKIES = ("bcookie", "bscookie", "lidc", "li_gc", "lang", "li_theme")


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse a `cookie:` header copied out of DevTools into a dict.

    Accepts what "Copy as cURL" or the Network tab's request-header view gives
    you: `name=value; name2="value2"; ...`. Quoted values keep their quotes,
    because LinkedIn's own jar stores JSESSIONID quoted and the server compares
    it that way.
    """
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if name:
            cookies[name] = value
    return cookies


def build_cookies(
    li_at: str, jsessionid: str, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """The cookie jar for one session.

    `extra` carries the device cookies from a real browser. They are merged
    underneath so an explicit li_at/JSESSIONID always wins.
    """
    cookies: dict[str, str] = dict(extra or {})
    if li_at:
        cookies["li_at"] = li_at
    if jsessionid:
        # LinkedIn stores this cookie *with* the quotes. Keep them here and
        # strip them only for the csrf-token header.
        value = jsessionid if jsessionid.startswith('"') else f'"{jsessionid}"'
        cookies["JSESSIONID"] = value
    return cookies

