import pytest
from fastapi.testclient import TestClient

from upgrade_chamber.api import app
from upgrade_chamber.baseline import BASELINE_VERSION, COMMIT_SHA, TARGET_VERSION
from upgrade_chamber.config import Settings
from upgrade_chamber.profiles import (
    ENABLED_PROFILES,
    UnsupportedProfileError,
    enabled_profiles,
    public_catalog,
    require_enabled_profile,
    require_profile_repository,
)


PROFILE_ID = "requests-unixsocket-historical-0.3.0-research"
PROFILE_URL = "https://github.com/msabramo/requests-unixsocket"


def test_health_and_unvalidated_catalog():
    client = TestClient(app)
    assert client.get("/healthz").json() == {"status": "ok"}

    response = client.get("/api/profiles")
    assert response.status_code == 200
    catalog = response.json()
    profile = catalog["profiles"][0]
    assert profile["id"] == PROFILE_ID
    assert profile["commit_sha"] == COMMIT_SHA
    candidate = catalog["research_candidates"][0]
    assert candidate["enabled"] is False
    assert candidate["status"] == "unvalidated"
    assert candidate["id"] == "requests-unixsocket-historical-0.3.0-research"
    assert candidate["commit_sha"] == "8449bc0f76a2ce410644b5e8aab45829ddca54f7"
    assert require_enabled_profile(candidate["id"]).id == candidate["id"]


def test_enabled_profiles_contains_exactly_the_validated_requests_profile():
    profiles = enabled_profiles()
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.id == PROFILE_ID
    assert profile.repository_url == PROFILE_URL
    assert profile.commit_sha == "8449bc0f76a2ce410644b5e8aab45829ddca54f7" == COMMIT_SHA
    assert profile.dependency == "requests"
    assert profile.python == "3.11"
    assert profile.baseline_version == "2.31.0" == BASELINE_VERSION
    assert profile.target_version == "2.34.2" == TARGET_VERSION
    assert profile.description and profile.disclosure and profile.evidence


def test_require_enabled_profile_round_trip_and_unknown_id():
    profile = enabled_profiles()[0]
    assert require_enabled_profile(profile.id) is profile
    with pytest.raises(UnsupportedProfileError):
        require_enabled_profile("requests-unixsocket-historical-0.3.0-disabled")


def test_public_catalog_includes_enabled_profile_and_keeps_research_candidates():
    catalog = public_catalog()
    assert [profile["id"] for profile in catalog["profiles"]] == [PROFILE_ID]
    profile = catalog["profiles"][0]
    assert profile["repository_url"] == PROFILE_URL
    assert profile["commit_sha"] == COMMIT_SHA
    assert profile["baseline_version"] == "2.31.0"
    assert profile["target_version"] == "2.34.2"
    assert profile["disclosure"]
    assert profile["evidence"]
    assert "urllib3" in profile["disclosure"] and "waitress" in profile["disclosure"]
    assert "2.31.0" in profile["disclosure"] and "2.34.2" in profile["disclosure"]
    assert "msabramo/requests-unixsocket" in profile["disclosure"]

    candidate = catalog["research_candidates"][0]
    assert candidate["enabled"] is False
    assert candidate["status"] == "unvalidated"
    assert candidate["commit_sha"] == COMMIT_SHA
    assert set(catalog) == {"profiles", "research_candidates"}


def test_require_profile_repository_admits_only_exact_match():
    profile = require_profile_repository(PROFILE_ID, PROFILE_URL, COMMIT_SHA)
    assert profile is ENABLED_PROFILES[0]

    with pytest.raises(UnsupportedProfileError):
        require_profile_repository(PROFILE_ID, "https://github.com/evil/requests-unixsocket", COMMIT_SHA)
    with pytest.raises(UnsupportedProfileError):
        require_profile_repository(PROFILE_ID, PROFILE_URL, "f" * 40)
    with pytest.raises(UnsupportedProfileError):
        require_profile_repository("requests-unixsocket-historical-0.3.0-unknown", PROFILE_URL, COMMIT_SHA)


def test_credentials_required_only_for_inference_and_never_repr():
    empty = Settings(vultr_inference_api_key=None, vultr_model_id=None)
    with pytest.raises(ValueError, match="VULTR_INFERENCE_API_KEY"):
        empty.require_inference_key()
    with pytest.raises(ValueError, match="VULTR_MODEL_ID"):
        empty.require_model_id()

    configured = Settings(
        vultr_inference_api_key="private-example-key",
        vultr_model_id="example-model",
    )
    assert configured.require_model_id() == "example-model"
    assert "private-example-key" not in repr(configured)
    assert "private-example-key" not in str(configured)
