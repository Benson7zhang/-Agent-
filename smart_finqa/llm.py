from __future__ import annotations

import json
import random
import re
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, runtime_checkable
from urllib import error, request

from .config import LLMConfig
from .logger import get_logger

_RETRYABLE_HTTP_STATUS = frozenset({408, 429})
_PERMANENT_PROVIDER_CODES = frozenset(
    {
        "billing_hard_limit_reached",
        "insufficient_quota",
        "invalid_api_key",
        "model_not_found",
        "permission_denied",
    }
)
_SAFE_PROVIDER_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_SCHEMA_NAME_CHARACTER = re.compile(r"[^A-Za-z0-9_-]")


class ModelClientError(RuntimeError):
    """Base error for explicit model-client failures."""


class ModelRequestError(ModelClientError):
    """The caller supplied an invalid model request."""


class ModelCapabilityError(ModelClientError):
    """The configured adapter cannot satisfy a requested capability."""


class ModelTransportError(ModelClientError):
    """The request failed before an HTTP response was received."""


class ModelHTTPError(ModelClientError):
    """The provider returned a non-success HTTP response."""

    def __init__(self, status_code: int, *, retryable: bool, provider_code: str | None = None) -> None:
        self.status_code = status_code
        self.retryable = retryable
        self.provider_code = provider_code
        code_suffix = f", provider_code={provider_code}" if provider_code else ""
        super().__init__(f"Model provider returned HTTP {status_code}{code_suffix}")


class ModelResponseError(ModelClientError):
    """The provider response cannot be parsed as the required JSON object."""


