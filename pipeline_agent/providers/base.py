"""Provider contract: one call in, one reply out."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..session import Message, ToolCall
from ..tools.base import ToolSpec


class ProviderError(Exception):
    """The model service refused or failed the request."""


class TransientProviderError(ProviderError):
    """Worth retrying: network trouble, rate limiting, 5xx."""


class ContextOverflowError(ProviderError):
    """The prompt no longer fits the model's context window."""


class AuthenticationError(ProviderError):
    """Credentials are missing or rejected."""


@dataclass
class ModelReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str
    model: str

    def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec]) -> ModelReply: ...


__all__ = [
    "AuthenticationError",
    "ContextOverflowError",
    "ModelReply",
    "Provider",
    "ProviderError",
    "TransientProviderError",
]
