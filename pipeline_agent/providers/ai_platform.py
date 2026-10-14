"""AI Platform provider (enterprise model gateway).

Authentication is two-legged: username, password and use case are exchanged at
an iB2B security-token endpoint for a short-lived JWT, which is then sent on
every model call in a configurable trust-token header together with tracking
ids. A Responses-style endpoint also receives it as a Bearer token. The JWT is
re-issued when it is near expiry and once more if the gateway answers 401/403.

Non-secret settings come from environment variables (Jenkins Folder
Properties); the username and password come from Jenkins Credentials.
"""

from __future__ import annotations

import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from ..redaction import Redactor
from ..session import Message
from ..tools.base import ToolSpec
from .base import AuthenticationError, ModelReply, ProviderError
from .http import HttpClient, HttpError, provider_error_from_http
from .openai_format import chat_messages, chat_tools, parse_reply, responses_input, responses_tools

DEFAULT_CHAT_URI = "/v1/api/v1/chat/completions"
DEFAULT_IB2B_URI = "/dsp/token"
DEFAULT_TOKEN_TTL_SECONDS = 30
TOKEN_REFRESH_MARGIN_SECONDS = 5
DEFAULT_TRACKING_PREFIX = "AGENT"

ENV_PREFIX = "AI_PLATFORM_"


def _join(host: str, uri: str) -> str:
    uri = (uri or "").strip()
    if uri.startswith("http://") or uri.startswith("https://"):
        return uri
    if uri and not uri.startswith("/"):
        uri = "/" + uri
    return (host or "").rstrip("/") + uri


