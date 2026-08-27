import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.fixtures import FixtureStore  # noqa: E402

SYNTHETIC = "priya-raghavan-synthetic"


@pytest.fixture(scope="session")
def fixture_store() -> FixtureStore:
    return FixtureStore(str(ROOT / "fixtures" / "raw"))


@pytest.fixture(scope="session")
def synthetic_payload(fixture_store) -> dict:
    fx = fixture_store.get(SYNTHETIC)
    assert fx is not None, "synthetic fixture is missing from fixtures/raw/"
    return fx.payload


@pytest.fixture
def demo_settings(tmp_path) -> Settings:
    return Settings(
        # _env_file=None is load-bearing: without it Settings reads the
        # developer's real .env, and LI_AT/JSESSIONID are *additive* with
        # LINKEDIN_SESSIONS -- so a live cookie would silently join the pool and
        # the suite could make real LinkedIn requests.
        _env_file=None,
        li_at="",
        jsessionid="",
        linkedin_sessions="",
        demo_mode=True,
        cache_path=str(tmp_path / "cache.sqlite3"),
        fixtures_dir=str(ROOT / "fixtures" / "raw"),
        min_delay_seconds=0.0,
        max_delay_seconds=0.0,
    )


@pytest.fixture
def live_settings(tmp_path) -> Settings:
    """Settings with a fake session, for tests that mock the transport."""
    return Settings(
        _env_file=None,          # never inherit real credentials -- see above
        li_at="",
        jsessionid="",
        linkedin_sessions="AQEDfake_li_at_value|ajax:9999999999",
        demo_mode=False,
        cache_path=str(tmp_path / "cache.sqlite3"),
        fixtures_dir=str(tmp_path / "no-fixtures"),
        min_delay_seconds=0.0,
        max_delay_seconds=0.0,
        max_profiles_per_hour=50,
        max_profiles_per_day=200,
    )


@pytest.fixture
def profile_view_response(synthetic_payload) -> str:
    return json.dumps(synthetic_payload)
