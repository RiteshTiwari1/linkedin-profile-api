"""Parser tests, driven by the recorded fixture rather than hand-built dicts."""

from app.linkedin.normalize import resolve
from app.models import Profile
from app.parsers import dash as dash_parser
from app.parsers import profile_view as pv
from app.parsers.common import (
    humanize_proficiency,
    parse_date_range,
    parse_organization,
    parse_vector_image,
    pick,
)


def parsed(payload) -> Profile:
    return pv.parse(resolve(payload), "priya-raghavan-synthetic")


def test_identity_fields(synthetic_payload):
    p = parsed(synthetic_payload)
    assert p.first_name == "Priya"
    assert p.last_name == "Raghavan"
    assert p.full_name == "Priya Raghavan"
    assert p.public_identifier == "priya-raghavan-synthetic"
    assert p.profile_url == "https://www.linkedin.com/in/priya-raghavan-synthetic"
    assert p.urn and p.urn.startswith("urn:li:fs_profile:")
    assert p.member_id == "ACoAAASYNTH01"
    assert p.pronouns == "she/her"  # SHE_HER is humanised, not passed through


def test_headline_about_and_location(synthetic_payload):
    p = parsed(synthetic_payload)
    assert "Principal Data Engineer" in p.headline
    assert p.about.startswith("I build data platforms")
    assert "\n\n" in p.about, "paragraph breaks in About must survive"
    assert p.location.text == "Bengaluru, Karnataka, India"
    assert p.location.country == "India"
    assert p.location.country_code == "IN"
    assert p.location.postal_code == "560001"
    assert p.industry == "Computer Software"


def test_all_sections_populate(synthetic_payload):
    p = parsed(synthetic_payload)
    assert len(p.experience) == 3
    assert len(p.education) == 2
    assert len(p.skills) == 15
    assert len(p.certifications) == 2
    assert len(p.languages) == 4
    assert len(p.projects) == 1
    assert len(p.publications) == 1
    assert len(p.honors) == 1
    assert len(p.courses) == 2
    assert len(p.volunteering) == 1
    assert len(p.test_scores) == 1
    assert len(p.organizations) == 1


def test_experience_detail(synthetic_payload):
    top = parsed(synthetic_payload).experience[0]
    assert top.title == "Principal Data Engineer"
    assert top.employment_type == "Full-time"
    assert top.company.name == "Northwind Analytics"
    assert top.company.linkedin_url == "https://www.linkedin.com/company/northwind-analytics"
    assert len(top.company.logo) == 2
    assert top.dates.start.year == 2022 and top.dates.start.month == 3
    assert top.dates.start.text == "Mar 2022"
    assert top.dates.is_current is True
    assert top.dates.end is None


def test_completed_role_gets_a_duration(synthetic_payload):
    past = parsed(synthetic_payload).experience[1]
    assert past.dates.is_current is False
    assert past.dates.duration_text == "2 yrs 8 mos"


def test_certification_range_is_not_a_duration(synthetic_payload):
    """Issue -> expiry is a validity window, not '3 yrs of experience'."""
    cert = parsed(synthetic_payload).certifications[0]
    assert cert.dates.start.text == "May 2023"
    assert cert.dates.end.text == "May 2026"
    assert cert.dates.duration_text is None
    assert cert.license_number == "AWS-PSA-884213"
    assert cert.authority.name == "Amazon Web Services"


def test_year_only_education_reports_years_only(synthetic_payload):
    edu = parsed(synthetic_payload).education[0]
    assert edu.dates.start.year == 2014 and edu.dates.start.month is None
    assert edu.dates.duration_text == "2 yrs"
    assert edu.school.name == "Indian Institute of Technology, Bombay"
    assert edu.school.linkedin_url.endswith("/school/iit-bombay")
    assert edu.degree == "Master of Technology - MTech"
    assert edu.grade == "9.1/10"


def test_language_proficiency_is_humanised(synthetic_payload):
    langs = {lang.name: lang.proficiency for lang in parsed(synthetic_payload).languages}
    assert langs["English"] == "Native or bilingual proficiency"
    assert langs["German"] == "Elementary proficiency"


