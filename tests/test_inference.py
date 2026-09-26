import json

import httpx
import pytest

from upgrade_chamber.config import Settings
from upgrade_chamber.inference import (
    MAX_PROMPT_BYTES,
    MAX_RESPONSE_BYTES,
    InferenceError,
    VultrInferenceClient,
    main,
)


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
            inference.chat_completion([{"role": "user", "content": "hello"}] * 25)


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


def test_structured_valid_dict_on_first_call():
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"status":"ok"}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7},
            },
        )

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = VultrInferenceClient(settings, client).chat_completion_structured(
            [{"role": "user", "content": "give json"}], max_tokens=64, correction_prompt="fix it",
        )
    assert result.ok is True
    assert result.attempts == 1
    assert result.content == {"status": "ok"}
    assert result.usage == {"prompt_tokens": 5, "completion_tokens": 7}
    assert result.error is None


def test_structured_retry_appends_correction_after_assistant_content():
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"status":"ok"}'}}]})

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = VultrInferenceClient(settings, client).chat_completion_structured(
            [{"role": "user", "content": "give json"}],
            max_tokens=64,
            correction_prompt="Return only the JSON object.",
        )
    assert result.ok is True
    assert result.attempts == 2
    assert result.content == {"status": "ok"}
    assert seen[1]["messages"] == [
        {"role": "user", "content": "give json"},
        {"role": "assistant", "content": "not json"},
        {"role": "user", "content": "Return only the JSON object."},
    ]


def test_structured_invalid_after_correction_retry_reports_failure():
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "still not json"}}], "usage": {"total_tokens": 9}},
        )

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = VultrInferenceClient(settings, client).chat_completion_structured(
            [{"role": "user", "content": "give json"}], max_tokens=64, correction_prompt="fix it",
        )
    assert result.ok is False
    assert result.attempts == 2
    assert result.content is None
    assert result.error == "structured response invalid after one correction retry"
    assert result.usage == {"total_tokens": 9}
    assert len(seen) == 2


def test_structured_non_dict_json_triggers_same_retry_path():
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": '["ok"]'}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"status":"ok"}'}}]})

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = VultrInferenceClient(settings, client).chat_completion_structured(
            [{"role": "user", "content": "give json"}], max_tokens=64, correction_prompt="fix it",
        )
    assert result.ok is True
    assert result.attempts == 2
    assert result.content == {"status": "ok"}
    assert seen[1]["messages"][1] == {"role": "assistant", "content": '["ok"]'}


def test_structured_bounds_fail_before_any_request():
    def forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent")

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        inference = VultrInferenceClient(settings, client)
        with pytest.raises(ValueError, match="byte limit"):
            inference.chat_completion_structured(
                [{"role": "user", "content": "é" * MAX_PROMPT_BYTES}],
                max_tokens=64,
                correction_prompt="fix it",
            )
        with pytest.raises(ValueError, match="bounded"):
            inference.chat_completion_structured(
                [{"role": "user", "content": "hello"}] * 25,
                max_tokens=64,
                correction_prompt="fix it",
            )


def test_structured_correction_exceeding_bounds_fails_before_retry_request():
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "no"}}]})

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        inference = VultrInferenceClient(settings, client)
        with pytest.raises(ValueError, match="byte limit"):
            inference.chat_completion_structured(
                [{"role": "user", "content": "hi"}],
                max_tokens=64,
                correction_prompt="é" * MAX_PROMPT_BYTES,
            )
    assert len(seen) == 1


def test_structured_usage_absent_still_ok():
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"status":"ok"}'}}]})

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = VultrInferenceClient(settings, client).chat_completion_structured(
            [{"role": "user", "content": "give json"}], max_tokens=64, correction_prompt="fix it",
        )
    assert result.ok is True
    assert result.attempts == 1
    assert result.content == {"status": "ok"}
    assert result.usage is None
    assert result.error is None


def test_empty_completion_retries_then_succeeds():
    seen = []
    sleeps = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) < 3:
            return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        content, _usage = VultrInferenceClient(settings, client).chat_completion_raw(
            [{"role": "user", "content": "hi"}], sleep=sleeps.append,
        )
    assert content == "hello"
    assert len(seen) == 3
    assert sleeps == [3.0, 3.0]


def test_empty_completion_fails_after_bounded_retries():
    seen = []
    sleeps = []

    def respond(_request: httpx.Request) -> httpx.Response:
        seen.append(_request)
        return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(InferenceError, match="no text content"):
            VultrInferenceClient(settings, client).chat_completion_raw(
                [{"role": "user", "content": "hi"}], sleep=sleeps.append,
            )
    assert len(seen) == 3
    assert sleeps == [3.0, 3.0]


def test_missing_choices_fail_immediately_without_retry():
    seen = []
    sleeps = []

    def respond(_request: httpx.Request) -> httpx.Response:
        seen.append(_request)
        return httpx.Response(200, json={})

    settings = Settings(vultr_inference_api_key="private-example-key", vultr_model_id="chosen-model")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(InferenceError, match="no choices"):
            VultrInferenceClient(settings, client).chat_completion_raw(
                [{"role": "user", "content": "hi"}], sleep=sleeps.append,
            )
    assert len(seen) == 1
    assert sleeps == []
