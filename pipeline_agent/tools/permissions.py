"""Deny-list policy for shell commands.

The agent runs with the node's full bash. The policy is the only guard rail,
so it errs on the side of blocking clearly destructive or outward-facing
commands. Patterns are shell-style globs matched against the whitespace
normalised, lower-cased command line.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_DENY_PATTERNS: tuple[str, ...] = (
    "*rm -rf /*",
    "*rm -rf ~*",
    "*rm -rf $home*",
    "*rm -rf .*",
    "*git push*--force*",
    "*git push*-f *",
    "*git push*-f",
    "*git branch -d*",
    "*git branch -D*",
    "*git reset --hard origin*",
    "*mvn*deploy*",
    "*mvn*release:*",
    "*gradle*publish*",
    "*gradle*upload*",
    "*docker push*",
    "*docker rm*",
    "*docker system prune*",
    "*kubectl delete*",
    "*kubectl apply*",
    "*helm install*",
    "*helm upgrade*",
    "*helm uninstall*",
    "*terraform apply*",
    "*terraform destroy*",
    "*aws * delete*",
    "*aws * terminate*",
    "sudo *",
    "* sudo *",
    "*shutdown*",
    "*reboot*",
    "*mkfs*",
    "*:(){*",
    "*crontab*",
    "*curl *| *sh*",
    "*wget *| *sh*",
)

DRY_RUN_DENY_PATTERNS: tuple[str, ...] = (
    "*git push*",
    "*gh pr create*",
    "*gh pr merge*",
    "*gh release*",
)

_WHITESPACE = re.compile(r"\s+")


def normalize_command(command: str) -> str:
    return _WHITESPACE.sub(" ", command.strip()).lower()


class CommandPolicy:
    def __init__(self, deny_patterns: Iterable[str] = (), *, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        patterns = [p.strip().lower() for p in deny_patterns if p and p.strip() and not p.strip().startswith("#")]
        if dry_run:
            patterns.extend(DRY_RUN_DENY_PATTERNS)
        self.deny_patterns: list[str] = list(dict.fromkeys(patterns))

    @classmethod
    def build(
        cls,
        *,
        deny_file: Optional[str | Path] = None,
        extra_patterns: Iterable[str] = (),
        dry_run: bool = False,
        include_defaults: bool = True,
    ) -> "CommandPolicy":
        patterns: list[str] = list(DEFAULT_DENY_PATTERNS) if include_defaults else []
        if deny_file:
            path = Path(deny_file)
            if path.exists():
                patterns.extend(path.read_text(encoding="utf-8").splitlines())
        patterns.extend(extra_patterns)
        return cls(patterns, dry_run=dry_run)

    def check(self, command: str) -> Optional[str]:
        """Return a reason when the command is blocked, otherwise None."""

        normalized = normalize_command(command)
        for pattern in self.deny_patterns:
            if fnmatch.fnmatchcase(normalized, pattern):
                if self.dry_run and pattern in DRY_RUN_DENY_PATTERNS:
                    return f"dry run: '{pattern}' is not allowed in this build"
                return f"matches deny pattern '{pattern}'"
        return None


__all__ = ["CommandPolicy", "DEFAULT_DENY_PATTERNS", "DRY_RUN_DENY_PATTERNS", "normalize_command"]
