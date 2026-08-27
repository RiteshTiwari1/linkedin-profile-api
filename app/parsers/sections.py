"""Section builders: one raw LinkedIn element -> one schema object.

Each builder accepts the field spellings from *both* API generations, so the
same function serves the legacy profileView payload and the newer dash /
GraphQL payloads. That is why the key lists look redundant -- they are the
observed aliases, not guesses.
"""

from __future__ import annotations

from typing import Any

from ..models import (
    Certification,
    Course,
    Education,
    Experience,
    Honor,
    Language,
    OrganizationMembership,
    Patent,
    Project,
    Publication,
    Skill,
    TestScore,
    Volunteering,
)
from .common import (
    as_int,
    humanize,
    humanize_proficiency,
    parse_date,
    parse_date_range,
    parse_organization,
    pick,
    text,
)


def _person_names(people: Any) -> list[str]:
    """Names from a list of people, whatever wrapper LinkedIn used.

    Observed live on projects: a contributor is not `{name: ...}` but
    `{standardizedContributor: {profile: {firstName, lastName, ...}}}` -- and the
    nested profile is a *complete* profile object, hundreds of fields deep.
    Reading only the name keeps the response small as well as correct. Authors
    and inventors use the same shape.
    """
    names: list[str] = []
    for person in people or []:
        if isinstance(person, str):
            name = text(person)
            if name:
                names.append(name)
            continue
        if not isinstance(person, dict):
            continue
        # Unwrap whichever nesting is in play.
        node = person
        for wrapper in (
            "standardizedContributor",
            "standardizedAuthor",
            "standardizedInventor",
            "profile",
        ):
            inner = node.get(wrapper) if isinstance(node, dict) else None
            if isinstance(inner, dict):
                node = inner
        name = text(pick(node, "name", "fullName"))
        if not name:
            first = text(pick(node, "firstName"))
            last = text(pick(node, "lastName"))
            name = " ".join(part for part in (first, last) if part) or None
        if name:
            names.append(name)
    return names


def build_experience(el: dict[str, Any]) -> Experience | None:
    if not isinstance(el, dict):
        return None
    company = parse_organization(
        pick(el, "company", "companyResolutionResult", "miniCompany"),
        fallback_name=pick(el, "companyName", "subtitle"),
    )
    skills = [
        s for s in (text(x) for x in (pick(el, "skills", default=[]) or []))
        if s
    ]
    return Experience(
        title=text(pick(el, "title", "name", "positionTitle")),
        employment_type=humanize(
            pick(el, "employmentType", "employmentTypeName", "employmentStatus")
        ),
        company=company,
        location=text(pick(el, "locationName", "location", "geoLocationName")),
        workplace_type=humanize(pick(el, "workplaceType", "workplaceTypeName")),
        description=text(pick(el, "description", "descriptionText")),
        dates=parse_date_range(el),
        skills=skills,
    )


def build_education(el: dict[str, Any]) -> Education | None:
    if not isinstance(el, dict):
        return None
    school = parse_organization(
        pick(el, "school", "schoolResolutionResult", "miniSchool"),
        fallback_name=pick(el, "schoolName", "title"),
    )
    return Education(
        school=school,
        degree=text(pick(el, "degreeName", "degree", "degreeNameText")),
        field_of_study=text(pick(el, "fieldOfStudy", "fieldOfStudyName")),
        grade=text(el.get("grade")),
        activities=text(pick(el, "activities", "activitiesAndSocieties")),
        description=text(el.get("description")),
        dates=parse_date_range(el),
    )


def build_skill(el: dict[str, Any]) -> Skill | None:
    if not isinstance(el, dict):
        return None
    name = text(pick(el, "name", "skillName", "title"))
    if not name:
        return None
    endorsements = as_int(
        pick(el, "endorsementCount", "numEndorsements", "endorsedCount", "insightText")
    )
    return Skill(
        name=name,
        endorsement_count=endorsements,
        endorsed_by_connections=(
            el.get("endorsedByViewer")
            if isinstance(el.get("endorsedByViewer"), bool)
            else None
        ),
    )


def build_certification(el: dict[str, Any]) -> Certification | None:
    if not isinstance(el, dict):
        return None
    authority = parse_organization(
        pick(el, "company", "companyResolutionResult", "authorityResolutionResult"),
        fallback_name=pick(el, "authority", "issuer", "subtitle"),
    )
    return Certification(
        name=text(pick(el, "name", "title")),
        authority=authority,
        license_number=text(pick(el, "licenseNumber", "credentialId")),
        url=text(pick(el, "url", "credentialUrl")),
        # Issue -> expiry, so no derived duration.
        dates=parse_date_range(el, duration=False),
    )


