"""HTTP surface tests through FastAPI's TestClient, in demo mode."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]

    def settings_override() -> Settings:
        return Settings(
            _env_file=None,      # never inherit the developer's real .env
            li_at="",
            jsessionid="",
            linkedin_sessions="",
            demo_mode=True,
            cache_path=str(tmp_path / "cache.sqlite3"),
            fixtures_dir=str(root / "fixtures" / "raw"),
            require_api_key=False,
        )

    get_settings.cache_clear()
    monkeypatch.setattr("app.main.get_settings", settings_override)
    app.dependency_overrides[get_settings] = settings_override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["fixtures"] >= 1


def test_root_redirects_to_docs(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/docs"


def test_openapi_documents_the_schema(client):
    spec = client.get("/openapi.json").json()
    assert "/v1/profile" in spec["paths"]
    profile = spec["components"]["schemas"]["Profile"]["properties"]
    for field in ("headline", "about", "experience", "education", "skills",
                  "certifications", "languages", "images"):
        assert field in profile, f"{field} must be documented"


def test_profile_happy_path(client):
    r = client.get("/v1/profile", params={"url": "https://www.linkedin.com/in/priya-raghavan-synthetic"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["meta"]["source"] == "fixture"
    p = body["profile"]
    assert p["full_name"] == "Priya Raghavan"
    assert p["current_company"] == "Northwind Analytics"
    assert len(p["skills"]) == 15
    assert p["images"]["profile_picture"][0]["url"].startswith("https://")


def test_nulls_are_present_not_omitted(client):
    """A stable key set matters more than a small payload."""
    p = client.get("/v1/profile", params={"url": "priya-raghavan-synthetic"}).json()["profile"]
    assert "contact_info" in p and p["contact_info"] is None
    assert "open_to_work" in p


@pytest.mark.parametrize(
    ("url", "status", "code"),
    [
        ("https://example.com/in/x", 400, "INVALID_URL"),
        ("https://www.linkedin.com/company/microsoft", 400, "INVALID_URL"),
        ("", 422, None),  # FastAPI validation -- url is required
        ("https://www.linkedin.com/in/not-recorded", 404, "FIXTURE_MISSING"),
    ],
)
def test_error_responses_carry_a_code(client, url, status, code):
    r = client.get("/v1/profile", params={"url": url} if url else {})
    assert r.status_code == status
    if code:
        assert r.json()["error"]["code"] == code
        assert r.json()["status"] == "error"


def test_batch(client):
    r = client.post(
        "/v1/profiles",
        json={"urls": ["priya-raghavan-synthetic", "https://example.com/in/x"]},
    )
    body = r.json()
    assert body["requested"] == 2
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    assert body["results"][1]["error"]["code"] == "INVALID_URL"


def test_batch_size_is_capped(client):
    r = client.post("/v1/profiles", json={"urls": ["x"] * 26})
    assert r.status_code == 422


def test_status_redacts_credentials(client):
    body = client.get("/v1/status").json()
    assert "sessions" in body and "rate_limit" in body and "cache" in body
    for session in body["sessions"]:
        assert "li_at" not in str(session).lower() or session["cookie"].count(".") >= 3


def test_cache_eviction_endpoint(client):
    assert client.delete("/v1/cache/anything").json()["status"] == "ok"


def test_api_key_enforced_when_configured(tmp_path, monkeypatch):
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]

    def settings_override() -> Settings:
        return Settings(
            _env_file=None,
            li_at="",
            jsessionid="",
            linkedin_sessions="",
            demo_mode=True,
            cache_path=str(tmp_path / "c.sqlite3"),
            fixtures_dir=str(root / "fixtures" / "raw"),
            require_api_key=True,
            api_keys="secret-one,secret-two",
        )

    get_settings.cache_clear()
    monkeypatch.setattr("app.main.get_settings", settings_override)
    app.dependency_overrides[get_settings] = settings_override
    try:
        with TestClient(app) as c:
            params = {"url": "priya-raghavan-synthetic"}
            assert c.get("/v1/profile", params=params).status_code == 401
            bad = c.get("/v1/profile", params=params, headers={"X-API-Key": "nope"})
            assert bad.status_code == 401
            ok = c.get("/v1/profile", params=params, headers={"X-API-Key": "secret-two"})
            assert ok.status_code == 200
            assert ok.json()["profile"]["full_name"] == "Priya Raghavan"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
