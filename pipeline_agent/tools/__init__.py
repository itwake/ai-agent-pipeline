"""Tool registry and the default tool set."""

from __future__ import annotations

from typing import Iterable, Optional

from .ask_user import AskUserTool
from .base import Tool, ToolContext, ToolError, ToolResult, ToolSpec
from .bash import BashTool
from .files import EditFileTool, ReadFileTool, WriteFileTool
from .permissions import CommandPolicy


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        self._tools[tool.spec.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]


def default_registry(*, include_ask_user: bool = True) -> ToolRegistry:
    tools: list[Tool] = [BashTool(), ReadFileTool(), WriteFileTool(), EditFileTool()]
    if include_ask_user:
        tools.append(AskUserTool())
    return ToolRegistry(tools)


__all__ = [
    "AskUserTool",
    "BashTool",
    "CommandPolicy",
    "EditFileTool",
    "ReadFileTool",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "WriteFileTool",
    "default_registry",
]