def is_responses_endpoint(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.rstrip("/").lower()
    return path.endswith("/responses") or path.endswith("/responses/compact")


@dataclass
class AIPlatformSettings:
    chat_host: str = ""
    chat_uri: str = DEFAULT_CHAT_URI
    responses_uri: str = ""
    responses_host: str = ""
    ib2b_host: str = ""
    ib2b_uri: str = DEFAULT_IB2B_URI
    username: str = ""
    password: str = ""
    usercase: str = ""
    token: str = ""
    trust_token_header: str = ""
    tracking_prefix: str = DEFAULT_TRACKING_PREFIX
    token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "AIPlatformSettings":
        def get(name: str, default: str = "") -> str:
            value = env.get(ENV_PREFIX + name)
            return value.strip() if isinstance(value, str) and value.strip() else default

        ttl_text = get("TOKEN_TTL_SECONDS", str(DEFAULT_TOKEN_TTL_SECONDS))
        try:
            ttl = max(1, int(ttl_text))
        except ValueError:
            ttl = DEFAULT_TOKEN_TTL_SECONDS
        return cls(
            chat_host=get("CHAT_HOST"),
            chat_uri=get("CHAT_URI", DEFAULT_CHAT_URI),
            responses_uri=get("RESPONSES_URI"),
            responses_host=get("RESPONSES_HOST"),
            ib2b_host=get("IB2B_HOST"),
            ib2b_uri=get("IB2B_URI", DEFAULT_IB2B_URI),
            username=get("USERNAME"),
            password=get("PASSWORD"),
            usercase=get("USERCASE"),
            token=get("TOKEN"),
            trust_token_header=get("TRUST_TOKEN_HEADER"),
            tracking_prefix=get("TRACKING_PREFIX", DEFAULT_TRACKING_PREFIX),
            token_ttl_seconds=ttl,
        )

    @property
    def endpoint_url(self) -> str:
        if self.responses_uri:
            return _join(self.responses_host or self.chat_host, self.responses_uri)
        return _join(self.chat_host, self.chat_uri)

    @property
    def ib2b_url(self) -> str:
        return _join(self.ib2b_host, self.ib2b_uri) if self.ib2b_host else ""

    def validate(self) -> None:
        problems: list[str] = []
        if not (self.chat_host or (self.responses_uri and self.responses_host)):
            problems.append(f"{ENV_PREFIX}CHAT_HOST (or {ENV_PREFIX}RESPONSES_HOST with {ENV_PREFIX}RESPONSES_URI)")
        if not self.trust_token_header:
            problems.append(f"{ENV_PREFIX}TRUST_TOKEN_HEADER")
        if not self.token:
            if not (self.username and self.password):
                problems.append(f"{ENV_PREFIX}USERNAME and {ENV_PREFIX}PASSWORD (or {ENV_PREFIX}TOKEN)")
            if not self.ib2b_host:
                problems.append(f"{ENV_PREFIX}IB2B_HOST")
        if problems:
            raise AuthenticationError("AI Platform configuration is incomplete; set " + ", ".join(problems))


class AIPlatformProvider:
    name = "ai_platform"

    def __init__(
        self,
        settings: AIPlatformSettings,
        *,
        model: str,
        effort: str = "high",
        http: Optional[HttpClient] = None,
        redactor: Optional[Redactor] = None,
    ) -> None:
        settings.validate()
        self.settings = settings
        self.model = model
        self.effort = effort
        self.redactor = redactor or Redactor()
        self.http = http or HttpClient(redactor=self.redactor)
        self.endpoint_url = settings.endpoint_url
        self.uses_responses = is_responses_endpoint(self.endpoint_url)
        self._token = settings.token
        self._expires_at: Optional[float] = time.time() + settings.token_ttl_seconds if settings.token else None
        self._lock = threading.Lock()
        self.redactor.add(settings.password)
        self.redactor.add(settings.token)

    # --- token handling ---------------------------------------------------

    @property
    def can_exchange(self) -> bool:
        return bool(self.settings.username and self.settings.password and self.settings.ib2b_url)

    def _token_fresh(self) -> bool:
        if not self._token:
            return False
        if self._expires_at is None:
            return True
        return self._expires_at > time.time() + TOKEN_REFRESH_MARGIN_SECONDS

    def ensure_token(self, *, force: bool = False) -> None:
        if not force and self._token_fresh():
            return
        with self._lock:
            if not force and self._token_fresh():
                return
            if not self.can_exchange:
                if self._token:
                    return
                raise AuthenticationError("AI Platform needs a token, or username/password plus the iB2B endpoint")
            self._exchange()

    def _exchange(self) -> None:
        body = {
            "input_token_state": {
                "token_type": "CREDENTIAL",
                "username": self.settings.username,
                "password": self.settings.password,
            },
            "output_token_state": {"token_type": "JWT"},
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        try:
            data = self.http.request_json("POST", self.settings.ib2b_url, headers=headers, payload=body)
        except HttpError as exc:
            raise AuthenticationError(f"AI Platform token exchange failed (HTTP {exc.status}): {exc.body[:300]}") from None
        token = str(data.get("issued_token") or "").strip()
        if not token:
            raise AuthenticationError("AI Platform token exchange did not return issued_token")
        self._token = token
        self._expires_at = time.time() + self.settings.token_ttl_seconds
        self.redactor.add(token)

    def _headers(self) -> dict[str, str]:
        tracking = f"{self.settings.tracking_prefix or DEFAULT_TRACKING_PREFIX}-{time.strftime('%Y%m%d%H%M%S', time.gmtime())}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            self.settings.trust_token_header: self._token,
            "x-correlation-id": tracking,
            "x-usersession-id": tracking,
        }
        if self.uses_responses:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # --- completion -------------------------------------------------------

    def build_payload(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> dict[str, Any]:
        if self.uses_responses:
            payload: dict[str, Any] = {
                "model": self.model,
                "input": responses_input(messages),
                "stream": False,
                "reasoning": {"effort": self.effort},
            }
            if tools:
                payload["tools"] = responses_tools(tools)
            if system:
                payload["instructions"] = system
            return payload
        payload = {
            "model": self.model,
            "messages": chat_messages(system, messages),
            "stream": False,
        }
        if tools:
            payload["tools"] = chat_tools(tools)
        if self.effort:
            payload["reasoning_effort"] = self.effort
        if self.settings.usercase:
            payload["user"] = self.settings.usercase
        return payload

    def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec]) -> ModelReply:
        payload = self.build_payload(system, messages, tools)
        for attempt in range(2):
            self.ensure_token()
            try:
                raw = self.http.request_json("POST", self.endpoint_url, headers=self._headers(), payload=payload)
            except HttpError as exc:
                if attempt == 0 and exc.status in (401, 403) and self.can_exchange:
                    self.ensure_token(force=True)
                    continue
                raise provider_error_from_http(exc, "AI Platform") from None
            return parse_reply(raw)
        raise ProviderError("AI Platform: request failed after token refresh")


__all__ = ["AIPlatformProvider", "AIPlatformSettings", "ENV_PREFIX", "is_responses_endpoint"]
