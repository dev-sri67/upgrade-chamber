"""Vultr-only inference transport and an explicit connectivity smoke command."""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from upgrade_chamber.config import Settings


VULTR_API_ROOT = "https://api.vultrinference.com/v1"
MAX_PROMPT_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_MESSAGES = 16


class InferenceError(RuntimeError):
    """A provider or response failure safe to show without provider response text."""


@dataclass(frozen=True)
class SmokeResult:
    ok: bool
    model_id: str
    outcome: str


@dataclass(frozen=True)
class StructuredResult:
    ok: bool
    content: dict | None      # parsed JSON object when ok
    usage: dict | None        # usage of the last call
    attempts: int             # 1 or 2
    error: str | None         # short honest reason when not ok


class VultrInferenceClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self._settings = settings
        self._client = client or httpx.Client(timeout=60.0, trust_env=False)
        self._owns_client = client is None

    def __enter__(self) -> "VultrInferenceClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._owns_client:
            self._client.close()

    def _request(self, method: str, path: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        key = self._settings.require_inference_key().get_secret_value()
        try:
            with self._client.stream(
                method,
                f"{VULTR_API_ROOT}{path}",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                follow_redirects=False,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise InferenceError(f"Vultr inference returned HTTP {response.status_code}")
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise InferenceError("Vultr inference response exceeds byte limit")
                    chunks.append(chunk)
        except httpx.RequestError:
            raise InferenceError("Vultr inference transport failed") from None
        try:
            data = json.loads(b"".join(chunks))
        except ValueError:
            raise InferenceError("Vultr inference returned invalid JSON") from None
        if not isinstance(data, dict):
            raise InferenceError("Vultr inference returned an unexpected response")
        return data

    def list_model_ids(self) -> list[str]:
        data = self._request("GET", "/models")
        models = data.get("data")
        if not isinstance(models, list):
            raise InferenceError("Vultr model list has an unexpected format")
        ids = [item.get("id") if isinstance(item, dict) else None for item in models]
        if any(not isinstance(model_id, str) or not model_id for model_id in ids):
            raise InferenceError("Vultr model list has an unexpected format")
        return ids

    def _validate_prompt(self, messages: list[dict[str, str]], max_tokens: int) -> None:
        if not 1 <= len(messages) <= MAX_MESSAGES or not 1 <= max_tokens <= 4096:
            raise ValueError("Inference messages and max_tokens must be bounded")
        if any(
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or not isinstance(message["role"], str)
            or message["role"] not in {"system", "user", "assistant"}
            or not isinstance(message["content"], str)
            or not message["content"]
            for message in messages
        ):
            raise ValueError("Inference messages must contain allowed roles and text")
        prompt_bytes = sum(len(message["content"].encode("utf-8")) for message in messages)
        if prompt_bytes > MAX_PROMPT_BYTES:
            raise ValueError("Inference prompt exceeds byte limit")

    def chat_completion(self, messages: list[dict[str, str]], max_tokens: int = 64) -> str:
        content, _usage = self.chat_completion_raw(messages, max_tokens)
        return content

    def chat_completion_raw(self, messages: list[dict[str, str]], max_tokens: int = 64) -> tuple[str, dict | None]:
        self._validate_prompt(messages, max_tokens)
        model_id = self._settings.require_model_id()
        data = self._request(
            "POST",
            "/chat/completions",
            payload={"model": model_id, "messages": messages, "max_tokens": max_tokens},
        )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise InferenceError("Vultr completion has no choices")
        first = choices[0]
        if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
            raise InferenceError("Vultr completion has an unexpected format")
        content = first["message"].get("content")
        if not isinstance(content, str):
            raise InferenceError("Vultr completion has no text content")
        usage = data.get("usage")
        return content, usage if isinstance(usage, dict) else None

    @staticmethod
    def _parse_json_object(content: str) -> dict | None:
        try:
            parsed = json.loads(content)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def chat_completion_structured(self, messages: list[dict[str, str]], *, max_tokens: int,
                                    correction_prompt: str) -> StructuredResult:
        self._validate_prompt(messages, max_tokens)
        content, usage = self.chat_completion_raw(messages, max_tokens)
        parsed = self._parse_json_object(content)
        if parsed is not None:
            return StructuredResult(ok=True, content=parsed, usage=usage, attempts=1, error=None)
        retry_messages = [
            *messages,
            {"role": "assistant", "content": content},
            {"role": "user", "content": correction_prompt},
        ]
        self._validate_prompt(retry_messages, max_tokens)
        retry_content, retry_usage = self.chat_completion_raw(retry_messages, max_tokens)
        retry_parsed = self._parse_json_object(retry_content)
        if retry_parsed is not None:
            return StructuredResult(ok=True, content=retry_parsed, usage=retry_usage, attempts=2, error=None)
        return StructuredResult(
            ok=False,
            content=None,
            usage=retry_usage,
            attempts=2,
            error="structured response invalid after one correction retry",
        )

    def smoke(self) -> SmokeResult:
        self._settings.require_inference_key()
        model_id = self._settings.require_model_id()
        content = self.chat_completion(
            [{"role": "user", "content": 'Return exactly this JSON object: {"status":"ok"}'}]
        )
        try:
            structured = json.loads(content)
        except ValueError:
            return SmokeResult(ok=False, model_id=model_id, outcome="invalid_structured_response")
        if structured != {"status": "ok"}:
            return SmokeResult(ok=False, model_id=model_id, outcome="invalid_structured_response")
        return SmokeResult(ok=True, model_id=model_id, outcome="structured_response_ok")


def main(argv: list[str] | None = None, *, settings: Settings | None = None,
         client: httpx.Client | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit Vultr inference connectivity checks")
    parser.add_argument("command", choices=("models", "smoke"))
    args = parser.parse_args(argv)
    try:
        resolved = settings or Settings()
        with VultrInferenceClient(resolved, client) as inference:
            if args.command == "models":
                print(json.dumps({"models": inference.list_model_ids()}))
                return 0
            result = inference.smoke()
            print(json.dumps(asdict(result)))
            return 0 if result.ok else 1
    except ValidationError:
        print("Invalid inference configuration", file=sys.stderr)
        return 2
    except (ValueError, InferenceError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
