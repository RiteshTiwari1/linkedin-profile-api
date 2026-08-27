"""The cookie-header extractor.

Copying a several-hundred-character header by hand is the step most likely to go
wrong, and a truncated paste fails looking like a bad cookie rather than a bad
copy. So the extractor accepts every form DevTools and terminals produce.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cookie_from_curl import extract, write_env  # noqa: E402

COOKIE = 'bcookie="v=2&abc"; JSESSIONID="ajax:111"; li_at=AQEDtoken; lidc="b=OB01"'


def test_single_quoted_h_header():
    assert extract(f"curl 'https://x' -H 'cookie: {COOKIE}' --compressed") == COOKIE


def test_double_quoted_capitalised_header():
    """Windows "Copy as cURL (cmd)" uses double quotes, 'Cookie', and escapes
    the quotes inside the value -- which is why the scanner honours backslashes.
    """
    escaped = COOKIE.replace('"', '\\"')
    assert extract(f'curl "https://x" -H "Cookie: {escaped}"') == COOKIE


def test_quotes_inside_cookie_values_are_not_treated_as_terminators():
    """bcookie="v=2&..." and lidc="b=OB01" carry their own quotes.

    A pattern that stops at the first quote silently truncates the header after
    `bcookie=`, and the resulting failure looks like an invalid cookie rather
    than a parsing bug.
    """
    got = extract(f"curl 'https://x' -H 'cookie: {COOKIE}'")
    assert got == COOKIE
    assert 'bcookie="v=2&abc"' in got
    assert 'lidc="b=OB01"' in got
    assert got.count(";") == 3, "every cookie must survive"


def test_long_form_header_flag():
    assert extract(f"curl --header 'cookie: {COOKIE}'") == COOKIE


def test_b_flag_with_and_without_prefix():
    assert extract(f"curl -b 'cookie: {COOKIE}'") == COOKIE
    assert extract(f"curl -b '{COOKIE}'") == COOKIE


def test_bare_header_value_pasted_alone():
    assert extract(COOKIE) == COOKIE
    assert extract(f"cookie: {COOKIE}") == COOKIE


def test_multiline_curl_with_backslashes():
    curl = f"""curl 'https://www.linkedin.com/feed/' \\
  -H 'accept: text/html' \\
  -H 'cookie: {COOKIE}' \\
  -H 'user-agent: Mozilla/5.0'"""
    assert extract(curl) == COOKIE


def test_rejects_input_with_no_cookies():
    assert extract("curl 'https://x' -H 'accept: */*'") is None
    assert extract("") is None
    assert extract("just some prose") is None


def test_picks_the_cookie_header_not_another_header():
    curl = (
        "curl 'https://x' -H 'x-li-track: {\"clientVersion\":\"1.2\"}' "
        f"-H 'cookie: {COOKIE}' -H 'referer: https://www.linkedin.com/'"
    )
    assert extract(curl) == COOKIE


def test_write_env_replaces_rather_than_appends(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LINKEDIN_COOKIE=old-value\nLOG_LEVEL=INFO\n")
    write_env(COOKIE, env)
    lines = env.read_text().splitlines()
    assert lines.count(f"LINKEDIN_COOKIE={COOKIE}") == 1
    assert "LINKEDIN_COOKIE=old-value" not in lines
    assert "LOG_LEVEL=INFO" in lines, "other settings must survive"


def test_write_env_creates_file_with_tight_permissions(tmp_path):
    env = tmp_path / ".env"
    write_env(COOKIE, env)
    assert env.read_text().strip() == f"LINKEDIN_COOKIE={COOKIE}"
    assert oct(env.stat().st_mode)[-3:] == "600", "a secret file must not be world-readable"


def test_extracted_cookie_survives_the_real_parser():
    """End to end: extractor output must feed Settings correctly."""
    from app.config import Settings

    s = Settings(_env_file=None, linkedin_cookie=extract(f"curl -H 'cookie: {COOKIE}'"),
                 li_at="", jsessionid="", linkedin_sessions="")
    assert s.sessions == [("AQEDtoken", "ajax:111")]
    assert s.device_cookies["bcookie"] == '"v=2&abc"'
