import pytest
from fastapi.testclient import TestClient

from upgrade_chamber.api import app
from upgrade_chamber.config import Settings
from upgrade_chamber.profiles import UnsupportedProfileError, require_enabled_profile


def test_health_and_unvalidated_catalog():
    client = TestClient(app)
    assert client.get("/healthz").json() == {"status": "ok"}

    response = client.get("/api/profiles")
    assert response.status_code == 200
    catalog = response.json()
    assert catalog["profiles"] == []
    candidate = catalog["research_candidates"][0]
    assert candidate["enabled"] is False
    assert candidate["status"] == "unvalidated"
    assert candidate["id"] == "requests-unixsocket-historical-0.3.0-research"
    assert candidate["commit_sha"] == "8449bc0f76a2ce410644b5e8aab45829ddca54f7"
    with pytest.raises(UnsupportedProfileError):
        require_enabled_profile(candidate["id"])


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
