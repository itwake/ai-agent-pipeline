"""GitHub Copilot provider (Responses API).

A GitHub token (``ghp_``, ``gho_``, ``github_pat_`` ...) is exchanged for a
short-lived Copilot session token at ``/copilot_internal/v2/token``; a token
that is already a Copilot session token is used as is. Requests go to the API
host the exchange names (``endpoints.api`` or the ``proxy-ep`` hint inside the
token), falling back to the public Copilot API host.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.parse
from typing import Any, Optional

from ..redaction import Redactor
from ..session import Message
from ..tools.base import ToolSpec
from .base import AuthenticationError, ModelReply, ProviderError
from .http import HttpClient, HttpError, provider_error_from_http
from .openai_format import NOOP_TOOL_RESPONSES, has_function_calls, parse_reply, responses_input, responses_tools

GITHUB_API_BASE_URL = "https://api.github.com"
COPILOT_API_BASE_URL = "https://api.githubcopilot.com"
TOKEN_PATH = "/copilot_internal/v2/token"
RESPONSES_PATH = "/responses"
REFRESH_MARGIN_SECONDS = 300
SOURCE_TOKEN_PREFIXES = ("ghp_", "ghu_", "gho_", "ghs_", "ghr_", "github_pat_")

PLUGIN_HEADERS = {
    "User-Agent": "GitHubCopilotChat/0.41.0",
    "Editor-Version": "vscode/1.133.0",
    "Editor-Plugin-Version": "copilot-chat/0.41.0",
    "Copilot-Integration-Id": "vscode-chat",
}

_PROXY_ENDPOINT = re.compile(r"(?:^|[;&,\s])proxy-ep=([^;&,\s]+)")


def is_source_token(token: str) -> bool:
    return token.strip().lower().startswith(SOURCE_TOKEN_PREFIXES)


def api_base_from_token(token: str) -> Optional[str]:
    match = _PROXY_ENDPOINT.search(token)
    if not match:
        return None
    raw = urllib.parse.unquote(match.group(1).strip())
    parsed = urllib.parse.urlparse(raw if "://" in raw else "https://" + raw)
    host = (parsed.netloc or parsed.path.split("/", 1)[0]).strip()
    if not host:
        return None
    if host.startswith("proxy."):
        host = "api." + host[len("proxy."):]
    return "https://" + host.rstrip("/")


class CopilotProvider:
    name = "github_copilot"

    def __init__(
        self,
        *,
        token: str,
        model: str,
        effort: str = "high",
        http: Optional[HttpClient] = None,
        redactor: Optional[Redactor] = None,
        github_api_base_url: Optional[str] = None,
        api_base_url: Optional[str] = None,
    ) -> None:
        credential = (token or "").strip()
        if not credential:
            raise AuthenticationError("GitHub Copilot token is required (COPILOT_GITHUB_TOKEN)")
        self.model = model
        self.effort = effort
        self.redactor = redactor or Redactor()
        self.http = http or HttpClient(redactor=self.redactor)
        self.github_api_base_url = (github_api_base_url or GITHUB_API_BASE_URL).rstrip("/")
        self._explicit_api_base = (api_base_url or "").rstrip("/") or None
        self._source_token = credential if is_source_token(credential) else None
        self._session_token = None if self._source_token else credential
        self._expires_at: Optional[int] = None
        self.api_base_url = self._explicit_api_base or COPILOT_API_BASE_URL
        self._lock = threading.Lock()
        self.redactor.add(credential)

    # --- token handling ---------------------------------------------------

    @property
    def token_source(self) -> str:
        return "github_exchange" if self._source_token else "copilot_token"

    def _refresh_due(self) -> bool:
        if self._source_token is None:
            return False
        if not self._session_token or self._expires_at is None:
            return True
        return self._expires_at - int(time.time()) < REFRESH_MARGIN_SECONDS

    def ensure_token(self, *, force: bool = False) -> None:
        if self._source_token is None:
            return
        if not force and not self._refresh_due():
            return
        with self._lock:
            if not force and not self._refresh_due():
                return
            self._exchange()

    def _exchange(self) -> None:
        headers = {"Authorization": f"Bearer {self._source_token}", "Accept": "application/json", **PLUGIN_HEADERS}
        try:
            data = self.http.request_json("GET", self.github_api_base_url + TOKEN_PATH, headers=headers)
        except HttpError as exc:
            if exc.status in (401, 403, 404):
                raise AuthenticationError(
                    f"GitHub Copilot token exchange failed (HTTP {exc.status}); the GitHub token is invalid, "
                    f"expired or the account has no Copilot access: {exc.body[:300]}"
                ) from None
            raise provider_error_from_http(exc, "GitHub Copilot token exchange") from None
        token = str(data.get("token") or "").strip()
        expires_at = data.get("expires_at")
        if not token or not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
            raise ProviderError("GitHub Copilot token exchange returned an unusable response")
        self._session_token = token
        self._expires_at = int(expires_at)
        self.redactor.add(token)
        endpoints = data.get("endpoints")
        api_host = endpoints.get("api") if isinstance(endpoints, dict) else None
        self.api_base_url = (
            self._explicit_api_base
            or (str(api_host).rstrip("/") if api_host else None)
            or api_base_from_token(token)
            or COPILOT_API_BASE_URL
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._session_token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.copilot-chat-preview+json",
            "Openai-Intent": "conversation-edits",
            "x-initiator": "agent",
            **PLUGIN_HEADERS,
        }

    # --- completion -------------------------------------------------------

    def build_payload(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> dict[str, Any]:
        items = responses_input(messages)
        tool_payload = responses_tools(tools)
        if not tool_payload and has_function_calls(items):
            tool_payload = [dict(NOOP_TOOL_RESPONSES)]
        payload: dict[str, Any] = {
            "model": self.model,
            "input": items,
            "stream": False,
            "reasoning": {"effort": self.effort},
        }
        if tool_payload:
            payload["tools"] = tool_payload
        if system:
            payload["instructions"] = system
        return payload

    def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec]) -> ModelReply:
        payload = self.build_payload(system, messages, tools)
        for attempt in range(2):
            self.ensure_token()
            try:
                raw = self.http.request_json("POST", self.api_base_url + RESPONSES_PATH, headers=self._headers(), payload=payload)
            except HttpError as exc:
                if attempt == 0 and exc.status == 401 and self._source_token is not None:
                    self.ensure_token(force=True)
                    continue
                raise provider_error_from_http(exc, "GitHub Copilot") from None
            return parse_reply(raw)
        raise ProviderError("GitHub Copilot: request failed after token refresh")


__all__ = ["CopilotProvider", "PLUGIN_HEADERS", "api_base_from_token", "is_source_token"]
