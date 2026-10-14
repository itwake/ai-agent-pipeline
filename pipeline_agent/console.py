"""Progress output for the Jenkins console log."""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Optional, TextIO

from .redaction import Redactor
from .tools.base import ToolResult

PREVIEW_LINES = 15
PREVIEW_CHARS = 1_500


class Console:
    def __init__(self, redactor: Redactor, stream: Optional[TextIO] = None, *, verbose: bool = False) -> None:
        self.redactor = redactor
        self._stream = stream
        self.verbose = verbose

    @property
    def stream(self) -> TextIO:
        # Resolved late so output follows a redirected sys.stdout.
        return self._stream if self._stream is not None else sys.stdout

    def _emit(self, tag: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        body = self.redactor.redact(text)
        lines = body.splitlines() or [""]
        first = True
        stream = self.stream
        for line in lines:
            prefix = f"{stamp} [{tag}] " if first else " " * (len(stamp) + 1) + "  "
            stream.write(prefix + line + "\n")
            first = False
        stream.flush()

    def info(self, text: str) -> None:
        self._emit("agent", text)

    def warn(self, text: str) -> None:
        self._emit("warn", text)

    def error(self, text: str) -> None:
        self._emit("error", text)

    def debug(self, text: str) -> None:
        if self.verbose:
            self._emit("debug", text)

    def model_call(self, iteration: int, limit: int, estimate: int, model: str) -> None:
        self._emit("model", f"call {iteration}/{limit} to {model} (about {estimate:,} prompt tokens)")

    def assistant(self, text: str) -> None:
        if text.strip():
            self._emit("assistant", text.strip())

    def tool_call(self, name: str, arguments: Any) -> None:
        if isinstance(arguments, dict) and name == "bash":
            command = str(arguments.get("command", ""))
            description = str(arguments.get("description", "") or "")
            timeout = arguments.get("timeout_seconds")
            workdir = arguments.get("workdir")
            extras = []
            if timeout:
                extras.append(f"timeout {timeout}s")
            if workdir and workdir != ".":
                extras.append(f"in {workdir}")
            suffix = f" ({', '.join(extras)})" if extras else ""
            head = f"$ {command}"
            if description:
                head = f"{description}\n$ {command}"
            self._emit("bash", head + suffix)
            return
        preview = json.dumps(arguments, ensure_ascii=False) if not isinstance(arguments, str) else arguments
        if len(preview) > 600:
            preview = preview[:600] + " ..."
        self._emit(f"tool:{name}", preview)

    def tool_result(self, name: str, result: ToolResult) -> None:
        status = "ok" if result.success else "failed"
        meta_bits = []
        if "exit_code" in result.meta:
            meta_bits.append(f"exit {result.meta['exit_code']}")
        if result.meta.get("timed_out"):
            meta_bits.append("timed out")
        if "duration_seconds" in result.meta:
            meta_bits.append(f"{result.meta['duration_seconds']}s")
        if result.meta.get("truncated"):
            meta_bits.append("output truncated")
        header = f"{name}: {status}" + (f" ({', '.join(meta_bits)})" if meta_bits else "")
        preview_lines = result.content.splitlines()
        shown = preview_lines[:PREVIEW_LINES]
        preview = "\n".join(shown)
        if len(preview) > PREVIEW_CHARS:
            preview = preview[:PREVIEW_CHARS] + " ..."
        if len(preview_lines) > PREVIEW_LINES:
            preview += f"\n... ({len(preview_lines) - PREVIEW_LINES} more lines)"
        self._emit("result", header + ("\n" + preview if preview.strip() else ""))

    def question(self, questions: list[dict[str, Any]]) -> None:
        lines = ["the agent needs an answer from the user; the build will pause for input:"]
        for index, question in enumerate(questions, start=1):
            header = question.get("header") or ""
            lines.append(f"{index}. {header + ': ' if header else ''}{question.get('question', '')}")
            for option in question.get("options") or []:
                description = option.get("description") or ""
                lines.append(f"   - {option.get('label', '')}" + (f": {description}" if description else ""))
        self._emit("question", "\n".join(lines))

    def summary(self, status: str, session: Any) -> None:
        usage = getattr(session, "usage", {}) or {}
        self._emit(
            "agent",
            f"finished with status {status}; model calls {usage.get('model_calls', 0)}, "
            f"input tokens {usage.get('input_tokens', 0):,}, output tokens {usage.get('output_tokens', 0):,}, "
            f"compactions {getattr(session, 'compactions', 0)}",
        )


__all__ = ["Console"]
