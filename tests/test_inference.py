import json

import httpx
import pytest

from upgrade_chamber.config import Settings
from upgrade_chamber.inference import MAX_PROMPT_BYTES, MAX_RESPONSE_BYTES, VultrInferenceClient, main


def test_smoke_uses_fixed_vultr_endpoint_and_records_outcome(capsys):
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"status":"ok"}'}}]})

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert main(["smoke"], settings=settings, client=client) == 0

    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": True, "model_id": "chosen-model", "outcome": "structured_response_ok"}
    assert seen[0].url == "https://api.vultrinference.com/v1/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer private-example-key"
    assert json.loads(seen[0].content)["model"] == "chosen-model"
    assert "private-example-key" not in json.dumps(result)


def test_models_command_is_explicit_and_does_not_require_chosen_model(capsys):
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.vultrinference.com/v1/models"
        return httpx.Response(200, json={"data": [{"id": "available-model"}]})

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id=None)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert main(["models"], settings=settings, client=client) == 0
    assert json.loads(capsys.readouterr().out) == {"models": ["available-model"]}


def test_missing_key_fails_without_request(capsys):
    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent")

    settings = Settings(vultr_inference_api_key=None, vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        assert main(["smoke"], settings=settings, client=client) == 2
    assert "VULTR_INFERENCE_API_KEY is required" in capsys.readouterr().err


def test_provider_error_body_and_key_are_never_printed(capsys):
    secret = "private-example-key"

    def reject(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"diagnostic includes {secret}")

    settings = Settings(vultr_inference_api_key=secret, vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(reject)) as client:
        assert main(["smoke"], settings=settings, client=client) == 2
    output = capsys.readouterr()
    assert "HTTP 401" in output.err
    assert secret not in output.err + output.out
    assert "diagnostic includes" not in output.err + output.out


def test_unstructured_completion_is_not_recorded_as_success(capsys):
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert main(["smoke"], settings=settings, client=client) == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "invalid_structured_response"


def test_prompt_schema_and_byte_limits_fail_before_request():
    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent")

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        inference = VultrInferenceClient(settings, client)
        with pytest.raises(ValueError, match="allowed roles"):
            inference.chat_completion([{"role": "tool", "content": "hello"}])
        with pytest.raises(ValueError, match="byte limit"):
            inference.chat_completion([{"role": "user", "content": "é" * MAX_PROMPT_BYTES}])
        with pytest.raises(ValueError, match="bounded"):
            inference.chat_completion([{"role": "user", "content": "hello"}] * 17)


def test_redirect_is_rejected_even_when_injected_client_follows_redirects(capsys):
    seen = []

    def redirect(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://example.com/steal"})

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
        assert main(["smoke"], settings=settings, client=client) == 2
    assert len(seen) == 1
    assert "HTTP 302" in capsys.readouterr().err


def test_provider_response_byte_cap(capsys):
    def oversized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(oversized)) as client:
        assert main(["smoke"], settings=settings, client=client) == 2
    assert "exceeds byte limit" in capsys.readouterr().err


def test_invalid_environment_is_reported_without_echoing_value(monkeypatch, capsys):
    invalid_model = "secret-looking-value-" * 20
    monkeypatch.setenv("VULTR_MODEL_ID", invalid_model)
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", "private-example-key")
    assert main(["smoke"]) == 2
    output = capsys.readouterr()
    assert "Invalid inference configuration" in output.err
    assert invalid_model not in output.err + output.out