class _DuplicateJSONKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ModelAttachment:
    """An inline attachment accepted by a structured model adapter."""

    mime_type: str
    data_url: str
    filename: str | None = None

    def __post_init__(self) -> None:
        if not self.mime_type or "/" not in self.mime_type:
            raise ModelRequestError("Attachment mime_type must be a valid MIME type")
        if not self.data_url.startswith("data:"):
            raise ModelRequestError("Attachment data_url must use an inline data: URL")
        if self.filename is not None and not self.filename.strip():
            raise ModelRequestError("Attachment filename cannot be blank")


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """Capabilities declared by a concrete provider adapter."""

    supports_json_object: bool = True
    supports_strict_json_schema: bool = False
    supports_vision: bool = False
    supports_file_inputs: bool = False
    max_context_tokens: int | None = None
    accepted_mime_types: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.max_context_tokens is not None and self.max_context_tokens < 1:
            raise ValueError("max_context_tokens must be positive when provided")
        object.__setattr__(self, "accepted_mime_types", frozenset(self.accepted_mime_types))


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """Provider-independent request for one versioned structured prompt."""

    stage: str
    prompt_id: str
    prompt_version: str
    system_prompt: str
    input_json: Any
    output_json_schema: Mapping[str, Any]
    attachments: tuple[ModelAttachment, ...] = ()
    idempotency_key: str | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        for field_name in ("stage", "prompt_id", "prompt_version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ModelRequestError(f"{field_name} must be a non-empty string")
        if not isinstance(self.system_prompt, str):
            raise ModelRequestError("system_prompt must be a string")
        if not isinstance(self.output_json_schema, Mapping):
            raise ModelRequestError("output_json_schema must be a JSON object")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ModelRequestError("idempotency_key cannot be blank")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ModelRequestError("timeout_seconds must be positive when provided")
        object.__setattr__(self, "attachments", tuple(self.attachments))
        if any(not isinstance(item, ModelAttachment) for item in self.attachments):
            raise ModelRequestError("attachments must contain ModelAttachment values")


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """Parsed response metadata plus the model-produced JSON object."""

    response_id: str | None
    model: str
    raw_json: dict[str, Any]
    usage: dict[str, Any]
    latency_ms: int
    finish_reason: str | None


@runtime_checkable
class StructuredModelClient(Protocol):
    @property
    def enabled(self) -> bool: ...

    @property
    def capabilities(self) -> ModelCapabilities: ...

    def invoke(self, model_request: ModelRequest, *, max_attempts: int = 3) -> ModelResponse: ...


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_json_object(raw_text: str, *, context: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, _DuplicateJSONKeyError) as exc:
        raise ModelResponseError(f"{context} is not valid unambiguous JSON") from exc
    if not isinstance(parsed, dict):
        raise ModelResponseError(f"{context} must be a JSON object")
    return parsed


def _provider_error_code(http_error: error.HTTPError) -> str | None:
    try:
        body = http_error.read(65_536)
    except OSError:
        return None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    provider_error = parsed.get("error")
    if not isinstance(provider_error, dict):
        return None
    for field_name in ("code", "type"):
        value = provider_error.get(field_name)
        if isinstance(value, str) and _SAFE_PROVIDER_CODE.fullmatch(value):
            return value
    return None


def _retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    if headers is None:
        return None
    raw_value = headers.get("Retry-After")
    if raw_value is None:
        return None
    try:
        seconds = float(raw_value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, seconds)


class OpenAICompatibleClient:
    """Structured client for OpenAI-compatible Chat Completions endpoints."""

    def __init__(
        self,
        config: LLMConfig,
        *,
        capabilities: ModelCapabilities | None = None,
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.config = config
        self._capabilities = capabilities or ModelCapabilities()
        self.logger = get_logger()
        self._opener = opener or request.urlopen
        self._sleep = sleeper
        self._jitter = jitter
        self._request_count = 0
        self._error_count = 0

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def invoke(self, model_request: ModelRequest, *, max_attempts: int = 3) -> ModelResponse:
        user_content = self._structured_user_content(model_request)
        return self._invoke_request(model_request, user_content=user_content, max_attempts=max_attempts)

    def complete_json(self, system_prompt: str, user_prompt: str, max_retries: int = 3) -> dict[str, Any]:
        """Compatibility entry point for existing planner and extraction call sites."""
        model_request = ModelRequest(
            stage="legacy-json-completion",
            prompt_id="legacy-json-completion",
            prompt_version="1.0.0",
            system_prompt=system_prompt,
            input_json=user_prompt,
            output_json_schema={"type": "object", "additionalProperties": True},
        )
        response = self._invoke_request(model_request, user_content=user_prompt, max_attempts=max_retries)
        return response.raw_json

    def embedding(self, text: str, max_retries: int = 3) -> list[float]:
        if not self.enabled or not self.config.embedding_model:
            raise ModelClientError("Embedding config not enabled")
        if not isinstance(text, str):
            raise ModelRequestError("Embedding input must be a string")

        payload = {"model": self.config.embedding_model, "input": text}
        started_at = time.monotonic()
        parsed = self._post_json(
            endpoint="embeddings",
            payload=payload,
            timeout_seconds=float(self.config.timeout_seconds),
            max_attempts=max_retries,
            idempotency_key=None,
        )
        data = parsed.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            self._error_count += 1
            raise ModelResponseError("Embedding response is missing data")
        raw_embedding = data[0].get("embedding")
        if not isinstance(raw_embedding, list):
            self._error_count += 1
            raise ModelResponseError("Embedding response is missing an embedding vector")
        try:
            result = [float(value) for value in raw_embedding]
        except (TypeError, ValueError) as exc:
            self._error_count += 1
            raise ModelResponseError("Embedding vector contains a non-numeric value") from exc

        elapsed = time.monotonic() - started_at
        self.logger.debug(
            f"Embedding request succeeded | elapsed={elapsed:.2f}s | model={self.config.embedding_model} | "
            f"text_len={len(text)}"
        )
        return result

    def get_stats(self) -> dict[str, int | float]:
        return {
            "total_requests": self._request_count,
            "total_errors": self._error_count,
            "success_rate": round((self._request_count - self._error_count) / max(self._request_count, 1) * 100, 2),
        }

    def _invoke_request(
        self,
        model_request: ModelRequest,
        *,
        user_content: str | list[dict[str, Any]],
        max_attempts: int,
    ) -> ModelResponse:
        if not self.enabled:
            raise ModelClientError("LLM config not enabled")
        self._validate_attempts(max_attempts)
        self._validate_capabilities(model_request)

        system_prompt = model_request.system_prompt
        if not self.capabilities.supports_strict_json_schema:
            schema_json = json.dumps(
                model_request.output_json_schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            system_prompt = (
                f"{system_prompt.rstrip()}\n\n"
                "输出必须逐字段满足下面的 JSON Schema。不得省略 required 字段，不得增加字段：\n"
                f"<output_json_schema>{schema_json}</output_json_schema>"
            )
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
            "response_format": self._response_format(model_request),
        }
        timeout_seconds = model_request.timeout_seconds or float(self.config.timeout_seconds)
        started_at = time.monotonic()
        parsed = self._post_json(
            endpoint="chat/completions",
            payload=payload,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            idempotency_key=model_request.idempotency_key,
        )
        response = self._parse_chat_response(parsed, started_at=started_at)
        self.logger.debug(
            f"LLM request succeeded | elapsed={response.latency_ms / 1000:.2f}s | model={response.model} | "
            f"prompt_id={model_request.prompt_id} | prompt_version={model_request.prompt_version}"
        )
        return response

    def _structured_user_content(self, model_request: ModelRequest) -> str | list[dict[str, Any]]:
        input_envelope = {
            "stage": model_request.stage,
            "prompt_id": model_request.prompt_id,
            "prompt_version": model_request.prompt_version,
            "input": model_request.input_json,
        }
        try:
            serialized_input = json.dumps(input_envelope, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ModelRequestError("input_json must be JSON serializable") from exc
        if not model_request.attachments:
            return serialized_input

        content: list[dict[str, Any]] = [{"type": "text", "text": serialized_input}]
        for attachment in model_request.attachments:
            if attachment.mime_type.startswith("image/"):
                content.append({"type": "image_url", "image_url": {"url": attachment.data_url}})
                continue
            file_part: dict[str, Any] = {"file_data": attachment.data_url}
            if attachment.filename:
                file_part["filename"] = attachment.filename
            content.append({"type": "file", "file": file_part})
        return content

    def _validate_capabilities(self, model_request: ModelRequest) -> None:
        if not self.capabilities.supports_json_object and not self.capabilities.supports_strict_json_schema:
            raise ModelCapabilityError("The configured model does not support structured JSON output")
        for attachment in model_request.attachments:
            is_image = attachment.mime_type.startswith("image/")
            if is_image and not self.capabilities.supports_vision:
                raise ModelCapabilityError("The configured model does not support image inputs")
            if not is_image and not self.capabilities.supports_file_inputs:
                raise ModelCapabilityError("The configured model does not support file inputs")
            accepted = self.capabilities.accepted_mime_types
            if accepted and attachment.mime_type not in accepted:
                raise ModelCapabilityError(f"The configured model does not accept MIME type {attachment.mime_type}")

    def _response_format(self, model_request: ModelRequest) -> dict[str, Any]:
        if not self.capabilities.supports_strict_json_schema:
            return {"type": "json_object"}
        schema_name = _SCHEMA_NAME_CHARACTER.sub("-", model_request.prompt_id)[:64] or "structured-response"
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": dict(model_request.output_json_schema),
            },
        }

    def _post_json(
        self,
        *,
        endpoint: str,
        payload: Mapping[str, Any],
        timeout_seconds: float,
        max_attempts: int,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        self._validate_attempts(max_attempts)
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ModelRequestError("Model request payload must be JSON serializable") from exc

        url = self.config.base_url.rstrip("/") + "/" + endpoint
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        for attempt in range(max_attempts):
            provider_request = request.Request(url=url, data=body, method="POST", headers=headers)
            self._request_count += 1
            try:
                with self._opener(provider_request, timeout=timeout_seconds) as response:
                    raw_body = response.read()
            except error.HTTPError as exc:
                self._error_count += 1
                provider_code = _provider_error_code(exc)
                retryable = (exc.code in _RETRYABLE_HTTP_STATUS or 500 <= exc.code <= 599) and (
                    provider_code not in _PERMANENT_PROVIDER_CODES
                )
                if not retryable or attempt == max_attempts - 1:
                    raise ModelHTTPError(
                        exc.code,
                        retryable=retryable,
                        provider_code=provider_code,
                    ) from exc
                wait_seconds = self._retry_delay(attempt, _retry_after_seconds(exc.headers))
                self.logger.warning(
                    f"Model HTTP request will retry | attempt={attempt + 1}/{max_attempts} | "
                    f"status={exc.code} | retry_in={wait_seconds:.2f}s"
                )
                self._sleep(wait_seconds)
                continue
            except (TimeoutError, socket.timeout) as exc:
                self._error_count += 1
                if attempt == max_attempts - 1:
                    raise ModelTransportError("Model request timed out") from exc
                wait_seconds = self._retry_delay(attempt, None)
                self.logger.warning(
                    f"Model request timeout will retry | attempt={attempt + 1}/{max_attempts} | "
                    f"retry_in={wait_seconds:.2f}s"
                )
                self._sleep(wait_seconds)
                continue
            except error.URLError as exc:
                self._error_count += 1
                if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                    if attempt < max_attempts - 1:
                        wait_seconds = self._retry_delay(attempt, None)
                        self.logger.warning(
                            f"Model request timeout will retry | attempt={attempt + 1}/{max_attempts} | "
                            f"retry_in={wait_seconds:.2f}s"
                        )
                        self._sleep(wait_seconds)
                        continue
                    raise ModelTransportError("Model request timed out") from exc
                raise ModelTransportError("Model transport failed") from exc

            try:
                decoded_body = raw_body.decode("utf-8")
            except UnicodeDecodeError as exc:
                self._error_count += 1
                raise ModelResponseError("Provider response is not valid UTF-8") from exc
            try:
                return _parse_json_object(decoded_body, context="Provider response")
            except ModelResponseError:
                self._error_count += 1
                raise

        raise AssertionError("Retry loop exited unexpectedly")

    def _parse_chat_response(self, parsed: Mapping[str, Any], *, started_at: float) -> ModelResponse:
        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            self._error_count += 1
            raise ModelResponseError("Chat response is missing choices")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            self._error_count += 1
            raise ModelResponseError("Chat response is missing string message content")
        try:
            raw_json = _parse_json_object(message["content"], context="Model message content")
        except ModelResponseError:
            self._error_count += 1
            raise

        response_id = parsed.get("id")
        if not isinstance(response_id, str):
            response_id = None
        response_model = parsed.get("model")
        if not isinstance(response_model, str) or not response_model:
            response_model = self.config.model
        usage = parsed.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        finish_reason = choice.get("finish_reason")
        if not isinstance(finish_reason, str):
            finish_reason = None
        return ModelResponse(
            response_id=response_id,
            model=response_model,
            raw_json=raw_json,
            usage=dict(usage),
            latency_ms=max(0, round((time.monotonic() - started_at) * 1000)),
            finish_reason=finish_reason,
        )

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        backoff = min(float(2**attempt), 10.0) + max(0.0, self._jitter())
        if retry_after is None:
            return backoff
        return max(backoff, retry_after)

    @staticmethod
    def _validate_attempts(max_attempts: int) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1 or max_attempts > 3:
            raise ModelRequestError("max_attempts must be an integer between 1 and 3")


class LLMClient(OpenAICompatibleClient):
    """Backward-compatible name for the default OpenAI-compatible adapter."""
