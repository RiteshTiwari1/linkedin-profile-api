"""The public response schema.

Design principles, since the brief leaves the schema to me:

1. `null` means "LinkedIn did not give us this", never "empty". Absent list
   sections come back as `[]` so a caller can iterate without guarding.
2. Dates are structured *and* raw. LinkedIn only ever gives month precision,
   and often only a year, so a plain "Jan 2019 - Present" string would force
   every consumer to write a parser. `{year, month}` plus the original text
   covers both machine and display use.
3. Every entity that has a LinkedIn page carries `linkedin_url` and `urn`, so
   results are joinable against other LinkedIn data.
4. Images are lists of sizes, not one URL, because that is what LinkedIn
   actually returns and callers need different sizes.
5. The envelope always reports provenance (`meta`) -- live vs cache, which
   strategy worked, what failed. Silence about degradation is a lie.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


class DateParts(_Base):
    """LinkedIn dates are partial by design. Keep the parts and the original."""

    year: int | None = None
    month: int | None = None
    day: int | None = None
    text: str | None = Field(default=None, description="As displayed, e.g. 'Jan 2019'.")


class DateRange(_Base):
    start: DateParts | None = None
    end: DateParts | None = None
    is_current: bool = False
    duration_text: str | None = Field(
        default=None, description="LinkedIn's own phrasing, e.g. '3 yrs 2 mos'."
    )


class ImageAsset(_Base):
    url: str
    width: int | None = None
    height: int | None = None
    expires_at: datetime | None = Field(
        default=None,
        description="LinkedIn media URLs are signed and expire; re-host if you need permanence.",
    )


class Organization(_Base):
    """A company, school, or issuing authority."""

    name: str | None = None
    urn: str | None = None
    linkedin_url: str | None = None
    logo: list[ImageAsset] = Field(default_factory=list)


class Location(_Base):
    text: str | None = Field(default=None, description="As displayed on the profile.")
    city: str | None = None
    country: str | None = None
    country_code: str | None = None
    postal_code: str | None = None


# ---------------------------------------------------------------------------
# Profile sections
# ---------------------------------------------------------------------------


class Experience(_Base):
    title: str | None = None
    employment_type: str | None = Field(
        default=None, description="Full-time, Contract, Internship, ..."
    )
    company: Organization | None = None
    location: str | None = None
    workplace_type: str | None = Field(default=None, description="Remote / Hybrid / On-site")
    description: str | None = None
    dates: DateRange = Field(default_factory=DateRange)
    skills: list[str] = Field(default_factory=list)


class Education(_Base):
    school: Organization | None = None
    degree: str | None = None
    field_of_study: str | None = None
    grade: str | None = None
    activities: str | None = None
    description: str | None = None
    dates: DateRange = Field(default_factory=DateRange)


class Skill(_Base):
    name: str
    endorsement_count: int | None = None
    endorsed_by_connections: bool | None = None


class Certification(_Base):
    name: str | None = None
    authority: Organization | None = None
    license_number: str | None = None
    url: str | None = None
    dates: DateRange = Field(default_factory=DateRange)


class Language(_Base):
    name: str
    proficiency: str | None = Field(
        default=None, description="LinkedIn's label, e.g. 'Native or bilingual proficiency'."
    )


class Project(_Base):
    name: str | None = None
    description: str | None = None
    url: str | None = None
    dates: DateRange = Field(default_factory=DateRange)
    contributors: list[str] = Field(default_factory=list)


class Publication(_Base):
    name: str | None = None
    publisher: str | None = None
    description: str | None = None
    url: str | None = None
    published_on: DateParts | None = None
    authors: list[str] = Field(default_factory=list)


class Honor(_Base):
    title: str | None = None
    issuer: str | None = None
    description: str | None = None
    issued_on: DateParts | None = None


class Course(_Base):
    name: str | None = None
    number: str | None = None


class Volunteering(_Base):
    role: str | None = None
    organization: Organization | None = None
    cause: str | None = None
    description: str | None = None
    dates: DateRange = Field(default_factory=DateRange)


class Patent(_Base):
    title: str | None = None
    number: str | None = None
    office: str | None = None
    description: str | None = None
    url: str | None = None
    issued_on: DateParts | None = None
    inventors: list[str] = Field(default_factory=list)


class TestScore(_Base):
    name: str | None = None
    score: str | None = None
    description: str | None = None
    taken_on: DateParts | None = None


class OrganizationMembership(_Base):
    name: str | None = None
    position: str | None = None
    description: str | None = None
    dates: DateRange = Field(default_factory=DateRange)


class ContactInfo(_Base):
    """Only ever populated for 1st-degree connections; null otherwise."""

    emails: list[str] = Field(default_factory=list)
    phone_numbers: list[str] = Field(default_factory=list)
    websites: list[str] = Field(default_factory=list)
    twitter_handles: list[str] = Field(default_factory=list)
    birthday: DateParts | None = None
    address: str | None = None


class Images(_Base):
    profile_picture: list[ImageAsset] = Field(default_factory=list)
    background: list[ImageAsset] = Field(default_factory=list)


class Profile(_Base):
    # Identity
    public_identifier: str
    profile_url: str
    urn: str | None = None
    member_id: str | None = None

    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    pronouns: str | None = None
    headline: str | None = None
    about: str | None = Field(default=None, description="The 'About' / summary section.")

    location: Location = Field(default_factory=Location)
    industry: str | None = None

    # Denormalised conveniences -- derived from experience[0], not separate data.
    current_title: str | None = None
    current_company: str | None = None

    # Social proof
    connection_count: int | None = None
    follower_count: int | None = None
    open_to_work: bool | None = None
    hiring: bool | None = None
    is_premium: bool | None = None
    is_influencer: bool | None = None

    images: Images = Field(default_factory=Images)

    # Sections
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    publications: list[Publication] = Field(default_factory=list)
    honors: list[Honor] = Field(default_factory=list)
    courses: list[Course] = Field(default_factory=list)
    volunteering: list[Volunteering] = Field(default_factory=list)
    patents: list[Patent] = Field(default_factory=list)
    test_scores: list[TestScore] = Field(default_factory=list)
    organizations: list[OrganizationMembership] = Field(default_factory=list)

    contact_info: ContactInfo | None = None


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

Source = Literal["live", "cache", "stale-cache", "fixture"]
Strategy = Literal["profile_view", "dash", "graphql", "fixture"]


class Meta(_Base):
    source: Source
    strategy: Strategy | None = None
    fetched_at: datetime
    age_seconds: int = 0
    stale: bool = False
    partial: bool = Field(
        default=False, description="True when one or more sections could not be retrieved."
    )
    sections_failed: list[str] = Field(default_factory=list)
    sections_truncated: list[str] = Field(
        default_factory=list,
        description="Populated at depth=shallow: LinkedIn's top cards cut long lists off.",
    )
    upstream_requests: int = 0
    duration_ms: int = 0


class ProfileResponse(_Base):
    status: Literal["ok"] = "ok"
    meta: Meta
    profile: Profile

