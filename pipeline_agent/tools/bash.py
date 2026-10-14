"""The bash tool: run a shell command on the node and report its output."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
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

MAX_VISIBLE_CHARS = 30_000
MAX_VISIBLE_LINES = 400

SPEC = ToolSpec(
    name="bash",
    description=(
        "Run a bash command on the build node and return its stdout, stderr and exit code. "
        "Commands run non-interactively in the workspace (or `workdir`, relative to it). "
        "Set `timeout_seconds` for anything that may run long, such as builds or test suites; "
        "the default is modest and the process is killed when it expires. Long output is "
        "truncated in the middle and the full text is saved to a file whose path is reported."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command line to run with bash -c."},
            "description": {"type": "string", "description": "One short sentence saying what the command does."},
            "timeout_seconds": {"type": "integer", "minimum": 1, "description": "Kill the command after this many seconds."},
            "workdir": {"type": "string", "description": "Working directory relative to the workspace. Defaults to the workspace root."},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
)


class BashTool:
    spec = SPEC

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = require_str(args, "command")
        reason = ctx.policy.check(command)
        if reason:
            return ToolResult(
                content=f"Command blocked by the pipeline policy ({reason}). Choose a different approach or ask the user.",
                success=False,
                meta={"blocked": True, "reason": reason},
            )

        requested = optional_int(args, "timeout_seconds")
        timeout = ctx.default_timeout if requested is None else requested
        timeout = max(1, min(int(timeout), int(ctx.max_timeout)))

        cwd = resolve_in_workspace(ctx.workspace, args.get("workdir") or ".")
        if not cwd.exists():
            raise ToolError(f"workdir does not exist: {workspace_relative(ctx.workspace, cwd)}")
        if not cwd.is_dir():
            raise ToolError(f"workdir is not a directory: {workspace_relative(ctx.workspace, cwd)}")

        started = time.monotonic()
        popen_kwargs: dict[str, Any] = {}
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(
            [ctx.shell, "-c", command],
            cwd=str(cwd),
            env=ctx.tool_env or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
        timed_out = False
        try:
            out_bytes, err_bytes = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(process)
            out_bytes, err_bytes = process.communicate()
        duration = time.monotonic() - started
        exit_code = process.returncode

        stdout = ctx.redactor.redact(out_bytes.decode("utf-8", errors="replace"))
        stderr = ctx.redactor.redact(err_bytes.decode("utf-8", errors="replace"))
        full = _format_output(stdout, stderr, exit_code, timed_out, timeout, duration)

        archive_path = _archive(ctx, full)
        visible, truncated = truncate_middle(full, MAX_VISIBLE_CHARS, MAX_VISIBLE_LINES)
        if truncated:
            visible += (
                f"\n[output truncated: {len(full)} characters total; the full output is in "
                f"{archive_path}. Use read_file with start_line/end_line to inspect it.]"
            )
        return ToolResult(
            content=visible,
            success=(exit_code == 0 and not timed_out),
            meta={
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration_seconds": round(duration, 1),
                "archive": archive_path,
                "truncated": truncated,
                "cwd": workspace_relative(ctx.workspace, cwd),
            },
        )


def _kill(process: subprocess.Popen) -> None:
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass


def _format_output(stdout: str, stderr: str, exit_code: Any, timed_out: bool, timeout: int, duration: float) -> str:
    parts: list[str] = []
    if stdout.strip():
        parts.append(f"<stdout>\n{stdout.rstrip()}\n</stdout>")
    if stderr.strip():
        parts.append(f"<stderr>\n{stderr.rstrip()}\n</stderr>")
    if not parts:
        parts.append("(no output)")
    if timed_out:
        parts.append(f"[timed out after {timeout}s; the process was killed. Re-run with a larger timeout_seconds if it needs more time.]")
    else:
        parts.append(f"[exit code: {exit_code}; {duration:.1f}s]")
    return "\n".join(parts)


def _archive(ctx: ToolContext, text: str) -> str:
    ctx.archive_dir.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in ctx.call_id)[:80] or "call"
    path = ctx.archive_dir / f"{safe_id}.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def truncate_middle(text: str, max_chars: int, max_lines: int) -> tuple[str, bool]:
    """Keep the head and the tail; builds print the useful error at the end."""

    lines = text.splitlines()
    if len(text) <= max_chars and len(lines) <= max_lines:
        return text, False
    head_lines = lines[: max_lines // 2]
    tail_lines = lines[-(max_lines // 2):] if len(lines) > max_lines else lines[max_lines // 2:]
    head = "\n".join(head_lines)
    tail = "\n".join(tail_lines)
    half = max_chars // 2
    if len(head) > half:
        head = head[:half]
    if len(tail) > half:
        tail = tail[-half:]
    marker = "\n\n... [middle of the output omitted] ...\n\n"
    return head + marker + tail, True


__all__ = ["BashTool", "SPEC", "truncate_middle"]
