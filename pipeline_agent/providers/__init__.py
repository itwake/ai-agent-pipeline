"""Provider factory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from ..redaction import Redactor
from . import catalog
from .ai_platform import AIPlatformProvider, AIPlatformSettings
from .base import (
    AuthenticationError,
    ContextOverflowError,
    ModelReply,
    Provider,
    ProviderError,
    TransientProviderError,
)
from .copilot import CopilotProvider
from .fake import FakeProvider
from .http import HttpClient

COPILOT_TOKEN_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GITHUB_COPILOT_TOKEN", "GITHUB_TOKEN")


@dataclass
class ProviderSettings:
    provider: str
    model: str = ""
    effort: str = catalog.DEFAULT_EFFORT
    timeout: float = 600.0
    fake_script: Optional[str] = None
    allow_unknown_model: bool = False

    def __post_init__(self) -> None:
        self.provider = catalog.normalize_provider(self.provider)
        self.model = catalog.normalize_model(self.model) or catalog.DEFAULT_MODEL[self.provider]
        self.effort = catalog.normalize_effort(self.effort)


def validate_model(settings: ProviderSettings) -> catalog.ModelInfo:
    info = catalog.lookup(settings.provider, settings.model)
    if info is None:
        if settings.allow_unknown_model:
            return catalog.ModelInfo(
                provider=settings.provider,
                model=settings.model,
                context_window=catalog.FALLBACK_CONTEXT_WINDOW,
                reserve_tokens=catalog.FALLBACK_RESERVE_TOKENS,
                note="not in the catalog; conservative context defaults",
            )
        known = ", ".join(catalog.models_for(settings.provider))
        raise ValueError(f"model '{settings.model}' is not available for {settings.provider}; known models: {known}")
    return info


def build_provider(settings: ProviderSettings, env: Mapping[str, str], redactor: Optional[Redactor] = None) -> Provider:
    redactor = redactor or Redactor()
    validate_model(settings)
    http = HttpClient(timeout=settings.timeout, redactor=redactor)

    if settings.provider == catalog.GITHUB_COPILOT:
        token = ""
        for name in COPILOT_TOKEN_ENV_VARS:
            value = env.get(name)
            if isinstance(value, str) and value.strip():
                token = value.strip()
                break
        if not token:
            raise AuthenticationError(
                "GitHub Copilot token not found; set COPILOT_GITHUB_TOKEN (a GitHub token with Copilot access)"
            )
        return CopilotProvider(
            token=token,
            model=settings.model,
            effort=settings.effort,
            http=http,
            redactor=redactor,
            github_api_base_url=(env.get("COPILOT_GITHUB_API_BASE_URL") or "").strip() or None,
            api_base_url=(env.get("COPILOT_API_BASE_URL") or "").strip() or None,
        )

    if settings.provider == catalog.AI_PLATFORM:
        return AIPlatformProvider(
            AIPlatformSettings.from_env(env),
            model=settings.model,
            effort=settings.effort,
            http=http,
            redactor=redactor,
        )

    if settings.provider == catalog.FAKE:
        if settings.fake_script:
            return FakeProvider.from_file(settings.fake_script, model=settings.model)
        return FakeProvider([], model=settings.model)

    raise ValueError(f"unsupported provider {settings.provider}")


__all__ = [
    "AIPlatformProvider",
    "AIPlatformSettings",
    "AuthenticationError",
    "COPILOT_TOKEN_ENV_VARS",
    "ContextOverflowError",
    "CopilotProvider",
    "FakeProvider",
    "HttpClient",
    "ModelReply",
    "Provider",
    "ProviderError",
    "ProviderSettings",
    "TransientProviderError",
    "build_provider",
    "catalog",
    "validate_model",
]
