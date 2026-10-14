"""Minimal JSON-over-HTTP client built on urllib (honours HTTPS_PROXY etc.)."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Optional

from ..redaction import Redactor
from .base import AuthenticationError, ContextOverflowError, ProviderError, TransientProviderError

_OVERFLOW_MARKERS = (
    "exceeds the context window",
    "context_length_exceeded",
    "context length exceeded",
    "maximum context length",
    "too many tokens",
    "input is too long",
    "prompt is too long",
    "request too large",
)
_TRANSIENT_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"HTTP {status} from {url}")
        self.status = status
        self.body = body
        self.url = url


class HttpClient:
    def __init__(self, *, timeout: float = 600.0, redactor: Optional[Redactor] = None) -> None:
        self.timeout = timeout
        self.redactor = redactor or Redactor()

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise HttpError(exc.code, self.redactor.redact(body), url) from None
        except urllib.error.URLError as exc:
            reason = self.redactor.redact(str(getattr(exc, "reason", exc)))
            raise TransientProviderError(f"network error calling {url}: {reason}") from None
        except (socket.timeout, TimeoutError):
            raise TransientProviderError(f"timed out after {self.timeout:.0f}s calling {url}") from None
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            snippet = self.redactor.redact(text[:300])
            raise ProviderError(f"non-JSON response from {url}: {snippet}") from exc
        if not isinstance(parsed, dict):
            raise ProviderError(f"unexpected JSON shape from {url}: {type(parsed).__name__}")
        return parsed


def provider_error_from_http(error: HttpError, label: str) -> ProviderError:
    """Map an HTTP failure to the provider error class the loop reacts to."""

    body_lower = error.body.lower()
    snippet = error.body.strip().replace("\n", " ")[:600]
    if error.status in (400, 413, 422) and (error.status == 413 or any(marker in body_lower for marker in _OVERFLOW_MARKERS)):
        return ContextOverflowError(f"{label}: context window exceeded (HTTP {error.status}): {snippet}")
    if error.status in (401, 403):
        return AuthenticationError(f"{label}: authentication failed (HTTP {error.status}): {snippet}")
    if error.status in _TRANSIENT_STATUSES:
        return TransientProviderError(f"{label}: HTTP {error.status}: {snippet}")
    return ProviderError(f"{label}: HTTP {error.status}: {snippet}")


__all__ = ["HttpClient", "HttpError", "provider_error_from_http"]
