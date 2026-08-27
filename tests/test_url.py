"""URL normalisation is the API's only input, so it gets the widest tests."""

import pytest

from app.errors import InvalidProfileUrl
from app.url import canonical_profile_url, extract_public_identifier


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.linkedin.com/in/satyanadella", "satyanadella"),
        ("https://www.linkedin.com/in/satyanadella/", "satyanadella"),
        ("http://linkedin.com/in/satyanadella", "satyanadella"),
        ("linkedin.com/in/satyanadella", "satyanadella"),
        ("www.linkedin.com/in/satyanadella", "satyanadella"),
        # Regional subdomains -- LinkedIn's own share links use these.
        ("https://in.linkedin.com/in/some-person", "some-person"),
        ("https://uk.linkedin.com/in/some-person", "some-person"),
        # Tracking parameters and fragments.
        ("https://www.linkedin.com/in/x-y-123?originalSubdomain=in", "x-y-123"),
        ("https://www.linkedin.com/in/x-y-123?trk=public_profile&foo=bar", "x-y-123"),
        ("https://www.linkedin.com/in/x-y-123/#experience", "x-y-123"),
        # Mobile-lite and legacy /pub/ forms.
        ("https://www.linkedin.com/mwlite/in/some-person", "some-person"),
        ("https://www.linkedin.com/pub/some-person/1/2a/3b4", "some-person"),
        # Percent-encoded non-ASCII vanity names.
        ("https://www.linkedin.com/in/%C3%A9lodie-martin", "élodie-martin"),
        # Case is preserved in the identifier but not required in the host.
        ("HTTPS://WWW.LINKEDIN.COM/IN/Foo-Bar", "Foo-Bar"),
        # A bare vanity name is accepted as a convenience.
        ("satyanadella", "satyanadella"),
        ("  satyanadella  ", "satyanadella"),
    ],
)
def test_accepts_every_real_world_form(raw, expected):
    assert extract_public_identifier(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://example.com/in/foo",
        "https://linkedin.com.evil.test/in/foo",   # suffix-confusion attempt
        "https://www.linkedin.com/feed/",
        "https://www.linkedin.com/company/microsoft",
        "https://www.linkedin.com/school/mit",
        "https://www.linkedin.com/in/",
        "https://www.linkedin.com/jobs/view/123",
        "feed",                                    # reserved word as bare vanity
    ],
)
def test_rejects_non_profiles(raw):
    with pytest.raises(InvalidProfileUrl):
        extract_public_identifier(raw)


def test_legacy_profile_view_urls_explain_themselves():
    with pytest.raises(InvalidProfileUrl) as exc:
        extract_public_identifier("https://www.linkedin.com/profile/view?id=12345")
    assert "copy the /in/<name> URL" in exc.value.message


def test_canonical_url_round_trips():
    url = canonical_profile_url("some-person")
    assert url == "https://www.linkedin.com/in/some-person"
    assert extract_public_identifier(url) == "some-person"
