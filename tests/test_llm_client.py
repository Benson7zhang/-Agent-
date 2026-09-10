from __future__ import annotations

import io
import json
import socket
from email.message import Message
from urllib import error

import pytest

from smart_finqa.config import LLMConfig
from smart_finqa.llm import (
    LLMClient,
    ModelAttachment,
    ModelCapabilities,
    ModelCapabilityError,
    ModelHTTPError,
    ModelRequest,
    ModelResponseError,
    ModelTransportError,
    OpenAICompatibleClient,
    StructuredModelClient,
)


class _Response:
    def __init__(self, payload: dict | str) -> None:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        self._body = body.encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _RecordingOpener:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[object] = []
        self.timeouts: list[float] = []

    def __call__(self, request: object, timeout: float) -> _Response:
        self.requests.append(request)
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, _Response)
        return outcome


def _config(api_key: str = "test-secret") -> LLMConfig:
    return LLMConfig(
        base_url="https://models.example.test/v1",
        api_key=api_key,
        model="finance-model",
        timeout_seconds=17,
    )


def _success(content: str = '{"status":"OK"}') -> _Response:
    return _Response(
        {
            "id": "response-1",
            "model": "finance-model-2026-09",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        }
    )


def _http_error(status: int, body: dict | None = None, retry_after: str | None = None) -> error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return error.HTTPError(
        "https://models.example.test/v1/chat/completions",
        status,
        "provider failure",
        headers,
        io.BytesIO(json.dumps(body or {}).encode("utf-8")),
    )


def _request(**overrides: object) -> ModelRequest:
    values = {
        "stage": "document-routing",
        "prompt_id": "document-routing",
        "prompt_version": "1.0.0",
        "system_prompt": "Return the requested JSON object.",
        "input_json": {"document_id": "doc-1", "text": "annual report"},
        "output_json_schema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
            "additionalProperties": False,
        },
        "attachments": (),
        "idempotency_key": "idem-1",
        "timeout_seconds": 11,
    }
    values.update(overrides)
    return ModelRequest(**values)


def test_structured_client_sends_schema_attachment_metadata_and_idempotency_key() -> None:
    opener = _RecordingOpener(_success())
    client = OpenAICompatibleClient(
        _config(),
        capabilities=ModelCapabilities(
            supports_strict_json_schema=True,
            supports_vision=True,
            accepted_mime_types=frozenset({"image/png"}),
        ),
        opener=opener,
    )
    request_model = _request(
        attachments=(
            ModelAttachment(
                mime_type="image/png",
                data_url="data:image/png;base64,ZmFrZQ==",
                filename="page-1.png",
            ),
        )
    )

    response = client.invoke(request_model)

    assert isinstance(client, StructuredModelClient)
    assert response.response_id == "response-1"
    assert response.model == "finance-model-2026-09"
    assert response.raw_json == {"status": "OK"}
    assert response.usage["total_tokens"] == 25
    assert response.finish_reason == "stop"
    assert response.latency_ms >= 0

    sent_request = opener.requests[0]
    assert sent_request.headers["Authorization"] == "Bearer test-secret"
    assert sent_request.headers["Idempotency-key"] == "idem-1"
    payload = json.loads(sent_request.data)
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "document-routing",
            "strict": True,
            "schema": request_model.output_json_schema,
        },
    }
    assert payload["messages"][1]["content"][0]["type"] == "text"
    input_envelope = json.loads(payload["messages"][1]["content"][0]["text"])
    assert input_envelope["prompt_id"] == "document-routing"
    assert input_envelope["prompt_version"] == "1.0.0"
    assert input_envelope["stage"] == "document-routing"
    assert input_envelope["input"]["document_id"] == "doc-1"
    assert payload["messages"][1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,ZmFrZQ=="},
    }
    assert opener.timeouts == [11]


