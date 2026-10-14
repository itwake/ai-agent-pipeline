"""A scripted provider for tests and for exercising the pipeline without a model.

The script is a list of replies. Each reply is an object with an optional
``text`` and an optional ``tool_calls`` list of ``{"name": ..., "arguments": {...}}``.
Replies are consumed in order; when the script runs out the provider answers
with a short completion message so a run always terminates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..session import Message, ToolCall
from ..tools.base import ToolSpec
from .base import ModelReply


class FakeProvider:
    name = "fake"

    def __init__(self, script: Optional[list[dict[str, Any]]] = None, *, model: str = "fake", cursor_file: Optional[Path] = None) -> None:
        self.model = model
        self._script: list[dict[str, Any]] = list(script or [])
        self.requests: list[dict[str, Any]] = []
        self._counter = 0
        # When the script comes from a file, the position is persisted next to it
        # so a later process (the pipeline's resume step) continues the script.
        self._cursor_file = cursor_file
        if cursor_file is not None and cursor_file.exists():
            try:
                consumed = int(cursor_file.read_text(encoding="utf-8").strip() or 0)
            except ValueError:
                consumed = 0
            self._counter = consumed
            self._script = self._script[consumed:]

    @classmethod
    def from_file(cls, path: str | Path, *, model: str = "fake") -> "FakeProvider":
        script_path = Path(path)
        data = json.loads(script_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("replies", [])
        if not isinstance(data, list):
            raise ValueError("fake script must be a JSON array of replies")
        return cls(data, model=model, cursor_file=script_path.with_name(script_path.name + ".cursor"))

    @property
    def remaining(self) -> int:
        return len(self._script)

    def _advance(self) -> None:
        self._counter += 1
        if self._cursor_file is not None:
            self._cursor_file.write_text(str(self._counter), encoding="utf-8")

    def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec]) -> ModelReply:
        self.requests.append({"system": system, "messages": [m.to_dict() for m in messages], "tools": [t.name for t in tools]})
        if not self._script:
            return ModelReply(text="Done (scripted provider has no more replies).", usage={"input_tokens": 10, "output_tokens": 5})
        step = self._script.pop(0)
        self._advance()
        calls: list[ToolCall] = []
        for index, raw_call in enumerate(step.get("tool_calls") or []):
            arguments = raw_call.get("arguments")
            if arguments is None:
                arguments = {}
            calls.append(
                ToolCall(
                    id=str(raw_call.get("id") or f"call_{self._counter}_{index}"),
                    name=str(raw_call.get("name", "")),
                    arguments=arguments if isinstance(arguments, dict) else None,
                    arguments_text=json.dumps(arguments) if isinstance(arguments, dict) else str(arguments),
                )
            )
        usage = step.get("usage") or {"input_tokens": 100, "output_tokens": 20}
        return ModelReply(text=str(step.get("text", "") or ""), tool_calls=calls, usage=dict(usage), finish_reason="tool_calls" if calls else "stop")


__all__ = ["FakeProvider"]
