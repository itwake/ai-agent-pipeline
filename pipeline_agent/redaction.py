"""Mask secrets before text reaches the model, the console or an archive."""

from __future__ import annotations

import re
from typing import Iterable

MASK = "***"

# Patterns that identify a secret by shape even when its value is unknown.
_SHAPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), MASK),
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"), r"\1" + MASK),
    (re.compile(r"(?i)(x-access-token:)[^@\s]+(@)"), r"\1" + MASK + r"\2"),
    # user:password@ inside URLs (git remotes, proxies)
    (re.compile(r"(?i)(https?://[^/\s:@]+:)[^@\s/]+(@)"), r"\1" + MASK + r"\2"),
    (re.compile(r"(?i)(password[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+"), r"\1" + MASK),
]


class Redactor:
    """Replace known secret values and secret-shaped strings with a mask."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: list[str] = []
        for secret in secrets:
            self.add(secret)

    def add(self, secret: str | None) -> None:
        if not secret:
            return
        value = str(secret).strip()
        # Very short values would mask ordinary words.
        if len(value) < 6 or value in self._secrets:
            return
        self._secrets.append(value)
        # Longest first so a secret that contains another is masked whole.
        self._secrets.sort(key=len, reverse=True)

    @property
    def secrets(self) -> list[str]:
        return list(self._secrets)

    def redact(self, text: str | None) -> str:
        if not text:
            return "" if text is None else text
        result = text
        for secret in self._secrets:
            if secret in result:
                result = result.replace(secret, MASK)
        for pattern, replacement in _SHAPE_PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    def __call__(self, text: str | None) -> str:
        return self.redact(text)


__all__ = ["MASK", "Redactor"]
