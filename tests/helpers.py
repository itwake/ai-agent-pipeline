"""Shared fixtures for the unit tests."""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from pipeline_agent.console import Console
from pipeline_agent.context import ContextManager, ContextSettings
from pipeline_agent.loop import AgentLoop, LoopSettings
from pipeline_agent.providers.fake import FakeProvider
from pipeline_agent.redaction import Redactor
from pipeline_agent.session import SessionStore
from pipeline_agent.tools import CommandPolicy, ToolContext, default_registry


def find_bash() -> str:
    for candidate in ("/bin/bash", "/usr/bin/bash"):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("bash")
    if not found:
        raise RuntimeError("bash is required for the tests")
    return found


class Harness:
    """A loop wired to a scripted provider inside a temporary workspace."""

    def __init__(self, script: Optional[list[dict[str, Any]]] = None, *, budget_tokens: int = 50_000, max_iterations: int = 20, dry_run: bool = False, keep_recent: int = 12) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="agent-test-"))
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.state_dir = self.root / "state"
        self.provider = FakeProvider(script or [])
        self.redactor = Redactor()
        self.output = io.StringIO()
        self.console = Console(self.redactor, stream=self.output)
        self.store = SessionStore(self.state_dir / "sessions")
        self.registry = default_registry()
        self.policy = CommandPolicy.build(dry_run=dry_run)
        self.context = ContextManager(self.provider, ContextSettings(budget_tokens=budget_tokens, keep_recent=keep_recent), store=self.store, console=self.console)
        self.loop = AgentLoop(
            provider=self.provider,
            registry=self.registry,
            store=self.store,
            context=self.context,
            system_prompt="You are a test agent.",
            tool_context_factory=self.tool_context,
            console=self.console,
            settings=LoopSettings(max_iterations=max_iterations),
        )

    def tool_context(self, session_id: str, call_id: str) -> ToolContext:
        env = dict(os.environ)
        return ToolContext(
            workspace=self.workspace,
            call_id=call_id,
            session_id=session_id,
            archive_dir=self.state_dir / "tool-output",
            redactor=self.redactor,
            policy=self.policy,
            shell=find_bash(),
            default_timeout=30,
            max_timeout=60,
            tool_env=env,
            console=self.console,
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def console_text(self) -> str:
        return self.output.getvalue()


def bash_call(command: str, **extra: Any) -> dict[str, Any]:
    arguments = {"command": command, **extra}
    return {"name": "bash", "arguments": arguments}


def ask_call(*questions: dict[str, Any]) -> dict[str, Any]:
    return {"name": "ask_user", "arguments": {"questions": list(questions)}}


def is_windows() -> bool:
    return sys.platform.startswith("win")