def test_images_are_all_sizes_sorted(synthetic_payload):
    images = parsed(synthetic_payload).images
    widths = [a.width for a in images.profile_picture]
    assert widths == [100, 200, 400, 800]
    assert all(a.url.startswith("https://media.licdn.com/") for a in images.profile_picture)
    assert images.profile_picture[0].expires_at is not None, "signed URLs carry an expiry"
    assert len(images.background) == 1


def test_derived_current_role_prefers_the_ongoing_one(synthetic_payload):
    p = parsed(synthetic_payload)
    assert p.current_title == "Principal Data Engineer"
    assert p.current_company == "Northwind Analytics"


def test_network_counts(synthetic_payload):
    p = parsed(synthetic_payload)
    assert p.connection_count == 1847
    assert p.follower_count == 3260


def test_contact_info_absent_means_null_not_empty():
    assert pv.parse_contact_info(None) is None
    assert pv.parse_contact_info({}) is None
    assert pv.parse_contact_info({"phoneNumbers": [], "websites": []}) is None


def test_contact_info_when_visible():
    info = pv.parse_contact_info(
        {
            "emailAddress": {"emailAddress": "a@b.test"},
            "phoneNumbers": [{"number": "+91 90000 00000"}],
            "websites": [{"url": "https://example.test"}],
            "twitterHandles": [{"name": "someone"}],
            "birthDateOn": {"month": 4, "day": 12},
        }
    )
    assert info.emails == ["a@b.test"]
    assert info.phone_numbers == ["+91 90000 00000"]
    assert info.websites == ["https://example.test"]
    assert info.twitter_handles == ["someone"]
    assert info.birthday.month == 4 and info.birthday.year is None


# --- resilience ------------------------------------------------------------


def test_parsers_never_raise_on_junk():
    """A malformed payload must degrade, not explode."""
    for junk in ({}, {"data": None}, {"data": {"profile": "oops"}}, {"included": [1, 2, 3]}):
        p = pv.parse(resolve(junk) or {}, "x")
        assert p.public_identifier == "x"
        assert p.experience == []
    for junk in ({}, {"included": []}, {"data": {}}):
        assert dash_parser.parse(junk, "x").public_identifier == "x"


def test_missing_fields_are_none_not_empty_string():
    p = pv.parse({"profile": {"firstName": "Solo"}}, "solo")
    assert p.first_name == "Solo"
    assert p.last_name is None
    assert p.headline is None
    assert p.about is None
    assert p.skills == []


def test_dash_field_spellings_are_handled():
    """dash uses dateRange/profilePicture where profileView uses timePeriod/picture."""
    r = parse_date_range({"dateRange": {"start": {"year": 2020, "month": 6}}})
    assert r.start.text == "Jun 2020" and r.is_current

    img = parse_vector_image(
        {
            "displayImageReference": {
                "vectorImage": {
                    "rootUrl": "https://r/",
                    "artifacts": [{"width": 200, "fileIdentifyingUrlPathSegment": "a.jpg"}],
                }
            }
        }
    )
    assert img[0].url == "https://r/a.jpg"


def test_dotted_type_keys_are_not_treated_as_paths():
    """`pick`'s "a.b" syntax must not shred `com.linkedin.common.VectorImage`."""
    node = {
        "com.linkedin.common.VectorImage": {
            "rootUrl": "https://r/",
            "artifacts": [{"width": 100, "fileIdentifyingUrlPathSegment": "x.jpg"}],
        }
    }
    assert parse_vector_image(node)[0].url == "https://r/x.jpg"


def test_organization_from_a_withheld_urn():
    org = parse_organization("urn:li:fs_miniCompany:404")
    assert org.urn == "urn:li:fs_miniCompany:404"
    assert org.name is None


def test_pick_helpers():
    assert pick({"a": {"b": "c"}}, "a.b") == "c"
    assert pick({"a": ""}, "a", "b", default="fallback") == "fallback"
    assert pick(None, "a") is None
    assert humanize_proficiency("SOMETHING_NEW") == "Something new"


# --- shapes observed on a real, well-filled dash profile -------------------
#
# Ten of the thirteen section builders were written against documented field
# names and had never seen real element data, because every profile tested
# earlier reported total 0 for them. Validating against a profile that actually
# had certifications, languages, projects, courses, honours, volunteering and
# test scores found three genuine bugs. Each is pinned below with the shape
# LinkedIn really sends.