def test_non_strict_adapter_uses_json_object_and_leaves_schema_validation_to_orchestrator() -> None:
    opener = _RecordingOpener(_success('{"status":"UNDECLARED"}'))
    client = OpenAICompatibleClient(_config(), opener=opener)

    response = client.invoke(_request())

    assert response.raw_json == {"status": "UNDECLARED"}
    payload = json.loads(opener.requests[0].data)
    assert payload["response_format"] == {"type": "json_object"}
    assert "<output_json_schema>" in payload["messages"][0]["content"]
    assert '"required":["status"]' in payload["messages"][0]["content"]


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_retryable_http_status_is_retried(status: int) -> None:
    waits: list[float] = []
    opener = _RecordingOpener(_http_error(status, retry_after="2"), _success())
    client = OpenAICompatibleClient(_config(), opener=opener, sleeper=waits.append, jitter=lambda: 0.0)

    response = client.invoke(_request(), max_attempts=2)

    assert response.raw_json == {"status": "OK"}
    assert len(opener.requests) == 2
    assert waits == [2.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_http_status_fails_without_retry(status: int) -> None:
    opener = _RecordingOpener(_http_error(status), _success())
    client = OpenAICompatibleClient(_config(), opener=opener)

    with pytest.raises(ModelHTTPError) as exc_info:
        client.invoke(_request(), max_attempts=2)

    assert exc_info.value.status_code == status
    assert exc_info.value.retryable is False
    assert len(opener.requests) == 1


def test_permanent_quota_error_on_429_fails_without_retry_and_does_not_leak_secret() -> None:
    secret = "never-print-this-key"
    opener = _RecordingOpener(
        _http_error(429, {"error": {"type": "insufficient_quota", "message": secret}}),
        _success(),
    )
    client = OpenAICompatibleClient(_config(secret), opener=opener)

    with pytest.raises(ModelHTTPError) as exc_info:
        client.invoke(_request(), max_attempts=2)

    assert exc_info.value.provider_code == "insufficient_quota"
    assert exc_info.value.retryable is False
    assert secret not in str(exc_info.value)
    assert len(opener.requests) == 1


def test_timeout_is_retried_but_non_timeout_url_error_is_not() -> None:
    retrying_opener = _RecordingOpener(socket.timeout("slow"), _success())
    retrying_client = OpenAICompatibleClient(
        _config(), opener=retrying_opener, sleeper=lambda _seconds: None, jitter=lambda: 0.0
    )
    assert retrying_client.invoke(_request(), max_attempts=2).raw_json == {"status": "OK"}
    assert len(retrying_opener.requests) == 2

    failing_opener = _RecordingOpener(error.URLError("dns failure"), _success())
    failing_client = OpenAICompatibleClient(_config(), opener=failing_opener)
    with pytest.raises(ModelTransportError, match="transport failed"):
        failing_client.invoke(_request(), max_attempts=2)
    assert len(failing_opener.requests) == 1


@pytest.mark.parametrize(
    "provider_response",
    [
        _Response("not-json"),
        _Response({"id": "response-1", "choices": []}),
        _success("not-json"),
        _success('{"status":"OK","status":"CONFLICT"}'),
        _success("[]"),
    ],
)
def test_invalid_response_fails_explicitly_without_retry(provider_response: _Response) -> None:
    opener = _RecordingOpener(provider_response, _success())
    client = OpenAICompatibleClient(_config(), opener=opener)

    with pytest.raises(ModelResponseError):
        client.invoke(_request(), max_attempts=2)

    assert len(opener.requests) == 1


def test_unsupported_attachment_capability_fails_before_network_call() -> None:
    opener = _RecordingOpener(_success())
    client = OpenAICompatibleClient(_config(), opener=opener)

    with pytest.raises(ModelCapabilityError, match="image inputs"):
        client.invoke(
            _request(
                attachments=(
                    ModelAttachment(
                        mime_type="image/png",
                        data_url="data:image/png;base64,ZmFrZQ==",
                    ),
                )
            )
        )

    assert opener.requests == []


def test_complete_json_keeps_legacy_entry_point() -> None:
    opener = _RecordingOpener(_success('{"intent":"single_metric"}'))
    client = LLMClient(_config(), opener=opener)

    result = client.complete_json("system", "legacy user prompt", max_retries=1)

    assert result == {"intent": "single_metric"}
    payload = json.loads(opener.requests[0].data)
    assert payload["messages"][1]["content"] == "legacy user prompt"
