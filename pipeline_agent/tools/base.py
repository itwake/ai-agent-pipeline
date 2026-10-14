"""Contracts shared by every tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

from ..redaction import Redactor


class ToolError(Exception):
    """A tool could not run with the arguments it was given."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolResult:
    content: str
    success: bool = True
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def pause(self) -> Optional[str]:
        """Set when the loop must stop and wait for the user (``question``)."""
        value = self.meta.get("pause")
        return str(value) if value else None


@dataclass
class ToolContext:
    workspace: Path
    call_id: str
    session_id: str
    archive_dir: Path
    redactor: Redactor
    policy: Any  # CommandPolicy; typed loosely to avoid an import cycle
    shell: str = "/bin/bash"
    default_timeout: int = 600
    max_timeout: int = 3600
    tool_env: dict[str, str] = field(default_factory=dict)
    console: Any = None


class Tool(Protocol):
    spec: ToolSpec

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


def require_str(args: dict[str, Any], key: str, *, required: bool = True, default: str = "") -> str:
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip() and required):
        if required:
            raise ToolError(f"'{key}' is required")
        return default
    if not isinstance(value, str):
        raise ToolError(f"'{key}' must be a string")
    return value


def optional_int(args: dict[str, Any], key: str) -> Optional[int]:
    value = args.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ToolError(f"'{key}' must be an integer")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ToolError(f"'{key}' must be an integer")


def resolve_in_workspace(workspace: Path, raw: str) -> Path:
    """Resolve ``raw`` against the workspace and refuse paths that escape it."""

    root = workspace.resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ToolError(f"path escapes the workspace: {raw}") from exc
    return resolved


def workspace_relative(workspace: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace.resolve())).replace("\\", "/") or "."
    except ValueError:
        return str(path)


__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "ToolSpec",
    "optional_int",
    "require_str",
    "resolve_in_workspace",
    "workspace_relative",
]
