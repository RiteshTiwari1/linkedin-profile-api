"""Parser for the legacy `profileView` payload.

profileView is the nicest thing LinkedIn still serves: after URN resolution it
is a plain domain model, with every section under a `<name>View.elements` list.
No UI component tree, no persisted-query hash. When this endpoint answers, one
request produces a complete profile, which is why it is strategy #1.

    {
      "profile": {firstName, lastName, headline, summary, locationName,
                  industryName, miniProfile: {publicIdentifier, picture, ...}},
      "positionView":      {"elements": [...]},
      "educationView":     {"elements": [...]},
      "skillView":         {"elements": [...]},
      "certificationView": {"elements": [...]},
      ...
    }
"""

from __future__ import annotations

from typing import Any

from ..models import ContactInfo, Images, Location, Profile
from ..url import canonical_profile_url
from .common import (
    as_int,
    compact,
    humanize_pronouns,
    parse_date,
    parse_vector_image,
    pick,
    text,
    urn_id,
)
from .sections import (
    build_certification,
    build_course,
    build_education,
    build_experience,
    build_honor,
    build_language,
    build_organization_membership,
    build_patent,
    build_project,
    build_publication,
    build_skill,
    build_test_score,
    build_volunteering,
)

# profileView key -> (builder, Profile attribute)
_VIEWS: tuple[tuple[str, Any, str], ...] = (
    ("positionView", build_experience, "experience"),
    ("educationView", build_education, "education"),
    ("skillView", build_skill, "skills"),
    ("certificationView", build_certification, "certifications"),
    ("languageView", build_language, "languages"),
    ("projectView", build_project, "projects"),
    ("publicationView", build_publication, "publications"),
    ("honorView", build_honor, "honors"),
    ("courseView", build_course, "courses"),
    ("volunteerExperienceView", build_volunteering, "volunteering"),
    ("patentView", build_patent, "patents"),
    ("testScoreView", build_test_score, "test_scores"),
    ("organizationView", build_organization_membership, "organizations"),
)


def parse(resolved: dict[str, Any], public_identifier: str) -> Profile:
    """Map a resolved profileView tree onto the response schema."""
    core = resolved.get("profile") if isinstance(resolved.get("profile"), dict) else {}
    mini = core.get("miniProfile") if isinstance(core.get("miniProfile"), dict) else {}

    first = text(pick(core, "firstName") or pick(mini, "firstName"))
    last = text(pick(core, "lastName") or pick(mini, "lastName"))
    full = " ".join(p for p in (first, last) if p) or None

    urn = pick(core, "entityUrn") or pick(mini, "entityUrn")

    profile = Profile(
        public_identifier=text(pick(mini, "publicIdentifier", "publicIdentifier"))
        or public_identifier,
        profile_url=canonical_profile_url(
            text(pick(mini, "publicIdentifier")) or public_identifier
        ),
        urn=urn if isinstance(urn, str) else None,
        member_id=urn_id(urn),
        first_name=first,
        last_name=last,
        full_name=full,
        pronouns=_pronouns(core),
        headline=text(pick(core, "headline") or pick(mini, "occupation")),
        about=text(pick(core, "summary")),
        location=_location(core),
        industry=text(pick(core, "industryName", "industry.name")),
        images=Images(
            profile_picture=parse_vector_image(pick(mini, "picture") or pick(core, "picture")),
            background=parse_vector_image(
                pick(mini, "backgroundImage") or pick(core, "backgroundPicture", "backgroundImage")
            ),
        ),
        is_influencer=_bool(pick(mini, "influencer")),
        is_premium=_bool(pick(mini, "premium")),
    )

    for view_key, builder, attribute in _VIEWS:
        elements = _elements(resolved.get(view_key))
        if not elements:
            continue
        built = compact([builder(el) for el in elements])
        if built:
            setattr(profile, attribute, built)

    _apply_network_info(profile, resolved)
    _apply_derived(profile)
    return profile


def _elements(view: Any) -> list[dict[str, Any]]:
    if isinstance(view, dict):
        elements = view.get("elements")
        if isinstance(elements, list):
            return [e for e in elements if isinstance(e, dict)]
    if isinstance(view, list):
        return [e for e in view if isinstance(e, dict)]
    return []


def _location(core: dict[str, Any]) -> Location:
    basic = pick(core, "location.basicLocation", default={}) or {}
    return Location(
        text=text(pick(core, "geoLocationName", "locationName", "location.defaultLocalizedName")),
        country=text(pick(core, "geoCountryName", "location.countryName")),
        country_code=(text(pick(basic, "countryCode")) or "").upper() or None,
        postal_code=text(pick(basic, "postalCode")),
    )


def _pronouns(core: dict[str, Any]) -> str | None:
    # LinkedIn stores either a standard enum or a custom string.
    return humanize_pronouns(pick(core, "customPronoun", "standardizedPronoun", "pronoun"))


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _apply_network_info(profile: Profile, resolved: dict[str, Any]) -> None:
    """Connection / follower counts, when the payload happens to carry them."""
    info = resolved.get("profileNetworkInfo")
    if not isinstance(info, dict):
        return
    profile.connection_count = as_int(pick(info, "connectionsCount", "connectionCount"))
    profile.follower_count = as_int(pick(info, "followersCount", "followerCount"))


def _apply_derived(profile: Profile) -> None:
    """current_title / current_company are conveniences, not new data.

    Prefer an explicitly ongoing role; fall back to the first listed one, which
    is how LinkedIn itself orders the section.
    """
    if not profile.experience:
        return
    ongoing = next((e for e in profile.experience if e.dates and e.dates.is_current), None)
    chosen = ongoing or profile.experience[0]
    profile.current_title = chosen.title
    profile.current_company = chosen.company.name if chosen.company else None


def parse_contact_info(resolved: dict[str, Any] | None) -> ContactInfo | None:
    """profileContactInfo. Returns real data only for 1st-degree connections."""
    if not isinstance(resolved, dict):
        return None
    node = resolved.get("data") if isinstance(resolved.get("data"), dict) else resolved

    emails = [e for e in [text(pick(node, "emailAddress.emailAddress", "emailAddress"))] if e]
    phones = []
    for phone in pick(node, "phoneNumbers", default=[]) or []:
        number = (
            text(pick(phone, "number", "phoneNumber"))
            if isinstance(phone, dict)
            else text(phone)
        )
        if number:
            phones.append(number)
    websites = []
    for site in pick(node, "websites", default=[]) or []:
        url = text(pick(site, "url")) if isinstance(site, dict) else text(site)
        if url:
            websites.append(url)
    twitter = []
    for handle in pick(node, "twitterHandles", default=[]) or []:
        name = text(pick(handle, "name")) if isinstance(handle, dict) else text(handle)
        if name:
            twitter.append(name)

    info = ContactInfo(
        emails=emails,
        phone_numbers=phones,
        websites=websites,
        twitter_handles=twitter,
        birthday=parse_date(node.get("birthDateOn") or node.get("birthDate")),
        address=text(node.get("address")),
    )
    # All-empty means "not visible to us"; say null rather than imply emptiness.
    if not any(
        (info.emails, info.phone_numbers, info.websites, info.twitter_handles,
         info.birthday, info.address)
    ):
        return None
    return info