def build_language(el: dict[str, Any]) -> Language | None:
    if not isinstance(el, dict):
        return None
    name = text(pick(el, "name", "languageName", "title"))
    if not name:
        return None
    return Language(
        name=name,
        proficiency=humanize_proficiency(
            pick(el, "proficiency", "proficiencyName", "caption", "subtitle")
        ),
    )


def build_project(el: dict[str, Any]) -> Project | None:
    if not isinstance(el, dict):
        return None
    return Project(
        name=text(pick(el, "title", "name")),
        description=text(el.get("description")),
        url=text(el.get("url")),
        dates=parse_date_range(el),
        contributors=_person_names(pick(el, "contributors", "members", default=[])),
    )


def build_publication(el: dict[str, Any]) -> Publication | None:
    if not isinstance(el, dict):
        return None
    return Publication(
        name=text(pick(el, "name", "title")),
        publisher=text(pick(el, "publisher", "publisherName")),
        description=text(el.get("description")),
        url=text(el.get("url")),
        published_on=parse_date(pick(el, "dateOn", "date", "publishedOn", "publishedDate")),
        authors=_person_names(pick(el, "authors", default=[])),
    )


def build_honor(el: dict[str, Any]) -> Honor | None:
    if not isinstance(el, dict):
        return None
    return Honor(
        title=text(pick(el, "title", "name")),
        issuer=text(pick(el, "issuer", "issuerName", "subtitle")),
        description=text(el.get("description")),
        issued_on=parse_date(pick(el, "issuedOn", "issueDate")),
    )


def build_course(el: dict[str, Any]) -> Course | None:
    if not isinstance(el, dict):
        return None
    name = text(pick(el, "name", "title"))
    if not name:
        return None
    return Course(name=name, number=text(pick(el, "number", "courseNumber")))


def build_volunteering(el: dict[str, Any]) -> Volunteering | None:
    if not isinstance(el, dict):
        return None
    org = parse_organization(
        pick(el, "company", "companyResolutionResult", "organizationResolutionResult"),
        fallback_name=pick(el, "companyName", "organizationName", "subtitle"),
    )
    return Volunteering(
        role=text(pick(el, "role", "title")),
        organization=org,
        cause=humanize(pick(el, "cause", "causeName")),
        description=text(el.get("description")),
        dates=parse_date_range(el),
    )


def build_patent(el: dict[str, Any]) -> Patent | None:
    if not isinstance(el, dict):
        return None
    return Patent(
        title=text(pick(el, "title", "name")),
        number=text(pick(el, "number", "applicationNumber", "patentNumber")),
        office=text(pick(el, "office", "issuer")),
        description=text(el.get("description")),
        url=text(el.get("url")),
        issued_on=parse_date(pick(el, "issuedOn", "issueDate", "filingDate", "dateOn")),
        inventors=_person_names(pick(el, "inventors", default=[])),
    )


def build_test_score(el: dict[str, Any]) -> TestScore | None:
    if not isinstance(el, dict):
        return None
    return TestScore(
        name=text(pick(el, "name", "title")),
        score=text(pick(el, "score", "scoreText")),
        description=text(el.get("description")),
        # dash names this `dateOn`; the legacy payload used `date`.
        taken_on=parse_date(pick(el, "dateOn", "date", "takenOn")),
    )


def build_organization_membership(el: dict[str, Any]) -> OrganizationMembership | None:
    if not isinstance(el, dict):
        return None
    return OrganizationMembership(
        name=text(pick(el, "name", "title", "organizationName")),
        position=text(pick(el, "position", "positionHeld", "subtitle")),
        description=text(el.get("description")),
        dates=parse_date_range(el),
    )


# Section name -> (builder, Profile attribute). Single source of truth so the
# two parsers and the section-level GraphQL fetches all agree.
SECTION_BUILDERS: dict[str, tuple[Any, str]] = {
    "experience": (build_experience, "experience"),
    "education": (build_education, "education"),
    "skills": (build_skill, "skills"),
    "certifications": (build_certification, "certifications"),
    "languages": (build_language, "languages"),
    "projects": (build_project, "projects"),
    "publications": (build_publication, "publications"),
    "honors": (build_honor, "honors"),
    "courses": (build_course, "courses"),
    "volunteering_experience": (build_volunteering, "volunteering"),
    "patents": (build_patent, "patents"),
    "test_scores": (build_test_score, "test_scores"),
    "organizations": (build_organization_membership, "organizations"),
}
