"""File tools: read with line numbers, write, and exact-string edit."""

from __future__ import annotations

from typing import Any

from .base import (
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
    optional_int,
    require_str,
    resolve_in_workspace,
    workspace_relative,
)

MAX_READ_LINES = 2000
MAX_READ_CHARS = 200_000

READ_SPEC = ToolSpec(
    name="read_file",
    description=(
        "Read a text file from the workspace with line numbers. Use start_line and end_line "
        "(1-based, inclusive) to read a slice of a large file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace (or absolute inside it)."},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)

WRITE_SPEC = ToolSpec(
    name="write_file",
    description="Create or overwrite a text file in the workspace. Parent directories are created.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
)

EDIT_SPEC = ToolSpec(
    name="edit_file",
    description=(
        "Replace an exact string in a file. old_string must appear exactly once unless replace_all "
        "is true. Include enough surrounding lines to make it unique. Prefer this over rewriting "
        "whole files."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        "required": ["path", "old_string", "new_string"],
        "additionalProperties": False,
    },
)


class ReadFileTool:
    spec = READ_SPEC

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_in_workspace(ctx.workspace, require_str(args, "path"))
        if not path.exists():
            raise ToolError(f"file not found: {workspace_relative(ctx.workspace, path)}")
        if path.is_dir():
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
            listing = "\n".join(entries[:500])
            return ToolResult(content=f"{workspace_relative(ctx.workspace, path)} is a directory with {len(entries)} entries:\n{listing}")
        raw = path.read_bytes()
        if b"\x00" in raw[:8000]:
            return ToolResult(content=f"{workspace_relative(ctx.workspace, path)} looks binary ({len(raw)} bytes); not shown.", success=False)
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        start = optional_int(args, "start_line") or 1
        end = optional_int(args, "end_line") or len(lines)
        if start < 1 or end < start:
            raise ToolError("start_line must be >= 1 and end_line >= start_line")
        selected = lines[start - 1 : end]
        truncated = False
        if len(selected) > MAX_READ_LINES:
            selected = selected[:MAX_READ_LINES]
            truncated = True
        width = len(str(start + len(selected)))
        rendered: list[str] = []
        total_chars = 0
        for index, line in enumerate(selected, start=start):
            rendered.append(f"{index:>{width}}| {line}")
            total_chars += len(line)
            if total_chars > MAX_READ_CHARS:
                truncated = True
                break
        body = ctx.redactor.redact("\n".join(rendered))
        note = ""
        if truncated:
            note = f"\n[truncated; file has {len(lines)} lines. Read a narrower range with start_line/end_line.]"
        elif not lines:
            note = "(empty file)"
        return ToolResult(content=body + note, meta={"lines": len(lines)})


class WriteFileTool:
    spec = WRITE_SPEC

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_in_workspace(ctx.workspace, require_str(args, "path"))
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("'content' must be a string")
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(content, encoding="utf-8")
        verb = "Overwrote" if existed else "Created"
        return ToolResult(content=f"{verb} {workspace_relative(ctx.workspace, path)} ({len(content)} characters).")


class EditFileTool:
    spec = EDIT_SPEC

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_in_workspace(ctx.workspace, require_str(args, "path"))
        old = args.get("old_string")
        new = args.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ToolError("'old_string' and 'new_string' must be strings")
        if not old:
            raise ToolError("'old_string' must not be empty")
        if not path.exists():
            raise ToolError(f"file not found: {workspace_relative(ctx.workspace, path)}")
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            return ToolResult(
                content="old_string was not found in the file. Read the file again and copy the text exactly (whitespace matters).",
                success=False,
            )
        replace_all = bool(args.get("replace_all"))
        if count > 1 and not replace_all:
            return ToolResult(
                content=f"old_string occurs {count} times; include more context to make it unique or set replace_all.",
                success=False,
            )
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        path.write_text(updated, encoding="utf-8")
        replaced = count if replace_all else 1
        return ToolResult(content=f"Replaced {replaced} occurrence(s) in {workspace_relative(ctx.workspace, path)}.")


__all__ = ["EditFileTool", "ReadFileTool", "WriteFileTool", "EDIT_SPEC", "READ_SPEC", "WRITE_SPEC"]
