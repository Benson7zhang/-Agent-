from __future__ import annotations

import json
import time
from typing import Any
from urllib import error, request

from .config import LLMConfig
from .logger import get_logger


class LLMClient:
    """
    OpenAI-compatible JSON client with retry logic and logging.
    When config is incomplete, methods raise RuntimeError and callers should fallback.
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.logger = get_logger()
        self._request_count = 0
        self._error_count = 0

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def complete_json(self, system_prompt: str, user_prompt: str, max_retries: int = 3) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("LLM config not enabled")

        url = self.config.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        last_error = None
        for attempt in range(max_retries):
            try:
                self._request_count += 1
                start_time = time.time()

                data = json.dumps(payload).encode("utf-8")
                req = request.Request(
                    url=url,
                    data=data,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.config.api_key}",
                    },
                )

                with request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                    body = resp.read().decode("utf-8")

                elapsed = time.time() - start_time

                parsed = json.loads(body)
                if "choices" not in parsed or not parsed["choices"]:
                    raise RuntimeError("Invalid LLM response: missing choices")
                content = parsed["choices"][0]["message"]["content"]
                result = json.loads(content)

                self.logger.debug(
                    f"LLM request succeeded | attempt={attempt+1} | elapsed={elapsed:.2f}s | "
                    f"model={self.config.model} | prompt_len={len(user_prompt)}"
                )
                return result

            except error.URLError as exc:
                last_error = exc
                self._error_count += 1
                wait_time = min(2 ** attempt, 10)  # Exponential backoff, max 10s
                self.logger.warning(
                    f"LLM request failed | attempt={attempt+1}/{max_retries} | "
                    f"error={type(exc).__name__} | retry_in={wait_time}s"
                )
                if attempt < max_retries - 1:
                    time.sleep(wait_time)

            except (KeyError, IndexError, json.JSONDecodeError) as exc:
                last_error = exc
                self._error_count += 1
                self.logger.error(f"Failed to parse LLM response | attempt={attempt+1} | error={exc}")
                if attempt < max_retries - 1:
                    time.sleep(1)

            except Exception as exc:
                last_error = exc
                self._error_count += 1
                self.logger.error(f"Unexpected LLM error | attempt={attempt+1} | error={exc}")
                if attempt < max_retries - 1:
                    time.sleep(1)

        raise RuntimeError(f"LLM request failed after {max_retries} attempts: {last_error}") from last_error

    def embedding(self, text: str, max_retries: int = 3) -> list[float]:
        if not self.enabled or not self.config.embedding_model:
            raise RuntimeError("Embedding config not enabled")

        url = self.config.base_url.rstrip("/") + "/embeddings"
        payload = {"model": self.config.embedding_model, "input": text}

        last_error = None
        for attempt in range(max_retries):
            try:
                self._request_count += 1
                start_time = time.time()

                req = request.Request(
                    url=url,
                    data=json.dumps(payload).encode("utf-8"),
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.config.api_key}",
                    },
                )

                with request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                    body = resp.read().decode("utf-8")

                elapsed = time.time() - start_time

                parsed = json.loads(body)
                if "data" not in parsed or not parsed["data"]:
                    raise RuntimeError("Invalid embedding response: missing data")
                result = [float(x) for x in parsed["data"][0]["embedding"]]

                self.logger.debug(
                    f"Embedding request succeeded | attempt={attempt+1} | elapsed={elapsed:.2f}s | "
                    f"text_len={len(text)}"
                )
                return result

            except error.URLError as exc:
                last_error = exc
                self._error_count += 1
                wait_time = min(2 ** attempt, 10)
                self.logger.warning(
                    f"Embedding request failed | attempt={attempt+1}/{max_retries} | "
                    f"error={type(exc).__name__} | retry_in={wait_time}s"
                )
                if attempt < max_retries - 1:
                    time.sleep(wait_time)

            except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                self._error_count += 1
                self.logger.error(f"Failed to parse embedding response | attempt={attempt+1} | error={exc}")
                if attempt < max_retries - 1:
                    time.sleep(1)

            except Exception as exc:
                last_error = exc
                self._error_count += 1
                self.logger.error(f"Unexpected embedding error | attempt={attempt+1} | error={exc}")
                if attempt < max_retries - 1:
                    time.sleep(1)

        raise RuntimeError(f"Embedding request failed after {max_retries} attempts: {last_error}") from last_error

    def get_stats(self) -> dict[str, int]:
        """Get API usage statistics."""
        return {
            "total_requests": self._request_count,
            "total_errors": self._error_count,
            "success_rate": round((self._request_count - self._error_count) / max(self._request_count, 1) * 100, 2),
        }

