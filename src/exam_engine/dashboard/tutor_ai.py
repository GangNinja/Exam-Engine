"""Model adapter for the tutor.

The primary path is a local Ollama model. An OpenAI-compatible chat endpoint is
also supported for deployments that prefer it. Both paths return plain text or a
JSON object; prompt construction and evidence selection stay in the tutor
service. Credentials are read from the environment and never serialized.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from exam_engine.config import TutorSettings

logger = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_ANSWER_FIELD = re.compile(r'\{\s*"answer"\s*:\s*"')
_JSON_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_MODEL_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_MODEL_LOCK = threading.Lock()
_MODEL_CACHE_SECONDS = 30.0


class TutorAIError(RuntimeError):
    """A model failure with a stable code and a safe user-facing message."""

    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


@dataclass(frozen=True)
class TutorModelStatus:
    available: bool
    provider: str
    model: str | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "provider": self.provider,
            "model": self.model,
            "detail": self.detail,
        }


def _ollama_models(base_url: str, timeout: float = 2.0) -> list[dict[str, Any]]:
    """Return installed Ollama models, caching failures briefly to avoid stalls."""

    key = base_url.rstrip("/")
    now = time.monotonic()
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None and now - cached[0] < _MODEL_CACHE_SECONDS:
            return cached[1]
    models: list[dict[str, Any]] = []
    try:
        request = urllib.request.Request(f"{key}/api/tags", method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.load(response)
        raw = payload.get("models") if isinstance(payload, dict) else None
        if isinstance(raw, list):
            models = [item for item in raw if isinstance(item, dict)]
    except (OSError, TimeoutError, ValueError, urllib.error.URLError):
        models = []
    with _MODEL_LOCK:
        _MODEL_CACHE[key] = (now, models)
    return models


def _clear_model_cache(base_url: str) -> None:
    with _MODEL_LOCK:
        _MODEL_CACHE.pop(base_url.rstrip("/"), None)


def _ensure_local_service(settings: TutorSettings) -> None:
    """Start an installed Ollama service when it is present but not running.

    The desktop app is self-contained for a local install: an installed Ollama
    with a completion model should not look "unavailable" merely because its
    background service was stopped.  This only launches the local executable;
    it never installs software or opens a network listener by itself.
    """

    if _ollama_models(settings.ollama_url):
        return
    executable = shutil.which("ollama")
    if not executable:
        return
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    try:
        subprocess.Popen([executable, "serve"], **kwargs)
    except (OSError, ValueError):
        return
    # Ollama needs a moment to bind its loopback port and enumerate models.
    for _ in range(12):
        time.sleep(0.5)
        _clear_model_cache(settings.ollama_url)
        if _ollama_models(settings.ollama_url):
            return


def detect_local_model(settings: TutorSettings, env: Mapping[str, str] | None = None) -> str | None:
    """Return a usable local completion model, or ``None`` when Ollama is absent."""

    env = os.environ if env is None else env
    preferred = (settings.model or env.get("EXAM_ENGINE_TUTOR_MODEL") or "").strip()
    _ensure_local_service(settings)
    models = _ollama_models(settings.ollama_url)
    names = [
        str(item.get("name") or item.get("model") or "").strip()
        for item in models
        if str(item.get("name") or item.get("model") or "").strip()
    ]
    if preferred:
        return preferred if not names or preferred in names else None
    if not names:
        return None
    for preferred_name in ("qwen3:8b", "qwen3:4b", "qwen2.5:7b", "llama3.2:3b"):
        if preferred_name in names:
            return preferred_name
    return sorted(names)[0]


def _partial_json_string(raw: str, start: int) -> tuple[str, bool]:
    """Decode a JSON string body that may still be arriving.

    Returns the decoded text and whether the closing quote has arrived. Escapes
    are only consumed once complete, so a chunk boundary cannot turn ``\n`` into
    ``n`` or lose a surrogate pair.
    """

    chars: list[str] = []
    index = start
    while index < len(raw):
        char = raw[index]
        if char == '"':
            return "".join(chars), True
        if char != "\\":
            chars.append(char)
            index += 1
            continue
        if index + 1 >= len(raw):
            break
        escape = raw[index + 1]
        mapped = _JSON_ESCAPES.get(escape)
        if mapped is not None:
            chars.append(mapped)
            index += 2
            continue
        if escape != "u" or index + 6 > len(raw):
            break
        try:
            code = int(raw[index + 2 : index + 6], 16)
        except ValueError:
            break
        if 0xD800 <= code <= 0xDBFF:
            if index + 12 > len(raw) or raw[index + 6 : index + 8] != "\\u":
                break
            try:
                low = int(raw[index + 8 : index + 12], 16)
            except ValueError:
                break
            if not 0xDC00 <= low <= 0xDFFF:
                break
            chars.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
            index += 12
            continue
        if 0xDC00 <= code <= 0xDFFF:
            break
        chars.append(chr(code))
        index += 6
    return "".join(chars), False


class _PartialAnswerExtractor:
    """Extract plain answer text from a structured JSON response as it streams."""

    def __init__(self) -> None:
        self._raw = ""
        self._start: int | None = None
        self._emitted = 0
        self._complete = False

    def feed(self, chunk: str) -> str:
        if self._complete or not chunk:
            return ""
        self._raw += chunk
        if self._start is None:
            match = _ANSWER_FIELD.search(self._raw)
            if match is None:
                return ""
            self._start = match.end()
        text, complete = _partial_json_string(self._raw, self._start)
        if complete:
            self._complete = True
        if len(text) <= self._emitted:
            return ""
        delta = text[self._emitted :]
        self._emitted = len(text)
        return delta


class TutorAI:
    """Resolve and call the configured tutor model."""

    def __init__(
        self,
        settings: TutorSettings,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.env = os.environ if env is None else env
        self._status: TutorModelStatus | None = None

    def status(self) -> TutorModelStatus:
        if self._status is None:
            self._status = self._resolve_status()
        return self._status

    def _resolve_status(self) -> TutorModelStatus:
        provider = (self.settings.provider or "auto").strip().casefold()
        if provider in {"off", "none", "disabled"}:
            return TutorModelStatus(False, "off", None, "Tutor AI is disabled by configuration.")

        if provider in {"auto", "ollama", "local"}:
            model = detect_local_model(self.settings, self.env)
            if model:
                return TutorModelStatus(
                    True,
                    "ollama",
                    model,
                    f"Local Ollama model {model} is ready.",
                )
            if provider == "ollama":
                return TutorModelStatus(
                    False,
                    "ollama",
                    None,
                    "Ollama is not running or has no completion model installed.",
                )

        base_url = (
            self.settings.llm_base_url or self.env.get("EXAM_ENGINE_TUTOR_LLM_BASE_URL") or ""
        ).strip()
        model = (self.settings.model or self.env.get("EXAM_ENGINE_TUTOR_LLM_MODEL") or "").strip()
        api_key = (
            self.env.get("EXAM_ENGINE_TUTOR_LLM_API_KEY") or self.env.get("OPENAI_API_KEY") or ""
        ).strip()
        if base_url and model and api_key:
            return TutorModelStatus(
                True,
                "openai",
                model,
                "An OpenAI-compatible tutor model is configured.",
            )
        return TutorModelStatus(
            False,
            "unavailable",
            None,
            "No local Ollama model or configured OpenAI-compatible tutor model is available.",
        )

    def chat_json(
        self,
        system: str,
        user: str,
        *,
        on_answer_chunk: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        status = self.status()
        if not status.available:
            raise TutorAIError("tutor_ai_unavailable", status.detail)
        if status.provider == "ollama":
            payload = self._ollama_payload(system, user, stream=on_answer_chunk is not None)
            if on_answer_chunk is not None:
                raw = self._post_ollama_stream(payload, on_answer_chunk)
                return self._parse_content(raw)
        else:
            payload = self._openai_payload(system, user)
        raw = self._post(payload, status.provider)
        parsed = self._parse_json(raw)
        return parsed

    def _ollama_payload(
        self,
        system: str,
        user: str,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        return {
            "model": self.status().model,
            "stream": stream,
            "think": False,
            "format": "json",
            "keep_alive": "10m",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {
                "temperature": 0.25,
                "num_ctx": 4096,
                "num_predict": 700,
            },
        }

    def _openai_payload(self, system: str, user: str) -> dict[str, Any]:
        return {
            "model": self.status().model,
            "temperature": 0.25,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }

    def _post(self, payload: dict[str, Any], provider: str) -> str:
        status = self.status()
        if provider == "ollama":
            url = f"{self.settings.ollama_url.rstrip('/')}/api/chat"
            headers = {"Content-Type": "application/json"}
        else:
            base_url = (
                self.settings.llm_base_url
                or self.env.get("EXAM_ENGINE_TUTOR_LLM_BASE_URL")
                or "https://api.openai.com/v1"
            ).strip()
            url = f"{base_url.rstrip('/')}/chat/completions"
            api_key = (
                self.env.get("EXAM_ENGINE_TUTOR_LLM_API_KEY")
                or self.env.get("OPENAI_API_KEY")
                or ""
            ).strip()
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - explicit configured endpoint
                request, timeout=self.settings.timeout_seconds
            ) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise TutorAIError(
                "tutor_ai_http_error",
                f"The tutor model rejected the request with HTTP {exc.code}.",
                detail={"status": exc.code, "provider": provider, "model": status.model},
            ) from exc
        except TimeoutError as exc:
            raise TutorAIError(
                "tutor_ai_timeout",
                "The tutor model did not finish before the configured timeout.",
                detail={"provider": provider, "model": status.model},
            ) from exc
        except OSError as exc:
            raise TutorAIError(
                "tutor_ai_unreachable",
                "The tutor model could not be reached.",
                detail={"provider": provider, "model": status.model, "error": type(exc).__name__},
            ) from exc
        if not raw.strip():
            raise TutorAIError("tutor_ai_empty", "The tutor model returned an empty response.")
        return raw

    def _post_ollama_stream(
        self,
        payload: dict[str, Any],
        on_answer_chunk: Callable[[str], None],
    ) -> str:
        """Read Ollama's NDJSON stream and emit decoded answer deltas."""

        status = self.status()
        url = f"{self.settings.ollama_url.rstrip('/')}/api/chat"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/x-ndjson",
            },
            method="POST",
        )
        parts: list[str] = []
        extractor = _PartialAnswerExtractor()
        try:
            with urllib.request.urlopen(  # noqa: S310 - explicit configured endpoint
                request, timeout=self.settings.timeout_seconds
            ) as response:
                for line in response:
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        event = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("error"):
                        raise TutorAIError(
                            "tutor_ai_stream_error",
                            "The tutor model stopped while streaming its answer.",
                            detail={"provider": "ollama", "model": status.model},
                        )
                    message = event.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if not isinstance(content, str) or not content:
                        continue
                    parts.append(content)
                    delta = extractor.feed(content)
                    if delta:
                        on_answer_chunk(delta)
        except TutorAIError:
            raise
        except urllib.error.HTTPError as exc:
            raise TutorAIError(
                "tutor_ai_http_error",
                f"The tutor model rejected the request with HTTP {exc.code}.",
                detail={"status": exc.code, "provider": "ollama", "model": status.model},
            ) from exc
        except TimeoutError as exc:
            raise TutorAIError(
                "tutor_ai_timeout",
                "The tutor model did not finish before the configured timeout.",
                detail={"provider": "ollama", "model": status.model},
            ) from exc
        except OSError as exc:
            raise TutorAIError(
                "tutor_ai_unreachable",
                "The tutor model could not be reached.",
                detail={"provider": "ollama", "model": status.model, "error": type(exc).__name__},
            ) from exc
        raw = "".join(parts)
        if not raw.strip():
            raise TutorAIError("tutor_ai_empty", "The tutor model returned an empty response.")
        return raw

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TutorAIError(
                "tutor_ai_invalid_json",
                "The tutor model returned an invalid structured response.",
                detail={"error": str(exc)[:160]},
            ) from exc
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                return TutorAI._parse_content(content)
        message = payload.get("message") if isinstance(payload, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return TutorAI._parse_content(content)
        if isinstance(payload, dict):
            return payload
        raise TutorAIError("tutor_ai_invalid_json", "The tutor model returned no JSON object.")

    @staticmethod
    def _parse_content(content: str) -> dict[str, Any]:
        cleaned = _JSON_FENCE.sub("", content.strip())
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise TutorAIError(
                "tutor_ai_invalid_json",
                "The tutor model did not return a valid JSON object.",
                detail={"error": str(exc)[:160]},
            ) from exc
        if not isinstance(parsed, dict):
            raise TutorAIError("tutor_ai_invalid_json", "The tutor response must be an object.")
        return parsed


__all__ = ["TutorAI", "TutorAIError", "TutorModelStatus", "detect_local_model"]
