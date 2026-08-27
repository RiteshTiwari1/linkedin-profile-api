"""LinkedIn profile URL normalisation.

The single input to this API is a URL typed or pasted by a human, so it arrives
in every shape LinkedIn has ever emitted:

    https://www.linkedin.com/in/satyanadella/
    linkedin.com/in/satyanadella?originalSubdomain=in
    https://in.linkedin.com/in/some-name-8a1b2c3
    https://www.linkedin.com/mwlite/in/some-name
    https://www.linkedin.com/pub/some-name/1/2a/3b4      (legacy)
    satyanadella                                          (bare vanity)

All of them reduce to one thing: the public identifier (vanity name).
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from .errors import InvalidProfileUrl

_ALLOWED_HOST = re.compile(r"(^|\.)linkedin\.com$", re.IGNORECASE)

# Ordered by specificity.
_PATH_PATTERNS = (
    re.compile(r"^/+(?:mwlite/)?in/(?P<vanity>[^/?#]+)", re.IGNORECASE),
    re.compile(r"^/+pub/(?P<vanity>[^/?#]+)", re.IGNORECASE),
    re.compile(r"^/+profile/view", re.IGNORECASE),  # very old ?id= form
)

# A vanity name is what LinkedIn allows: letters (incl. unicode), digits, hyphens.
_VANITY_OK = re.compile(r"^[\w\-À-￿]{2,120}$", re.UNICODE)

_RESERVED = {
    "feed", "jobs", "company", "school", "learning", "messaging", "mynetwork",
    "notifications", "groups", "events", "posts", "pulse", "help", "legal",
    "login", "signup", "checkpoint", "uas", "search", "sales", "talent",
}


def _clean(vanity: str) -> str:
    vanity = unquote(vanity).strip().strip("/")
    # Strip a trailing tracking fragment some share links append.
    vanity = vanity.split("?", 1)[0].split("#", 1)[0]
    return vanity


def extract_public_identifier(raw: str) -> str:
    """Reduce any accepted input form to a LinkedIn public identifier.

    Raises InvalidProfileUrl rather than guessing, so a typo surfaces as a 400
    instead of a confusing 404 from LinkedIn.
    """
    if not raw or not raw.strip():
        raise InvalidProfileUrl("No URL was supplied.")

    candidate = raw.strip()

    # Bare vanity name -- no scheme, no dots, no slashes.
    if "/" not in candidate and "." not in candidate:
        vanity = _clean(candidate)
        if _VANITY_OK.match(vanity) and vanity.lower() not in _RESERVED:
            return vanity
        raise InvalidProfileUrl(f"'{raw}' is not a valid LinkedIn vanity name.")

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", candidate):
        candidate = "https://" + candidate

    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()

    if not _ALLOWED_HOST.search(host):
        raise InvalidProfileUrl(
            f"Host '{host or 'unknown'}' is not a LinkedIn domain.",
            details={"expected": "*.linkedin.com"},
        )

    path = parsed.path or "/"
    for pattern in _PATH_PATTERNS:
        match = pattern.match(path)
        if not match:
            continue
        if "vanity" not in (match.groupdict() or {}):
            raise InvalidProfileUrl(
                "Legacy /profile/view URLs are not supported; open the profile "
                "in a browser and copy the /in/<name> URL instead."
            )
        vanity = _clean(match.group("vanity"))
        if not vanity:
            raise InvalidProfileUrl("The URL has no profile identifier in it.")
        if vanity.lower() in _RESERVED:
            raise InvalidProfileUrl(f"'{vanity}' is a LinkedIn system page, not a profile.")
        if not _VANITY_OK.match(vanity):
            raise InvalidProfileUrl(f"'{vanity}' is not a valid LinkedIn vanity name.")
        return vanity

    raise InvalidProfileUrl(
        "That is a LinkedIn URL but not a profile URL. Profile URLs look like "
        "https://www.linkedin.com/in/<name>.",
        details={"path": path},
    )


def canonical_profile_url(public_identifier: str) -> str:
    return f"https://www.linkedin.com/in/{public_identifier}"
