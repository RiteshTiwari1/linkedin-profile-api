"""Parser for the dash / GraphQL payloads.

These are the modern endpoints, and they are structurally hostile: the profile
page is delivered as a *UI component tree* (cards containing
`textComponent` / `entityComponent` / `fixedListComponent` nodes), and the tree
gets reshuffled whenever LinkedIn redesigns anything.

So this parser deliberately ignores the tree. Instead it reaches into the
`included` entity pool and pulls objects out **by `$type`**, which is stable
across UI changes because those types are LinkedIn's own backend model names.
A redesign moves a card; it does not rename
`com.linkedin.voyager.dash.identity.profile.Position`.

In practice the live payload carries both: the `Profile` entity references a
`CollectionResponse` per section, and the typed entities sit in `included`. This
parser reads the references first (right order, exact paging) and falls back to
type-scanning to fill any gaps.
"""

from __future__ import annotations

from typing import Any

from ..linkedin.normalize import build_index, entities_of_type, resolve
from ..models import Images, Location, Profile
from ..url import canonical_profile_url
from .common import (
    as_int,
    compact,
    humanize_pronouns,
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

# $type suffix -> (builder, Profile attribute). Suffix match, so both the
# `voyager.dash.identity.profile.*` and `voyager.identity.profile.*` namespaces
# are covered by one entry.
_TYPED_SECTIONS: tuple[tuple[tuple[str, ...], Any, str], ...] = (
    ((".profile.Position",), build_experience, "experience"),
    ((".profile.Education",), build_education, "education"),
    ((".profile.Skill",), build_skill, "skills"),
    ((".profile.Certification",), build_certification, "certifications"),
    ((".profile.Language", ".profile.ProfileLanguage"), build_language, "languages"),
    ((".profile.Project",), build_project, "projects"),
    ((".profile.Publication",), build_publication, "publications"),
    ((".profile.Honor",), build_honor, "honors"),
    ((".profile.Course",), build_course, "courses"),
    ((".profile.VolunteerExperience",), build_volunteering, "volunteering"),
    ((".profile.Patent",), build_patent, "patents"),
    ((".profile.TestScore",), build_test_score, "test_scores"),
    ((".profile.Organization",), build_organization_membership, "organizations"),
)

# The Profile entity references one CollectionResponse per section:
#   *profileSkills, *profileEducations, *profileCertifications, ...
# Each carries `elements` plus `paging.total`, so reading sections this way gives
# both the right order *and* an exact answer to "was this truncated?" -- which
# type-scanning `included` cannot provide. Type-scanning stays as the fallback
# for payloads where the Profile entity is absent or the refs are unresolved.
_COLLECTIONS: tuple[tuple[str, Any, str], ...] = (
    ("profileEducations", build_education, "education"),
    ("profileSkills", build_skill, "skills"),
    ("profileCertifications", build_certification, "certifications"),
    ("profileLanguages", build_language, "languages"),
    ("profileProjects", build_project, "projects"),
    ("profilePublications", build_publication, "publications"),
    ("profileHonors", build_honor, "honors"),
    ("profileCourses", build_course, "courses"),
    ("profileVolunteerExperiences", build_volunteering, "volunteering"),
    ("profilePatents", build_patent, "patents"),
    ("profileTestScores", build_test_score, "test_scores"),
    ("profileOrganizations", build_organization_membership, "organizations"),
)

_ATTR_TO_BUILDER: dict[str, Any] = {attr: builder for _, builder, attr in _COLLECTIONS}

_PROFILE_TYPES = (
    "com.linkedin.voyager.dash.identity.profile.Profile",
    ".identity.profile.Profile",
    ".identity.shared.MiniProfile",
)


def parse(payload: dict[str, Any], public_identifier: str) -> Profile:
    """Map a dash / GraphQL payload onto the response schema."""
    core = _core_profile(payload)

    first = text(pick(core, "firstName"))
    last = text(pick(core, "lastName"))
    full = " ".join(p for p in (first, last) if p) or None
    urn = pick(core, "entityUrn", "objectUrn")

    resolved_id = text(pick(core, "publicIdentifier")) or public_identifier

    profile = Profile(
        public_identifier=resolved_id,
        profile_url=canonical_profile_url(resolved_id),
        urn=urn if isinstance(urn, str) else None,
        member_id=urn_id(urn),
        first_name=first,
        last_name=last,
        full_name=full,
        pronouns=humanize_pronouns(
            pick(
                core,
                "customPronoun",
                "standardizedPronoun",
                # dash wraps it in a union: {standardizedPronoun: "SHE_HER"} or
                # {customPronoun: "..."}.
                "pronounUnion.standardizedPronoun",
                "pronounUnion.customPronoun",
            )
        ),
        headline=text(pick(core, "headline", "occupation")),
        about=text(pick(core, "summary", "about")),
        location=_location(core),
        industry=text(pick(core, "industry.name", "industryName", "industryV2.name")),
        images=Images(
            profile_picture=parse_vector_image(
                pick(core, "profilePicture", "picture") or {}
            ),
            background=parse_vector_image(
                pick(core, "backgroundPicture", "backgroundImage") or {}
            ),
        ),
        is_premium=_bool(pick(core, "premium", "premiumSubscriber")),
        is_influencer=_bool(pick(core, "influencer")),
        open_to_work=_open_to_work(core),
    )

    # Preferred path: the Profile's own section collections.
    for key, builder, attribute in _COLLECTIONS:
        built = compact([builder(el) for el in _elements(core.get(key))])
        if built:
            setattr(profile, attribute, built)

    experience = _positions_from_groups(core)
    if experience:
        profile.experience = experience

    # Fallback: scan `included` by $type for anything the references did not
    # yield. Only fills gaps, so it can never overwrite better data.
    for suffixes, builder, attribute in _TYPED_SECTIONS:
        if getattr(profile, attribute, None):
            continue
        built = compact([builder(el) for el in entities_of_type(payload, *suffixes)])
        if built:
            setattr(profile, attribute, built)

    _apply_counts(profile, payload)
    _apply_derived(profile)
    return profile


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _elements(collection: Any) -> list[dict[str, Any]]:
    """The `elements` list of a resolved CollectionResponse."""
    if isinstance(collection, dict):
        elements = collection.get("elements")
        if isinstance(elements, list):
            return [e for e in elements if isinstance(e, dict)]
    if isinstance(collection, list):
        return [e for e in collection if isinstance(e, dict)]
    return []


def _positions_from_group_list(groups: list[dict[str, Any]]) -> list[Any]:
    """Flatten PositionGroups into Experience entries."""
    built: list[Any] = []
    for group in groups:
        positions = _elements(group.get("profilePositionInPositionGroup"))
        if not positions:
            # A group with no expanded positions is still one role.
            positions = [group]
        for position in positions:
            merged = dict(position)
            for inherited in ("companyName", "company", "companyUrn"):
                if not merged.get(inherited) and group.get(inherited):
                    merged[inherited] = group[inherited]
            entry = build_experience(merged)
            if entry:
                built.append(entry)
    return compact(built)


def parse_collection(payload: dict[str, Any], attribute: str) -> list[Any]:
    """Parse a standalone per-section collection response into schema objects.

    Same builders as the embedded path, so a paged-in skill is indistinguishable
    from one that arrived with the profile.
    """
    resolved = resolve(payload)
    elements = _elements(resolved if isinstance(resolved, dict) else None)
    if not elements:
        elements = _elements((resolved or {}).get("elements"))
    if attribute == "experience":
        return _positions_from_group_list(elements)
    builder = _ATTR_TO_BUILDER.get(attribute)
    if builder is None:
        return []
    return compact([builder(el) for el in elements])


def collection_total(payload: dict[str, Any]) -> int | None:
    """`paging.total` from a standalone collection response."""
    resolved = resolve(payload)
    paging = (resolved or {}).get("paging") if isinstance(resolved, dict) else None
    total = paging.get("total") if isinstance(paging, dict) else None
    return total if isinstance(total, int) else None


def _positions_from_groups(core: dict[str, Any]) -> list[Any]:
    """Experience, read through PositionGroup.

    dash groups roles by company: a PositionGroup carries the company and date
    span, and the individual Positions hang off it under
    `profilePositionInPositionGroup`. Flattening the groups preserves LinkedIn's
    own ordering, and the group supplies the company name when a Position omits
    it (which happens for promotions within one employer).
    """
    return _positions_from_group_list(_elements(core.get("profilePositionGroups")))


def collection_paging(payload: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """Per-section (retrieved, total) from LinkedIn's own paging metadata.

    Lets the service report truncation exactly instead of guessing. A section
    with total 0 is genuinely empty -- not missing, not truncated.
    """
    core = _core_profile(payload)
    out: dict[str, tuple[int, int]] = {}
    pairs = list(_COLLECTIONS) + [("profilePositionGroups", None, "experience")]
    for key, _, attribute in pairs:
        collection = core.get(key)
        if not isinstance(collection, dict):
            continue
        paging = collection.get("paging")
        total = paging.get("total") if isinstance(paging, dict) else None
        if isinstance(total, int):
            out[attribute] = (len(_elements(collection)), total)
    return out


def _core_profile(payload: dict[str, Any]) -> dict[str, Any]:
    candidates = entities_of_type(payload, *_PROFILE_TYPES)
    if candidates:
        # The richest one: a MiniProfile stub can shadow the real Profile.
        return max(candidates, key=lambda c: len(c))
    data = payload.get("data")
    if isinstance(data, dict):
        index = build_index(payload)
        for value in data.values():
            if isinstance(value, str) and value in index:
                return index[value]
        return data
    return {}


def _location(core: dict[str, Any]) -> Location:
    geo = pick(core, "geoLocation.geo", "geo", default={}) or {}
    return Location(
        text=text(
            pick(
                core,
                "geoLocationName",
                "locationName",
                "geoLocation.geo.defaultLocalizedName",
                "location.defaultLocalizedName",
            )
        )
        or text(pick(geo, "defaultLocalizedName")),
        country=text(pick(geo, "country.defaultLocalizedName", "countryName")),
        country_code=(text(pick(core, "location.countryCode", "countryCode")) or "").upper()
        or None,
        postal_code=text(pick(core, "location.postalCode", "postalCode")),
    )


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _open_to_work(core: dict[str, Any]) -> bool | None:
    """The #OpenToWork photo frame is the only reliable public signal."""
    frame = pick(core, "profilePicture.frameType", "memberRelationship.frameType")
    if isinstance(frame, str):
        return "OPEN_TO_WORK" in frame.upper()
    return None


def _apply_counts(profile: Profile, payload: dict[str, Any]) -> None:
    for entity in entities_of_type(
        payload, ".ProfileNetworkInfo", ".FollowingState", ".MemberRelationship"
    ):
        connections = as_int(pick(entity, "connectionsCount", "connectionCount"))
        followers = as_int(pick(entity, "followerCount", "followersCount"))
        if connections is not None:
            profile.connection_count = connections
        if followers is not None:
            profile.follower_count = followers


def _apply_derived(profile: Profile) -> None:
    if not profile.experience:
        return
    ongoing = next((e for e in profile.experience if e.dates and e.dates.is_current), None)
    chosen = ongoing or profile.experience[0]
    profile.current_title = chosen.title
    profile.current_company = chosen.company.name if chosen.company else None