def test_project_contributors_are_deeply_nested():
    """A contributor is not `{name: ...}`.

    dash sends `{standardizedContributor: {profile: {firstName, lastName, …}}}`,
    and that nested profile is a *complete* profile object hundreds of fields
    deep. Reading only the name keeps the response small as well as correct.
    """
    from app.parsers.sections import build_project

    project = build_project(
        {
            "title": "Some Project",
            "url": "https://example.test/p",
            "contributors": [
                {
                    "standardizedContributor": {
                        "profile": {
                            "firstName": "Harshet",
                            "lastName": "Jain",
                            "headline": "…hundreds of other fields…",
                        }
                    }
                },
                {"standardizedContributor": {"profile": {"firstName": "Solo"}}},
                {"name": "Legacy Shape"},
                "Plain String",
            ],
        }
    )
    assert project.contributors == ["Harshet Jain", "Solo", "Legacy Shape", "Plain String"]


def test_publication_authors_and_patent_inventors_use_the_same_wrapper():
    from app.parsers.sections import build_patent, build_publication

    pub = build_publication(
        {
            "name": "A Paper",
            "authors": [{"standardizedAuthor": {"profile": {"firstName": "A", "lastName": "B"}}}],
            "dateOn": {"month": 6, "year": 2023},
        }
    )
    assert pub.authors == ["A B"]
    assert pub.published_on.text == "Jun 2023"

    patent = build_patent(
        {
            "title": "A Patent",
            "inventors": [{"standardizedInventor": {"profile": {"fullName": "C D"}}}],
            "issuedOn": {"year": 2020},
        }
    )
    assert patent.inventors == ["C D"]
    assert patent.issued_on.year == 2020


def test_test_score_date_field_is_called_dateOn_in_dash():
    """The legacy payload used `date`; dash uses `dateOn`. Missing it silently
    dropped every test-score date."""
    from app.parsers.sections import build_test_score

    score = build_test_score(
        {"name": "AWS CSA Associate", "score": "720+", "dateOn": {"month": 8, "year": 2021}}
    )
    assert score.taken_on.text == "Aug 2021"
    # legacy spelling still works
    assert build_test_score({"name": "x", "date": {"year": 2019}}).taken_on.year == 2019


def test_honor_date_field_is_called_issuedOn_in_dash():
    from app.parsers.sections import build_honor

    honor = build_honor(
        {"title": "AWS Community Builder", "issuer": "AWS", "issuedOn": {"month": 5, "year": 2021}}
    )
    assert honor.issued_on.text == "May 2021"


def test_single_word_enums_are_humanised_too():
    """dash returns both "SCIENCE_AND_TECHNOLOGY" and plain "EDUCATION" for the
    same field, so requiring an underscore left half of them raw."""
    from app.parsers.common import humanize

    assert humanize("EDUCATION") == "Education"
    assert humanize("SCIENCE_AND_TECHNOLOGY") == "Science and technology"
    assert humanize("FULL_TIME") == "Full time"
    assert humanize("REMOTE") == "Remote"
    # Short all-caps values are legitimate and must survive untouched.
    assert humanize("IN") == "IN"
    assert humanize("US") == "US"


def test_certification_authority_comes_from_the_company_reference():
    """Verified live: dash carries both a plain `authority` string and a resolved
    `company`, and the company is the better source (it has the URL and logo)."""
    from app.parsers.sections import build_certification

    cert = build_certification(
        {
            "name": "Red Hat Certified System Administrator (RHCSA)",
            "authority": "Red Hat",
            "licenseNumber": "210-196-887",
            "url": "https://rhtapps.redhat.com/verify?certId=210-196-887",
            "company": {
                "name": "Red Hat",
                "entityUrn": "urn:li:fsd_company:3545",
                "universalName": "red-hat",
            },
            "dateRange": {"start": {"month": 10, "year": 2022}, "end": {"month": 10, "year": 2025}},
        }
    )
    assert cert.authority.name == "Red Hat"
    assert cert.authority.linkedin_url == "https://www.linkedin.com/company/red-hat"
    assert cert.license_number == "210-196-887"
    assert cert.dates.start.text == "Oct 2022"
    assert cert.dates.end.text == "Oct 2025"
    assert cert.dates.duration_text is None, "issue->expiry is not a tenure"
