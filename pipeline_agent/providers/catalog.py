"""Known providers, their models and context sizing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

GITHUB_COPILOT = "github_copilot"
AI_PLATFORM = "ai_platform"
FAKE = "fake"

_PROVIDER_ALIASES = {
    "github_copilot": GITHUB_COPILOT,
    "github-copilot": GITHUB_COPILOT,
    "githubcopilot": GITHUB_COPILOT,
    "copilot": GITHUB_COPILOT,
    "ai_platform": AI_PLATFORM,
    "ai-platform": AI_PLATFORM,
    "aiplatform": AI_PLATFORM,
    "fake": FAKE,
    "scripted": FAKE,
}

EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "high"
SAFETY_MARGIN_TOKENS = 8_000
FALLBACK_CONTEXT_WINDOW = 64_000
FALLBACK_RESERVE_TOKENS = 8_000


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    model: str
    context_window: int
    reserve_tokens: int
    note: str = ""


def _m(provider: str, model: str, window: int, reserve: int, note: str = "") -> ModelInfo:
    return ModelInfo(provider=provider, model=model, context_window=window, reserve_tokens=reserve, note=note)


CATALOG: dict[str, dict[str, ModelInfo]] = {
    GITHUB_COPILOT: {
        "gpt-6-sol": _m(GITHUB_COPILOT, "gpt-6-sol", 1_050_000, 128_000, "balanced default for agentic coding"),
        "gpt-6-astra": _m(GITHUB_COPILOT, "gpt-6-astra", 1_050_000, 128_000, "strongest reasoning"),
        "gpt-6-luna": _m(GITHUB_COPILOT, "gpt-6-luna", 1_050_000, 128_000, "fast and cheap"),
        "gpt-5.6-sol": _m(GITHUB_COPILOT, "gpt-5.6-sol", 1_000_000, 128_000),
        "gpt-5.6-luna": _m(GITHUB_COPILOT, "gpt-5.6-luna", 1_000_000, 128_000),
        "gpt-5.6-terra": _m(GITHUB_COPILOT, "gpt-5.6-terra", 1_000_000, 128_000),
    },
    AI_PLATFORM: {
        "gpt-5.4": _m(AI_PLATFORM, "gpt-5.4", 1_000_000, 128_000),
        "gpt-5.6-sol": _m(AI_PLATFORM, "gpt-5.6-sol", 1_000_000, 128_000),
        "gpt-5.6-luna": _m(AI_PLATFORM, "gpt-5.6-luna", 1_000_000, 128_000),
        "gpt-5.6-terra": _m(AI_PLATFORM, "gpt-5.6-terra", 1_000_000, 128_000),
    },
    FAKE: {
        "fake": _m(FAKE, "fake", 1_000_000, 8_000, "scripted provider for tests and dry runs"),
    },
}

DEFAULT_MODEL: dict[str, str] = {
    GITHUB_COPILOT: "gpt-6-sol",
    AI_PLATFORM: "gpt-5.4",
    FAKE: "fake",
}


def normalize_provider(name: str) -> str:
    key = (name or "").strip().lower()
    if key not in _PROVIDER_ALIASES:
        known = ", ".join(sorted({v for v in _PROVIDER_ALIASES.values() if v != FAKE}))
        raise ValueError(f"unknown provider '{name}'; known providers: {known}")
    return _PROVIDER_ALIASES[key]


def normalize_model(model: str) -> str:
    return "-".join((model or "").strip().lower().split())


def models_for(provider: str) -> list[str]:
    return list(CATALOG.get(provider, {}).keys())


def lookup(provider: str, model: str) -> Optional[ModelInfo]:
    return CATALOG.get(provider, {}).get(normalize_model(model))


def normalize_effort(value: str) -> str:
    effort = (value or "").strip().lower()
    if effort not in EFFORT_LEVELS:
        raise ValueError(f"unknown thinking level '{value}'; use one of {', '.join(EFFORT_LEVELS)}")
    return effort


def context_budget(window: int, reserve: int) -> int:
    """Prompt tokens the agent may use once the model's reply headroom is held back."""

    margin = min(SAFETY_MARGIN_TOKENS, window // 20)
    return max(4_000, window - reserve - margin)


__all__ = [
    "AI_PLATFORM",
    "CATALOG",
    "DEFAULT_EFFORT",
    "DEFAULT_MODEL",
    "EFFORT_LEVELS",
    "FAKE",
    "FALLBACK_CONTEXT_WINDOW",
    "FALLBACK_RESERVE_TOKENS",
    "GITHUB_COPILOT",
    "ModelInfo",
    "context_budget",
    "lookup",
    "models_for",
    "normalize_effort",
    "normalize_model",
    "normalize_provider",
]
